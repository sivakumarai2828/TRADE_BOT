"""Swing-strategy backtester — validate the stock pullback strategy on history.

Replays the same RSI + EMA pullback logic the live picks use, with ATR-based
stops/targets, fees and slippage, across a basket of symbols. Reports edge
(win rate, W/L ratio, expectancy, profit factor, max drawdown) and can sweep
stop/target parameters so you can SEE which risk/reward actually makes money
before risking real capital.

Usage:
    python3 backtest.py                      # default basket + parameter sweep
    python3 backtest.py AAPL NVDA V          # specific symbols
    python3 backtest.py --years 3 --sweep    # 3y history, sweep stop/target grid

Data: yfinance (free, no key). Indicators reused from daybot.indicators
(same code the live bot runs — RSI no-loss bug fixed).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd
import yfinance as yf

from daybot.indicators import calculate_ema, calculate_rsi, calculate_atr


# ── Strategy config ──────────────────────────────────────────────────────────
@dataclass
class Strategy:
    rsi_lo: float = 35.0          # pullback zone lower bound
    rsi_hi: float = 55.0          # pullback zone upper bound (no chasing > this)
    ema_trend: int = 50           # must be above this EMA = uptrend filter
    ema_near: int = 20            # entry near this EMA
    near_pct: float = 0.03        # price within ±3% of EMA20
    stop_atr: float = 1.5         # stop = entry − stop_atr × ATR
    tgt_atr: float = 2.5          # target = entry + tgt_atr × ATR
    trail_atr: float = 0.0        # >0 enables trailing stop at trail_atr × ATR
    max_hold: int = 10            # time exit after N bars
    fee_pct: float = 0.0          # commission (RH stocks = 0)
    slippage_pct: float = 0.05    # entry+exit slippage each side (%)


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    bars_held: int
    reason: str
    pnl_pct: float


@dataclass
class Result:
    trades: list = field(default_factory=list)

    def add(self, t: Trade):
        self.trades.append(t)


# ── Core simulation ──────────────────────────────────────────────────────────
def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # yfinance multiindex / column normalize
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df["rsi"] = calculate_rsi(df["close"])
    df["ema_t"] = calculate_ema(df["close"], 50)
    df["ema_n"] = calculate_ema(df["close"], 20)
    df["atr"] = calculate_atr(df)   # indicators.calculate_atr uses lowercase high/low/close
    return df.dropna()


def backtest_symbol(symbol: str, df: pd.DataFrame, s: Strategy) -> list[Trade]:
    df = _prep(df)
    trades: list[Trade] = []
    in_pos = False
    entry = stop = target = trail = 0.0
    entry_i = 0
    rows = df.reset_index()
    date_col = rows.columns[0]

    for i in range(1, len(rows)):
        r = rows.iloc[i]
        price = float(r["close"])
        atr = float(r["atr"])

        if not in_pos:
            # Entry: pullback in uptrend, near EMA20
            rsi = float(r["rsi"])
            above_trend = price > float(r["ema_t"])
            near_ema = abs(price - float(r["ema_n"])) / float(r["ema_n"]) <= s.near_pct
            if s.rsi_lo <= rsi <= s.rsi_hi and above_trend and near_ema and atr > 0:
                fill = price * (1 + s.slippage_pct / 100)
                entry = fill
                stop = entry - s.stop_atr * atr
                target = entry + s.tgt_atr * atr
                trail = entry - s.trail_atr * atr if s.trail_atr > 0 else 0.0
                entry_i = i
                in_pos = True
            continue

        # In position — check exits (intrabar: stop priority, then target)
        lo = float(r["low"]); hi = float(r["high"])
        reason = exit_px = None

        if s.trail_atr > 0:
            trail = max(trail, price - s.trail_atr * atr)

        if lo <= stop or (s.trail_atr > 0 and lo <= trail):
            reason = "stop"; exit_px = max(stop, trail if s.trail_atr > 0 else stop)
        elif hi >= target:
            reason = "target"; exit_px = target
        elif i - entry_i >= s.max_hold:
            reason = "time"; exit_px = price

        if reason:
            ex = exit_px * (1 - s.slippage_pct / 100)
            gross = (ex - entry) / entry * 100
            net = gross - 2 * s.fee_pct
            trades.append(Trade(
                symbol=symbol,
                entry_date=str(rows.iloc[entry_i][date_col])[:10],
                exit_date=str(r[date_col])[:10],
                entry=round(entry, 2), exit=round(ex, 2),
                bars_held=i - entry_i, reason=reason, pnl_pct=round(net, 2),
            ))
            in_pos = False
    return trades


# ── Metrics ──────────────────────────────────────────────────────────────────
def metrics(trades: list[Trade], start_equity: float = 1000.0) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    wr = len(wins) / len(trades) * 100
    avg_w = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
    avg_l = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
    wl = (avg_w / abs(avg_l)) if avg_l else float("inf")
    gross_w = sum(t.pnl_pct for t in wins)
    gross_l = abs(sum(t.pnl_pct for t in losses))
    pf = gross_w / gross_l if gross_l else float("inf")
    expectancy = sum(t.pnl_pct for t in trades) / len(trades)

    # Equity curve (compounded, equal % per trade in chronological order)
    eq = start_equity
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_date):
        eq *= (1 + t.pnl_pct / 100)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
    total_ret = (eq / start_equity - 1) * 100

    by_reason = {}
    for t in trades:
        b = by_reason.setdefault(t.reason, [0, 0.0])
        b[0] += 1; b[1] += t.pnl_pct

    return {
        "trades": len(trades), "win_rate": round(wr, 1),
        "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "wl_ratio": round(wl, 2), "profit_factor": round(pf, 2),
        "expectancy": round(expectancy, 3), "total_return": round(total_ret, 1),
        "max_drawdown": round(max_dd, 1), "by_reason": by_reason,
    }


def fetch(symbol: str, years: int) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=f"{years}y", interval="1d",
                         progress=False, auto_adjust=True)
        return df if df is not None and len(df) > 60 else None
    except Exception as e:
        print(f"  ! {symbol} fetch failed: {e}", file=sys.stderr)
        return None


def run(symbols: list[str], s: Strategy, years: int) -> dict:
    all_trades = []
    data = {}
    for sym in symbols:
        df = data.get(sym) or fetch(sym, years)
        if df is None:
            continue
        data[sym] = df
        all_trades += backtest_symbol(sym, df, s)
    return metrics(all_trades), all_trades, data


# ── CLI ──────────────────────────────────────────────────────────────────────
DEFAULT_BASKET = ["AAPL", "NVDA", "V", "MSFT", "AMZN", "AMD", "GOOGL", "META", "JPM", "SPY"]


def _print(label: str, m: dict):
    if not m.get("trades"):
        print(f"{label}: no trades"); return
    print(f"{label}: {m['trades']} trades | WR {m['win_rate']}% | "
          f"avgW {m['avg_win']}% avgL {m['avg_loss']}% | W/L {m['wl_ratio']} | "
          f"PF {m['profit_factor']} | exp {m['expectancy']}%/trade | "
          f"total {m['total_return']}% | maxDD {m['max_drawdown']}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=[])
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--sweep", action="store_true", help="sweep stop/target grid")
    ap.add_argument("--stop", type=float, default=1.5)
    ap.add_argument("--target", type=float, default=2.5)
    ap.add_argument("--trail", type=float, default=0.0)
    args = ap.parse_args()

    symbols = args.symbols or DEFAULT_BASKET
    print(f"Backtest — {len(symbols)} symbols, {args.years}y daily bars: {', '.join(symbols)}\n")

    if args.sweep:
        cache: dict = {}
        for sym in symbols:                       # fetch once, reuse across grid
            df = fetch(sym, args.years)
            if df is not None:
                cache[sym] = df
        tgts = (2.0, 2.5, 3.0, 4.0)
        print("Parameter sweep — total % return over period (rows=stop×ATR, cols=target×ATR):\n")
        print("  stop\\tgt " + "".join(f"{t:>8.1f}" for t in tgts))
        for stop in (1.0, 1.5, 2.0, 2.5):
            cells = []
            for tgt in tgts:
                s = Strategy(stop_atr=stop, tgt_atr=tgt, trail_atr=args.trail)
                m, _ = _run_cached(symbols, s, args.years, cache)
                cells.append(f"{m.get('total_return', 0):>8.0f}")
            print(f"  {stop:>6.1f} " + "".join(cells))
        print("\n(higher = better R:R. Negative everywhere = no edge.)\n")
        s = Strategy(stop_atr=args.stop, tgt_atr=args.target, trail_atr=args.trail)
        m, trades = _run_cached(symbols, s, args.years, cache)
        _print(f"Default (stop {args.stop}×ATR, tgt {args.target}×ATR)", m)
        if m.get("by_reason"):
            for r, (n, p) in sorted(m["by_reason"].items(), key=lambda x: x[1][1]):
                print(f"    {r:8} n={n:<3} sum_pnl={p:+.1f}%")
    else:
        s = Strategy(stop_atr=args.stop, tgt_atr=args.target, trail_atr=args.trail)
        m, trades, _ = run(symbols, s, args.years)
        _print(f"Strategy (stop {args.stop}×ATR, tgt {args.target}×ATR, trail {args.trail})", m)
        if m.get("by_reason"):
            print("  exits:")
            for r, (n, p) in sorted(m["by_reason"].items(), key=lambda x: x[1][1]):
                print(f"    {r:8} n={n:<3} sum_pnl={p:+.1f}%")
        # per-symbol
        print("  per symbol:")
        bysym = {}
        for t in trades:
            bysym.setdefault(t.symbol, []).append(t)
        for sym, ts in sorted(bysym.items(), key=lambda x: sum(t.pnl_pct for t in x[1])):
            tot = sum(t.pnl_pct for t in ts)
            w = sum(1 for t in ts if t.pnl_pct > 0)
            print(f"    {sym:6} {len(ts)} trades, WR {w/len(ts)*100:.0f}%, sum {tot:+.1f}%")


def _run_cached(symbols, s, years, cache):
    all_trades = []
    for sym in symbols:
        df = cache.get(sym)
        if df is None:
            df = fetch(sym, years)
            if df is None:
                continue
            cache[sym] = df
        all_trades += backtest_symbol(sym, df, s)
    return metrics(all_trades), all_trades


if __name__ == "__main__":
    main()
