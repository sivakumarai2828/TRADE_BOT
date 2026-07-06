#!/bin/bash
# Runs ON THE VM. Installs TRADE_BOT_V2 (bot + dashboard) and decommissions V1.
# Handles the case where V1 lives under a DIFFERENT user's home.
# V1 code is archived (tar.gz), not lost — and it's also on GitHub.

set -e
V2="$HOME/TRADE_BOT_V2"

# Find the old bot folder under any user's home
V1=$(sudo sh -c 'ls -d /home/*/TRADE_BOT 2>/dev/null' | head -1 || true)

echo "=== [1/6] Python packages (venv311 in $HOME) ==="
if [ ! -d "$HOME/venv311" ]; then python3 -m venv "$HOME/venv311"; fi
"$HOME/venv311/bin/pip" install -q -r "$V2/requirements.txt"

echo "=== [2/6] Build .env (reusing keys from old bot: ${V1:-none found}) ==="
if [ ! -f "$V2/.env" ]; then
    cp "$V2/.env.example" "$V2/.env"
    if [ -n "$V1" ] && sudo test -f "$V1/.env"; then
        for key in ANTHROPIC_API_KEY TELEGRAM_TOKEN TELEGRAM_CHAT_ID; do
            val=$(sudo grep -E "^${key}=" "$V1/.env" | head -1 | cut -d= -f2-)
            if [ -n "$val" ]; then
                sed -i "s|^${key}=.*|${key}=${val}|" "$V2/.env"
                echo "  copied $key from old bot"
            fi
        done
    fi
fi

echo "=== [3/6] Stop and remove OLD bot services ==="
sudo systemctl stop trade-bot trade-bot-monitor 2>/dev/null || true
sudo systemctl disable trade-bot trade-bot-monitor 2>/dev/null || true
sudo rm -f /etc/systemd/system/trade-bot.service /etc/systemd/system/trade-bot-monitor.service
sudo systemctl daemon-reload
echo "  old services stopped, disabled, unit files removed"

echo "=== [4/6] Remove old watchdog cron (all users) ==="
(crontab -l 2>/dev/null | grep -v "watchdog" | crontab -) || true
if [ -n "$V1" ]; then
    OLDUSER=$(basename "$(dirname "$V1")")
    (sudo crontab -l -u "$OLDUSER" 2>/dev/null | grep -v "watchdog" | sudo crontab -u "$OLDUSER" -) || true
fi

echo "=== [5/6] Archive old bot code ==="
if [ -n "$V1" ]; then
    sudo tar czf "$HOME/TRADE_BOT_V1_archive_$(date +%Y%m%d).tar.gz" -C "$(dirname "$V1")" TRADE_BOT
    sudo rm -rf "$V1"
    sudo chown "$USER:$USER" "$HOME"/TRADE_BOT_V1_archive_*.tar.gz
    echo "  archived and removed $V1"
fi

echo "=== [6/6] Install and start V2 services (bot + dashboard) ==="
for svc in trade-bot-v2 trade-bot-v2-api; do
    sudo cp "$V2/${svc}.service" "/etc/systemd/system/${svc}.service"
    sudo sed -i "s|__USER__|$USER|g" "/etc/systemd/system/${svc}.service"
done
sudo systemctl daemon-reload
sudo systemctl enable trade-bot-v2 trade-bot-v2-api
sudo systemctl restart trade-bot-v2 trade-bot-v2-api
sleep 3
sudo systemctl status trade-bot-v2 trade-bot-v2-api --no-pager -l | head -20

echo ""
echo "=== MIGRATION DONE ==="
echo "Dashboard:    http://$(curl -s ifconfig.me 2>/dev/null):8000"
echo "V2 logs:      sudo journalctl -u trade-bot-v2 -f"
echo "Test cycle:   $HOME/venv311/bin/python3 $V2/main.py once us"
