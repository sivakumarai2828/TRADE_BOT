# TRADE_BOT_V2 — Strategy Document

> Version 2.0 | July 2026 | Paper trading (US: USD, India: INR)
> Core idea: **Claude decides trades. Code enforces limits. Human governs the system.**

---

## 1. Philosophy

The V1 bot failed because rules made the decisions and the AI could only veto.
It traded 1-minute noise, used the same fixed 2% stop for everything, forced
same-day exits, and never learned from its losses.

V2 inverts this. Claude is the portfolio manager with full discretion inside
hard risk caps. Its instructions are built around five principles:

1. **Expectancy over activity.** It is never required to trade. Doing nothing
   is a respected answer. Forced trades were V1's #1 failure mode.
2. **Asymmetry.** Only enter when the realistic target is at least 2x the
   distance to the stop.
3. **Regime first.** If the overall market (SPY/QQQ or Nifty) is weak or
   choppy, hold cash without apology.
4. **Learn from the record.** Its own trade history, stats, and self-review
   memos are in every prompt. Documented mistakes must not repeat.
5. **Stops mean something.** Placed where the thesis is invalidated, not at
   arbitrary percentages. Honored automatically by code.

---

## 2. Markets and style

| | US | India |
|---|---|---|
| Universe | 38 liquid large caps (tech, semis, finance, healthcare, energy) | Nifty top-40 |
| Benchmarks | SPY, QQQ, VIX | Nifty 50, Bank Nifty |
| Style | Swing, daily bars, hold days–weeks | Same |
| Execution | Local paper ledger + optional Alpaca paper mirror | Local paper ledger (INR) |
| Decision time | 14:30 UTC (10:00 AM New York) | 04:30 UTC (10:00 AM IST) |

The AI may also request up to 5 extra symbols per cycle ("watch_next") that
get full data next cycle — the universe is a starting point, not a cage.

---

## 3. The daily decision cycle

Once per market per day, Claude receives:

- **Market regime** — benchmark trend, RSI, 1w/1m/3m returns, VIX
- **Candidates** — for each stock: price, SMA50/200, EMA20, RSI, ATR,
  returns over 5d/1m/3m/6m, distance from 52-week high/low, volume trend
- **Portfolio** — cash, equity, every open position with live P&L, its
  original stop/target/thesis
- **Track record** — win rate, avg win vs avg loss, total P&L, last 15
  closed trades with thesis vs outcome
- **Its own latest self-review memo**
- **The hard caps** (so it sizes trades that will pass validation)

It returns a JSON plan: BUY (with stop, target, thesis, confidence),
SELL (with reason), ADJUST (move stop/target), plus a 1–3 sentence market
view — or an empty plan.

---

## 4. Hard risk caps (code-enforced, AI cannot override)

| Rule | Default | Effect |
|---|---|---|
| Max risk per trade | 1.5% of equity | Loss if stop hits is always small |
| Max position size | 20% of equity | No concentration blowups |
| Max open positions | 6 per market | Forced diversification |
| Max new entries | 2/day per market | No revenge-trading sprees |
| Stop distance | 1%–15% from entry | No fake or absurd stops |
| Stop direction | Up only | Winners get protected, never re-exposed |
| Daily loss halt | −3% of equity in a day | No new buys until tomorrow |
| Kill switch | −10% from equity peak | Sell everything, halt until human reset |
| Long only, no leverage | Always (paper phase) | — |

**Position sizing** is derived from risk, not conviction:
`shares = (equity × 1.5%) ÷ (entry − stop)`, then clamped by the 20% position
cap and available cash — the smallest number wins, rounded to whole shares.

Example with $1,000: entry $50, stop $47 → risk budget $15 ÷ $3 = 5 shares,
position cap $200 ÷ $50 = 4 shares → **buys 4 shares**. Worst case −$12,
target case (+$9/share) +$36.

---

## 5. Position management

- Every 20 minutes during market hours, code checks each holding:
  price ≤ stop → sell immediately; price ≥ target → sell immediately.
  No AI call, no hesitation.
- In the daily cycle the AI may raise stops (trail winners), take profits
  early, or cut a loser before the stop with a stated reason.
- No pyramiding (adding to an existing position) in v2.0.

---

## 6. The learning loop

1. Every trade is journaled with its **thesis** at entry and **outcome** at exit.
2. Every AI decision (including "do nothing") is stored with full context.
3. **Every Saturday**, Claude reads its closed trades and stats, compares
   thesis vs outcome, and writes a ≤250-word self-review memo: patterns in
   wins/losses, mistakes to stop repeating, adjustments for next week.
4. That memo is injected into **every decision prompt** the following week.

This is the compounding edge V1 never had: the same intelligence that trades
also audits itself weekly, in writing, with consequences.

---

## 7. Human governance

| When | Human decision |
|---|---|
| Setup | Capital, risk caps, markets on/off (`.env`) |
| Daily | Passive: read Telegram reports; can stop the service anytime |
| Kill switch fired | Only a human can investigate and reset |
| After 4+ weeks paper | Whether the record earns real money, and how much |

**Go-live criteria (all required):** ≥20 closed trades, positive total P&L,
average win > average loss, max drawdown < 7%, no single symbol causing
repeated losses.

---

## 8. What this strategy does NOT do

No intraday scalping, no options, no crypto, no shorting, no leverage, no
averaging down, no trading during account halts, and no guarantee of profit —
the caps limit damage; the AI's job is to find the returns. If it can't
demonstrate them on paper, it never touches real money.
