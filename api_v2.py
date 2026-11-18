"""Dashboard API for TRADE_BOT_V2/V3.

Read endpoints are open (as before). Control endpoints (/api/control/*,
POST only) require the X-Dashboard-Token header to match DASHBOARD_TOKEN and
are disabled entirely when DASHBOARD_TOKEN is unset. The API never trades
itself: it writes flags and queues commands that the bot process executes
(see botv2/control.py), so a crash here still cannot affect the bot.

Runs as its own systemd service (trade-bot-v2-api) on port 8000 so a crash
here can never affect the trading bot. Serves dashboard.html at / and JSON at
/api/summary, /api/trades, /api/activity, /api/decisions.

Broker mode (BROKER_TRADING=true, started with BOTV2_ENV_FILE=.env.live by
trade-bot-live-api.service): the same pages show the real-order bot. Cash,
equity and day P&L come from the Alpaca account, only the US market is
listed, every control endpoint is refused (read-only: entries are approved
in Telegram), and the server listens on 127.0.0.1 only, so it is reached
through an SSH tunnel and never through the public Vercel proxy.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from botv2 import control, suggest
from botv2.config import check_broker_config, load_config
from botv2.ledger import PaperLedger
from botv2.storage import make_journal

app = Flask(__name__)
CORS(app)

CFG = load_config()
BASE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = make_journal(CFG.db_path)  # SQLite or Supabase, same interface

# Which account the pages are showing: "paper" (virtual ledger),
# "alpaca-paper" (real orders, Alpaca practice account) or "live" (real money).
# "demo" is the local simulation (tools/demo_stack.py), so its pages can say so.
MODE = ("live" if CFG.live_real_money else "alpaca-paper" if CFG.broker_trading
        else "demo" if os.getenv("BOTV2_DEMO") else "paper")
BROKER = None                      # AlpacaLedger in broker mode, built on first use


def _broker():
    global BROKER
    if BROKER is None and _read_only():
        from botv2.alpaca_ledger import AlpacaClient, AlpacaLedger
        BROKER = AlpacaLedger(_journal(), "US",
                              AlpacaClient(CFG.alpaca_key, CFG.alpaca_secret, CFG.alpaca_base_url),
                              CFG.max_order_usd)
    return BROKER


def _read_only() -> bool:
    return MODE in ("live", "alpaca-paper")       # the broker-mode dashboards

# The Supabase client is not safe to share across threads: parallel requests
# (the control page loads 4 endpoints at once) failed with 500s. Requests are
# few and short, so serialise them all behind one lock.
_REQUEST_LOCK = threading.Lock()


@app.before_request
def _serialise():
    _REQUEST_LOCK.acquire()


@app.teardown_request
def _release(_exc=None):
    if _REQUEST_LOCK.locked():
        _REQUEST_LOCK.release()


def _journal():
    return JOURNAL


@app.route("/")
def index():
    return send_from_directory(BASE, "dashboard.html")


_health_cache = {"checked": 0.0, "alive": False}


@app.route("/health")
def health():
    """Uptime-monitor endpoint: 200 while the trading bot heartbeat is fresh,
    503 if the bot has been silent for 2+ minutes. Cached 30s because external
    monitors poll every ~15s and each check hits the journal backend."""
    now = time.time()
    if now - _health_cache["checked"] > 30:
        try:
            hb = float(_journal().kv_get("heartbeat", "0") or 0)
            _health_cache["alive"] = (now - hb) < 120
        except Exception:
            _health_cache["alive"] = False
        _health_cache["checked"] = now
    return jsonify({"ok": _health_cache["alive"]}), (200 if _health_cache["alive"] else 503)


@app.route("/api/summary")
def summary():
    j = _journal()
    hb = float(j.kv_get("heartbeat", "0") or 0)
    out = {"_bot_alive": (time.time() - hb) < 120, "_mode": MODE}
    broker = _broker()
    markets = ((("US", CFG.us_start_cash, "USD"),) if broker else
               (("US", CFG.us_start_cash, "USD"), ("INDIA", CFG.india_start_cash, "INR")))
    for market, start_cash, currency in markets:
        if broker:
            # Real account: every number is Alpaca's, never our own arithmetic.
            try:
                acct = broker.account_snapshot()
                positions = broker.positions()
            except Exception as exc:
                return jsonify({"error": f"Alpaca account unavailable: {exc}"[:300],
                                "_mode": MODE}), 502
            cash, equity, day_pnl = acct["cash"], acct["equity"], acct["day_pnl"]
            start = float(j.kv_get(f"{market}_start_cash") or 0)
        else:
            ledger = PaperLedger(j, market, start_cash)
            positions = ledger.positions()
            cash, day_pnl = ledger.cash, ledger.day_pnl()
            equity = round(cash + sum(p["market_value"] for p in positions), 2)
            start = float(j.kv_get(f"{market}_start_cash", str(start_cash)))
        last_dec = j.recent_decisions(market, 1)
        view = ""
        if last_dec:
            try:
                view = json.loads(last_dec[0]["raw_json"]).get("market_view", "")
            except Exception:
                pass
        out[market] = {
            "currency": currency,
            "cash": cash,
            "equity": equity,
            "total_return_pct": round((equity / start - 1) * 100, 2) if start else 0,
            "day_pnl": day_pnl,
            "killed": j.kv_get(f"{market}_killed") == "1",
            "positions": [
                {k: p.get(k) for k in ("symbol", "qty", "entry", "last_price", "stop",
                                       "target", "unrealized_pnl", "unrealized_pnl_pct",
                                       "thesis")}
                for p in positions
            ],
            "stats": j.stats(market),
            "market_view": view,
            "market_view_ts": last_dec[0]["ts"] if last_dec else None,
            "memo": j.latest_memo(market) or "",
            # Suggest-only market: no orders, a ranked list of buy suggestions instead.
            "suggest_only": (CFG.india_mode if market == "INDIA" else CFG.us_mode) == "suggest"
                            and not broker,
            "suggestions": suggest.attach_approvals(
                suggest.recent(j, market), control._load(j, "approval_requests", []), market),
            "suggestions_today": suggest.today(market),
            # Did today's scan already run? Lets the pages tell "nothing passed"
            # apart from "not scanned yet".
            "scanned_today": bool(last_dec) and suggest.day_of(last_dec[0]["ts"], market) == suggest.today(market),
            "option_ideas": _india_option_ideas(j) if market == "INDIA" else [],
        }
    if CFG.options_enabled and not broker:
        try:
            from botv2.options_desk import OptionsDesk
            out["OPTIONS"] = OptionsDesk(j, CFG, chains=object()).summary()   # no quotes fetched here
        except Exception as exc:
            out["OPTIONS"] = {"error": f"options summary failed: {type(exc).__name__}"}
    return jsonify(out)


def _india_option_ideas(j) -> list:
    if not CFG.india_options_enabled:
        return []
    try:
        from botv2.india_options import IndiaOptionIdeas
        return IndiaOptionIdeas(j, CFG).recent()
    except Exception:
        return []


@app.route("/api/trades")
def trades():
    market = request.args.get("market", "US")
    return jsonify(_journal().all_trades(market, 50))


@app.route("/api/activity")
def activity():
    """Merged feed: AI decisions + closed trades, newest first."""
    market = request.args.get("market", "US")
    feed = []
    for r in _journal().recent_decisions(market, 15):
        try:
            d = json.loads(r["raw_json"])
            feed.append({"ts": r["ts"], "kind": "decision",
                         "text": d.get("market_view", ""),
                         "actions": len(d.get("actions", []))})
        except Exception:
            pass
    for t in _journal().recent_closed(market, 15):
        feed.append({"ts": t["ts_close"], "kind": "trade",
                     "text": f"Closed {t['symbol']} ({t['exit_reason']})", "pnl": t["pnl"]})
    feed.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return jsonify(feed[:20])


@app.route("/api/decisions")
def decisions():
    market = request.args.get("market", "US")
    rows = _journal().recent_decisions(market, 10)
    for r in rows:
        try:
            r["decision"] = json.loads(r.pop("raw_json"))
        except Exception:
            r["decision"] = {}
    return jsonify(rows)


# ── control (v3) ─────────────────────────────────────────────────
@app.route("/control")
def control_page():
    return send_from_directory(BASE, "control.html")


def _token_ok() -> bool:
    expected = os.getenv("DASHBOARD_TOKEN", "")
    given = request.headers.get("X-Dashboard-Token", "")
    return bool(expected) and hmac.compare_digest(expected, given)


def _authorized() -> bool:
    if _read_only():               # broker mode: no token unlocks anything
        return False
    return _token_ok()


def _deny():
    if _read_only():
        return jsonify({"error": "read-only: this dashboard shows the real-order bot. "
                                 "Approve entries in Telegram."}), 403
    if not os.getenv("DASHBOARD_TOKEN"):
        return jsonify({"error": "controls disabled: set DASHBOARD_TOKEN in the API's .env"}), 403
    return jsonify({"error": "wrong or missing dashboard token"}), 401


@app.route("/api/control")
def control_state():
    snap = control.snapshot(_journal(), {
        "skeptic": CFG.skeptic_enabled, "approval": CFG.approval_mode == "telegram"})
    snap["mode"], snap["read_only"] = MODE, _read_only()
    snap["controls_enabled"] = bool(os.getenv("DASHBOARD_TOKEN")) and not _read_only()
    if _read_only():
        snap["markets"] = {"US": snap["markets"]["US"]}
        snap["gates"]["approval"] = True       # forced on by the bot in broker mode
    return jsonify(snap)


@app.route("/api/control/auth", methods=["POST"])
def control_auth():
    return (jsonify({"ok": True}) if _authorized() else _deny())


@app.route("/api/control/flag", methods=["POST"])
def control_flag():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    flag, value = body.get("flag"), bool(body.get("value"))
    j = _journal()
    if flag == "paused":
        market = body.get("market")
        if market not in ("US", "INDIA"):
            return jsonify({"error": "market must be US or INDIA"}), 400
        control.set_paused(j, market, value)
    elif flag in ("skeptic", "approval"):
        control.set_gate(j, flag, value)
    else:
        return jsonify({"error": f"unknown flag {flag!r}"}), 400
    return jsonify({"ok": True, "flag": flag, "value": value})


@app.route("/api/control/cmd", methods=["POST"])
def control_cmd():
    if not _authorized():
        return _deny()
    body = request.get_json(silent=True) or {}
    try:
        item = control.enqueue(_journal(), body.get("cmd", ""), body.get("market"),
                               body.get("arg"), source="dashboard")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "queued": item})


if __name__ == "__main__":
    errs = check_broker_config(CFG)
    if errs:
        raise SystemExit("Refusing to start the broker-mode dashboard:\n  - " + "\n  - ".join(errs))
    # Broker mode listens on localhost only (SSH tunnel); paper mode as before.
    host = os.getenv("API_HOST", "127.0.0.1" if _read_only() else "0.0.0.0")
    if _read_only() and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Broker-mode dashboard must listen on localhost (API_HOST=127.0.0.1)")
    app.run(host=host, port=int(os.getenv("API_PORT", "8001" if _read_only() else "8000")))
