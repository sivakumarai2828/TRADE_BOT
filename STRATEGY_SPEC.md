# TRADE_BOT_V2 — Stock Selection Specification

> **Snapshot: 29 August 2026.** Paper trading only. Nothing here is investment advice.
> Written to be self-contained, so a reviewer can critique the strategy without reading the source.
>
> Companion web version: https://claude.ai/code/artifact/4033a16b-d55e-4934-a717-880ceb899d07
> The older `STRATEGY.md` is the original July design document and is now partly out of date.

**System in one line:** a language model acts as portfolio manager and proposes trades;
deterministic code enforces risk limits it cannot override.

| | |
|---|---|
| Markets | US equities, India NSE |
| Capital | $5,000 (US), ₹2,50,000 (India) — paper |
| Live since | 6 July 2026 |
| Closed trades | 19 |
| Model | `anthropic/claude-sonnet-5` via OpenRouter |
| Decision cadence | Once per market per day + 20-minute stop monitor |

---

## 0. Division of authority

The model has full discretion over **which** stocks to buy, where to place stops and
targets, and when to exit. It has **zero** authority over how much to buy or whether an
order is permitted.

| Layer | Owns |
|---|---|
| **MODEL** | Stock selection, entry timing, stop placement, target placement, exit decisions, market regime read |
| **CODE** | Position size, reward/risk gate, stop-distance bounds, earnings block, position count, daily entry limit, daily loss halt, kill switch, stop-ratchet enforcement |

This split matters for review: if the strategy is bad, the fault is in the prompt and the
data. If the system blows up, the fault is in the risk layer. They fail independently.

**Schedule**

- India decision cycle — 10:00 IST daily
- US decision cycle — 10:00 ET daily (DST-safe via `zoneinfo`)
- Stop/target monitor — every 20 minutes during market hours, **no model call required**
- Weekly self-review — Saturdays 08:00 UTC

---

## 1. Universe

**162 US names** — mega-cap tech, semis, software, financials, healthcare, industrials,
energy, materials, consumer, telecom, utilities, a few REITs.

**105 India names** — roughly Nifty 100 plus liquid mid-caps.

The model may additionally request up to 5 extra tickers per cycle (`watch_next`) that get
fetched next time, so the pool is not strictly fixed.

> **Recently quadrupled.** The universe was 39 US names until 25 Aug 2026. At that size a
> genuine quality filter produced 0–1 buyable candidates on a typical day, making full
> capital deployment arithmetically impossible. Expanding the pool raised deployment
> without relaxing any standard.

---

## 2. Per-stock data

Computed fresh each cycle from one year of daily bars (yfinance, auto-adjusted).

| Field | Definition |
|---|---|
| `price` | Latest close |
| `sma50` / `sma150` / `sma200` | Simple moving averages |
| `ema20` | 20-day exponential moving average |
| `rsi14` | 14-day Relative Strength Index |
| `atr14` | 14-day Average True Range |
| `ret_5d_pct` / `ret_1m_pct` / `ret_3m_pct` / `ret_6m_pct` | Trailing returns |
| `pct_off_52w_high` | Distance below the 52-week high |
| `pct_above_52w_low` | Distance above the 52-week low |
| `low_20d` | Lowest low of last 20 sessions — structural stop reference |
| `vol20_vs_vol50` | Volume expansion ratio |
| `stage2_uptrend` | Minervini trend-template boolean (below) |
| `rs_3m_vs_benchmark` | 3-month return minus benchmark 3-month return |
| `days_to_earnings` | Days to next scheduled report; `null` when unknown |
| `last_5_closes` | Raw recent price action |

### The Stage-2 flag

A single boolean encoding Mark Minervini's trend template. True only when **all** hold:

```
price > SMA50 > SMA150 > SMA200
SMA200 today > SMA200 twenty-two sessions ago     # 200-day rising ~1 month
price >= 52-week low  x 1.30                      # at least 30% off the low
price >= 52-week high x 0.75                      # within 25% of the high
```

Supplied as **context, not a hard gate** — code will execute a buy where this is false,
provided the risk rules pass.

---

## 3. Market context

Benchmarks fetched with the same indicator set:

- US — `SPY`, `QQQ`, `^VIX`
- India — `^NSEI`, `^NSEBANK`

One derived figure is appended: **breadth**, the percentage of the universe trading above
its own 200-day average.

### What the filter actually removes

Measured live on 25 Aug 2026 against the US universe, applying the model's own stated screen:

| Step | Names remaining |
|---|---|
| Universe | **162** |
| Stage-2 uptrend | 42 |
| ...and within 3% of 52w high | 11 |
| ...and RSI 50–65 | **1** |
| ...and no earnings within 3 days | **1** |

The same funnel on the old 39-name universe ended at **zero**.

⚠️ The RSI 50–65 band is the **model's self-imposed preference, not a coded rule**. It is
the single most aggressive narrowing step (11 → 1) and a reasonable thing to challenge.

---

## 4. Instructions given to the model

Verbatim system prompt, current as of 29 Aug 2026:

```text
You are the portfolio manager of a swing-trading book. You have FULL
discretion within the hard risk caps provided — you pick symbols, entries,
stops, targets, position intent, exits, and sizing. Your job is to keep the
book INVESTED in the best opportunities available, not to wait for perfection.

Principles you are evaluated on:
- Quality first, but selectivity is not inaction: pick the highest-expectancy
  candidates available and deploy into them. Chasing extended, broken or
  overbought names remains forbidden - that is what quality means here.
- Asymmetry: only enter when your realistic target is at least 2x the
  distance to your stop.
- Regime: only a clearly BEARISH benchmark (below a falling 50DMA) justifies
  sitting in cash. A choppy, indecisive or merely non-trending tape does NOT.
- Learn from your own record: your trade history, stats, and your latest
  self-review memo are included. Do not repeat documented mistakes.
- Stops are honored by code automatically. Set them where the thesis is
  invalidated, not at arbitrary percentages.
- You may raise stops on winners (never lower them), take full profits, or
  cut losers early with a reason. Partial exits are not supported — SELL
  closes the whole position.
- BUY orders without a target above entry, or with reward less than 2x the
  stop distance, are rejected by code. BEFORE submitting any BUY, compute it
  yourself: R = (target - entry) / (entry - stop). If R < 2.0 the order is
  WASTED - it gets rejected and that capital stays idle. When a setup you like
  gives R < 2.0, either place the stop tighter at real structure (e.g.
  low_20d), or raise the target to the next genuine resistance if realistic,
  or drop the name and pick another. NEVER put a target near the entry price.
  State the R you calculated in each BUY thesis.
- BUY orders within 3 days of a scheduled earnings report are rejected by
  code. Candidate data includes days_to_earnings where known — plan entries
  around it, and consider earnings risk on positions you already hold.
- Candidate data includes stage2_uptrend (Minervini trend-template pass),
  rs_3m_vs_benchmark (3-month relative strength vs the market), and low_20d
  (recent swing low, a natural stop reference). The regime block includes
  universe breadth. These are context, not rules — but your documented losses
  came from entries that failed exactly these checks.

- CAPITAL DEPLOYMENT IS A REQUIREMENT, not a preference. Your portfolio
  reports pct_deployed and target_pct_deployed. Unless the benchmark regime is
  clearly bearish (benchmark below its 50DMA and falling) or a halt is active,
  you MUST work toward the target every cycle: rank every candidate that
  satisfies the hard caps and BUY the best available, up to the daily entry
  limit. Do NOT hold cash because no candidate is 'perfect' - a merely good
  setup that clears the code caps beats idle cash, and your own screen is
  stricter than the caps require. Finishing below target is acceptable ONLY if
  the regime is bearish, a halt is active, or no candidate can satisfy the
  CODE caps (2R reward/risk, stop distance, no earnings within 3 days); say
  which one in market_view.

Respond with ONLY a JSON object, no markdown fences, matching:
{
  "market_view": "1-3 sentences on regime and what you're doing about it",
  "actions": [
    {"action": "BUY",  "symbol": "XYZ", "stop": 123.4, "target": 150.0,
     "thesis": "one or two sentences", "confidence": 0.0-1.0},
    {"action": "SELL", "symbol": "XYZ", "reason": "why exiting now"},
    {"action": "ADJUST", "symbol": "XYZ", "stop": 130.0, "target": 155.0,
     "reason": "why moving levels"}
  ],
  "watch_next": ["symbols you want extra data on next cycle (max 5)"]
}
An empty actions list is acceptable ONLY when you are at or above target
deployment, or the regime is bearish, or a halt is active.
```

### What else is in the prompt

Alongside the instructions the model receives, every cycle:

- the full candidate array (all fields from section 2)
- the regime block including breadth
- its current portfolio with live unrealised P&L per position
- its lifetime trade statistics
- its last 15 closed trades — **original thesis versus actual outcome**
- its most recent weekly self-review memo
- the hard caps, rendered as JSON, so it knows the boundaries

---

## 5. Hard gates

Applied in code to every proposed BUY. Failure means the order is silently discarded.

| Gate | Rule | Configurable |
|---|---|---|
| Direction | `stop < entry` | no — long only |
| Target present | `target > entry` | no |
| **Reward / risk** | `(target − entry) / (entry − stop) >= 2.0` | **no — hardcoded** |
| Stop not too tight | stop distance `>= 1%` | no |
| Stop not too wide | stop distance `<= 15%` | no |
| Earnings proximity | `days_to_earnings > 3` | no |
| Open positions | `< 8` per market | env |
| New entries today | `< 5` per market | env |
| Already held | no pyramiding | no |
| Daily loss halt | day P&L `> −3%` of equity | env |
| Kill switch | equity `>` peak × 0.90 | env |

**Notes**

- The earnings gate is deliberately asymmetric: a `null` value (common for Indian tickers,
  whose calendars are often missing) never blocks a trade. Missing data means "unknown",
  never "safe" — but the alternative would silently freeze much of the India book.
- Tripping the kill switch **flattens every position** and halts that market until a key is
  manually cleared in the database.
- Cap values are sanity-checked at startup; the process refuses to run with unsafe numbers
  (risk/trade ≤ 2%, position ≤ 34%, kill switch ≤ 15%, entries/day 1–5).

---

## 6. Position sizing

The model has **no input** here.

```python
max_loss        = equity * 0.02              # risk budget for this trade
qty_by_risk     = max_loss / (entry - stop)
qty_by_position = (equity * 0.25) / entry    # concentration cap
qty_by_cash     = cash / entry               # no leverage

qty = floor(min(qty_by_risk, qty_by_position, qty_by_cash))
if qty < 1: reject
```

The binding constraint shifts with stop width:

| Stop distance | Limited by | Resulting position |
|---|---|---|
| 5–8% | 25% position cap | 25% of equity |
| 10% | 2% risk budget | 20% of equity |
| 12% | 2% risk budget | 16.7% of equity |

So **wider stops automatically produce smaller positions** — risk per trade stays
near-constant regardless of the model's stop choice.

**Simulated costs:** 0.05% per side (US), 0.15% per side (India), applied to entry and exit.

### Deployment target

A separate figure, currently **80%** (`TARGET_DEPLOYMENT_PCT`), tells the model how much of
the book should be invested. It is not a cap — the book can reach 95% — it is a floor the
model must justify falling below. Setting it to `0` restores fully discretionary cash
management with no code change.

---

## 7. Exits

Three independent paths out of a position:

1. **Stop hit** — checked every 20 minutes against the live price. Sells at the *observed*
   price, so a fast move can fill below the stop.
2. **Target hit** — same monitor, full position closed.
3. **Model exit** — during the daily cycle the model may `SELL` with a stated reason,
   typically because its thesis broke before either level was reached.

**Stops ratchet upward only.** The model may raise a stop on any cycle; code rejects any
attempt to lower one, and rejects a stop set at or above the current price. In practice
this appears to be where most of the edge comes from: once a stop clears the entry price
the trade cannot lose, and several exits recorded as `stop_loss` have been *profitable*.

### Worked example — AAPL, July 2026

| | |
|---|---|
| Entry | 3 shares @ $311.12 |
| Initial stop | ~$300 (20-day EMA, where the thesis breaks) |
| Target | $336 |
| Reward/risk | **2.24R** |
| Risk taken | $33 = **0.67% of equity** |

Stop raised across six sessions: `304 → 305 → 314.50 → 321.50 → 323`. On 16 July the stop
cleared the entry price — from that point the trade could not lose. Price fell on 23 July;
the monitor sold at $321.50 for **+$30.67 (+3.29%)**, logged as `stop_loss`.

The stock closed at $332.73 the next day — the cost of the same mechanism.

There are **no partial exits**. `SELL` closes the entire position.

---

## 8. Feedback loop

Every Saturday the model reviews its own closed trades — original thesis against actual
outcome — and writes a memo of at most 250 words: patterns in wins and losses, mistakes to
stop repeating, adjustments for the coming week. That memo is injected into every
subsequent decision prompt.

It demonstrably influences behaviour. Recent market views cited it unprompted:

> "GRASIM already burned me on a chase before"

> "avoiding the RSI 60–65 marginal-match mistake from my memo"

Whether this constitutes learning or merely narrative consistency is an open question.

---

## 9. Live results

Paper trading, 6 July – 29 August 2026. **Sample is far too small for significance.**

| Metric | US | India |
|---|---|---|
| Equity | $5,007.93 | ₹2,49,739 |
| Total return | +0.16% | −0.10% |
| Closed trades | 10 | 9 |
| Win rate | 50.0% | 33.3% |
| Average win | +$40.77 | +₹1,938.54 |
| Average loss | −$38.70 | −₹1,438.57 |
| Realised P&L | +$10.34 | −₹2,815.83 |
| Deployed | 95.4% | 95.4% |
| Open positions | 5 | 4 |

Open US positions carry 2.05R to 2.83R. India's realised figure is dominated by three early
trades that chased breakouts at highs — a pattern the model subsequently identified in its
own memo and stopped repeating.

### Promotion criteria (none yet met in full)

- [ ] 20+ closed trades
- [ ] Positive combined P&L
- [ ] Average win > average loss in **both** books
- [ ] Maximum drawdown < 7%
- [ ] Unit tests covering the risk layer

**There are no automated tests at all today.** That is the single largest objection to
running real money through this system.

---

## 10. Known weaknesses

Where a second opinion would be most useful.

**1. Selection may be indistinguishable from momentum beta.**
Every filter — Stage-2, relative strength, proximity to highs — selects for recent winners.
In a rising market that is hard to separate from simply owning high-beta stocks. Backtests
on the same universe showed buy-and-hold **outperforming** this logic over 2023–2026
(+112% vs +64%); the strategy's advantage appeared only in the 2021–23 drawdown.

**2. The RSI 50–65 preference is unvalidated.**
It is the model's own invention, not a coded rule, and it is the harshest step in the funnel
(11 candidates → 1). No backtest supports this specific band.

**3. No partial exits.**
Positions are all-or-nothing. Staged profit-taking tested *worse* than the current rule
(win rate rose 50% → 61% while average win fell 13.4% → 7.7%), but that test used mechanical
ladders rather than model judgment.

**4. Trailing stops get shaken out before rebounds.**
Across 239 historical stop events, 65% of stopped positions recovered above the stop within
10 sessions in a bull market. Giving stops a two-week grace period nearly doubled
bull-market returns but halved bear-market returns and produced a 53% drawdown — so it was
rejected.

**5. Self-reported confidence is meaningless.**
Each BUY carries a confidence score. It is not calibrated and nothing consumes it.

**6. Reliability has been the real failure mode, not strategy.**
Two multi-day outages: a network error killed the process for 7 days in August, and a
model-configuration error produced empty responses for 3 more. Both were silent.
Failed-cycle alerting was only added on 29 Aug 2026.

**7. Single data source.**
All prices, indicators and earnings dates come from yfinance daily bars. Earnings calendars
for Indian tickers are frequently missing, silently disabling that gate for much of the
India universe.

### Questions worth putting to a reviewer

1. Is the 2R minimum the right gate, or does it systematically exclude high-probability,
   low-payoff setups worth taking?
2. Does an 80% deployment floor conflict with quality selection in a way that will show up
   as worse expectancy?
3. Should the Stage-2 flag be promoted from context to a hard gate?
4. Is a daily decision cadence appropriate, or does a 20-minute stop monitor combined with
   daily entries create a mismatch?
5. With 19 closed trades, what sample size would you require before allocating real capital?

---

*Figures captured 29 August 2026. The system is under active modification; the prompt in
section 4 changes as the strategy evolves.*
