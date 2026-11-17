#!/bin/bash
# Runs ON THE VM. Switches the live bot + API from ~/TRADE_BOT_V2 to ~/TRADE_BOT_V3.
#
# Same service names (trade-bot-v2, trade-bot-v2-api) so monitors, docs and
# habits keep working; only the folder they run from changes. V2's folder and
# its .env are left untouched. If V3 does not report a fresh heartbeat within
# ~90 s, the services are pointed back at V2 automatically.
#
# Manual rollback any time:   ~/TRADE_BOT_V3/vm_deploy_v3.sh --rollback

set -euo pipefail
V2="$HOME/TRADE_BOT_V2"
V3="$HOME/TRADE_BOT_V3"
PY="$HOME/venv311/bin/python3"
SERVICES="trade-bot-v2 trade-bot-v2-api"

point_services_at() {   # $1 = folder name (TRADE_BOT_V2 or TRADE_BOT_V3)
    for svc in $SERVICES; do
        sudo cp "$HOME/$1/${svc}.service" "/etc/systemd/system/${svc}.service"
        sudo sed -i "s|__USER__|$USER|g; s|/TRADE_BOT_V2|/$1|g" "/etc/systemd/system/${svc}.service"
    done
    sudo systemctl daemon-reload
    sudo systemctl restart $SERVICES
}

if [ "${1:-}" = "--rollback" ]; then
    echo "=== Rolling back: services -> TRADE_BOT_V2 ==="
    point_services_at TRADE_BOT_V2
    sudo systemctl status $SERVICES --no-pager | grep -E "Active|Loaded"
    exit 0
fi

[ -d "$V3" ] || { echo "!! $V3 not found — copy the folder first"; exit 1; }
[ -f "$V2/.env" ] || { echo "!! $V2/.env not found — nothing to copy keys from"; exit 1; }

echo "=== [1/5] Python packages ==="
"$HOME/venv311/bin/pip" install -q -r "$V3/requirements.txt"

echo "=== [2/5] .env for V3 (copied from V2, new settings appended) ==="
if [ ! -f "$V3/.env" ]; then
    cp "$V2/.env" "$V3/.env"
    echo "  copied V2 .env"
fi
add() { grep -qE "^$1=" "$V3/.env" || { echo "$1=$2" >> "$V3/.env"; echo "  added $1"; }; }
add SKEPTIC_ENABLED true
add SKEPTIC_MODEL ""
add APPROVAL_MODE telegram
add APPROVAL_TIMEOUT_MIN 390
# 2026-11: approvals stay open until the market closes (no Telegram needed)
grep -qE "^APPROVAL_TIMEOUT_MIN=90$" "$V3/.env" && sed -i 's/^APPROVAL_TIMEOUT_MIN=90$/APPROVAL_TIMEOUT_MIN=390/' "$V3/.env" && echo "  APPROVAL_TIMEOUT_MIN 90 -> 390"
add APPROVAL_MAX_DRIFT_PCT 0.015
add INDIA_MODE suggest          # India: daily buy suggestions only, no orders
if ! grep -qE "^DASHBOARD_TOKEN=.+" "$V3/.env"; then
    sed -i '/^DASHBOARD_TOKEN=/d' "$V3/.env"
    echo "DASHBOARD_TOKEN=$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')" >> "$V3/.env"
    echo "  generated DASHBOARD_TOKEN"
fi
chmod 600 "$V3/.env"
sed -i 's/\r$//' "$V3"/*.service "$V3"/*.sh "$V3/.env"

echo "=== [3/5] Pre-flight checks ==="
set -a; . "$V3/.env"; set +a
if [ "${APPROVAL_MODE:-telegram}" = "telegram" ] && { [ -z "${TELEGRAM_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; }; then
    echo "!! APPROVAL_MODE=telegram but TELEGRAM_TOKEN/CHAT_ID are empty in $V3/.env — V3 would refuse to start. Aborting, V2 still live."
    exit 1
fi
hook=$(curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo" | "$PY" -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("url",""))' 2>/dev/null || true)
if [ -n "$hook" ]; then
    echo "!! A Telegram webhook is set on this bot token ($hook)."
    echo "   Approve/Skip buttons need it removed. Aborting, V2 still live."
    exit 1
fi
(cd "$V3" && "$PY" -c "import main, api_v2, botv2.control, botv2.skeptic, botv2.approval") \
    || { echo "!! V3 failed to import — aborting, V2 still live"; exit 1; }
echo "  telegram creds ok, no webhook, code imports"

echo "=== [4/5] Switch services to V3 ==="
for svc in $SERVICES; do sudo cp "/etc/systemd/system/${svc}.service" "$HOME/${svc}.service.bak-$(date +%Y%m%d-%H%M)"; done
point_services_at TRADE_BOT_V3

echo "=== [5/5] Health check (up to 90 s) ==="
ok=""
for i in $(seq 1 18); do
    sleep 5
    st=$(curl -s localhost:8000/api/control || true)
    if echo "$st" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("bot_alive") and d.get("status",{}).get("version")=="v3" else 1)' 2>/dev/null; then
        ok=1; break
    fi
done
if [ -z "$ok" ]; then
    echo "!! V3 did not report a v3 heartbeat in 90 s. Last logs:"
    sudo journalctl -u trade-bot-v2 -n 25 --no-pager || true
    echo "=== ROLLING BACK to V2 ==="
    point_services_at TRADE_BOT_V2
    exit 1
fi

TOKEN=$(grep -E "^DASHBOARD_TOKEN=" "$V3/.env" | cut -d= -f2-)
echo ""
echo "=== V3 IS LIVE ==="
sudo systemctl status $SERVICES --no-pager | grep -E "●|Active"
echo ""
echo "Dashboard token (keep it private):  $TOKEN"
echo "Logs:      sudo journalctl -u trade-bot-v2 -f"
echo "Rollback:  ~/TRADE_BOT_V3/vm_deploy_v3.sh --rollback"
