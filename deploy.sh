#!/bin/bash
# Run this on the Google Cloud VM to set up and start the trade bot

set -e

echo "=== Installing dependencies ==="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git

echo "=== Setting up project ==="
cd /home/$USER

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python packages
pip install flask flask-cors ccxt anthropic ta pandas supabase requests python-dotenv

echo "=== Installing systemd service ==="
sudo cp /home/$USER/TRADE_BOT/trade-bot.service /etc/systemd/system/trade-bot.service
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/trade-bot.service
sudo systemctl daemon-reload
sudo systemctl enable trade-bot
sudo systemctl start trade-bot

echo ""
echo "=== Done! ==="
echo "Bot is running. Check status with: sudo systemctl status trade-bot"
echo "View logs with: sudo journalctl -u trade-bot -f"
echo "Dashboard: http://$(curl -s ifconfig.me):8000"
