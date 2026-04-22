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

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $1" | tee -a "$LOGFILE"
}

log "=== Maintenance started ==="

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
    curl -s -X POST "$API/close" > /dev/null
    sleep 15  # wait for positions to close
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
sleep 8

# --- Step 4: Start crypto bot after restart ---
log "Starting crypto bot..."
curl -s -X POST "$API/start" > /dev/null
sleep 2

STATUS=$(curl -s "$API/status" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print('running' if d.get('running') else 'stopped')
except:
    print('unknown')
" 2>/dev/null || echo "unknown")

log "Crypto bot status after restart: $STATUS"
log "=== Maintenance complete ==="

# Keep last 30 days of logs
tail -n 500 "$LOGFILE" > "${LOGFILE}.tmp" && mv "${LOGFILE}.tmp" "$LOGFILE"
