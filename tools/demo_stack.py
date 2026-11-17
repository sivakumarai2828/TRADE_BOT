"""Run the whole V3 stack locally with a simulated market — no keys, no money.

    python tools/demo_stack.py
    open http://localhost:8000/control      (token: demo)

What is real: the bot's MarketRunners, risk layer, paper ledger, Skeptic gate,
ApprovalDesk, control plane, the API and the control page.
What is simulated: prices (a gentle wave around fixed levels), the AI
portfolio manager's ideas, the Skeptic's rulings, and Telegram (printed here).
Everything lives in a throwaway SQLite file; nothing touches the VM.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

DB = os.path.join(tempfile.mkdtemp(), "demo.db")
for k in ("SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
          "OPENROUTER_API_KEY", "ALPACA_KEY", "ALPACA_SECRET"):
    os.environ[k] = ""
os.environ.update(BOTV2_DB=DB, DASHBOARD_TOKEN=os.getenv("DEMO_TOKEN", "demo"),
                  US_START_CASH="5000", INDIA_START_CASH="250000",
                  INDIA_MODE=os.getenv("INDIA_MODE", "suggest"),   # India: daily suggestions only
                  BOTV2_DEMO="1",                                   # pages show a DEMO banner
                  CAP_MAX_POSITION_PCT="0.25", CAP_MAX_RISK_PER_TRADE_PCT="0.02",
                  CAP_MAX_OPEN_POSITIONS="8", CAP_MAX_NEW_ENTRIES_PER_DAY="5")

from botv2 import control, data, executor, notify   # noqa: E402
from botv2.approval import ApprovalDesk              # noqa: E402
from botv2.config import load_config                 # noqa: E402
from botv2.executor import MarketRunner               # noqa: E402
from botv2.journal import Journal                     # noqa: E402
from botv2.ledger import PaperLedger                  # noqa: E402
from botv2.skeptic import Skeptic                     # noqa: E402

# ── simulated market ─────────────────────────────────────────────
BASE = {"NVDA": 182.4, "AMD": 240.0, "PLTR": 160.0, "MU": 120.0, "ARM": 150.0,
        "RKLB": 31.0, "SPY": 680.0,
        "RELIANCE.NS": 1450.0, "TCS.NS": 4100.0, "INFY.NS": 1900.0, "HDFCBANK.NS": 1750.0,
        "^NSEI": 25900.0}
T0 = time.time()


def use_real_closes() -> None:
    """Start each symbol at its last real close so the page shows believable
    numbers. Needs the network; without it the built-in levels above are kept.
    DEMO_REAL_PRICES=0 skips it."""
    if os.getenv("DEMO_REAL_PRICES", "1") == "0":
        return
    try:
        import yfinance as yf
        closes = yf.download(list(BASE), period="5d", auto_adjust=True, progress=False)["Close"].ffill().iloc[-1]
        got = {s: round(float(v), 2) for s, v in closes.items() if v == v and v > 0}
        BASE.update(got)
        print(f"  Using last real closes for {len(got)}/{len(BASE)} symbols.")
    except Exception as exc:
        print(f"  Real prices unavailable ({type(exc).__name__}); using built-in demo levels.")


use_real_closes()


def price(sym: str) -> float | None:
    b = BASE.get(sym)
    if b is None:
        return None
    phase = sum(map(ord, sym)) % 7
    wave = 0.012 * math.sin((time.time() - T0) / 45 + phase)
    return round(b * (1 + wave), 2)


def snapshot(sym: str, **_):
    px = price(sym)
    if px is None:
        return None
    hot = sym in ("PLTR",)
    return {"symbol": sym, "price": px, "rsi14": 81 if hot else 55,
            "sma50": round(px / (1.25 if hot else 1.05), 2), "sma200": round(px * 0.85, 2),
            "atr14": round(px * 0.025, 2), "stage2_uptrend": True,
            "ret_3m_pct": 30.0 if hot else 8.0, "days_to_earnings": 30, "low_20d": round(px * .96, 2)}


data.fetch_price = price
data.fetch_snapshot = snapshot
data.fetch_market_context = lambda benches: [{"symbol": b, "price": price(b) or 1, "sma50": (price(b) or 1) * .97,
                                              "ret_3m_pct": 6.0} for b in benches]
executor.universe_for = lambda m: ((["NVDA", "AMD", "PLTR", "MU", "ARM", "RKLB"], ["SPY"]) if m == "US"
                                   else (["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS"], ["^NSEI"]))
notify._send = lambda msg: print("📱", msg.replace("\n", " | ")[:160])


# ── simulated AI (portfolio manager + skeptic) ───────────────────
class FakePM:
    # (symbol, setup, thesis, confidence): confidence ranks the suggestions list
    IDEAS = {"US": [("ARM", "PULLBACK", "Pullback to 50DMA, clean base", 0.8),
                    ("PLTR", "BREAKOUT", "Breakout to new highs", 0.6),
                    ("AMD", "PULLBACK", "Holding 50-day support", 0.55)],
             "INDIA": [("INFY.NS", "PULLBACK", "IT bounce off the 50DMA", 0.75),
                       ("TCS.NS", "PULLBACK", "Holding its rising 50DMA", 0.6),
                       ("HDFCBANK.NS", "BREAKOUT", "Near highs, volume only average", 0.45)]}

    def __init__(self, market):
        self.market = market

    def decide(self, prompt):
        acts = []
        for sym, setup, thesis, conf in self.IDEAS[self.market]:
            px = price(sym)
            stop = round(px * 0.97, 2)
            acts.append({"action": "BUY", "symbol": sym, "setup": setup, "stop": stop,
                         # 3R leaves room for the demo's price wobble, so an approval can
                         # still clear the bot's 2R re-check at fill time
                         "target": round(px + 3.0 * (px - stop), 2), "thesis": thesis + " (3.0R)",
                         "confidence": conf})
        return {"market_view": "Constructive tape; adding selectively on pullbacks.",
                "actions": acts, "watch_next": []}

    def self_review(self, *a):
        return "demo memo"


class FakeSkepticAI:
    def _call(self, system, user, max_tokens):
        sym = json.loads(user.split("\n", 1)[1].rsplit("\n\n", 1)[0])["proposal"]["symbol"]
        if sym == "PLTR":
            return json.dumps({"ruling": "SKIP", "summary": "RSI 81 and 25% above the 50DMA: chasing.",
                               "reasons": ["Overbought", "Extended", "Stop not at structure"]})
        return json.dumps({"ruling": "BUY", "summary": "Pullback to support with a clean stop; objections are minor.",
                           "reasons": ["Sector beta", "Target near resistance", "Volume soft"]})


class PrintTelegram:
    def post(self, url, json=None, timeout=None):
        if url.endswith("sendMessage"):
            print("📱", json["text"].replace("\n", " | ")[:160])
        return types.SimpleNamespace(json=lambda: {"result": {"message_id": 1}})

    def get(self, url, params=None, timeout=None):
        return types.SimpleNamespace(json=lambda: {"result": []})


# ── build the bot exactly like main.py does, minus the network ──
cfg = load_config()
journal = Journal(DB)
skeptic = Skeptic(FakeSkepticAI())
desk = ApprovalDesk(journal, "DEMO", "0", cfg.approval_timeout_min, cfg.approval_max_drift_pct,
                    http=PrintTelegram())
runners = {
    "US": MarketRunner("US", "USD", cfg, journal, FakePM("US"),
                       PaperLedger(journal, "US", cfg.us_start_cash), skeptic=skeptic, approvals=desk),
    "INDIA": MarketRunner("INDIA", "INR", cfg, journal, FakePM("INDIA"),
                          PaperLedger(journal, "INDIA", cfg.india_start_cash), skeptic=skeptic, approvals=desk,
                          suggest_only=cfg.india_mode == "suggest"),
}
desk.runners = runners


def seed():
    """Positions and watches placed relative to the current price, so the
    numbers stay sensible whatever each stock really trades at."""
    us, ind = runners["US"].ledger, runners["INDIA"].ledger

    def hold(ledger, sym, budget, stop_pct, r, thesis, setup):
        px = price(sym)
        stop = round(px * (1 - stop_pct), 2)
        ledger.buy(sym, max(1, int(budget / px)), stop, round(px + r * (px - stop), 2),
                   f"{thesis} ({r:.1f}R)", meta={"setup_type": setup, "planned_r": r})

    hold(us, "NVDA", 1100, 0.025, 2.0, "Pullback to EMA20 in a stage-2 trend", "PULLBACK")
    hold(us, "MU", 1100, 0.05, 2.4, "Memory upcycle continuation", "CONTINUATION")
    if not runners["INDIA"].suggest_only:      # a suggestions-only market holds nothing
        hold(ind, "RELIANCE.NS", 43000, 0.03, 2.4, "Base breakout", "BREAKOUT")
        hold(ind, "TCS.NS", 40000, 0.03, 2.5, "Pullback to 50DMA", "PULLBACK")
    # a closed winner and loser for the activity feed
    for sym, ex in (("RKLB", 1.06), ("HDFCBANK.NS", 0.98)):
        m = "INDIA" if sym.endswith(".NS") else "US"
        tid = journal.open_trade(m, sym, 10, BASE[sym], BASE[sym] * .96, BASE[sym] * 1.1, "seed", {})
        journal.close_trade(tid, round(BASE[sym] * ex, 2), "take_profit" if ex > 1 else "stop_loss")
    rk, inf = price("RKLB"), price("INFY.NS")
    journal.add_watch("US", "RKLB", "PULLBACK", round(rk * .97, 2), round(rk * .94, 2), round(rk * 1.06, 2),
                      "Wants a 3% dip to support (3R)")
    journal.add_watch("INDIA", "INFY.NS", "BREAKOUT", round(inf * 1.02, 2), round(inf * .99, 2),
                      round(inf * 1.09, 2), "Buy only on a break 2% higher")
    journal.log_decision("INDIA", {"market_view": "Nifty above its 50DMA; breadth improving.", "actions": []})
    runners["US"].run_cycle()           # AI ideas -> caps -> Skeptic -> approvals
    if runners["INDIA"].suggest_only:
        runners["INDIA"].run_cycle()    # AI ideas -> caps -> Skeptic -> ranked suggestions
    control.mark(journal, "US", "cycle")
    control.mark(journal, "INDIA", "cycle")


def api():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    import api_v2
    api_v2.app.run(host="127.0.0.1", port=int(os.getenv("API_PORT", "8000")),
                   debug=False, use_reloader=False)


def bot_loop():
    from main import MARKETS
    started = time.time()
    n = 0
    while True:
        try:
            journal.kv_set("heartbeat", str(time.time()))
            control.apply_flags(journal, runners, skeptic, desk, cfg)
            control.drain(journal, runners, desk)
            control.write_status(journal, runners, started, MARKETS)
            desk.poll()
            if n % 10 == 0:
                for m, r in runners.items():
                    r.monitor_positions()
                    control.mark(journal, m, "monitor")
        except Exception as exc:
            print("loop error:", exc)
        n += 1
        time.sleep(3)


if __name__ == "__main__":
    seed()
    threading.Thread(target=bot_loop, daemon=True).start()
    port = os.getenv("API_PORT", "8000")
    print(f"\n  Control dashboard:  http://localhost:{port}/control")
    print(f"  Token:              {os.environ['DASHBOARD_TOKEN']}")
    print("  Simulated market. Ctrl+C to stop.\n")
    api()
