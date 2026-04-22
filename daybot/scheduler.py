"""Automated scheduler for the day trading bot.

All jobs run inside the Flask process — no external cron needed.
Schedule (all times US/Eastern):
  Mon–Fri 20:00  Evening analysis     — Claude sub-agent deep analysis for next day
  Mon–Fri 09:00  Pre-market analysis  — price confirmation of evening watchlist (or fallback)
  Mon–Fri 09:35  Auto-start bot       — begins trading loop
  Mon–Fri 15:55  Auto-stop bot        — ensures loop is stopped after EOD close
  Daily   00:00  Daily reset          — clears halted flag, resets counters
  Sunday  08:00  Weekly report        — Telegram summary of the week
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: BackgroundScheduler | None = None


def _et_offset() -> int:
    """Returns UTC offset for Eastern Time: -4 (EDT summer) or -5 (EST winter)."""
    # APScheduler uses timezone strings — use America/New_York for DST handling
    return 0  # we pass timezone="America/New_York" to CronTrigger directly


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def _get_api_keys() -> tuple[str, str, str]:
    """Read API keys from env vars — works before the bot is manually started."""
    import os
    return (
        os.getenv("ANTHROPIC_API_KEY", ""),
        os.getenv("EXCHANGE_API_KEY", ""),
        os.getenv("EXCHANGE_API_SECRET", ""),
    )


def job_evening_analysis() -> None:
    """8:00 PM ET Mon–Fri — Claude sub-agent deep analysis for next day's watchlist."""
    try:
        from .evening_agent import run_evening_analysis
        anthropic_key, alpaca_key, alpaca_secret = _get_api_keys()
        if not alpaca_key:
            logging.warning("Scheduler: evening analysis skipped — EXCHANGE_API_KEY not set")
            return
        run_evening_analysis(
            anthropic_api_key=anthropic_key,
            alpaca_api_key=alpaca_key,
            alpaca_secret_key=alpaca_secret,
        )
    except Exception as exc:
        logging.exception("Scheduler: evening analysis failed: %s", exc)


def job_premarket() -> None:
    """9:00 AM ET — Price confirmation of evening watchlist (or full scan fallback)."""
    try:
        from .premarket import run_premarket_analysis
        anthropic_key, alpaca_key, alpaca_secret = _get_api_keys()
        if not alpaca_key:
            logging.warning("Scheduler: pre-market skipped — EXCHANGE_API_KEY not set")
            return
        run_premarket_analysis(
            anthropic_api_key=anthropic_key,
            alpaca_api_key=alpaca_key,
            alpaca_secret_key=alpaca_secret,
        )
    except Exception as exc:
        logging.exception("Scheduler: pre-market analysis failed: %s", exc)


def job_autostart() -> None:
    """9:35 AM ET — Auto-start the day bot if not already running."""
    try:
        from .state import day_state
        if day_state.running:
            logging.info("Scheduler: bot already running — skip auto-start")
            return

        from .blueprint import _start_bot_internal
        _start_bot_internal()
        logging.info("Scheduler: day bot auto-started at 9:35 AM ET")
        day_state.add_log("Scheduler", "Auto-started at 9:35 AM ET", "positive")
    except Exception as exc:
        logging.exception("Scheduler: auto-start failed: %s", exc)


def job_autostop() -> None:
    """3:55 PM ET — Ensure bot is stopped after EOD close window."""
    try:
        from .state import day_state
        from .blueprint import _stop_bot_internal
        if not day_state.running:
            return
        _stop_bot_internal()
        logging.info("Scheduler: day bot auto-stopped at 3:55 PM ET")
        day_state.add_log("Scheduler", "Auto-stopped after market close", "warning")
    except Exception as exc:
        logging.exception("Scheduler: auto-stop failed: %s", exc)


def job_daily_reset() -> None:
    """Midnight UTC — reset daily halted flag so bot trades again tomorrow."""
    try:
        from .state import day_state
        with day_state._lock:
            day_state.metrics.daily_loss_halted = False
            day_state.metrics.trades_today = 0
            day_state.metrics.wins_today = 0
            day_state.metrics.losses_today = 0
            day_state.metrics.daily_pnl = 0.0
            day_state.metrics.daily_pnl_pct = 0.0
        day_state.add_log("Scheduler", "Daily reset — counters cleared", "neutral")
        logging.info("Scheduler: daily reset complete")
    except Exception as exc:
        logging.exception("Scheduler: daily reset failed: %s", exc)


def job_weekly_report() -> None:
    """Sunday 8:00 AM ET — send weekly performance summary to Telegram."""
    try:
        from .db import get_recent_market_sessions, get_recent_trades
        from .state import day_state
        import os, requests

        sessions = get_recent_market_sessions(limit=5)
        if not sessions:
            return

        total_trades = sum(s.get("total_trades", 0) for s in sessions)
        total_wins = sum(s.get("wins", 0) for s in sessions)
        total_losses = sum(s.get("losses", 0) for s in sessions)
        total_pnl = sum(s.get("daily_pnl", 0) for s in sessions)
        win_rate = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0

        lines = [f"📊 <b>Day Bot Weekly Report</b>"]
        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"📅 Last 5 sessions")
        lines.append(f"💰 Total P&L: <b>${total_pnl:+.2f}</b>")
        lines.append(f"🎯 {total_trades} trades — {total_wins}W/{total_losses}L ({win_rate:.0f}% win rate)")
        lines.append(f"━━━━━━━━━━━━━━━")
        for s in sessions:
            regime_emoji = "📈" if s.get("market_regime") == "trending_up" else "📉" if s.get("market_regime") == "trending_down" else "↔️"
            lines.append(
                f"{regime_emoji} {s['trade_date']}: SPY {s.get('spy_return_pct', 0):+.1f}% | "
                f"Bot ${s.get('daily_pnl', 0):+.2f} ({s.get('wins', 0)}W/{s.get('losses', 0)}L)"
            )
        lines.append(f"━━━━━━━━━━━━━━━")
        lines.append(f"💼 Portfolio: <b>${day_state.metrics.portfolio_value:,.2f}</b>")

        message = "\n".join(lines)
        token = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
        logging.info("Scheduler: weekly report sent")
    except Exception as exc:
        logging.exception("Scheduler: weekly report failed: %s", exc)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    tz = "America/New_York"

    # Evening analysis: 8:00 PM ET, Mon–Fri (next day prep)
    _scheduler.add_job(
        job_evening_analysis, CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone=tz),
        id="evening_analysis", name="Evening sub-agent analysis",
    )
    # Pre-market: 9:00 AM ET, Mon–Fri
    _scheduler.add_job(
        job_premarket, CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=tz),
        id="premarket", name="Pre-market analysis",
    )
    # Auto-start: 9:35 AM ET, Mon–Fri
    _scheduler.add_job(
        job_autostart, CronTrigger(day_of_week="mon-fri", hour=9, minute=35, timezone=tz),
        id="autostart", name="Auto-start bot",
    )
    # Auto-stop: 3:55 PM ET, Mon–Fri
    _scheduler.add_job(
        job_autostop, CronTrigger(day_of_week="mon-fri", hour=15, minute=55, timezone=tz),
        id="autostop", name="Auto-stop bot",
    )
    # Daily reset: midnight UTC
    _scheduler.add_job(
        job_daily_reset, CronTrigger(hour=0, minute=0, timezone="UTC"),
        id="daily_reset", name="Daily reset",
    )
    # Weekly report: Sunday 8:00 AM ET
    _scheduler.add_job(
        job_weekly_report, CronTrigger(day_of_week="sun", hour=8, minute=0, timezone=tz),
        id="weekly_report", name="Weekly report",
    )

    _scheduler.start()
    logging.info(
        "Day bot scheduler started — jobs: evening-analysis 8:00PM, pre-market 9:00AM, "
        "auto-start 9:35AM, auto-stop 3:55PM, daily-reset midnight, weekly-report Sunday 8AM (all ET)"
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logging.info("Day bot scheduler stopped")
