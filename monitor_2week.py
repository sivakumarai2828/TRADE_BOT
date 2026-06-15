"""2-Week Performance Monitor — daily snapshot of all 3 bots.

Run manually:   python monitor_2week.py
Auto-runs:      added to scheduler at 4:00 PM ET every trading day

Tracks:
  - Day Bot    ($1,000 paper)  → daily P&L, win rate, trade count
  - Options    ($1,000 paper)  → daily P&L, win rate, contract details
  - Crypto     (REAL money)    → daily P&L, balance, win rate

Sends Telegram summary each evening.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, date

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

MONITOR_FILE = os.path.join(os.path.dirname(__file__), "monitor_log.json")
MONITOR_START = os.getenv("MONITOR_START_DATE", str(date.today()))
MONITOR_DAYS  = int(os.getenv("MONITOR_DAYS", "14"))

# ── Targets (what we expect in 2 weeks) ─────────────────────────────────────
_TARGETS = {
    "day":     {"conservative": 20.0,  "likely": 40.0,   "best": 80.0},   # $ profit
    "options": {"conservative": 0.0,   "likely": 150.0,  "best": 400.0},  # $ profit
    "crypto":  {"conservative": 15.0,  "likely": 35.0,   "best": 70.0},   # $ profit (per $1K real)
}


def _load_log() -> dict:
    if not os.path.exists(MONITOR_FILE):
        return {"start_date": MONITOR_START, "days": [], "summary": {}}
    try:
        with open(MONITOR_FILE) as f:
            return json.load(f)
    except Exception:
        return {"start_date": MONITOR_START, "days": [], "summary": {}}


def _save_log(data: dict) -> None:
    with open(MONITOR_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _get_day_bot_snapshot() -> dict:
    """Pull today's metrics from day bot state."""
    try:
        from daybot.state import day_state
        m = day_state.metrics
        return {
            "running":       day_state.running,
            "portfolio":     round(m.portfolio_value or 1000.0, 2),
            "daily_pnl":     round(m.daily_pnl or 0.0, 2),
            "trades_today":  m.trades_today,
            "wins_today":    m.wins_today,
            "losses_today":  m.losses_today,
            "win_rate":      round(m.wins_today / max(1, m.wins_today + m.losses_today) * 100, 1),
            "mode":          m.mode if hasattr(m, "mode") else "SAFE",
            "halted":        m.daily_loss_halted,
        }
    except Exception as exc:
        logging.warning("Day bot snapshot failed: %s", exc)
        return {"portfolio": 1000.0, "daily_pnl": 0.0, "trades_today": 0, "wins_today": 0, "losses_today": 0, "win_rate": 0.0}


def _get_options_snapshot() -> dict:
    """Pull today's metrics from options bot state."""
    try:
        from optionsbot.state import options_state
        m = options_state.metrics
        open_pos = len(options_state.positions)
        return {
            "balance":       round(m.balance, 2),
            "daily_pnl":     round(m.daily_pnl, 2),
            "wins_today":    m.wins_today,
            "losses_today":  m.losses_today,
            "total_trades":  m.total_trades,
            "win_rate":      round(m.win_rate, 1),
            "open_positions": open_pos,
            "total_pnl":     round(m.total_pnl, 2),
            "mode":          m.mode,
            "halted":        m.daily_loss_halted,
        }
    except Exception as exc:
        logging.warning("Options snapshot failed: %s", exc)
        return {"balance": 1000.0, "daily_pnl": 0.0, "wins_today": 0, "losses_today": 0, "win_rate": 0.0, "total_pnl": 0.0}


def _get_crypto_snapshot() -> dict:
    """Pull today's metrics from crypto bot state."""
    try:
        from state import bot_state
        m = bot_state.metrics
        open_count = sum(1 for p in bot_state.positions.values() if p is not None)
        daily_pnl = round(m.balance - m.daily_start_balance, 2) if m.daily_start_balance > 0 else 0.0
        return {
            "running":       bot_state.running,
            "balance":       round(m.balance, 2),
            "daily_pnl":     daily_pnl,
            "total_pnl":     round(m.pnl, 2),
            "trades_today":  m.daily_trades_count,
            "total_trades":  m.total_trades,
            "wins":          m.win_count,
            "losses":        m.loss_count,
            "win_rate":      round(m.win_count / max(1, m.win_count + m.loss_count) * 100, 1),
            "open_positions": open_count,
            "mode":          m.mode if hasattr(m, "mode") else "SAFE",
            "halted":        m.daily_loss_halted,
        }
    except Exception as exc:
        logging.warning("Crypto snapshot failed: %s", exc)
        return {"balance": 0.0, "daily_pnl": 0.0, "total_pnl": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0}


def _get_capital_snapshot() -> dict:
    """Pull capital manager summary."""
    try:
        from capital_manager import capital_manager
        return capital_manager.status()
    except Exception as exc:
        logging.warning("Capital snapshot failed: %s", exc)
        return {}


def take_snapshot() -> dict:
    """Record today's performance snapshot."""
    today = str(date.today())
    snapshot = {
        "date":    today,
        "time":    datetime.now(timezone.utc).isoformat(),
        "day":     _get_day_bot_snapshot(),
        "options": _get_options_snapshot(),
        "crypto":  _get_crypto_snapshot(),
        "capital": _get_capital_snapshot(),
    }

    log = _load_log()
    # Replace today's entry if already exists, else append
    existing = [d for d in log["days"] if d["date"] != today]
    existing.append(snapshot)
    log["days"] = existing

    # Running totals
    day_pnl_total     = sum(d["day"].get("daily_pnl", 0) for d in log["days"])
    opts_pnl_total    = snapshot["options"].get("total_pnl", 0)
    crypto_pnl_total  = snapshot["crypto"].get("total_pnl", 0)
    total_pnl         = day_pnl_total + opts_pnl_total + crypto_pnl_total

    days_elapsed = len({d["date"] for d in log["days"]})
    log["summary"] = {
        "days_elapsed":       days_elapsed,
        "days_remaining":     max(0, MONITOR_DAYS - days_elapsed),
        "day_pnl_total":      round(day_pnl_total, 2),
        "options_pnl_total":  round(opts_pnl_total, 2),
        "crypto_pnl_total":   round(crypto_pnl_total, 2),
        "total_pnl":          round(total_pnl, 2),
        "on_track":           total_pnl >= (days_elapsed / MONITOR_DAYS) * _TARGETS["day"]["likely"],
    }

    _save_log(log)
    return snapshot


def print_report() -> None:
    """Print and Telegram the 2-week monitoring report."""
    snap = take_snapshot()
    log  = _load_log()
    summ = log["summary"]

    day  = snap["day"]
    opts = snap["options"]
    cry  = snap["crypto"]
    cap  = snap["capital"]

    day_start  = 1000.0
    opts_start = 1000.0
    cry_start  = cry.get("balance", 0) - cry.get("total_pnl", 0)

    day_total_pnl  = summ.get("day_pnl_total", 0)
    opts_total_pnl = summ.get("options_pnl_total", 0)
    cry_total_pnl  = summ.get("crypto_pnl_total", 0)
    total_pnl      = summ.get("total_pnl", 0)

    lines = [
        f"📊 <b>2-Week Monitor — Day {summ['days_elapsed']}/{MONITOR_DAYS}</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"📈 <b>Day Bot</b> (Paper $1,000)",
        f"  Today:  ${day.get('daily_pnl', 0):+.2f} | Trades: {day.get('trades_today', 0)} ({day.get('wins_today', 0)}W/{day.get('losses_today', 0)}L)",
        f"  Total:  ${day_total_pnl:+.2f} ({day_total_pnl/day_start*100:+.1f}%) | WR: {day.get('win_rate', 0):.0f}%",
        f"  Mode:   {day.get('mode', 'SAFE')} {'⛔ HALTED' if day.get('halted') else '✅ Active'}",
        f"",
        f"⚡ <b>Options Bot</b> (Paper $1,000)",
        f"  Today:  ${opts.get('daily_pnl', 0):+.2f} | Open: {opts.get('open_positions', 0)} contracts",
        f"  Total:  ${opts_total_pnl:+.2f} ({opts_total_pnl/opts_start*100:+.1f}%) | WR: {opts.get('win_rate', 0):.0f}%",
        f"  Trades: {opts.get('total_trades', 0)} | Mode: {opts.get('mode', 'SAFE')} {'⛔ HALTED' if opts.get('halted') else '✅ Active'}",
        f"",
        f"₿ <b>Crypto Bot</b> (REAL MONEY 🔴)",
        f"  Today:  ${cry.get('daily_pnl', 0):+.2f} | Trades: {cry.get('trades_today', 0)}",
        f"  Total:  ${cry_total_pnl:+.2f} | Balance: ${cry.get('balance', 0):.2f}",
        f"  WR: {cry.get('win_rate', 0):.0f}% | Open: {cry.get('open_positions', 0)} {'⛔ HALTED' if cry.get('halted') else '✅ Active'}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"💰 <b>Combined P&L: ${total_pnl:+.2f}</b>",
        f"📅 Days left: {summ['days_remaining']}",
        f"🎯 2-week target range: $50–$500",
    ]

    report = "\n".join(lines)
    print(report.replace("<b>", "").replace("</b>", ""))

    try:
        from telegram_notify import _send
        _send(report)
        logging.info("2-week monitor report sent to Telegram")
    except Exception as exc:
        logging.warning("Telegram send failed: %s", exc)

    return summ


if __name__ == "__main__":
    print_report()
