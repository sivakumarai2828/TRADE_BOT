# TRADE_BOT_V2 — AI Portfolio Manager

## v3 branch: control dashboard

`http://<api-host>:8000/control` shows both bots (running / paused /
kill-switched / offline), every open position with its stop→target range,
pending approvals, armed WATCH entries, Skeptic rulings and activity.
With `DASHBOARD_TOKEN` set in the API's `.env` you can unlock controls:
pause/resume new entries per bot, run an AI cycle now, check stops now,
close one position or all, approve/skip entries, and switch the Skeptic and
Telegram-approval gates. The API only writes flags and queues commands
(`botv2/control.py`); the bot process executes them within ~20 s.
Pause stops new entries only — stops, targets and the kill switch keep running.

Try it locally with a simulated market (no keys, nothing touches the VM):

    python tools/demo_stack.py      # then open http://localhost:8000/control, token: demo

## v3 branch: Skeptic + per-trade approval

Every NEW entry (BUY, REPLACE, WATCH trigger) now passes, in order:

1. **Code risk caps** (unchanged) — cheap checks first.
2. **Skeptic** (`botv2/skeptic.py`) — a second AI call names the three
   strongest reasons the trade fails and rules BUY / WAIT / SKIP. Only BUY
   continues. Errors or unparseable replies count as SKIP (fail-closed).
   Rulings are kept in kv `<MARKET>_skeptic_log` (last 100) — check the
   refusal rate; a Skeptic that approves everything is not doing its job.
3. **Your approval** (`botv2/approval.py`) — Telegram message with
   ✅ Approve / ⏭ Skip. On Approve the bot re-checks halts, earnings, caps
   and price (cancels if price ran >1.5% or is at the stop) before filling.
   Unanswered requests expire after 90 min. Exits are never gated.

Telegram commands: `/pending`, `/approve <id>`, `/reject <id>`.
Shell: `python main.py pending | approve <id> | reject <id>`.
Turn either gate off with `SKEPTIC_ENABLED=false` / `APPROVAL_MODE=off`.
The scheduler refuses to start with `APPROVAL_MODE=telegram` and no
Telegram credentials. Only one process may poll the bot token
(no webhook, no second bot on the same token).

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

## Broker mode — real orders at Alpaca (paper first, then live)

`BROKER_TRADING=true` swaps the virtual ledger for `botv2/alpaca_ledger.py`:
orders go to Alpaca, the journal records only real fills, every position
gets a GTC stop order resting at Alpaca, and each monitor pass reconciles
the journal with the broker. It runs as its own service with its own
settings and SQLite journal, next to (or instead of) the paper bot.

The bot refuses to start unless the setup is safe (`check_broker_config`):
Telegram approvals on, US only, separate journal, no Supabase, a
`MAX_ORDER_USD` ceiling, and the live URL only with `LIVE_REAL_MONEY=true`.

On the VM, after a normal deploy:

```bash
cd ~/TRADE_BOT_V3
cp .env.live.example .env.live && chmod 600 .env.live
nano .env.live          # Alpaca PAPER keys, separate Telegram bot token, AI keys
sudo cp trade-bot-live.service /etc/systemd/system/
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/trade-bot-live.service
sudo systemctl daemon-reload && sudo systemctl enable --now trade-bot-live
sudo journalctl -u trade-bot-live -f      # expect "PAPER Alpaca account ACTIVE"
```

Dashboard for the broker-mode bot (same pages, read from Alpaca, view only,
localhost only, so it never goes through the public Vercel link):

```bash
sudo cp trade-bot-live-api.service /etc/systemd/system/
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/trade-bot-live-api.service
sudo systemctl daemon-reload && sudo systemctl enable --now trade-bot-live-api
# on your own computer:
gcloud compute ssh konda@trade-bot-vm --project=trade-bot-2025 --zone=us-central1-a -- -N -L 8001:localhost:8001
# then open http://localhost:8001 (positions) or http://localhost:8001/control (approvals, watch list)
```

A banner on both pages says which account is shown (`ALPACA PAPER` or
`LIVE · REAL MONEY`). Entries are approved in Telegram. After a kill switch:
`BOTV2_ENV_FILE=.env.live python3 main.py reset-kill us`.

Going live later = in `.env.live` set the LIVE keys,
`ALPACA_BASE_URL=https://api.alpaca.markets`, `LIVE_REAL_MONEY=true`, then
`sudo systemctl restart trade-bot-live`. Telegram messages are prefixed
`🧪 ALPACA-PAPER` or `💵 LIVE`.

## Strategy lab — test a rule on history before trading it

`tools/strategy_lab.py` runs entry/exit rules through `botv2/backtest.py`
(research only; the live bot never imports it). Signals at a day's close
trade at the next open; stops/targets fill on daily open/high/low, gaps fill
at the open; cash only, sales settle next day, costs on both sides.

```bash
python3 tools/strategy_lab.py                 # all strategies: TRAIN 2018-22 vs TEST 2023-now
python3 tools/strategy_lab.py --sweep         # target x stop grid
python3 tools/strategy_lab.py --only rotation_monthly --slots 5 --offset 7 --exclude SMCI,APP,PLTR
python3 tools/strategy_lab.py --capital 437 --whole-shares
python3 tools/strategy_lab.py --trades rotation_monthly
```

A rule passes only if it beats **Universe hold** (all universe stocks, equal
weight) in both windows. Read results with two cautions:

- The universe is today's list. SMCI, APP and PLTR traded at $6-11 in
  January 2023 and are on it because they later soared. Re-run any
  "winner" with `--exclude SMCI,APP,PLTR` and several `--offset` values.
- Findings on 2026-11-14: no fixed target/stop rule (including +10%/-5%)
  beat Universe hold. Monthly momentum rotation did, but only through
  those three names; without them it matched the benchmark with -50%
  drawdowns.

`tools/backtest.py` is the older, narrower stop-vs-no-stop comparison.

## India: daily buy suggestions only (`INDIA_MODE=suggest`)

The bot opens no India position, paper or real. The 10:00 IST cycle still
runs the AI, the hard risk rules and the Skeptic; every entry that survives
is stored as a suggestion (`botv2/suggest.py`), ranked HIGH / MEDIUM / LOW by
the AI's confidence, sent to Telegram as one list and shown on the dashboard
(India tab) and the control page. Each suggestion gives a buy-near price,
stop, target, reward:risk and a size as a share of your own capital. A WATCH
whose level is reached during the session is sent as a single suggestion.
India paper positions opened before the switch keep their stops and targets
until they close. `INDIA_MODE=trade` restores the paper book.

The same ranked list is kept for the US. With `US_MODE=trade` (default) the
bot trades as before and the Suggestions tab shows each day's ideas with what
the bot did ("Sent to Telegram for your approval" or "Bought by the bot").
`US_MODE=suggest` makes the US suggestions-only as well (not allowed together
with `BROKER_TRADING=true`).

## Trade Desk page in the Claude app (`claude_app/trade_desk.html`)

A Claude artifact (published from this file) for approving from the Claude
app: pending buy requests with Approve / Skip, today's ranked US and India
suggestions with what happened to each, recent decisions and open positions.
It reads the bot's Supabase journal through the viewer's Supabase connector
(`v2_kv`, `v2_trades`) and refreshes every 30 s. Approve / Skip append one
`approve` / `reject` command to `ctl_queue` in a single SQL statement; the
bot runs it on its next loop pass, exactly like the control page. It works
with the Supabase-journal (paper) bot only: the broker-mode bot keeps its
own SQLite journal on the VM, so its approvals stay in Telegram.

## Options research (Phase 1) — `tools/options_lab.py`

Replays each lab strategy's trades as at-the-money calls, 30–60 days to
expiry, exits at +100% / -50% / the stock signal / 21 days left
(`botv2/options_bt.py`). Option prices are MODELED with Black-Scholes from
realized volatility (no real option price history yet: needs Alpaca keys or
a paid feed), with a spread cost per side.

Result on 2026-11-17: calls failed the gate for every strategy, at every
size (5–33% premium per trade) and even with optimistic costs (1% spread,
implied vol = realized). At 5% premium most capital sits idle; at 20–33%
the worst drop reaches -55% to -94%. On a $437 account at 5% per trade, no
contract was affordable on any signal. Phase 2 (building it) is not
justified by these numbers.
