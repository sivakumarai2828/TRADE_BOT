"""Read-only dashboard API for TRADE_BOT_V2.

Runs as its own systemd service (trade-bot-v2-api) on port 8000 so a crash
here can never affect the trading bot. Serves dashboard.html at / and JSON at
/api/summary, /api/trades, /api/activity, /api/decisions.
"""

from __future__ import annotations

import json
import os
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from botv2.config import load_config
from botv2.ledger import PaperLedger
from botv2.storage import make_journal

app = Flask(__name__)
CORS(app)

CFG = load_config()
BASE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = make_journal(CFG.db_path)  # SQLite or Supabase, same interface


def _journal():
    return JOURNAL


@app.route("/")
def index():
    return send_from_directory(BASE, "dashboard.html")


@app.route("/api/summary")
def summary():
    j = _journal()
    hb = float(j.kv_get("heartbeat", "0") or 0)
    out = {"_bot_alive": (time.time() - hb) < 120}
    for market, start_cash, currency in (("US", CFG.us_start_cash, "USD"),
                                         ("INDIA", CFG.india_start_cash, "INR")):
        ledger = PaperLedger(j, market, start_cash)
        positions = ledger.positions()
        equity = round(ledger.cash + sum(p["market_value"] for p in positions), 2)
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
            "cash": ledger.cash,
            "equity": equity,
            "total_return_pct": round((equity / start - 1) * 100, 2) if start else 0,
            "day_pnl": ledger.day_pnl(),
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
        }
    return jsonify(out)


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("API_PORT", "8000")))
