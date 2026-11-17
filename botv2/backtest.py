"""Portfolio backtest engine for daily-bar swing strategies.

Research tool only: nothing here is imported by the live bot.

A Strategy is a set of date x ticker tables (signals known at the CLOSE of a
day) plus exit rules. The simulator trades them the way the bot can actually
trade, with no look-ahead:

* a signal at the close of day t is acted on at the OPEN of day t+1;
* stops and targets are checked against each day's open/high/low. A gap
  through a level fills at the open (worse than the stop, better than the
  target). If a day touches both stop and target, the stop is assumed first;
* a fixed number of slots, each sized at equity / slots, cash only (no
  margin), optional whole shares, a cost charged on every buy and sell;
* sale proceeds settle the next day (cash-account rule), so money from a
  sale cannot fund a buy the same morning.

Known limits, stated in every report: daily bars only, and the universe is
whatever list you pass in. A list of today's large companies leaves out the
ones that failed (survivorship bias), which flatters every strategy and the
equal-weight benchmark alike. That is why strategies are judged against the
equal-weight benchmark of the SAME universe, not just against SPY.
"""

from __future__ import annotations

import hashlib
import math
import os
import pickle
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ── data ─────────────────────────────────────────────────────────
@dataclass
class Prices:
    """Wide tables (index = trading days, columns = tickers), adjusted prices."""
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame

    @classmethod
    def from_frames(cls, frames: dict[str, pd.DataFrame]) -> "Prices":
        def wide(col):
            return pd.DataFrame({t: f[col] for t, f in frames.items()}).sort_index()
        return cls(wide("Open"), wide("High"), wide("Low"), wide("Close"), wide("Volume"))

    def drop(self, tickers) -> "Prices":
        keep = [c for c in self.close.columns if c not in set(tickers)]
        return Prices(self.open[keep], self.high[keep], self.low[keep],
                      self.close[keep], self.volume[keep])


def load_prices(tickers: list[str], start: str, end: str | None = None,
                cache_dir: str = ".bt_cache") -> Prices:
    """Daily adjusted OHLCV from Yahoo, cached on disk per (tickers, dates)."""
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    tickers = sorted(set(tickers))
    key = hashlib.sha1(f"{','.join(tickers)}|{start}|{end}".encode()).hexdigest()[:16]
    path = os.path.join(cache_dir, f"px_{key}.pkl")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    import yfinance as yf

    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    frames = {}
    for t in tickers:
        try:
            f = raw[t].dropna(subset=["Close"])
        except KeyError:
            continue
        if len(f) > 300:
            f.index = pd.DatetimeIndex(f.index).tz_localize(None)
            frames[t] = f[["Open", "High", "Low", "Close", "Volume"]]
    px = Prices.from_frames(frames)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump(px, fh)
    return px


# ── indicators (wide in, wide out) ───────────────────────────────
def sma(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.rolling(n).mean()


def rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(100).where(close.notna())


# ── strategy + simulator ─────────────────────────────────────────
@dataclass
class Strategy:
    name: str
    entry: pd.DataFrame                 # bool: signal at close t -> buy at open t+1
    score: pd.DataFrame                 # ranks same-day candidates, higher first
    exit: pd.DataFrame | None = None    # bool: signal at close t -> sell at open t+1
    take_profit: float | None = None    # 0.10 = sell at +10% from the entry open
    stop_loss: float | None = None      # 0.05 = sell at -5% from the entry open
    trail: float | None = None          # 0.15 = stop follows 15% under the highest close
    max_hold: int | None = None         # sell at the close after this many trading days
    cooldown: int = 0                   # days to stay out of a ticker after a losing exit
    note: str = ""


@dataclass
class _Pos:
    shares: float
    cost: float
    entry_px: float
    entry_i: int
    stop: float | None
    target: float | None
    hi: float
    last: float


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    exposure: float                     # average fraction of equity invested
    capital: float = field(default=0.0)


TRADE_COLS = ["ticker", "entry_date", "exit_date", "entry_px", "exit_px",
              "ret", "pnl", "days", "reason", "open"]


def simulate(px: Prices, strat: Strategy, start: str | None = None, end: str | None = None,
             capital: float = 10_000.0, slots: int = 3, cost: float = 0.0005,
             whole_shares: bool = False, settle_days: int = 1) -> Result:
    idx, cols = px.close.index, px.close.columns
    O, H, L, C = px.open.values, px.high.values, px.low.values, px.close.values

    def table(df):
        return df.reindex(index=idx, columns=cols)

    def flags(df):
        return table(df).astype("boolean").fillna(False).to_numpy(dtype=bool)

    ent = flags(strat.entry)
    sc = table(strat.score).to_numpy(dtype=float)
    ex = None if strat.exit is None else flags(strat.exit)

    i0 = max(1, idx.searchsorted(pd.Timestamp(start))) if start else 1
    i1 = idx.searchsorted(pd.Timestamp(end), side="right") if end else len(idx)

    cash, pending = float(capital), []          # pending: (available_from_i, amount)
    pos: dict[int, _Pos] = {}
    cool: dict[int, int] = {}
    trades, eq, inv = [], [], []

    def close(i, j, price, reason, still_open=False):
        p = pos.pop(j)
        proceeds = p.shares * price * (1 - cost)
        ret = proceeds / p.cost - 1
        trades.append((cols[j], idx[p.entry_i], idx[i], p.entry_px, price, ret,
                       proceeds - p.cost, i - p.entry_i, reason, still_open))
        if ret <= 0 and strat.cooldown:
            cool[j] = i + strat.cooldown
        return proceeds

    for i in range(i0, i1):
        cash += sum(a for when, a in pending if when <= i)
        pending = [(when, a) for when, a in pending if when > i]

        def sell(j, price, reason):
            nonlocal cash
            proceeds = close(i, j, price, reason)
            if settle_days:
                pending.append((i + settle_days, proceeds))
            else:
                cash += proceeds

        # A. at the open: signal exits and gaps through a level
        for j in list(pos):
            p, o = pos[j], O[i, j]
            if np.isnan(o):
                continue
            if ex is not None and ex[i - 1, j]:
                sell(j, o, "signal")
            elif p.stop is not None and o <= p.stop:
                sell(j, o, "stop_gap")
            elif p.target is not None and o >= p.target:
                sell(j, o, "target_gap")

        # B. at the open: new entries from yesterday's signals, best score first
        free = slots - len(pos)
        if free > 0:
            cand = np.flatnonzero(ent[i - 1] & ~np.isnan(sc[i - 1]) & ~np.isnan(O[i]))
            if cand.size:
                cand = cand[np.argsort(-sc[i - 1, cand], kind="stable")]
                held = sum(p.shares * (O[i, j] if not np.isnan(O[i, j]) else p.last)
                           for j, p in pos.items())
                budget = (cash + sum(a for _, a in pending) + held) / slots
                for j in cand:
                    if free == 0:
                        break
                    if j in pos or cool.get(j, 0) > i:
                        continue
                    price = O[i, j] * (1 + cost)
                    shares = min(budget, cash) / price
                    if whole_shares:
                        shares = math.floor(shares)
                    if shares * price < 1.0:
                        continue
                    cash -= shares * price
                    o = O[i, j]
                    pos[j] = _Pos(shares, shares * price, o, i,
                                  o * (1 - strat.stop_loss) if strat.stop_loss else None,
                                  o * (1 + strat.take_profit) if strat.take_profit else None,
                                  o, o)
                    free -= 1

        # C. during the day: stop first, then target; D. at the close: time exit, trail
        for j in list(pos):
            p = pos[j]
            if np.isnan(C[i, j]):
                continue
            if p.stop is not None and L[i, j] <= p.stop:
                sell(j, p.stop, "stop")
            elif p.target is not None and H[i, j] >= p.target:
                sell(j, p.target, "target")
            elif strat.max_hold and i - p.entry_i >= strat.max_hold:
                sell(j, C[i, j], "time")
            else:
                p.last = C[i, j]
                if strat.trail:
                    p.hi = max(p.hi, C[i, j])
                    p.stop = max(p.stop or 0.0, p.hi * (1 - strat.trail))

        mv = sum(p.shares * p.last for p in pos.values())
        total = cash + sum(a for _, a in pending) + mv
        eq.append(total)
        inv.append(mv / total if total else 0.0)

    for j in list(pos):                          # still held at the end: mark, don't count as closed
        close(i1 - 1, j, pos[j].last, "open", still_open=True)

    return Result(pd.Series(eq, index=idx[i0:i1], dtype=float),
                  pd.DataFrame(trades, columns=TRADE_COLS),
                  float(np.mean(inv)) if inv else 0.0, capital)


# ── benchmarks ───────────────────────────────────────────────────
def buy_hold(close: pd.Series, start: str | None, end: str | None, capital: float) -> pd.Series:
    s = close.loc[start:end].dropna()
    return capital * s / s.iloc[0]


def equal_weight(px: Prices, start: str | None, end: str | None, capital: float) -> pd.Series:
    """Every ticker in the universe held in equal weight, rebalanced daily, no
    costs. The fair yardstick: it carries the same survivorship bias."""
    r = px.close.pct_change(fill_method=None).loc[start:end].mean(axis=1).fillna(0.0)
    r.iloc[0] = 0.0
    return capital * (1 + r).cumprod()


# ── metrics ──────────────────────────────────────────────────────
def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0


def metrics(equity: pd.Series, trades: pd.DataFrame | None = None,
            capital: float | None = None) -> dict:
    capital = capital or float(equity.iloc[0])
    years = max(len(equity) / 252, 1e-9)
    daily = equity.pct_change().dropna()
    out = {
        "final": float(equity.iloc[-1]),
        "total_return": float(equity.iloc[-1] / capital - 1),
        "cagr": float((equity.iloc[-1] / capital) ** (1 / years) - 1) if equity.iloc[-1] > 0 else -1.0,
        "max_dd": max_drawdown(equity),
        "sharpe": float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0.0,
    }
    if trades is None:
        return out
    t = trades[~trades["open"]].sort_values("exit_date")
    wins, losses = t[t.ret > 0], t[t.ret <= 0]
    streak = worst = 0
    for r in t.ret:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    out.update({
        "trades": int(len(t)),
        "win_rate": float(len(wins) / len(t)) if len(t) else 0.0,
        "avg_win": float(wins.ret.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.ret.mean()) if len(losses) else 0.0,
        "expectancy": float(t.ret.mean()) if len(t) else 0.0,
        "profit_factor": float(wins.pnl.sum() / -losses.pnl.sum()) if len(losses) and losses.pnl.sum() < 0 else float("inf"),
        "avg_days": float(t.days.mean()) if len(t) else 0.0,
        "losing_streak": int(worst),
    })
    return out
