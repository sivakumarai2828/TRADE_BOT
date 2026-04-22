#!/usr/bin/env bash
# Watchdog: runs every 5 min via cron. Restarts trade-bot if down, alerts Telegram.
set -euo pipefail

SERVICE="trade-bot"
ENV_FILE="/home/sivakumarkondapalle/TRADE_BOT/.env"
LOG="$HOME/watchdog.log"

# Read Telegram creds from .env
TOKEN=$(grep -E "^TELEGRAM_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
CHAT_ID=$(grep -E "^TELEGRAM_CHAT_ID=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)

_tg() {
    [ -n "$TOKEN" ] && [ -n "$CHAT_ID" ] && \
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "parse_mode=HTML" \
        --data-urlencode "text=$1" > /dev/null 2>&1 || true
}

if ! systemctl is-active --quiet "$SERVICE"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG: $SERVICE DOWN — restarting" | tee -a "$LOG"
    _tg "⚠️ <b>Trade Bot crashed</b> — auto-restarting now..."
    sudo systemctl restart "$SERVICE"
    sleep 10
    if systemctl is-active --quiet "$SERVICE"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG: $SERVICE restarted OK" | tee -a "$LOG"
        _tg "✅ <b>Trade Bot restarted</b> — service is back online."
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') WATCHDOG: $SERVICE FAILED to restart!" | tee -a "$LOG"
        _tg "🔴 <b>Trade Bot FAILED to restart</b> — manual intervention needed!"
    fi
fi
