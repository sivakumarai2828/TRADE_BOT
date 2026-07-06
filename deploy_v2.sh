#!/bin/bash
# Deploy TRADE_BOT_V2 to the existing Google Cloud VM.
# Coexists with V1: nothing in TRADE_BOT/ is touched or deleted.
# Run from /home/$USER after copying/cloning the TRADE_BOT_V2 folder there.

set -e

echo "=== Installing V2 Python packages (reuses existing venv311) ==="
if [ ! -d "/home/$USER/venv311" ]; then
    python3 -m venv /home/$USER/venv311
fi
/home/$USER/venv311/bin/pip install -r /home/$USER/TRADE_BOT_V2/requirements.txt

echo "=== Checking .env ==="
if [ ! -f "/home/$USER/TRADE_BOT_V2/.env" ]; then
    cp /home/$USER/TRADE_BOT_V2/.env.example /home/$USER/TRADE_BOT_V2/.env
    echo "!! Created .env from example — EDIT IT (ANTHROPIC_API_KEY, TELEGRAM_*) then re-run."
    exit 1
fi

echo "=== (Optional) stop V1 so both bots don't trade at once ==="
echo "    sudo systemctl stop trade-bot trade-bot-monitor   # <- run manually if desired"

echo "=== Installing systemd service ==="
sudo cp /home/$USER/TRADE_BOT_V2/trade-bot-v2.service /etc/systemd/system/trade-bot-v2.service
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/trade-bot-v2.service
sudo systemctl daemon-reload
sudo systemctl enable trade-bot-v2
sudo systemctl restart trade-bot-v2

echo ""
echo "=== Done ==="
echo "Status:  sudo systemctl status trade-bot-v2"
echo "Logs:    sudo journalctl -u trade-bot-v2 -f"
echo "Test:    /home/$USER/venv311/bin/python3 /home/$USER/TRADE_BOT_V2/main.py once us"
