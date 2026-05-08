# AI Stock Magic — Strategy Reference

> Updated: 2026-04-25
> Paper trading on Alpaca. No real money at risk.

---

## Table of Contents

1. [How Claude AI Is Used](#1-how-claude-ai-is-used)
2. [Stock Scanning — How Candidates Are Found](#2-stock-scanning--how-candidates-are-found)
3. [Day Bot Strategy](#3-day-bot-strategy)
4. [Crypto Bot Strategy](#4-crypto-bot-strategy)
5. [Adaptive Mode System](#5-adaptive-mode-system)
6. [3-Bucket Profit Harvesting](#6-3-bucket-profit-harvesting)
7. [Auto-Shield System](#7-auto-shield-system)
8. [Risk Management Rules](#8-risk-management-rules)
9. [Full Decision Flow (Every Cycle)](#9-full-decision-flow-every-cycle)

---

## 1. How Claude AI Is Used

Claude is used in **3 distinct roles**. It never acts alone — always paired with technical rules.

### Role 1 — Trade Signal Validator (Intraday)

Runs every scan cycle on any symbol where technical rules say BUY or SELL.
Claude is **skipped entirely** when rules say HOLD (saves API cost).

**What Claude receives:**

| Input | Example |
|---|---|
| Symbol + price | `NVDA $875.20` |
| EMA-50 | `$861.00` |
| RSI (14-bar) | `47.3` |
| Volume vs 20-bar avg | `2.1M vs 1.4M avg` |
| Trend direction | `up / down / sideways` |
| Rule engine decision | `BUY (Setup A)` |
| 4-week weekly returns | `W1: +2.1% | W2: -0.8% | W3: +1.4% | W4: +3.2%` |
| 4-week range | `low $820 → high $890, currently at 78% of range` |
| Support / Resistance | `support $848, resistance $882` |
| Volume trend | `rising / falling / flat` |
| Bot's own history on symbol | `12 trades — 8W/4L (67% win), avg PnL +$4.20` |
| Last 5 trades on symbol | `WIN +$6.10 (take_profit) | LOSS -$3.40 (stop_loss) | ...` |
| Last 3 market sessions | `SPY +0.4% (trending_up) — bot: 2W/1L PnL +$18.50` |

**What Claude returns:**
```json
{
  "decision": "BUY",
  "confidence": 0.78,
  "reason": "4-week uptrend intact, RSI pullback near EMA with rising volume"
}
```

**Gate:** Both rules AND Claude must agree → trade executes. Any mismatch = HOLD.
**Confidence minimum:** 0.65 (day bot) / 0.55 (crypto bot). Below threshold → treated as HOLD.

---

### Role 2 — Long-Term Stock/Crypto Picker (Harvest Bucket 2)

Triggered at end of day when daily profit ≥ $50. Claude selects one asset from candidates
for a **60–90 day hold targeting +30%**.

**Prompt sent to Claude:**
```
I have $85.00 in day-trading profits to invest long-term.
Bot type: day | Market regime: trending_up
Candidates: AAPL, NVDA, MSFT, TSLA, AMZN, META, AMD, GOOGL

Select the single best US stock for a 60-90 day hold targeting 30% gain.
Consider: trend strength, recent momentum, volume trends, support levels, market conditions.
If market is bearish or no strong candidate exists, return SKIP.

Respond with JSON only:
{"symbol": "NVDA", "reason": "one sentence", "confidence": 0.82}
or {"symbol": "SKIP", "reason": "why skipping", "confidence": 0.0}
```

**Gate:** confidence < 0.60 or symbol = SKIP → money stays in trading base, no position opened.

---

### Role 3 — Compound Picker (Harvest Bucket 3)

Triggered when a Bucket 2 position hits +30% target. Claude picks one asset from candidates
for a **2–4 week hold targeting +15%**.

**Prompt sent to Claude:**
```
I have $42.00 in harvest profits to compound short-term.
Bot type: day | Market regime: trending_up
Candidates: AAPL, NVDA, MSFT, TSLA, AMZN, META, AMD, GOOGL

Select the best US stock for a 2-4 week hold targeting 15% gain.
Prioritize strong near-term catalysts, breakout setups, or oversold recovery plays.
If no strong setup exists, return SKIP.
```

**Gate:** Same — confidence < 0.60 = SKIP.

---

## 2. Stock Scanning — How Candidates Are Found

Bot scans a curated universe of 33 highly liquid US stocks every 15 minutes via Alpaca snapshots.

### Full Universe (33 symbols)
```
Tech:      AAPL  MSFT  NVDA  AMZN  META  GOOGL  TSLA  AMD  NFLX  CRM
Growth:    SHOP  SQ    COIN  PLTR  RBLX  SNAP   UBER  ABNB SOFI  RIVN
           NIO   BABA  PYPL  HOOD
Finance:   JPM   BAC   GS    V     MA    XLF
ETFs:      SPY   QQQ   IWM
```

### 3 Scan Filters (applied to live snapshots)

| Filter | Criteria | Why |
|---|---|---|
| **Gap Stocks** | \|open − prev_close\| / prev_close ≥ 1% | Pre-market catalyst |
| **Top Movers** | \|close − prev_close\| / prev_close ≥ 1.5% | Momentum |
| **High Volume** | today_volume ≥ 1.5× estimated avg daily vol | Conviction |

SPY and QQQ always included regardless of filters (market pulse).

**Output:** Union of all 3 filters → up to 15 candidates passed to signal engine.
Fallback: if < 6 found → pad with AAPL, MSFT, NVDA, TSLA, AMZN, META, AMD, GOOGL.

---

## 3. Day Bot Strategy

**Exchange:** Alpaca (US equities, paper)
**Symbols:** Up to 15 from scanner, re-scanned every 15 minutes
**Bars:** 1-minute candles

### Trading Windows (ET)

| Window | Action |
|---|---|
| 09:50 – 11:30 | Active — buy and sell |
| 14:00 – 15:30 | Active — buy and sell |
| 15:30 – 15:50 | Close-only — exit all open positions |
| All other times | Scan only — no new entries |

> 09:35–09:50 intentionally skipped — institutions still positioning, spreads wide.

### Technical Indicators

| Indicator | Config | Purpose |
|---|---|---|
| EMA | 50-bar (1-min) | Trend direction |
| RSI | 14-bar | Momentum / overbought / oversold |
| VWAP | Daily rolling | Intraday fair value anchor |
| Volume | 20-bar rolling avg | Conviction filter |

### BUY Signals

**Setup A — Pullback Dip** *(all modes)*
```
price > EMA-50                         ← uptrend confirmed
0% ≤ (price − EMA) / EMA ≤ 3%         ← near EMA support, not extended
30 ≤ RSI ≤ 52                          ← oversold to neutral (pullback zone)
volume ≥ 1.2× 20-bar avg               ← volume confirms
price ≥ VWAP                           ← above fair value
no open position in symbol
no earnings within 2 days
```

**Setup B — Momentum Breakout** *(AGGRESSIVE mode only)*
```
price > EMA-50                         ← uptrend
(price − EMA) / EMA > 3%              ← strong breakout above EMA
55 ≤ RSI ≤ 70                          ← momentum, not yet overbought
volume ≥ 1.2× 20-bar avg
price ≥ VWAP
no open position in symbol
```

Setup B blocked in SAFE and SHIELD modes.

### SELL Signals *(requires open position)*
```
RSI > 72                               → overbought exit
price < EMA × 0.995                    → trend break (0.5% below EMA)
```

### Zones Where Bot Always HOLDs (No Edge)
```
RSI 45–55                              → neutral zone, no edge
volume < 1.2× avg with no position    → low conviction
price < VWAP                           → bearish bias
earnings within 2 days                → gap risk too high
Claude disagrees with rules            → conflicting signals
Claude confidence < 0.65               → uncertain
```

### Stop Loss & Take Profit (Mode-Dependent)

| Mode | Position Size | Stop Loss | Take Profit |
|---|---|---|---|
| SAFE | 15% portfolio | 1.0% | 2.5% |
| AGGRESSIVE | 25% portfolio | 1.5% | 5.0% |
| SHIELD | 3% portfolio | 1.0% | 2.0% |

Base defaults (before mode override): SL 1.5%, TP 4.0%, position 20%.

---

## 4. Crypto Bot Strategy

**Exchange:** Alpaca (crypto, paper)
**Symbols:** BTC/USD, ETH/USD, SOL/USD
**Schedule:** Runs 24/7
**Bars:** 1-minute candles

### Technical Indicators

| Indicator | Config | Purpose |
|---|---|---|
| SMA | 50-bar (1-min) | Trend direction |
| RSI | 14-bar | Momentum |
| ATR | 14-bar | Dynamic stop-loss sizing |
| Volume | 20-bar rolling avg | Conviction filter |
| RSI (1-hour) | 14-bar | Higher-timeframe trend gate |

### BUY Signals

**Setup A — Dip Buy** *(all modes)*
```
RSI < 38                               ← oversold
price > SMA × 0.99                     ← not more than 1% below SMA
volume ≥ 1.2× 20-bar avg
1-hour trend ≠ "down"                  ← multi-timeframe gate
```

**Setup B — Momentum Breakout** *(AGGRESSIVE mode only)*
```
50 ≤ RSI ≤ 65                          ← bullish momentum zone
price > SMA × 1.001                    ← price above SMA
volume ≥ 1.2× 20-bar avg
1-hour trend ≠ "down"
```

### SELL Signal
```
RSI > 70 + volume confirmed            → exit
```

### Multi-Timeframe Filter (1-Hour)
- Fetches 1h candles → computes 20-SMA + RSI
- If 1h trend = "down" → BUY blocked for all symbols
- SELL never blocked — exits always allowed
- 1h data cached 30 minutes

### Stop Loss & Take Profit (Mode-Dependent)

| Mode | Trade Size | Stop Loss | Take Profit |
|---|---|---|---|
| SAFE | $100 | 2.0% | 6.0% |
| AGGRESSIVE | $150 | 2.0% | 8.0% |
| SHIELD | $30 | 1.5% | 4.0% |

Dynamic stop: 2× ATR below entry (fallback: 2.0% fixed).
Trailing stop: 1.5% below highest price reached (enabled by default).

### AI Validation (Claude Haiku — cost optimised)
- Same logic as day bot but confidence gate = 0.55
- Response cached 10 minutes if RSI < 2pt change AND price < 0.3% change

---

## 5. Adaptive Mode System

Both bots run a ModeManager that evaluates performance every cycle and auto-switches modes.
**Anti-flip guard:** minimum 2 completed trades before any switch allowed.

### Day Bot Mode Switching

```
→ SHIELD:       consecutive_losses ≥ 3   OR  daily_pnl ≤ −3%
→ SAFE:         consecutive_losses ≥ 1   OR  win_rate (last 5) < 50%
→ AGGRESSIVE:   consecutive_wins ≥ 3     AND  SPY daily return ≥ +0.3%
                AND consecutive_losses == 0
```

### Crypto Bot Mode Switching

```
→ SHIELD:       consecutive_losses ≥ 3   OR  daily_loss ≥ 5%
→ SAFE:         consecutive_losses ≥ 1   OR  win_rate (last 10) < 50%
→ AGGRESSIVE:   consecutive_wins ≥ 3     AND  BTC 1h trend = "up"
```

### Mode Lifecycle

```
Startup           → SAFE (always)
3 wins + market   → AGGRESSIVE (scale up)
Any loss          → SAFE (scale back)
3 losses          → SHIELD (protect capital)
2 wins in SHIELD  → SAFE (gradual recovery)
3 wins + market   → AGGRESSIVE (if market confirms)
```

Telegram alert fires on every mode change:
`🚀 DayBot Mode: SAFE → AGGRESSIVE | size 25% | SL 1.5% TP 5.0%`

---

## 6. 3-Bucket Profit Harvesting

Daily trading profits extracted into a separate compounding portfolio.
Trading base ($1,000 day / $500 crypto) never depleted by harvesting.

### The 3 Buckets

```
Bucket 1 — Active Trading
  Always funded at base amount.
  Mode controls sizing within this bucket.

Bucket 2 — Long-Term Portfolio
  Source: daily profits when profit ≥ $50
  Hold: 60–90 days
  Target: +30%
  Picker: Claude (Role 2)

Bucket 3 — Compound Portfolio
  Source: 50% of Bucket 2 gains (profit only, never capital)
  Hold: 2–4 weeks (max 30 days)
  Target: +15%
  Picker: Claude (Role 3)
```

### Flow

```
End of Day: daily_pnl ≥ $50?
  YES → Claude picks long-term candidate (Bucket 2)
        → Open position, log to Supabase

Daily Monitor: Bucket 2 hits +30%?
  YES → Close position
        Capital → Recycle into next Bucket 2 (Claude picks again)
        Profit (gains only):
          50% → Add to active trading base
          50% → Open Bucket 3 position (Claude picks)

Daily Monitor: Bucket 3 hits +15%?
  YES → Close position
        50% total proceeds → Active trading base
        50% total proceeds → Next Bucket 2 position

Position expires (max hold days exceeded)?
  → Force close, same split applies
  → If loss: full capital → next Bucket 2 (no base addition)
```

### Key Rules
- Only **profit** ever at risk in Bucket 3 — original capital always recycled
- Claude returns SKIP if market regime is bearish → money stays in base
- Crypto long-term: BTC/USD or ETH/USD only (more stable than SOL)
- Day bot long-term: any stock from scanner or large-cap fallback list

### Telegram Alerts
```
🌱 DayBot Harvest: Extracted $85.00 profit → long-term position opened | regime: trending_up
🎯 DayBot Harvest Target Hit! [long_term] NVDA: +31.2% ($26.50) — Profits reinvested.
```

---

## 7. Auto-Shield System

Runs independently of the Mode Manager inside `record_trade_result()`.
Both systems coexist — Mode Manager controls SL/TP and setups, Auto-Shield controls position size.

### Day Bot Auto-Shield

```
Activates:    consecutive_losses ≥ 2
  → position_size_pct → 1% (micro-size)
  → trade_mode → "house_money"

Deactivates:  consecutive_wins ≥ 2
  → position_size_pct → restored to pre-shield value
  → trade_mode → restored to pre-shield mode
```

### Crypto Bot Auto-Shield

```
Activates when ANY of:
  - consecutive_losses ≥ 5
  - win_rate (last 20 trades) < 40%
  - balance dropped ≥ 10% from peak_balance
  → trade_size_mode → "house_money" (0.3× normal size)

Deactivates when ALL of:
  - win_rate (last 10 trades) ≥ 55%
  - consecutive_losses == 0
```

---

## 8. Risk Management Rules

### Day Bot Hard Limits

| Rule | Limit |
|---|---|
| Max trades per day | 6 |
| Max concurrent positions | 3 |
| Daily loss halt | 5% of portfolio → all trading stops |
| Budget cap | $1,000 paper |
| Earnings blackout | No trade within 2 days of earnings |
| VWAP gate | Price must be ≥ VWAP to buy |

### Crypto Bot Hard Limits

| Rule | Limit |
|---|---|
| Max concurrent positions | 2 |
| Daily loss halt | 5% drop from daily start balance |
| Cooldown after close | 10 cycles (~10 min) before re-entering same symbol |
| Trailing stop | 1.5% below highest price reached |

### Position Sizing by Mode

**Day Bot**

| Mode | Size | Risk/Trade |
|---|---|---|
| SAFE | 15% ($150) | SL 1.0% = max $1.50 risk |
| AGGRESSIVE | 25% ($250) | SL 1.5% = max $3.75 risk |
| SHIELD | 3% ($30) | SL 1.0% = max $0.30 risk |

**Crypto Bot**

| Mode | Size | Risk/Trade |
|---|---|---|
| SAFE | $100 | SL 2.0% = max $2.00 risk |
| AGGRESSIVE | $150 | SL 2.0% = max $3.00 risk |
| SHIELD | $30 | SL 1.5% = max $0.45 risk |

---

## 9. Full Decision Flow (Every Cycle)

```
Every 15 minutes (day bot) / Every 60 seconds (crypto bot):

STEP 1 — Mode Check
  ModeManager evaluates: wins, losses, market trend
  → Sets mode: SAFE | AGGRESSIVE | SHIELD
  → If switched: update state + fire Telegram alert

STEP 2 — Scan / Watchlist
  Day bot:    MarketScanner fetches snapshots of 33 stocks
              Filters: gap ≥1% OR mover ≥1.5% OR volume ≥1.5×
              → Up to 15 candidates
  Crypto bot: BTC/USD, ETH/USD, SOL/USD (fixed, 24/7)

STEP 3 — Technical Indicators (per symbol)
  Compute: EMA-50 or SMA-50, RSI-14, Volume vs avg, VWAP (day), ATR (crypto)
  → Evaluate setup conditions → raw signal: BUY | SELL | HOLD

STEP 4 — Setup Filter
  If mode = SAFE or SHIELD → block Setup B (breakout)
  If RSI 45–55 → force HOLD
  If below VWAP → force HOLD (day bot)
  If earnings within 2 days → force HOLD (day bot)

STEP 5 — Multi-Timeframe Filter
  Day bot:    Check SPY daily return ≥ +0.3% for AGGRESSIVE gate
  Crypto bot: Fetch 1h candles → compute 1h trend
              If 1h trend = "down" → block all BUY signals

STEP 6 — Claude AI Validation
  If raw signal = HOLD → SKIP (Claude not called)
  If raw signal = BUY or SELL:
    → Build prompt with technicals + 4-week history + bot trade history
    → Call Claude → get decision + confidence + reason
    → If Claude disagrees with rules → HOLD
    → If confidence < threshold → HOLD
    → If Claude confirms → proceed

STEP 7 — Risk Gates
  Check: daily loss limit not breached
  Check: max concurrent positions not exceeded
  Check: no existing position in symbol (for BUY)
  Check: max trades/day not exceeded (day bot)
  All pass → execute trade

STEP 8 — Execute Trade
  Size = mode-controlled position %
  Set stop-loss and take-profit per mode
  Day bot: trailing stop not used
  Crypto bot: trailing stop at 1.5% below highest price

STEP 9 — Post-Trade Auto-Shield Check
  After each CLOSE: count consecutive losses
  ≥ 2 losses (day) or ≥ 5 losses (crypto) → activate shield micro-sizing
  ≥ 2 wins after shield → deactivate, restore normal size

END OF DAY (day bot) / Rolling daily reset (crypto):

STEP 10 — Harvest Check
  daily_pnl ≥ $50?
    YES → Call Claude (Role 2) with candidate list + market regime
          Claude returns symbol + confidence
          confidence ≥ 0.60 → open Bucket 2 long-term position
          SKIP or low confidence → profit stays in trading base

STEP 11 — Monitor Open Harvest Positions
  Bucket 2 hit +30%?
    → Close, recycle capital, split gains (50% base / 50% → Bucket 3 via Claude)
  Bucket 3 hit +15%?
    → Close, split proceeds (50% base / 50% → new Bucket 2 via Claude)
  Max hold days exceeded?
    → Force close, apply same split logic
```

---

## Summary: What Claude Decides vs What Rules Decide

| Decision | Who Makes It |
|---|---|
| Which stocks to scan | Scanner filters (gap, mover, volume) |
| BUY/SELL signal generation | Technical rules (RSI, EMA, VWAP, Volume) |
| Signal confirmation | Claude AI (must agree with rules) |
| Confidence gate (reject weak signals) | Claude AI |
| Mode switching (SAFE/AGGRESSIVE/SHIELD) | ModeManager (rule-based, win/loss streak) |
| Position sizing | Mode (rule-based) |
| Stop loss / Take profit levels | Mode (rule-based) |
| Long-term stock pick (Bucket 2) | Claude AI |
| Compound stock pick (Bucket 3) | Claude AI |
| Market regime assessment | Rules (SPY return, BTC 1h trend) |
| SKIP harvesting in bad markets | Claude AI |
