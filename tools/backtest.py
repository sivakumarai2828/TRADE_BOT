"""Reusable strategy backtest — STOP vs NO-STOP on real historical prices.

Tests the core risk-management question honestly: does riding stocks down with
no stop ('it will come back') beat cutting losers? Shows per-stock outcomes
(survivorship bias), final return, and — the number that matters — max drawdown.

Run on the VM (has yfinance): /home/konda/venv311/bin/python3 tools/backtest.py

Examples:
  # default basket, both a bear and a bull period
  python3 tools/backtest.py

  # your own stocks and dates
  python3 tools/backtest.py --tickers NVDA,PLTR,SMCI --start 2024-01-01 --end 2026-01-01

  # try a different stop level (fraction, negative)
  python3 tools/backtest.py --stop -0.15
"""
from __future__ import annotations

import argparse

import yfinance as yf

DEFAULT_TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "META",
                   "GOOGL", "TSLA", "AMD", "NFLX", "PYPL"]
COST = 0.001  # 0.1% round-trip fee + slippage


def sma(vals, n, i):
    return None if i + 1 < n else sum(vals[i + 1 - n:i + 1]) / n


def no_stop_curve(data, slot):
    days = min(len(v) for v in data.values())
    shares = {n: slot / data[n][0] for n in data}
    return [sum(shares[n] * data[n][i] for n in data) for i in range(days)]


def stop_curve(data, slot, stop):
    """Cut any name at `stop`; re-enter only when it closes back above 50-SMA."""
    days = min(len(v) for v in data.values())
    state = {n: ("hold", slot / data[n][0], data[n][0]) for n in data}
    curve = []
    for i in range(days):
        total = 0.0
        for n in data:
            px = data[n][i]
            s = state[n]
            if s[0] == "hold":
                _, sh, entry = s
                if px / entry - 1 <= stop:
                    state[n] = ("cash", sh * px * (1 - COST))
                    total += state[n][1]
                else:
                    total += sh * px
            else:
                cash = s[1]
                ma = sma(data[n], 50, i)
                if ma is not None and px > ma:
                    sh = cash * (1 - COST) / px
                    state[n] = ("hold", sh, px)
                    total += sh * px
                else:
                    total += cash
        curve.append(total)
    return curve


def maxdd(curve):
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd * 100


def run(tickers, start, end, capital, stop, label=""):
    slot = capital / len(tickers)
    data = {}
    for n in tickers:
        df = yf.Ticker(n).history(start=start, end=end, interval="1d", auto_adjust=True)
        if df is not None and len(df) > 60:
            data[n] = list(df["Close"])
    if not data:
        print(f"no data for {start}..{end}")
        return
    print(f"\n{'='*60}\n{label or f'{start} to {end}'}\n{'='*60}")
    print("Per-stock BUY&HOLD (did it 'come back'?):")
    for n in data:
        r = (data[n][-1] / data[n][0] - 1) * 100
        dip = (min(data[n]) / data[n][0] - 1) * 100
        print(f"   {n:6s} end {r:+7.1f}%   (worst dip {dip:+7.1f}%)")
    ns, st = no_stop_curve(data, slot), stop_curve(data, slot, stop)
    print(f"\nPORTFOLIO (${capital:,.0f} start):")
    print(f"   NO-STOP  (ride it down)    final ${ns[-1]:8,.0f}  {(ns[-1]/capital-1)*100:+6.1f}%   max drawdown {maxdd(ns):6.1f}%")
    print(f"   STOP{int(stop*-100):<3d} (cut & re-enter)   final ${st[-1]:8,.0f}  {(st[-1]/capital-1)*100:+6.1f}%   max drawdown {maxdd(st):6.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--stop", type=float, default=-0.08)
    args = ap.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    if args.start and args.end:
        run(tickers, args.start, args.end, args.capital, args.stop)
    else:  # default demo: one bear, one bull
        run(tickers, "2021-06-01", "2023-06-01", args.capital, args.stop,
            "BEAR->RECOVERY 2021-06 to 2023-06")
        run(tickers, "2023-06-01", "2025-06-01", args.capital, args.stop,
            "BULL 2023-06 to 2025-06")


if __name__ == "__main__":
    main()
