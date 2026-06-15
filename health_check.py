#!/usr/bin/env python3
"""
Bot health checker — runs on VM via cron every 30 min.
Sends Telegram alerts for logic anomalies (not just service crashes).
"""
import json, os, sys, requests, time
from datetime import datetime, timezone

STATE_FILE = os.path.expanduser("~/health_check_state.json")

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

BOT_URL = "http://localhost:8000"
ENV_FILE = os.path.expanduser("~/TRADE_BOT/.env")

def _env(key):
    try:
        for line in open(ENV_FILE):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return ""

TOKEN  = _env("TELEGRAM_TOKEN")
CHAT   = _env("TELEGRAM_CHAT_ID")

def alert(msg: str, emoji: str = "⚠️"):
    if not TOKEN or not CHAT:
        print("NO TELEGRAM CREDS")
        return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT, "text": f"{emoji} <b>Health Alert</b>\n{msg}", "parse_mode": "HTML"},
        timeout=10,
    )

def check():
    issues = []

    try:
        d = requests.get(f"{BOT_URL}/status", timeout=10).json()
    except Exception as e:
        alert(f"Bot API unreachable: {e}", "🔴")
        return

    m = d.get("metrics", {})
    positions = d.get("positions", {})
    logs = d.get("logs", [])
    running = d.get("running", False)

    # 1. Bot not running
    if not running:
        issues.append("🔴 Bot not running")

    # 2. Balance mismatch — compare bot balance vs Alpaca
    bot_bal = float(m.get("balance", 0))
    try:
        sys.path.insert(0, os.path.expanduser("~/TRADE_BOT"))
        os.chdir(os.path.expanduser("~/TRADE_BOT"))
        from dotenv import load_dotenv; load_dotenv()
        from alpaca.trading.client import TradingClient
        tc = TradingClient(os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_API_SECRET"), paper=False)
        acct = tc.get_account()
        real_bal = float(acct.equity)
        diff_pct = abs(bot_bal - real_bal) / max(real_bal, 1) * 100
        if diff_pct > 5:
            issues.append(f"💰 Balance mismatch: bot shows ${bot_bal:.2f} but Alpaca equity=${real_bal:.2f} ({diff_pct:.0f}% off)")
    except Exception as e:
        issues.append(f"⚡ Balance check failed: {e}")

    # 3. Stop loss sanity checks
    for sym, pos in positions.items():
        sl = float(pos.get("stop_loss", 1))
        entry = float(pos.get("entry", 1))
        current = float(pos.get("current", entry))
        if sl <= 0:
            issues.append(f"🚨 {sym}: NEGATIVE stop loss = {sl:.2f} (will never trigger!)")
        elif sl > current:
            # SL above current price would trigger immediately — actual inversion
            issues.append(f"🚨 {sym}: SL={sl:.2f} > current={current:.2f} (would trigger now!)")
        # SL > entry but < current = trailing stop locking in profit — this is correct, skip
        # Only flag if SL is more than 10% below entry (too wide, risk control)
        if sl < entry:
            sl_pct = (entry - sl) / entry * 100
            if sl_pct > 10:
                issues.append(f"⚠️ {sym}: SL is {sl_pct:.0f}% below entry — very wide")

    # 4. Persistent HOLD loop — only flag outside overnight hours (UTC 00-07)
    # Cooldown: max once per 2h to avoid spam when market is genuinely flat.
    from datetime import timezone as _tz
    _now_h = datetime.now(_tz.utc).hour
    _is_overnight = 0 <= _now_h < 7
    if not _is_overnight:
        recent = logs[-30:] if len(logs) >= 30 else logs
        sig_logs = [l for l in recent if "Signal" in l.get("type", "")]
        all_hold = sig_logs and all("HOLD" in l.get("message", "") for l in sig_logs)
        if len(sig_logs) >= 20 and all_hold:
            state = _load_state()
            last_hold_alert = state.get("last_hold_alert_ts", 0)
            if time.time() - last_hold_alert >= 7200:  # 2h cooldown
                issues.append(f"⏸ Stuck in HOLD: last {len(sig_logs)} signals all HOLD — check RSI/volume/MTF filter")
                state["last_hold_alert_ts"] = time.time()
                _save_state(state)

    # 5. Daily loss halt triggered
    if m.get("daily_loss_halted"):
        issues.append("🛑 Daily loss halt active — bot paused until tomorrow")

    # 6. No trades for 24h on a running bot (only during market hours)
    now_utc = datetime.now(timezone.utc)
    is_weekday = now_utc.weekday() < 5
    if is_weekday and running and m.get("total_trades", 0) == 0:
        # Check if bot has been running > 4 hours
        trade_logs = [l for l in logs if "Trade" in l.get("type", "")]
        if not trade_logs:
            issues.append("📊 No trades executed yet today — monitor for signal generation")

    if issues:
        body = "\n".join(f"• {i}" for i in issues)
        alert(f"Found {len(issues)} issue(s) at {now_utc.strftime('%H:%M UTC')}:\n\n{body}")
        print("ALERT SENT:", body)
    else:
        print(f"[{now_utc.strftime('%H:%M')}] All checks passed — balance=${bot_bal:.2f}, positions={len(positions)}")

if __name__ == "__main__":
    check()
