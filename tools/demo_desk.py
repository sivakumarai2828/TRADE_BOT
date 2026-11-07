"""Local walkthrough of the v3 desk gates — no API keys, no network, no money.

Runs the REAL MarketRunner, RiskEngine, PaperLedger, Skeptic and ApprovalDesk
against a throwaway SQLite file. Only three things are faked:
  - prices/snapshots   (fixed numbers, markets are not needed)
  - the AI             (scripted PM proposals and Skeptic rulings)
  - Telegram           (messages are printed here; taps are simulated)

    python tools/demo_desk.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("TELEGRAM_TOKEN", None)          # notify.py must not reach Telegram

from botv2 import data, notify                   # noqa: E402
from botv2.approval import ApprovalDesk          # noqa: E402
from botv2.config import HardCaps                # noqa: E402
from botv2.executor import MarketRunner          # noqa: E402
from botv2.journal import Journal                # noqa: E402
from botv2.ledger import PaperLedger             # noqa: E402
from botv2.skeptic import Skeptic                # noqa: E402

LINE = "─" * 72


def say(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


def plain(html_text: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


# ── fake market ──────────────────────────────────────────────────
PRICES = {"NVDA": 182.40, "PLTR": 160.00, "AMD": 240.00, "MU": 120.00}
SNAPS = {
    "NVDA": {"symbol": "NVDA", "price": 182.40, "rsi14": 56, "sma50": 171, "sma200": 150,
             "stage2_uptrend": True, "rs_3m_vs_benchmark": 9.5, "days_to_earnings": 20},
    "PLTR": {"symbol": "PLTR", "price": 160.00, "rsi14": 81, "sma50": 128, "sma200": 101,
             "stage2_uptrend": True, "rs_3m_vs_benchmark": 31.0, "days_to_earnings": 12},
    "AMD":  {"symbol": "AMD", "price": 240.00, "rsi14": 52, "sma50": 228, "sma200": 190,
             "stage2_uptrend": True, "rs_3m_vs_benchmark": 4.1, "days_to_earnings": 25},
    "MU":   {"symbol": "MU", "price": 120.00, "rsi14": 49, "sma50": 115, "sma200": 98,
             "stage2_uptrend": True, "rs_3m_vs_benchmark": 6.0, "days_to_earnings": 40},
}
data.fetch_price = lambda s: PRICES.get(s)
data.fetch_snapshot = lambda s, **kw: SNAPS.get(s)
notify._send = lambda msg: print("  📱 Telegram:", plain(msg).replace("\n", "\n               "))


# ── scripted Skeptic (stands in for the second AI call) ──────────
class ScriptedSkepticAI:
    RULINGS = {
        "PLTR": ("SKIP", ["RSI 81 — extended 25% above the 50DMA, buying the top of the move",
                          "Stop sits in empty air, not under real structure",
                          "Relative strength this stretched usually mean-reverts first"],
                 "Great stock, terrible entry — chasing an overbought spike."),
        "NVDA": ("BUY", ["Earnings in 20 days could cut the hold short",
                         "Semis are crowded; a sector pullback would hit it",
                         "2.0R target is only just above the minimum"],
                 "Objections are manageable: stage-2 trend, pullback to support, clean stop."),
        "AMD":  ("BUY", ["Relative strength only +4%, weakest of the group",
                         "Target near prior high may cap the move",
                         "Volume confirmation is soft"],
                 "Acceptable setup at support with a defined stop."),
        "MU":   ("BUY", ["Memory cycle risk", "Sector beta", "Target is ambitious"],
                 "Reasonable pullback entry."),
    }

    def _call(self, system, user, max_tokens):
        sym = re.search(r'"symbol": "(\w+)"', user).group(1)
        ruling, reasons, summary = self.RULINGS[sym]
        return json.dumps({"reasons": reasons, "ruling": ruling, "summary": summary})


# ── fake Telegram transport for the ApprovalDesk ─────────────────
class ConsoleTelegram:
    def __init__(self):
        self.updates, self.next_msg = [], 1000

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        if method == "sendMessage":
            print("  📱 Telegram:", plain(json["text"]).replace("\n", "\n               "))
            if json.get("reply_markup"):
                btns = json["reply_markup"]["inline_keyboard"][0]
                print("               [ " + " ]  [ ".join(b["text"] for b in btns) + " ]")
        elif method == "editMessageText":
            last = plain(json["text"]).strip().splitlines()[-1]
            print(f"  📱 Telegram (message updated): {last}")
        elif method == "answerCallbackQuery":
            print(f"  📱 Telegram (tap result): {json['text']}")
        self.next_msg += 1
        return types.SimpleNamespace(json=lambda: {"result": {"message_id": self.next_msg}})

    def get(self, url, params=None, timeout=None):
        ups, self.updates = self.updates, []
        return types.SimpleNamespace(json=lambda: {"result": ups})

    def tap(self, button: str, req_id: str, chat: str = "42"):
        self.updates.append({"update_id": len(req_id) + self.next_msg, "callback_query": {
            "id": "cb", "data": f"{'ap' if button == 'approve' else 'rj'}:{req_id}",
            "message": {"chat": {"id": int(chat)}}}})


def main() -> None:
    db = os.path.join(tempfile.mkdtemp(), "demo.db")
    journal = Journal(db)
    caps = HardCaps(max_position_pct=0.25, max_risk_per_trade_pct=0.02,
                    max_open_positions=8, max_new_entries_per_day=5)
    tg = ConsoleTelegram()
    desk = ApprovalDesk(journal, "DEMO", "42", timeout_min=90,
                        max_drift_pct=0.015, http=tg)
    runner = MarketRunner(
        "US", "USD", types.SimpleNamespace(caps=caps, target_deployment_pct=0.0),
        journal, ai=None, ledger=PaperLedger(journal, "US", 5000.0),
        skeptic=Skeptic(ScriptedSkepticAI()), approvals=desk)
    runner._regime_label = "BULL"
    runner._snapshots = SNAPS
    desk.runners = {"US": runner}

    say("STEP 1 — The PM (AI portfolio manager) proposes 4 BUYs at 10:00 ET")
    actions = [
        {"action": "BUY", "symbol": "MU",   "setup": "PULLBACK", "stop": 116.0, "target": 123.0,
         "thesis": "Pullback to 50DMA (0.75R)"},
        {"action": "BUY", "symbol": "PLTR", "setup": "BREAKOUT", "stop": 152.0, "target": 180.0,
         "thesis": "Momentum breakout to new highs (2.5R)"},
        {"action": "BUY", "symbol": "NVDA", "setup": "PULLBACK", "stop": 178.80, "target": 189.60,
         "thesis": "Pullback to EMA20 in stage-2 trend (2.0R)"},
        {"action": "BUY", "symbol": "AMD",  "setup": "PULLBACK", "stop": 234.0, "target": 252.0,
         "thesis": "Holding 50DMA support (2.0R)"},
    ]
    for a in actions:
        print(f"  PM → BUY {a['symbol']:5} stop {a['stop']:>7}  target {a['target']:>7}  — {a['thesis']}")

    say("STEP 2 — Gate 1: code risk caps  →  Gate 2: Skeptic  →  Gate 3: you")
    by_symbol: dict = {}
    for a in actions:
        print(f"\n▶ {a['symbol']}")
        before = len(desk.pending())
        runner._apply(a, by_symbol, halt=None, earnings={s: v["days_to_earnings"] for s, v in SNAPS.items()})
        skeptic_log = json.loads(journal.kv_get("US_skeptic_log") or "[]")
        reviewed = any(e["symbol"] == a["symbol"] for e in skeptic_log)
        if not reviewed:
            print("  ⛔ Rejected by CODE risk caps (reward/risk < 2.0R) — Skeptic never called")
        elif len(desk.pending()) > before:
            print("  ⏳ Passed caps + Skeptic → waiting for your tap. Nothing bought yet.")

    print(f"\n  Positions right now: {[p['symbol'] for p in runner.ledger.positions()] or 'none'}")
    pend = {r["symbol"]: r["id"] for r in desk.pending()}
    print(f"  Pending approvals:  {', '.join(f'{s} ({i})' for s, i in pend.items())}")

    say("STEP 3 — You tap ✅ Approve on NVDA (price barely moved: 182.40 → 182.10)")
    PRICES["NVDA"] = 182.10
    tg.tap("approve", pend["NVDA"])
    desk.poll()

    say("STEP 4 — AMD rallies 2.5% before you tap ✅ Approve (240.00 → 246.00)")
    PRICES["AMD"] = 246.00
    tg.tap("approve", pend["AMD"])
    desk.poll()
    print("  → Bot refuses: buying 2.5% higher wrecks the planned reward/risk.")

    say("STEP 5 — Result")
    for p in runner.ledger.positions():
        print(f"  HOLDING {p['symbol']}: {p['qty']:.0f} sh @ {p['entry']:.2f}  "
              f"stop {p['stop']}  target {p['target']}")
    print(f"  Cash left: ${runner.ledger.cash:,.2f} of $5,000 paper")
    log_ = json.loads(journal.kv_get("US_skeptic_log"))
    buys = sum(e["ruling"] == "BUY" for e in log_)
    print(f"  Skeptic: reviewed {len(log_)}, approved {buys}, refused {len(log_) - buys}")
    print("\n  Of 4 AI ideas: 1 failed the caps, 1 refused by Skeptic, 1 cancelled at fill,")
    print("  1 bought — only after your tap and a re-check at the live price.\n")


if __name__ == "__main__":
    main()
