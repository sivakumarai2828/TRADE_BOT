"""Options research: replay stock-strategy entries as long calls (Phase 1).

Research only; nothing here is imported by the live bot.

Real option price history needs a paid source or Alpaca keys, so this module
MODELS option prices with Black-Scholes:

* volatility = the stock's realized volatility over the last 30 trading days,
  times `iv_premium` (implied volatility usually trades above realized), with
  a floor, recomputed each day the position is held;
* r = `rate`, no dividends (calls on dividend payers are slightly overpriced
  here, which flatters nothing);
* every buy pays and every sell gives up `half_spread` of the option price,
  at least `min_tick` per share (wide spreads on cheap contracts are the
  biggest real-world cost of small option trades);
* positions are valued and exits checked at each day's close.

Contract: a call struck at the money (nearest strike step), on the first
monthly expiry (third Friday) between `dte_min` and `dte_max` calendar days.

Exits, first to happen: option value +`take_profit` or -`stop_loss` from
the price paid, the stock strategy's own exit, or `exit_dte` days to expiry.

The stock strategy's trades come from botv2.backtest.simulate, so entries
are exactly the ones the share version took. A call that exits early frees
its slot but no new trade replaces it before the share version's exit; that
leaves options slightly under-invested, which is stated in every report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


# ── pricing ──────────────────────────────────────────────────────
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes price of a European call (per share). T in years."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def strike_step(S: float) -> float:
    """Listed strike spacing near the money for US single stocks."""
    return 1.0 if S < 50 else 2.5 if S < 100 else 5.0 if S < 300 else 10.0


def atm_strike(S: float) -> float:
    step = strike_step(S)
    return round(S / step) * step


def third_friday(year: int, month: int) -> pd.Timestamp:
    first = pd.Timestamp(year=year, month=month, day=1)
    return first + pd.Timedelta(days=(4 - first.weekday()) % 7 + 14)


def pick_expiry(day: pd.Timestamp, dte_min: int = 30, dte_max: int = 60) -> pd.Timestamp | None:
    """First monthly expiry (third Friday) between dte_min and dte_max days out."""
    y, m = day.year, day.month
    for _ in range(4):
        exp = third_friday(y, m)
        if dte_min <= (exp - day).days <= dte_max:
            return exp
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return None


# ── settings + result ────────────────────────────────────────────
@dataclass
class OptionRules:
    premium_pct: float = 0.05     # premium paid per trade, share of equity at entry
    max_open: int = 3             # same slot count as the share version
    take_profit: float = 1.00     # +100% on the option
    stop_loss: float = 0.50       # -50% on the option
    exit_dte: int = 21            # sell with this many days left
    dte_min: int = 30
    dte_max: int = 60
    iv_premium: float = 1.15      # implied vol = realized vol x this
    vol_floor: float = 0.15
    rate: float = 0.04
    half_spread: float = 0.03     # cost per side as a share of the option price
    min_tick: float = 0.05        # ...but at least this per share
    premium_usd: float | None = None   # fixed dollars per trade instead of premium_pct
    otm: float = 0.0              # strike this far above the price (0.10 = 10% out of the money)
    fractional: bool = False      # research only: allow part-contracts so any budget can be tested


@dataclass
class OptionResult:
    equity: pd.Series
    trades: pd.DataFrame


def _cost(price: float, rules: OptionRules) -> float:
    return max(price * rules.half_spread, rules.min_tick)


def simulate_calls(px, stock_trades: pd.DataFrame, start: str, end: str,
                   capital: float, rules: OptionRules = OptionRules()) -> OptionResult:
    """Replay each closed stock trade as a call bought at the same open."""
    close = px.close
    vol = close.pct_change(fill_method=None).rolling(30).std() * math.sqrt(252)
    days = close.loc[start:end].index
    t = stock_trades[~stock_trades["open"]].sort_values("entry_date").reset_index(drop=True)
    by_day: dict = {}
    for row in t.itertuples():
        by_day.setdefault(row.entry_date, []).append(row)

    cash, held, out, curve = float(capital), [], [], []

    def sigma_at(sym, day):
        v = vol.at[day, sym] if sym in vol.columns else float("nan")
        return max(rules.vol_floor, (v if v == v else 0.3) * rules.iv_premium)

    def value(pos, day):
        S = close.at[day, pos["sym"]]
        if S != S:
            return pos["last"]
        T = max(0.0, (pos["exp"] - day).days / 365.0)
        return bs_call(S, pos["K"], T, rules.rate, sigma_at(pos["sym"], day))

    for day in days:
        # 1. exits at today's close
        for pos in list(held):
            v = value(pos, day)
            pos["last"] = v
            dte = (pos["exp"] - day).days
            reason = None
            if v >= pos["paid"] * (1 + rules.take_profit):
                reason = "target"
            elif v <= pos["paid"] * (1 - rules.stop_loss):
                reason = "stop"
            elif day >= pos["stock_exit"]:
                reason = "stock_exit:" + pos["stock_reason"]
            elif dte <= rules.exit_dte:
                reason = "time"
            if reason:
                sell = max(0.0, v - _cost(v, rules))
                proceeds = sell * 100 * pos["n"]
                cash += proceeds
                held.remove(pos)
                out.append((pos["sym"], pos["entry"], day, pos["K"], pos["exp"], pos["paid"], sell,
                            proceeds / pos["cost"] - 1, proceeds - pos["cost"], reason, pos["stock_ret"]))
        # 2. entries at the open, priced off the open
        for row in by_day.get(day, []):
            if len(held) >= rules.max_open:
                continue
            S = px.open.at[day, row.ticker]
            exp = pick_expiry(day, rules.dte_min, rules.dte_max)
            if S != S or exp is None:
                continue
            K = atm_strike(S * (1 + rules.otm))
            mid = bs_call(S, K, (exp - day).days / 365.0, rules.rate, sigma_at(row.ticker, day))
            ask = mid + _cost(mid, rules)
            equity = cash + sum(p["last"] * 100 * p["n"] for p in held)
            budget = min(cash, rules.premium_usd if rules.premium_usd else equity * rules.premium_pct)
            n = budget / (ask * 100) if rules.fractional else int(budget // (ask * 100))   # whole contracts
            if n <= 0 or (not rules.fractional and n < 1):
                out.append((row.ticker, day, day, K, exp, ask, ask, 0.0, 0.0, "unaffordable", row.ret))
                continue
            cost = ask * 100 * n
            cash -= cost
            held.append({"sym": row.ticker, "entry": day, "K": K, "exp": exp, "paid": ask, "n": n,
                         "cost": cost, "last": mid, "stock_exit": row.exit_date,
                         "stock_reason": row.reason, "stock_ret": row.ret})
        curve.append(cash + sum(p["last"] * 100 * p["n"] for p in held))

    for pos in held:   # still open at the end: marked, not counted as closed
        out.append((pos["sym"], pos["entry"], days[-1], pos["K"], pos["exp"], pos["paid"], pos["last"],
                    pos["last"] * 100 * pos["n"] / pos["cost"] - 1, pos["last"] * 100 * pos["n"] - pos["cost"],
                    "open", pos["stock_ret"]))
    cols = ["ticker", "entry_date", "exit_date", "strike", "expiry", "paid", "exit_px",
            "ret", "pnl", "reason", "stock_ret"]
    return OptionResult(pd.Series(curve, index=days, dtype=float), pd.DataFrame(out, columns=cols))
