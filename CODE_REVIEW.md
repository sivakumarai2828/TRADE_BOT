# TRADE_BOT_V2 — Code Review and Strategy Implementation Notes

Review date: 2026-07-05
Project folder: `C:\Users\konda\Downloads\Projects\Trade_App\TRADE_BOT_V2`

## 1. Executive Summary

The project strategy is directionally strong: Claude/AI acts as the portfolio manager, deterministic code enforces hard risk limits, and the human user governs the system. This matches the stated V2 core idea: **Claude decides trades, code enforces limits, human governs the system** [1].

However, the current code should be treated as **paper-trading only**. It is not yet safe enough for real-money/live broker execution without fixes.

### Overall verdict

- **Strategy design:** Good.
- **Project structure:** Good.
- **Paper testing readiness:** Acceptable with caution.
- **Real-money readiness:** Not yet.
- **Main risk:** Some strategy promises are currently prompt-only or partially implemented, not fully enforced in code.

---

## 2. What the Strategy Intends

The documented strategy is a swing-trading system for US and Indian equities. It uses daily bars and aims to hold positions for days to weeks. The US universe is 38 liquid large caps, while India uses Nifty top-40 names. The US benchmark context is SPY, QQQ, and VIX; India uses Nifty 50 and Bank Nifty [1].

The intended daily cycle is:

1. Fetch market regime data.
2. Fetch candidate stock data.
3. Send portfolio state to Claude.
4. Send trade record and self-review memo.
5. Send hard caps.
6. Claude returns a JSON plan containing BUY, SELL, ADJUST, watchlist, or no action [1].

This is a good architecture because it separates judgment from risk control:

- AI handles discretionary judgment.
- Code enforces risk and position limits.
- Human controls capital, settings, and kill-switch recovery.

---

## 3. Strong Parts of the Code

### 3.1 Clean module separation

The project is organized well:

- `main.py` — scheduler and CLI entry point.
- `botv2/config.py` — configuration and hard caps.
- `botv2/risk.py` — risk enforcement.
- `botv2/executor.py` — applies AI actions through risk layer.
- `botv2/ledger.py` — SQLite-backed paper ledger.
- `botv2/ai_pm.py` — Claude prompt and response parsing.
- `botv2/data.py` — yfinance market data.
- `botv2/journal.py` — decisions, trades, memos, key-value state.
- `botv2/alpaca_mirror.py` — optional Alpaca paper mirror.
- `botv2/notify.py` — Telegram notifications.

This separation makes the bot easier to understand and maintain.

### 3.2 Risk-based sizing is implemented

`risk.py` correctly sizes positions using:

```python
max_loss = equity * caps.max_risk_per_trade_pct
qty_by_risk = max_loss / (price - stop)
qty_by_pos_cap = (equity * caps.max_position_pct) / price
qty_by_cash = cash / price
qty = min(qty_by_risk, qty_by_pos_cap, qty_by_cash)
```

This matches the strategy idea that position size should be based on loss-to-stop, then clamped by position size and available cash.

### 3.3 Max position, max trade risk, max positions, and max daily entries exist

The following are implemented in `RiskEngine.validate_buy()`:

- Stop must be below entry.
- Stop distance must be between minimum and maximum distance.
- Max open positions enforced.
- Max new entries per day enforced.
- Size is clamped by risk, position cap, and cash.

This is a good foundation.

### 3.4 Kill switch exists

`RiskEngine.check_halts()` tracks equity peak and can kill-switch the bot if equity drops too far from peak.

This is directionally correct, but it needs to be called more often. See critical issue #5 below.

### 3.5 No pyramiding

The executor skips BUY if the symbol is already held:

```python
if sym in by_symbol:
    log.info("BUY %s skipped — already held (no pyramiding in v2.0)", sym)
    return
```

This matches the V2 conservative design.

### 3.6 Weekly AI self-review exists

The project includes a weekly self-review loop. Claude reads recent closed trades and stats, then writes a memo that is included in future prompts.

This supports the strategy's learning-loop concept.

---

## 4. Critical Issues to Fix

## Issue 1 — Stop/target monitor does not truly use intraday/live price

### Current behavior

`executor.py` runs `monitor_positions()` every 20 minutes during market hours.

But position prices come from `ledger.positions()`, which calls `data.fetch_price()`.

Current `fetch_price()` uses:

```python
yf.Ticker(symbol).history(period="5d", interval="1d")
```

That is daily-bar data, not proper intraday/live data.

### Why this matters

The strategy says the bot should monitor stops/targets during the session. But with daily candles, the bot may not see an intraday stop break until too late.

Example:

- A stock enters at 100.
- Stop is 95.
- Intraday price falls to 94.
- Daily price fetch may not reflect the current intraday move correctly.
- The bot may fail to exit when intended.

### Recommended fix

Use an intraday price function for monitoring:

```python
def fetch_intraday_price(symbol: str) -> float | None:
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="1d", interval="5m")
        if df is None or df.empty:
            df = yf.Ticker(symbol).history(period="5d", interval="1d")
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception:
        return None
```

Then use this in `monitor_positions()`.

Better design:

- `fetch_snapshot()` — daily context for AI.
- `fetch_price()` — latest executable/monitoring price.
- `fetch_daily_close()` — daily-bar backfill only.

---

## Issue 2 — 2R reward/risk is not enforced by code

### Current behavior

The strategy says entries should only be taken when realistic target is at least 2x the distance to stop.

But `risk.py` does not validate target at all.

A bad trade like this could pass:

```json
{
  "action": "BUY",
  "symbol": "AAPL",
  "stop": 190,
  "target": 202
}
```

If entry is 200:

- Risk = 10
- Reward = 2
- Reward/risk = 0.2R

This violates the strategy but current code may accept it.

### Recommended fix

Change `validate_buy()` to accept target:

```python
def validate_buy(self, symbol: str, price: float, stop: float, target: float | None,
                 equity: float, cash: float, open_positions: int) -> BuyVerdict:
```

Then add:

```python
if target is None or target <= price:
    return BuyVerdict(False, reason=f"{symbol}: target must be above entry")

risk = price - stop
reward = target - price
if reward < 2 * risk:
    return BuyVerdict(False, reason=f"{symbol}: reward/risk {reward / risk:.2f}R < 2R minimum")
```

Update `executor.py` to pass target into `validate_buy()`.

This is a high-priority fix because asymmetry is one of the main pillars of the strategy.

---

## Issue 3 — Duplicate SELL actions can double-credit cash

### Current behavior

In `executor.py`, open positions are loaded once:

```python
by_symbol = {p["symbol"]: p for p in positions}
```

If Claude returns duplicate SELL actions for the same symbol, the first sell closes the position and adds cash. But the second SELL can still find the old symbol in `by_symbol` and call `_exit()` again.

This can cause:

- Paper ledger cash to be overstated.
- Alpaca mirror to receive duplicate sell orders.
- Possible short exposure in Alpaca paper account.

### Recommended fix

After a successful SELL, remove the symbol from `by_symbol`:

```python
if kind == "SELL" and sym in by_symbol:
    self._exit(by_symbol[sym], f"ai_exit: {act.get('reason', '')[:80]}")
    by_symbol.pop(sym, None)
```

Also harden `ledger.sell()` and/or `journal.close_trade()` so already-closed trades cannot be closed again.

Example:

```python
row = c.execute(
    "SELECT qty, entry, status FROM trades WHERE id=?",
    (trade_id,)
).fetchone()

if not row or row["status"] != "open":
    return
```

This is one of the most important fixes.

---

## Issue 4 — Daily loss halt ignores unrealized losses

### Current behavior

`ledger.day_pnl()` only counts realized P&L from trades closed today:

```python
closed = [t for t in self.journal.recent_closed(self.market, 50)
          if (t["ts_close"] or 0) >= midnight]
return round(sum(t["pnl"] or 0 for t in closed), 2)
```

It ignores open/unrealized losses.

### Why this matters

If open positions are deeply red but not closed yet, the daily halt may not trigger. The AI could continue opening new trades during a bad day.

### Recommended fix

Store start-of-day equity and calculate:

```python
day_pnl = current_equity - day_start_equity
```

Suggested keys:

- `{market}_day_key`
- `{market}_day_start_equity`

Pseudo-code:

```python
def day_pnl(self) -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key_day = f"{self.market}_day_key"
    key_equity = f"{self.market}_day_start_equity"

    if self.journal.kv_get(key_day) != today:
        self.journal.kv_set(key_day, today)
        self.journal.kv_set(key_equity, str(self.equity()))
        return 0.0

    start_equity = float(self.journal.kv_get(key_equity, str(self.equity())))
    return round(self.equity() - start_equity, 2)
```

For best accuracy, use market-local day instead of UTC day.

---

## Issue 5 — Kill switch only runs during AI decision cycle

### Current behavior

`risk.check_halts()` is called in `run_cycle()`, but not in `monitor_positions()`.

That means the bot may only detect max drawdown during the daily AI cycle.

### Why this matters

A kill switch should work during the session too, especially if prices are moving quickly.

### Recommended fix

Call `check_halts()` at the start of `monitor_positions()`:

```python
def monitor_positions(self) -> None:
    equity = self.ledger.equity()
    day_pnl = self.ledger.day_pnl()
    halt = self.risk.check_halts(equity, day_pnl)

    if halt and "KILL SWITCH" in halt:
        fills = self.ledger.flatten_all("kill_switch")
        for f in fills:
            if self.alpaca:
                self.alpaca.order(f["symbol"], f["qty"], "sell")
        notify.alert(f"🔴 {self.market} {halt}")
        return

    for pos in self.ledger.positions():
        ...
```

---

## 5. Important Non-Critical Issues

## Issue 6 — ADJUST validation is too weak

### Current behavior

The code prevents lowering stops:

```python
if new_stop is not None and pos["stop"] and float(new_stop) < float(pos["stop"]):
    new_stop = None
```

This is good, but incomplete.

### Missing checks

The bot should also reject:

- Stop above current price.
- Target below current price.
- Target below stop.
- Stop/target combinations that destroy reward/risk.
- Invalid numbers, `NaN`, or negative values.

### Recommended fix

Add `validate_adjust()` to `RiskEngine`.

Suggested rules:

```python
if new_stop is not None:
    if new_stop < old_stop:
        reject
    if new_stop >= current_price:
        reject

if new_target is not None:
    if new_target <= current_price:
        reject
    if new_stop is not None and new_target <= new_stop:
        reject
```

---

## Issue 7 — Prompt says partial exits are allowed, but code does not support them

In `ai_pm.py`, the system prompt says:

```text
You may raise stops on winners, take partial/full profits...
```

But the executor only supports full SELL.

### Recommended fix

Simplest fix: remove “partial” from the prompt.

Better future fix: support partial exits with JSON like:

```json
{"action": "SELL", "symbol": "AAPL", "qty_pct": 50, "reason": "trim after target hit"}
```

Then implement partial close logic in ledger and executor.

---

## Issue 8 — US schedule has daylight-saving-time problem

Current code schedules US cycle at:

```python
schedule.every().day.at("14:30").do(us_cycle)
```

The comment says this equals 10:00 AM New York.

That is true during daylight saving time, but not during standard time. During winter, 14:30 UTC is 9:30 AM New York.

### Recommended fix

Use timezone-aware scheduling with `zoneinfo`.

Example:

```python
from zoneinfo import ZoneInfo

ny_now = datetime.now(ZoneInfo("America/New_York"))
if ny_now.hour == 10 and ny_now.minute == 0:
    runners["US"].run_cycle()
```

For India:

```python
india_now = datetime.now(ZoneInfo("Asia/Kolkata"))
```

---

## Issue 9 — Alpaca mirror can desync from local ledger

### Current behavior

The local paper ledger is source of truth. Alpaca is only mirrored after local fill:

```python
fill = self.ledger.buy(...)
if fill:
    if self.alpaca:
        self.alpaca.order(sym, fill["qty"], "buy")
```

If Alpaca order fails, local ledger still thinks the trade happened.

### For paper trading

This is acceptable if clearly understood.

### For real trading

Not acceptable without reconciliation.

Before live trading, add:

- Alpaca order ID storage.
- Order status checking.
- Daily reconciliation between broker positions and local ledger.
- Broker-side stop/bracket orders.
- Failure alerts when mirror order fails.

---

## Issue 10 — Full prompt/context is not stored with decisions

The strategy says decisions and context should be logged for learning.

Current `journal.log_decision()` stores the decision JSON but not the full prompt/context.

### Recommended fix

Short-term:

```python
self.journal.log_decision(self.market, decision, note=prompt[:50000])
```

Better:

Add a `prompt_context` column to the `decisions` table.

---

## Issue 11 — Model name may be invalid

Default model:

```python
anthropic_model: str = "claude-fable-5"
```

Verify this model exists in the Anthropic account. If not, `python main.py once us` will fail.

Recommended: use a model name that is confirmed available in the account.

---

## Issue 12 — `AI_DECISION_MAX_TOKENS` is not loaded from env

`V2Config` has:

```python
ai_decision_max_tokens: int = 4000
```

But `load_config()` does not load it from `.env`.

### Recommended fix

Add:

```python
ai_decision_max_tokens=_i("AI_DECISION_MAX_TOKENS", "4000")
```

Also add to `.env.example`:

```env
AI_DECISION_MAX_TOKENS=4000
```

---

## Issue 13 — Telegram HTML should be escaped

`notify.py` sends messages with:

```python
parse_mode="HTML"
```

But AI-generated thesis/reason text is not escaped.

If Claude outputs `<`, `>`, or `&`, Telegram formatting can break.

### Recommended fix

Use Python's `html.escape()`:

```python
import html
safe_thesis = html.escape(thesis[:200])
```

Apply this to thesis, reason, market view, and any AI-generated text.

---

## Issue 14 — yfinance fetching is sequential and may be slow

The bot fetches many symbols one by one. This can be slow and may hit rate limits.

### Recommended improvements

- Add caching.
- Add retries.
- Add timeout handling.
- Use batch `yf.download()` where possible.
- Optionally parallelize fetching.

For paper testing this is acceptable, but for reliability it should be improved.

---

## Issue 15 — Config safety sanity checks should be stricter

Current config sanity checks cover:

- max position pct
- max risk per trade pct
- kill switch pct

Add checks for:

```python
assert 0 < caps.daily_loss_halt_pct <= 0.05
assert 1 <= caps.max_open_positions <= 10
assert 1 <= caps.max_new_entries_per_day <= 5
assert 0.005 <= caps.min_stop_distance_pct <= 0.05
assert 0.05 <= caps.max_stop_distance_pct <= 0.25
```

Also load `min_stop_distance_pct` and `max_stop_distance_pct` from env only if you really want them tunable. Otherwise keep them hardcoded.

---

## 6. Recommended Priority Fix Order

### Must fix before real-money use

1. Fix duplicate SELL/double-cash bug.
2. Use intraday/latest prices for stop and target monitoring.
3. Enforce 2R reward/risk in code.
4. Include unrealized P&L in daily loss halt.
5. Run kill-switch checks during monitor loop.
6. Add broker reconciliation if using Alpaca beyond paper.

### Should fix before serious paper-test period

7. Add stronger ADJUST validation.
8. Remove or implement partial exits.
9. Fix US daylight-saving-time scheduling.
10. Store full prompt/context with decisions.
11. Escape Telegram HTML.
12. Add unit tests.

---

## 7. Suggested Unit Tests

Add tests for these cases:

### Risk tests

- BUY accepted when all caps pass.
- BUY rejected when stop is above entry.
- BUY rejected when stop distance is too tight.
- BUY rejected when stop distance is too wide.
- BUY rejected when target is less than 2R.
- Position size respects max risk per trade.
- Position size respects max position percentage.
- BUY rejected when max open positions reached.
- BUY rejected when max daily entries reached.

### Ledger tests

- Cash decreases correctly after BUY.
- Cash increases correctly after SELL.
- Duplicate SELL cannot double-credit cash.
- Closed trade cannot be closed again.
- P&L calculation is correct.

### Executor tests

- BUY blocked during halt.
- SELL allowed during halt.
- ADJUST cannot lower stop.
- ADJUST rejects invalid stop/target.
- Symbol cannot be bought twice.

### Kill switch tests

- Equity peak is stored.
- Drawdown below kill threshold triggers halt.
- Kill switch flattens all positions.
- Killed bot rejects new buys until manual reset.

---

## 8. Example Patch Ideas

## Patch A — Enforce 2R in `risk.py`

```python
def validate_buy(self, symbol: str, price: float, stop: float, target: float | None,
                 equity: float, cash: float, open_positions: int) -> BuyVerdict:
    caps = self.caps

    if price <= 0 or stop <= 0:
        return BuyVerdict(False, reason="invalid price/stop")

    if stop >= price:
        return BuyVerdict(False, reason=f"{symbol}: stop {stop} must be below entry {price}")

    if target is None or target <= price:
        return BuyVerdict(False, reason=f"{symbol}: target must be above entry")

    risk = price - stop
    reward = target - price
    if reward < 2 * risk:
        return BuyVerdict(False, reason=f"{symbol}: reward/risk {reward / risk:.2f}R < 2R minimum")

    stop_dist = risk / price
    ...
```

Then in `executor.py`:

```python
target = float(act.get("target") or 0) or None
verdict = self.risk.validate_buy(
    sym, price, float(act.get("stop", 0)), target,
    self.ledger.equity(), self.ledger.cash, len(by_symbol),
)
```

---

## Patch B — Prevent duplicate SELL in `executor.py`

```python
if kind == "SELL" and sym in by_symbol:
    self._exit(by_symbol[sym], f"ai_exit: {act.get('reason', '')[:80]}")
    by_symbol.pop(sym, None)
```

---

## Patch C — Prevent closing already closed trade in `journal.py`

```python
def close_trade(self, trade_id: int, exit_price: float, reason: str) -> None:
    with self._conn() as c:
        row = c.execute(
            "SELECT qty, entry, status FROM trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        if not row or row["status"] != "open":
            return
        ...
```

---

## Patch D — Intraday price for monitoring in `data.py`

```python
def fetch_intraday_price(symbol: str) -> float | None:
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="1d", interval="5m")
        if df is None or df.empty:
            df = yf.Ticker(symbol).history(period="5d", interval="1d")
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as exc:
        log.warning("intraday price fetch failed for %s: %s", symbol, exc)
        return None
```

Then use this inside monitor logic.

---

## 9. Live Trading Readiness Checklist

Do not use real money until all of these are true:

- [ ] Duplicate SELL bug fixed.
- [ ] Stop/target monitor uses latest intraday prices.
- [ ] 2R minimum enforced in code.
- [ ] Daily halt includes unrealized P&L.
- [ ] Kill switch runs during monitor loop.
- [ ] ADJUST validation added.
- [ ] Telegram notifications are escaped and reliable.
- [ ] Alpaca order failure alerts added.
- [ ] Broker reconciliation implemented.
- [ ] Full prompt/context logging implemented.
- [ ] 4+ weeks paper trading completed.
- [ ] 20+ closed trades completed.
- [ ] Positive total P&L.
- [ ] Average win greater than average loss.
- [ ] Max drawdown below 7%.
- [ ] No single symbol repeatedly causing losses.

---

## 10. Final Recommendation

The strategy is good and the architecture is promising. The project is suitable for controlled paper testing, especially because the core risk engine already limits position size and risk per trade.

But it is **not ready for real-money execution** yet.

Fix the high-priority safety issues first:

1. Duplicate SELL/double-cash bug.
2. Intraday stop/target monitoring.
3. Code-enforced 2R reward/risk.
4. Daily halt with unrealized P&L.
5. Kill switch during monitoring.

After these fixes, run the bot on paper for several weeks and judge it by the documented go-live requirements.
