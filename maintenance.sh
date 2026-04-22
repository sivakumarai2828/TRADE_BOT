#!/bin/bash
# Daily maintenance — runs at 2 AM ET (6 AM UTC) via cron
# 1. Close all open crypto positions gracefully
# 2. Stop both bots
# 3. Restart the trade-bot service (clears Python cache + connections)
# 4. Auto-start crypto bot after restart
# Day bot is already stopped by 3:55 PM ET scheduler — no action needed

set -e

LOGFILE="/home/$(whoami)/maintenance.log"
API="http://localhost:8000"

# Load env vars for Telegram
if [ -f "/home/$(whoami)/TRADE_BOT/.env" ]; then
    export $(grep -v '^#' "/home/$(whoami)/TRADE_BOT/.env" | xargs)
fi

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $1" | tee -a "$LOGFILE"
}

tg() {
    local msg="$1"
    if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}&text=${msg}&parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

DATE=$(date -u '+%Y-%m-%d %H:%M UTC')
log "=== Maintenance started ==="
tg "🔧 <b>Daily Maintenance Started</b>%0A${DATE}%0AClosing positions and restarting service..."

# --- Step 1: Close all open crypto positions ---
log "Closing open crypto positions..."
POSITIONS=$(curl -s "$API/positions" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(len(d) if isinstance(d, dict) else 0)
except:
    print(0)
" 2>/dev/null || echo "0")

if [ "$POSITIONS" -gt "0" ]; then
    log "Found $POSITIONS open position(s) — closing..."
    tg "⚠️ Found ${POSITIONS} open position(s) — closing before restart..."
    curl -s -X POST "$API/close" > /dev/null
    sleep 15
    log "Positions closed."
else
    log "No open positions — safe to restart."
fi

# --- Step 2: Stop crypto bot gracefully ---
log "Stopping crypto bot..."
curl -s -X POST "$API/stop" > /dev/null 2>&1 || true
sleep 3

# --- Step 3: Restart service ---
log "Restarting trade-bot service..."
sudo systemctl restart trade-bot
sleep 10

# --- Step 4: Start crypto bot after restart ---
log "Starting crypto bot..."
curl -s -X POST "$API/start" > /dev/null
sleep 3

# --- Step 5: Health check and report ---
CRYPTO_STATUS=$(curl -s "$API/status" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    m = d['metrics']
    running = d.get('running', False)
    balance = m.get('balance', 0)
    print(f\"running={running} balance=\${balance:.2f}\")
except:
    print('unknown')
" 2>/dev/null || echo "unknown")

SERVER_MEM=$(free -h | awk '/^Mem:/ {printf "%s used / %s total", $3, $2}')
SERVER_SWAP=$(free -h | awk '/^Swap:/ {printf "%s used / %s total", $3, $2}')
SERVER_LOAD=$(uptime | awk -F'load average:' '{print $2}' | xargs)

log "Crypto bot: $CRYPTO_STATUS"
log "Memory: $SERVER_MEM | Swap: $SERVER_SWAP | Load: $SERVER_LOAD"
log "=== Maintenance complete ==="

# Send all-clear Telegram
tg "✅ <b>Maintenance Complete</b>%0A━━━━━━━━━━━━━━━%0A🤖 Crypto Bot: ${CRYPTO_STATUS}%0A🕐 Day Bot: auto-starts at 9:35 AM ET%0A━━━━━━━━━━━━━━━%0A💾 RAM: ${SERVER_MEM}%0A🔄 Swap: ${SERVER_SWAP}%0A⚡ Load: ${SERVER_LOAD}%0A${DATE}"

# Keep log manageable
tail -n 500 "$LOGFILE" > "${LOGFILE}.tmp" && mv "${LOGFILE}.tmp" "$LOGFILE"
