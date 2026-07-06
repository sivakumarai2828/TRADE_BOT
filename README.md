# TRADE_BOT_V2 — AI Portfolio Manager

## What changed vs V1

| | V1 (old) | V2 (this) |
|---|---|---|
| Who decides | Rule engine (EMA/RSI); Claude only vetoes | **Claude is the portfolio manager** — picks symbols, entries, stops, targets, exits |
| Timeframe | 1-minute bars, same-day close | Daily bars, swing holds (days–weeks) |
| Learning | None | Trade journal + weekly AI self-review memo fed back into every decision |
| Sizing | Fixed dollar size | Risk-based (% of equity to stop), AI-chosen within caps |
| Safety | Scattered rules | One risk layer with hard caps AI cannot override |
| Markets | Crypto + US intraday + options | US stocks (swing) + India NSE (swing), paper |

The AI is free inside hard caps: max 20% equity per position, max 1.5% equity
risk per trade, max 6 positions, max 2 new entries/day, -3% daily loss halts
new buys, -10% drawdown flattens everything and halts (kill switch). Stops can
only ever move UP. Long-only in paper phase.

## How a day works (UTC)

- **04:30** India cycle: fetch Nifty-40 data + regime → Claude returns action plan → risk layer validates → virtual INR ledger executes
- **14:30** US cycle: same for the US universe → local ledger + optional Alpaca paper mirror
- **Every 20 min** during each market's hours: code enforces stops/targets (no AI call)
- **Saturday 08:00** Claude reviews its own closed trades and writes a self-review memo used in all next week's prompts

## Setup (local test)

```bash
cd TRADE_BOT_V2
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY (+ Telegram if wanted)
python main.py once us      # run one US decision cycle now
python main.py status       # view portfolio
```

## Deploy to the existing Google VM (V1 untouched)

```bash
# from your machine
scp -r TRADE_BOT_V2 <user>@<vm-ip>:/home/<user>/

# on the VM
cd /home/$USER/TRADE_BOT_V2
chmod +x deploy_v2.sh && ./deploy_v2.sh
# edit .env when prompted, then run ./deploy_v2.sh again

# recommended: stop V1 so only one bot trades
sudo systemctl stop trade-bot trade-bot-monitor
sudo systemctl disable trade-bot trade-bot-monitor   # keep files, just don't autostart
```

V2 runs as its own service `trade-bot-v2`, in its own folder, with its own
`.env` and database — nothing from V1 is modified, so you can switch back
anytime with `sudo systemctl start trade-bot`.

Watchdog (optional): copy the V1 `watchdog.sh` pattern, change `SERVICE="trade-bot-v2"`.

## Files

```
main.py                 scheduler + CLI (once/review/status)
botv2/config.py         env config + HARD CAPS
botv2/universe.py       US 38-stock + Nifty-40 starting universes (AI can request more)
botv2/data.py           yfinance daily-bar snapshots (context, not gates)
botv2/ai_pm.py          Claude portfolio manager prompt + self-review
botv2/risk.py           hard-cap enforcement, kill switch, sizing
botv2/executor.py       applies AI plan through risk layer
botv2/ledger.py         virtual paper ledger (SQLite)
botv2/alpaca_mirror.py  optional Alpaca paper mirroring for US fills
botv2/journal.py        trades, decisions, memos, state (SQLite)
botv2/notify.py         Telegram (same env vars as V1)
```

## Before going live (do not skip)

Paper-trade at least 4 weeks and require: 20+ closed trades, positive total
P&L, avg win > avg loss, max drawdown < 7%. No trading system — AI or not —
can guarantee profits; hard caps limit damage, they don't create returns.
