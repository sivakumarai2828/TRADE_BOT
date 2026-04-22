# AI Trading Bot — Complete Strategy Reference

> Last updated: 2026-04-22
> Both bots run in **paper trading mode** on Alpaca.
> All parameters are configurable via `.env` unless noted otherwise.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Day Bot Strategy](#2-day-bot-strategy)
3. [Crypto Bot Strategy](#3-crypto-bot-strategy)
4. [Adaptive Mode System (Both Bots)](#4-adaptive-mode-system-both-bots)
5. [3-Bucket Profit Harvesting System](#5-3-bucket-profit-harvesting-system)
6. [Risk Management](#6-risk-management)
7. [Auto-Shield System](#7-auto-shield-system)
8. [VM Infrastructure & Safety](#8-vm-infrastructure--safety)
9. [Environment Variables Reference](#9-environment-variables-reference)
10. [Supabase Tables Reference](#10-supabase-tables-reference)

---

## 1. Architecture Overview

```
GCP VM (104.155.150.69:8000)
  └── api.py (Flask)
        ├── /daybot/*  → daybot/blueprint.py   (Day Bot)
        └── /*         → execution.py + strategy.py  (Crypto Bot)

Netlify (tradebottrade28.netlify.app)
  └── React Dashboard → Edge Function → VM API

Telegram Bot → telegram_bot.py → both bots
```

### Shared Components
| Component | File | Used By |
|---|---|---|
| Adaptive Mode Manager | `daybot/mode_manager.py`, `crypto_mode_manager.py` | Both |
| Profit Harvesting | `harvest/manager.py` | Both |
| Harvest DB | `harvest/db.py` | Both |
| Claude Picker | `harvest/picker.py` | Both |
| Telegram Alerts | `telegram_notify.py` | Both |
| Supabase Persistence | `daybot/db.py`, `persistence.py` | Both |
| VM Watchdog | `watchdog.sh` (cron every 5 min) | Both |

---

## 2. Day Bot Strategy

**Exchange:** Alpaca (US equities, paper)
**Symbols:** 10-stock watchlist, scanned every 15 minutes
**Bars:** 1-minute candles, EMA-50 + RSI-14 + VWAP + Volume

### Trading Windows (ET)
| Window | Type |
|---|---|
| 09:50 – 11:30 | Active trading (morning) |
| 14:00 – 15:30 | Active trading (afternoon) |
| 15:30 – 15:50 | Close-only (exit all positions) |
| All other times | Scan watchlist only, no new entries |

> Opening 9:35–9:50 is intentionally skipped — institutions are still positioning.

### Indicators
| Indicator | Period | Purpose |
|---|---|---|
| EMA | 50-bar (1-min) | Trend direction |
| RSI | 14-bar | Momentum / overbought / oversold |
| VWAP | Daily rolling | Intraday fair value |
| Volume | 20-bar rolling avg | Conviction filter |

### Signal Generation

#### BUY — Setup A: Pullback Dip
```
price > EMA-50                    ← uptrend confirmed
0% ≤ (price − EMA) / EMA ≤ 3%   ← near EMA support
30 ≤ RSI ≤ 52                    ← oversold to neutral (pullback zone)
volume ≥ 1.2× 20-bar avg         ← volume confirms
no open position in symbol
```

#### BUY — Setup B: Momentum Breakout *(AGGRESSIVE mode only)*
```
price > EMA-50                    ← uptrend
(price − EMA) / EMA > 3%         ← strong breakout above EMA
55 ≤ RSI ≤ 70                    ← momentum, not yet overbought
volume ≥ 1.2× 20-bar avg
no open position in symbol
```
> Setup B is **blocked** in SAFE and SHIELD modes.

#### BUY Filters (applied after signal)
- VWAP: price must be ≥ VWAP (below VWAP = bearish bias)
- Earnings: no trade within 2 days of earnings
- AI validation: Claude must agree (confidence ≥ 0.65)
- RSI 45–55 zone: always HOLD (no edge in neutral zone)
- Volume < 1.2× avg with no position: always HOLD

#### SELL Conditions (requires open position)
```
RSI > 72                  → overbought exit
price < EMA × 0.995       → trend break (0.5% below EMA)
```

#### Stop Loss & Take Profit
Overridden by Adaptive Mode (see Section 4). Base config defaults:
```
Stop Loss:    1.5% below entry    (DAY_STOP_LOSS_PCT)
Take Profit:  4.0% above entry    (DAY_TAKE_PROFIT_PCT)
Risk/Reward:  1 : 2.67
```

### AI Validation (Claude Sonnet)
1. Rule signal is computed first
2. If rule = HOLD → Claude skipped entirely (cost saving)
3. Claude receives: symbol, price, RSI, EMA, volume, weekly 4-week context, Supabase trade history
4. **Both must agree** → disagreement = HOLD
5. Claude confidence < 0.65 → treated as HOLD

### Position Management
```
Max trades/day:      6   (DAY_MAX_TRADES)
Max concurrent:      3   (DAY_MAX_CONCURRENT)
Position size:       20% of portfolio  (DAY_POSITION_SIZE_PCT) — overridden by mode
Max daily loss:      5%  (DAY_MAX_DAILY_LOSS_PCT) → halt all trading
Budget cap:          $1,000            (DAY_PAPER_BUDGET)
```

---

## 3. Crypto Bot Strategy

**Exchange:** Alpaca (crypto, paper)
**Symbols:** BTC/USD, ETH/USD, SOL/USD (runs 24/7)
**Bars:** 1-minute candles, SMA-50 + RSI-14 + ATR-14 + Volume

### Indicators
| Indicator | Period | Purpose |
|---|---|---|
| SMA | 50-bar (1-min) | Trend direction |
| RSI | 14-bar | Momentum |
| ATR | 14-bar | Dynamic stop-loss sizing |
| Volume | 20-bar rolling avg | Conviction filter |
| RSI (1-hour) | 14-bar | Higher-timeframe trend filter |

### Signal Generation

#### BUY — Setup A: Dip Buy
```
RSI < 38                          ← oversold (rsi_oversold setting)
price > SMA × 0.99                ← not more than 1% below SMA
volume ≥ 1.2× 20-bar avg
```

#### BUY — Setup B: Momentum Breakout *(AGGRESSIVE mode only)*
```
50 ≤ RSI ≤ 65                    ← bullish momentum zone
price > SMA × 1.001               ← price above SMA
volume ≥ 1.2× 20-bar avg
```

#### Multi-Timeframe Filter (HTF)
- Fetches 1-hour candles → computes 20-SMA and RSI
- If 1h trend = "down" → **BUY signal blocked**
- SELL signals never blocked — exits always allowed
- HTF cached 30 minutes

#### SELL Conditions
```
RSI > 70 (rsi_overbought) + volume confirmed → SELL
```

#### Stop Loss & Take Profit
Overridden by Adaptive Mode (see Section 4). Defaults:
```
Stop Loss:  2×ATR below entry (dynamic) or 2.0% fixed fallback
Take Profit: 6.0% above entry
Trailing Stop: 1.5% below highest reached price (enabled by default)
```

### AI Validation (Claude Haiku — cost optimised)
- Confidence gate: ≥ 0.55
- Response cached 10 minutes if RSI/price barely changed (< 2pt RSI, < 0.3% price)
- Both rule + Claude must agree

### Position Management
```
Trade size:         $100 per trade  (TRADE_SIZE_USDT) — multiplied by mode
Max concurrent:     2 open positions at once
Cooldown after close: 10 cycles (~10 min) before re-entering same symbol
Daily loss halt:    5% drop from daily_start_balance
```

---

## 4. Adaptive Mode System (Both Bots)

Both bots run a `ModeManager` that evaluates performance every cycle and switches
between three modes. **Anti-flip guard:** minimum 2 completed trades before any switch.

### Day Bot Modes (`daybot/mode_manager.py`)

| Mode | Position Size | Stop Loss | Take Profit | Breakout Allowed |
|---|---|---|---|---|
| **SAFE** | 15% | 1.0% | 2.5% | ❌ |
| **AGGRESSIVE** | 25% | 1.5% | 5.0% | ✅ |
| **SHIELD** | 3% | 1.0% | 2.0% | ❌ |

#### Switching Rules
```
→ SHIELD:      consecutive_losses ≥ 3   OR  daily_pnl ≤ −3%
→ SAFE:        consecutive_losses ≥ 1   OR  win_rate (last 5) < 50%
→ AGGRESSIVE:  consecutive_wins ≥ 3     AND  SPY daily return ≥ +0.3%
               AND consecutive_losses == 0
```

### Crypto Bot Modes (`crypto_mode_manager.py`)

| Mode | Trade Size Multiplier | Stop Loss | Take Profit | Breakout Allowed |
|---|---|---|---|---|
| **SAFE** | ×1.0 ($100) | 2.0% | 6.0% | ❌ |
| **AGGRESSIVE** | ×1.5 ($150) | 2.0% | 8.0% | ✅ |
| **SHIELD** | ×0.3 ($30) | 1.5% | 4.0% | ❌ |

#### Switching Rules
```
→ SHIELD:      consecutive_losses ≥ 3   OR  daily_loss ≥ 5%
→ SAFE:        consecutive_losses ≥ 1   OR  win_rate (last 10) < 50%
→ AGGRESSIVE:  consecutive_wins ≥ 3     AND  BTC 1h trend = "up"
```

### Mode Lifecycle
```
Startup       → SAFE (always)
Hot streak    → AGGRESSIVE (scale up)
Any loss      → SAFE (scale back)
3 losses      → SHIELD (protect capital)
2 wins        → SAFE (gradual recovery)
3 wins        → AGGRESSIVE (if market confirms)
```

### Telegram Alerts
Mode change fires an alert: `🚀 DayBot Mode: SAFE → AGGRESSIVE | size 25% | SL 1.5% TP 5.0%`

### Env Vars
```
MODE_MIN_TRADES_BEFORE_SWITCH=2
MODE_AGGRESSIVE_WIN_STREAK=3
MODE_AGGRESSIVE_SPY_MIN_PCT=0.3
MODE_SHIELD_LOSS_STREAK=3
MODE_SHIELD_DAILY_LOSS_PCT=3.0   (day) / 5.0 (crypto)
MODE_SAFE_LOSS_STREAK=1
```

---

## 5. 3-Bucket Profit Harvesting System

### Concept
Daily trading profits are extracted to a separate portfolio that compounds independently.
The active trading base ($1,000 day / $500 crypto) never decreases from harvesting.

```
Bucket 1 — Active Trading
  Always funded at base amount. SAFE/AGGRESSIVE/SHIELD controls sizing.

Bucket 2 — Long-Term Portfolio
  Funded by: daily profits when profit ≥ $50 (HARVEST_EXTRACT_THRESHOLD)
  Hold: 60–90 days
  Target: +30%
  Claude picks the stock/crypto

Bucket 3 — Compound Portfolio
  Funded by: 50% of Bucket 2 profits (gains only, not capital)
  Hold: 2–4 weeks (max 30 days)
  Target: +15%
  Claude picks the stock/crypto
```

### Flow Diagram
```
EOD: daily_pnl ≥ $50?
  YES → Claude picks long-term candidate
        → Open Bucket 2 position
        → Log to harvest_positions (Supabase)

Daily Monitor: Bucket 2 position hits +30%?
  YES → Close position
        Capital ($amount_invested) → Open next Bucket 2 position (Claude picks)
        Profit ($gain):
          50% → Add to active trading base (paper_usdt)
          50% → Open Bucket 3 position (Claude picks)

Daily Monitor: Bucket 3 position hits +15%?
  YES → Close position
        50% of total proceeds → Active trading base
        50% of total proceeds → Open next Bucket 2 position

Position expires (max hold days exceeded)?
  → Force close, same split logic applies
  → If loss: full capital → next Bucket 2 position (no base addition)
```

### Rules
- Only the **profit portion** is ever at risk in Bucket 3
- Original capital always recycled into the next long-term position
- Claude skips candidates and parks money in base if market is bearish
- Crypto long-term: only BTC/USD or ETH/USD (more stable)
- Day bot long-term: any stock from current watchlist or large-cap fallback

### Claude Picker Logic
**Long-term pick prompt includes:**
- Amount to invest
- Candidate list (watchlist or large caps)
- Market regime (trending_up / trending_down / choppy)
- Bot type (day or crypto)
- Target: 30% in 60–90 days

**Compound pick prompt includes:**
- Same but target: 15% in 2–4 weeks
- Focus: near-term catalysts, breakout setups, oversold recovery

**Claude returns:** `{ "symbol": "AAPL", "confidence": 0.82, "reason": "..." }`
Skip if confidence < 0.60 or symbol = "SKIP".

### Telegram Alerts
```
🌱 DayBot Harvest: Extracted $85.00 profit → long-term position opened | regime: trending_up
🎯 DayBot Harvest Target Hit! [long_term] NVDA: +31.2% ($26.50) — Profits reinvested.
```

### Env Vars
```
HARVEST_EXTRACT_THRESHOLD=50      # min daily profit to trigger extraction
HARVEST_LONG_TERM_TARGET_PCT=30   # target % for long-term positions
HARVEST_COMPOUND_TARGET_PCT=15    # target % for compound positions
```

---

## 6. Risk Management

### Day Bot
| Rule | Value | Env Var |
|---|---|---|
| Max trades/day | 6 | `DAY_MAX_TRADES` |
| Max concurrent | 3 | `DAY_MAX_CONCURRENT` |
| Daily loss halt | 5% of portfolio | `DAY_MAX_DAILY_LOSS_PCT` |
| Budget cap | $1,000 | `DAY_PAPER_BUDGET` |
| Stop loss | mode-controlled (1.0–1.5%) | `DAY_STOP_LOSS_PCT` (default) |
| Take profit | mode-controlled (2.0–5.0%) | `DAY_TAKE_PROFIT_PCT` (default) |

### Crypto Bot
| Rule | Value | Env Var |
|---|---|---|
| Max concurrent | 2 positions | hardcoded in `execution.py` |
| Daily loss halt | 5% from daily start | `DAILY_LOSS_LIMIT_PCT` in `state.py` |
| Stop loss | mode-controlled or 2×ATR | `STOP_LOSS_PCT` |
| Take profit | mode-controlled | `TAKE_PROFIT_PCT` |
| Trailing stop | 1.5% below highest price | `TRAILING_STOP_PCT` |
| Cooldown after trade | 10 cycles (~10 min) | hardcoded in `execution.py` |

---

## 7. Auto-Shield System

Runs inside the existing `record_trade_result()` — independent of the Adaptive Mode Manager.
Both systems coexist: Shield handles position sizing, Mode Manager handles SL/TP and setup filtering.

### Day Bot Shield (`daybot/state.py`)
```
Activates: consecutive_losses ≥ shield_loss_streak (default 2)
  → position_size_pct → shield_size_pct (1%)
  → trade_mode → "house_money"
Deactivates: consecutive_wins ≥ shield_recovery_wins (default 2)
  → position_size_pct → normal_size_pct (restored from config)
  → trade_mode → pre_shield_mode (restored)
```

### Crypto Bot Shield (`state.py`)
```
Activates when ANY of:
  - consecutive_losses ≥ 5
  - win_rate (last 20) < 40%
  - balance dropped ≥ 10% from peak_balance
  → trade_size_mode → "house_money"
Deactivates when:
  - win_rate (last 10) ≥ 55%
  - consecutive_losses == 0
```

---

## 8. VM Infrastructure & Safety

### VM Watchdog (`watchdog.sh`)
- Cron: `*/5 * * * *`
- Checks `systemctl is-active trade-bot`
- If down: restarts service + sends Telegram alert
- Systemd: `Restart=on-failure RestartSec=30`

### Heartbeat Watchdog (in-process, `api.py`)
- Checks every 5 minutes if `_last_cycle_time` is stale (> 10 min)
- Sends Telegram alert if bot loop appears stuck

### Position Reconciliation (crypto bot)
- On `/start`, compares Supabase positions vs actual exchange balances
- Clears "ghost" positions (bot thinks open, exchange already closed them)

### Supabase Persistence
- Crypto bot: metrics + settings + positions saved after every trade
- Day bot: trades + symbol stats + market sessions saved after every trade
- Harvest positions: saved when opened, updated daily, closed when target hit

---

## 9. Environment Variables Reference

### Core
```
EXCHANGE_API_KEY          Alpaca API key (both bots)
EXCHANGE_API_SECRET       Alpaca secret (both bots)
ANTHROPIC_API_KEY         Claude API key (both bots)
ANTHROPIC_MODEL           Claude model (default: claude-sonnet-4-6)
SUPABASE_URL              Supabase project URL
SUPABASE_KEY              Supabase anon key
TELEGRAM_TOKEN            Telegram bot token
TELEGRAM_CHAT_ID          Telegram chat ID
```

### Day Bot
```
DAY_MAX_TRADES=6
DAY_MAX_CONCURRENT=3
DAY_POSITION_SIZE_PCT=0.20
DAY_MAX_DAILY_LOSS_PCT=0.05
DAY_SCAN_INTERVAL_MINUTES=15
DAY_LOOP_INTERVAL_SECONDS=60
DAY_STOP_LOSS_PCT=0.015
DAY_TAKE_PROFIT_PCT=0.04
DAY_PAPER_BUDGET=1000
```

### Crypto Bot
```
TRADE_SIZE_USDT=100
STOP_LOSS_PCT=0.02
TAKE_PROFIT_PCT=0.06
TRAILING_STOP_PCT=0.015
USE_TRAILING_STOP=true
POLLING_SECONDS=60
SYMBOL=BTC/USDT
```

### Adaptive Mode
```
MODE_MIN_TRADES_BEFORE_SWITCH=2
MODE_AGGRESSIVE_WIN_STREAK=3
MODE_AGGRESSIVE_SPY_MIN_PCT=0.3
MODE_SHIELD_LOSS_STREAK=3
MODE_SHIELD_DAILY_LOSS_PCT=3.0
MODE_SAFE_LOSS_STREAK=1
```

### Harvest
```
HARVEST_EXTRACT_THRESHOLD=50
HARVEST_LONG_TERM_TARGET_PCT=30
HARVEST_COMPOUND_TARGET_PCT=15
```

---

## 10. Supabase Tables Reference

### Existing Tables
| Table | Bot | Purpose |
|---|---|---|
| `daybot_trades` | Day | Every closed trade |
| `daybot_symbol_stats` | Day | Win rate / PnL per symbol |
| `daybot_market_sessions` | Day | Daily SPY return + regime |
| `claude_signal_cache` | Crypto | Claude response cache |
| `bot_state` | Crypto | Metrics + settings + positions |
| `crypto_trades` | Crypto | Every closed trade |

### New Tables (Harvest System)
```sql
CREATE TABLE harvest_positions (
  id               BIGSERIAL PRIMARY KEY,
  bot              TEXT NOT NULL,
  bucket           TEXT NOT NULL,
  symbol           TEXT NOT NULL,
  amount_invested  FLOAT NOT NULL,
  entry_price      FLOAT NOT NULL,
  quantity         FLOAT NOT NULL,
  current_price    FLOAT,
  target_pct       FLOAT NOT NULL,
  pnl_pct          FLOAT DEFAULT 0,
  status           TEXT DEFAULT 'open',
  ai_reason        TEXT,
  max_hold_days    INT DEFAULT 90,
  created_at       TIMESTAMPTZ DEFAULT now(),
  closed_at        TIMESTAMPTZ,
  exit_price       FLOAT,
  realized_pnl     FLOAT
);

CREATE TABLE harvest_log (
  id               BIGSERIAL PRIMARY KEY,
  bot              TEXT NOT NULL,
  extracted_amount FLOAT NOT NULL,
  trigger_pnl      FLOAT NOT NULL,
  created_at       TIMESTAMPTZ DEFAULT now()
);
```

---

## Decision Flow Summary

```
Every cycle (both bots):
  1. ModeManager.evaluate()  → SAFE | AGGRESSIVE | SHIELD
     └─ if switched: update state + send Telegram alert

  2. Compute rule-based signal  (RSI, EMA/SMA, Volume)
     └─ filter Setup B if mode = SAFE or SHIELD

  3. Multi-timeframe filter  (SPY for day / BTC 1h for crypto)

  4. AI validation  (Claude)
     └─ skip if rule = HOLD (cost saving)
     └─ require confidence ≥ 0.65 (day) / 0.55 (crypto)
     └─ require rule == Claude decision

  5. Risk gates
     └─ daily loss halt / max concurrent / earnings / VWAP

  6. Execute trade with mode-specific SL/TP + position size

  7. Auto-Shield check after each close
     └─ independent of ModeManager

EOD / daily reset:
  8. Harvest check
     └─ if daily_pnl ≥ $50: Claude picks long-term candidate → open position
     └─ monitor open positions for +30% / +15% targets
     └─ on target: split gains per 3-bucket rules
```
