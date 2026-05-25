#!/usr/bin/env python3
"""
log_monitor.py — Real-time trade-bot log monitor with auto-fix + Telegram alerts.

Runs as a separate systemd service (trade-bot-monitor.service).
Tails journalctl for trade-bot, detects problem patterns,
auto-fixes where possible, alerts Telegram on every action.

Patterns monitored:
  STALL       — no "Cycle BEGIN" for 15 min  → restart bot
  TSLA_LEAK   — "TSLA: bar fetch" in logs    → restart bot (daybot leaked)
  PF_FLOOD    — get_portfolio_value 5+ fails  → restart bot
  INSUF_BAL   — 40310000 insufficient balance → alert (sizing bug)
  CYCLE_ERR   — 3+ cycle errors in 10 min    → alert (network issues)
  CONSEC_SL   — 3 stop-losses in 60 min      → alert (flash crash risk)
  LOW_BAL     — equity < $400               → alert (capital depleted)
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import logging
from collections import deque
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SERVICE      = "trade-bot"
ENV_FILE     = os.path.expanduser("~/TRADE_BOT/.env")
LOG_FILE     = os.path.expanduser("~/trade_bot_monitor.log")
CHECK_INTERVAL = 60          # seconds between periodic balance checks
STALL_MINUTES  = 15          # no Cycle BEGIN for this long = stall
COOLDOWN_SECS  = 300         # min gap between same-type actions (prevent restart loops)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MONITOR] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def _tg(token: str, chat_id: str, msg: str) -> None:
    """Send Telegram message — fire and forget."""
    if not token or not chat_id:
        return
    try:
        import urllib.request, urllib.parse, json
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logging.warning("Telegram send failed: %s", e)


def _restart_bot(token: str, chat_id: str, reason: str) -> None:
    """Restart trade-bot service and notify."""
    logging.warning("AUTO-FIX: restarting trade-bot — reason: %s", reason)
    _tg(token, chat_id,
        f"🔧 <b>Auto-fix triggered</b>\n"
        f"Reason: <code>{reason}</code>\n"
        f"Action: restarting trade-bot service...")
    try:
        subprocess.run(["sudo", "systemctl", "restart", SERVICE], timeout=30)
        time.sleep(10)
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "active":
            logging.info("Restart OK")
            _tg(token, chat_id, "✅ <b>trade-bot restarted successfully</b>")
        else:
            logging.error("Restart FAILED")
            _tg(token, chat_id, "🔴 <b>trade-bot FAILED to restart</b> — manual action needed!")
    except Exception as e:
        logging.error("Restart exception: %s", e)
        _tg(token, chat_id, f"🔴 Restart exception: <code>{e}</code>")


def _check_balance(token: str, chat_id: str, low_bal_threshold: float = 400.0) -> None:
    """Periodic Alpaca equity check — alert if below threshold."""
    env = _load_env()
    api_key = env.get("EXCHANGE_API_KEY", "")
    api_secret = env.get("EXCHANGE_API_SECRET", "")
    if not api_key:
        return
    try:
        sys_path = os.path.expanduser("~/TRADE_BOT")
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, api_secret, paper=False)
        acct = client.get_account()
        equity = float(acct.equity)
        logging.info("Balance check: equity=$%.2f", equity)
        if equity < low_bal_threshold:
            _tg(token, chat_id,
                f"🚨 <b>LOW BALANCE ALERT</b>\n"
                f"Equity: <b>${equity:.2f}</b> (threshold: ${low_bal_threshold:.0f})\n"
                f"Started at $500 — consider pausing bot.")
    except Exception as e:
        logging.warning("Balance check failed: %s", e)


# ---------------------------------------------------------------------------
# Pattern state
# ---------------------------------------------------------------------------

class MonitorState:
    def __init__(self) -> None:
        self.last_cycle_begin: datetime = datetime.utcnow()
        self.pf_fail_times:  deque[datetime] = deque(maxlen=10)
        self.cycle_err_times: deque[datetime] = deque(maxlen=10)
        self.sl_times:        deque[datetime] = deque(maxlen=10)
        self.tsla_count:      int = 0
        # Cooldown tracker: action_type → last_fired datetime
        self.last_action: dict[str, datetime] = {}

    def can_act(self, action: str) -> bool:
        last = self.last_action.get(action)
        if last is None:
            return True
        return (datetime.utcnow() - last).total_seconds() > COOLDOWN_SECS

    def mark_acted(self, action: str) -> None:
        self.last_action[action] = datetime.utcnow()


def _process_line(line: str, state: MonitorState, token: str, chat_id: str) -> None:
    now = datetime.utcnow()

    # --- Cycle BEGIN — update stall timer ---
    if "Cycle BEGIN" in line:
        state.last_cycle_begin = now
        state.tsla_count = 0  # reset TSLA counter on healthy cycle

    # --- TSLA leak (daybot running on crypto VM) ---
    if "TSLA: bar fetch returned None" in line:
        state.tsla_count += 1
        if state.tsla_count >= 5 and state.can_act("tsla_leak"):
            state.mark_acted("tsla_leak")
            state.tsla_count = 0
            _restart_bot(token, chat_id, "Daybot leaked onto crypto VM (TSLA bar fetch spam)")

    # --- get_portfolio_value flood ---
    if "get_portfolio_value failed" in line:
        state.pf_fail_times.append(now)
        recent = [t for t in state.pf_fail_times if (now - t).total_seconds() < 300]
        if len(recent) >= 5 and state.can_act("pf_flood"):
            state.mark_acted("pf_flood")
            _restart_bot(token, chat_id, "get_portfolio_value failing 5+ times in 5 min")

    # --- Insufficient balance (40310000) ---
    if "40310000" in line and "insufficient" in line.lower():
        if state.can_act("insuf_bal"):
            state.mark_acted("insuf_bal")
            _tg(token, chat_id,
                "⚠️ <b>Insufficient balance error (40310000)</b>\n"
                "Sizing bug may have returned. Check execution.py buying_power logic.")

    # --- Cycle errors (network/API) ---
    if "Cycle error" in line:
        state.cycle_err_times.append(now)
        recent = [t for t in state.cycle_err_times if (now - t).total_seconds() < 600]
        if len(recent) >= 3 and state.can_act("cycle_err"):
            state.mark_acted("cycle_err")
            _tg(token, chat_id,
                f"⚠️ <b>3 cycle errors in 10 min</b>\n"
                f"Last error: <code>{line.strip()[-120:]}</code>\n"
                f"Network/Alpaca API issue — monitoring.")

    # --- Consecutive stop-losses ---
    if 'reason="stop_loss"' in line or "stop_loss" in line and "close" in line.lower():
        state.sl_times.append(now)
        recent = [t for t in state.sl_times if (now - t).total_seconds() < 3600]
        if len(recent) >= 3 and state.can_act("consec_sl"):
            state.mark_acted("consec_sl")
            _tg(token, chat_id,
                "🔴 <b>3 stop-losses in 60 min</b>\n"
                "Possible flash crash or bad market conditions.\n"
                "Consider pausing bot via Telegram /stop command.")

    # --- Double-thread detection ---
    if "start already in progress" in line.lower() or "thread already alive" in line.lower():
        if state.can_act("double_thread"):
            state.mark_acted("double_thread")
            _restart_bot(token, chat_id, "Double-thread detected")


def _check_stall(state: MonitorState, token: str, chat_id: str) -> None:
    """Called periodically — check if bot cycle has stalled."""
    mins_since = (datetime.utcnow() - state.last_cycle_begin).total_seconds() / 60
    if mins_since >= STALL_MINUTES:
        if state.can_act("stall"):
            state.mark_acted("stall")
            _restart_bot(token, chat_id,
                f"Bot stalled — no Cycle BEGIN for {mins_since:.0f} min")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    logging.info("Log monitor starting — watching service: %s", SERVICE)
    env = _load_env()
    token   = env.get("TELEGRAM_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logging.warning("No Telegram creds — alerts will be logged only")

    _tg(token, chat_id, "🟢 <b>Log monitor started</b> — watching trade-bot for issues")

    state = MonitorState()
    last_bal_check = datetime.utcnow()

    # Tail journalctl in real-time
    proc = subprocess.Popen(
        ["journalctl", "-u", SERVICE, "-f", "--no-pager", "-n", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    logging.info("Tailing journalctl for %s...", SERVICE)

    import select
    while True:
        # Non-blocking read with 5-sec timeout for periodic checks
        ready, _, _ = select.select([proc.stdout], [], [], 5.0)
        if ready:
            line = proc.stdout.readline()
            if line:
                _process_line(line, state, token, chat_id)
        else:
            # Timeout — run periodic checks
            _check_stall(state, token, chat_id)
            now = datetime.utcnow()
            if (now - last_bal_check).total_seconds() > CHECK_INTERVAL:
                last_bal_check = now
                _check_balance(token, chat_id)

        # Check if tailed process died
        if proc.poll() is not None:
            logging.error("journalctl process died — restarting tail")
            time.sleep(5)
            proc = subprocess.Popen(
                ["journalctl", "-u", SERVICE, "-f", "--no-pager", "-n", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )


if __name__ == "__main__":
    main()
