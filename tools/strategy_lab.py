"""Strategy lab — test trading rules on history before the bot trades them.

Runs each strategy through botv2/backtest.py on the bot's US universe, on two
separate windows: TRAIN (where rules may be tuned) and TEST (never tuned on).
A rule is only worth trading if it beats the benchmarks on TEST.

Benchmarks:
  SPY hold        the index, bought once and held
  Universe hold   every stock in the universe, equal weight. This is the fair
                  one: the universe is today's big companies, so it shares the
                  strategies' survivorship bias.
  No-skill picks  same slots, stocks picked at random (median of several seeds).

Examples:
  python3 tools/strategy_lab.py                      # all strategies, 3 slots
  python3 tools/strategy_lab.py --slots 5
  python3 tools/strategy_lab.py --only rotation_monthly,dip_rsi2
  python3 tools/strategy_lab.py --capital 437 --whole-shares   # the small live account
  python3 tools/strategy_lab.py --sweep                # target x stop grid
  python3 tools/strategy_lab.py --trades rotation_monthly      # list its TEST trades
  python3 tools/strategy_lab.py --only rotation_monthly --offset 7 --exclude NVDA,PLTR   # robustness
  python3 tools/strategy_lab.py --list

Add a strategy: write a function below that returns a Strategy and decorate
it with @strategy. Signals may only use data up to that day's close.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from botv2.backtest import (Prices, Strategy, buy_hold, equal_weight, load_prices,  # noqa: E402
                            metrics, rsi, simulate, sma)
from botv2.universe import US_UNIVERSE  # noqa: E402

DATA_START = "2017-06-01"            # one year of warm-up for 252-day indicators
TRAIN = ("2018-07-02", "2022-12-31")
TEST_START = "2023-01-01"
BENCH = "SPY"


class Ind:
    """Indicators shared by all strategies (wide tables, date x ticker)."""

    def __init__(self, px: Prices, spy: pd.Series):
        c = self.c = px.close
        self.s5, self.s50, self.s200 = sma(c, 5), sma(c, 50), sma(c, 200)
        self.hi252 = px.high.rolling(252).max()
        self.newhi = c > px.high.shift(1).rolling(252).max()
        self.near = c >= 0.97 * self.hi252
        self.stage2 = (c > self.s50) & (self.s50 > self.s200) & (self.s200 > self.s200.shift(20))
        self.volx = px.volume / px.volume.rolling(50).mean()
        self.mom6 = c.shift(21) / c.shift(147) - 1          # 6 months, skipping the last month
        self.mom12 = c.shift(21) / c.shift(252) - 1
        self.vol60 = c.pct_change(fill_method=None).rolling(60).std()
        self.rsi2, self.rsi14 = rsi(c, 2), rsi(c, 14)
        up = (spy > spy.rolling(200).mean()).reindex(c.index).fillna(False)
        self.market_up = pd.DataFrame({t: up for t in c.columns})
        self.day_no = pd.Series(np.arange(len(c)), index=c.index)
        self.offset = 0                  # shifts rebalance days (--offset), to test schedule luck

    def every(self, n: int) -> pd.DataFrame:
        """True on every n-th trading day (rebalance days), for all tickers."""
        on = (self.day_no + self.offset) % n == 0
        return pd.DataFrame({t: on for t in self.c.columns})

    def rank(self, score: pd.DataFrame) -> pd.DataFrame:
        return score.rank(axis=1, ascending=False)


STRATEGIES: dict = {}


def strategy(fn):
    STRATEGIES[fn.__name__] = fn
    return fn


# ── fixed target / stop (the "+10% / -5%" family) ───────────────
@strategy
def fixed_10_5_strongest(i: Ind, slots: int) -> Strategy:
    """Strong uptrend near its high, strongest 6-month momentum first. +10% / -5%."""
    return Strategy("fixed_10_5_strongest", i.stage2 & i.near & i.market_up, i.mom6,
                    take_profit=0.10, stop_loss=0.05, max_hold=60)


@strategy
def fixed_10_5_breakout(i: Ind, slots: int) -> Strategy:
    """New 52-week high on 1.5x volume. +10% / -5%."""
    return Strategy("fixed_10_5_breakout", i.newhi & i.stage2 & (i.volx > 1.5) & i.market_up,
                    i.volx, take_profit=0.10, stop_loss=0.05, max_hold=60)


@strategy
def fixed_20_7_strongest(i: Ind, slots: int) -> Strategy:
    """Same entries as fixed_10_5_strongest, wider: +20% / -7%."""
    return Strategy("fixed_20_7_strongest", i.stage2 & i.near & i.market_up, i.mom6,
                    take_profit=0.20, stop_loss=0.07, max_hold=120)


# ── let winners run ──────────────────────────────────────────────
@strategy
def trail_strongest(i: Ind, slots: int) -> Strategy:
    """Strongest uptrends, no profit target: -8% stop that trails 15% under the high."""
    return Strategy("trail_strongest", i.stage2 & i.near & i.market_up, i.mom6,
                    stop_loss=0.08, trail=0.15, cooldown=10)


@strategy
def trail_breakout(i: Ind, slots: int) -> Strategy:
    """Volume breakouts, no target: -7% stop trailing 12% under the high."""
    return Strategy("trail_breakout", i.newhi & i.stage2 & (i.volx > 1.5) & i.market_up,
                    i.volx, stop_loss=0.07, trail=0.12, cooldown=10)


@strategy
def trend_hold_50dma(i: Ind, slots: int) -> Strategy:
    """Strongest uptrends, held until the close drops under the 50-day average."""
    return Strategy("trend_hold_50dma", i.stage2 & i.near & i.market_up, i.mom6,
                    exit=i.c < i.s50, stop_loss=0.10, cooldown=10)


# ── momentum rotation (hold the strongest, swap on a schedule) ──
def _rotation(name, i: Ind, slots, score, every, risk_off=False):
    rk, rebal = i.rank(score), i.every(every)
    entry = rebal & (rk <= slots) & (i.c > i.s200) & i.market_up
    out = rebal & ((rk > 3 * slots) | (i.c < i.s200))
    if risk_off:
        out = out | ~i.market_up
    return Strategy(name, entry, score, exit=out)


@strategy
def rotation_monthly(i: Ind, slots: int) -> Strategy:
    """Each month hold the top 6-month-momentum stocks; drop one when it leaves the top group."""
    return _rotation("rotation_monthly", i, slots, i.mom6, 21)


@strategy
def rotation_monthly_12m(i: Ind, slots: int) -> Strategy:
    """Monthly rotation ranked on 12-month momentum."""
    return _rotation("rotation_monthly_12m", i, slots, i.mom12, 21)


@strategy
def rotation_monthly_riskoff(i: Ind, slots: int) -> Strategy:
    """Monthly rotation that goes to cash whenever SPY is under its 200-day average."""
    return _rotation("rotation_monthly_riskoff", i, slots, i.mom6, 21, risk_off=True)


@strategy
def rotation_weekly(i: Ind, slots: int) -> Strategy:
    """Same as rotation_monthly but re-ranked every week."""
    return _rotation("rotation_weekly", i, slots, i.mom6, 5)


@strategy
def rotation_smooth(i: Ind, slots: int) -> Strategy:
    """Monthly rotation ranked on momentum per unit of volatility (steady climbers)."""
    return _rotation("rotation_smooth", i, slots, i.mom6 / i.vol60, 21, risk_off=True)


# ── buy the dip (many small wins, few big losses) ───────────────
@strategy
def dip_rsi2(i: Ind, slots: int) -> Strategy:
    """Uptrend stock sharply oversold (2-day RSI < 10); sell on the first close above the 5-day average."""
    return Strategy("dip_rsi2", (i.c > i.s200) & (i.rsi2 < 10) & i.market_up, -i.rsi2,
                    exit=i.c > i.s5, stop_loss=0.10, max_hold=10)


@strategy
def dip_rsi14(i: Ind, slots: int) -> Strategy:
    """Uptrend stock with 14-day RSI < 35. +10% / -5%."""
    return Strategy("dip_rsi14", (i.c > i.s200) & (i.rsi14 < 35) & i.market_up, -i.rsi14,
                    take_profit=0.10, stop_loss=0.05, max_hold=60)


def no_skill(i: Ind, seed: int, tp=0.10, sl=0.05) -> Strategy:
    rnd = pd.DataFrame(np.random.default_rng(seed).random(i.c.shape), index=i.c.index, columns=i.c.columns)
    return Strategy("no_skill", i.market_up & i.c.notna(), rnd, take_profit=tp, stop_loss=sl, max_hold=60)


# ── reporting ────────────────────────────────────────────────────
def pct(x, signed=True):
    return f"{100 * x:+.1f}%" if signed else f"{100 * x:.1f}%"


def run_window(px, strat, window, a):
    r = simulate(px, strat, window[0], window[1], a.capital, a.slots, a.cost, a.whole_shares)
    return metrics(r.equity, r.trades, a.capital) | {"exposure": r.exposure}, r


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest strategies on the bot's US universe.")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--slots", type=int, default=3)
    ap.add_argument("--cost", type=float, default=0.0005, help="cost per side, fraction of price")
    ap.add_argument("--whole-shares", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated strategy names")
    ap.add_argument("--end", default=None, help="last day (default: today)")
    ap.add_argument("--seeds", type=int, default=15, help="random runs for the no-skill baseline")
    ap.add_argument("--sweep", action="store_true", help="target x stop grid on the strongest-uptrend entries")
    ap.add_argument("--trades", default="", help="print this strategy's TEST-window trades")
    ap.add_argument("--exclude", default="", help="comma-separated tickers to drop from the universe")
    ap.add_argument("--offset", type=int, default=0, help="shift rebalance days by this many days")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        for n, fn in STRATEGIES.items():
            print(f"{n:26s} {fn.__doc__.strip()}")
        return

    px_all = load_prices(US_UNIVERSE + [BENCH], DATA_START, a.end)
    drop = [t.strip().upper() for t in a.exclude.split(",") if t.strip()]
    spy, px = px_all.close[BENCH], px_all.drop([BENCH] + drop)
    ind = Ind(px, spy)
    ind.offset = a.offset
    test = (TEST_START, str(px.close.index[-1].date()))
    windows = {"TRAIN": TRAIN, "TEST": test}
    print(f"Universe: {px.close.shape[1]} US stocks | ${a.capital:,.0f}, {a.slots} slots, "
          f"{'whole' if a.whole_shares else 'fractional'} shares, cost {a.cost:.2%}/side")
    print(f"TRAIN {TRAIN[0]} to {TRAIN[1]}  |  TEST {test[0]} to {test[1]} (never tuned on)\n")

    if a.trades:
        _, r = run_window(px, STRATEGIES[a.trades](ind, a.slots), test, a)
        print(r.trades.assign(ret=(100 * r.trades.ret).round(1), pnl=r.trades.pnl.round(2))
              .to_string(index=False))
        return

    bench = {}
    for w, (s, e) in windows.items():
        bench[w] = {"SPY hold": metrics(buy_hold(spy, s, e, a.capital)),
                    "Universe hold": metrics(equal_weight(px, s, e, a.capital))}

    if a.sweep:
        entry = ind.stage2 & ind.near & ind.market_up
        print("TEST window, strongest-uptrend entries. Each cell: yearly return / win rate")
        stops = (0.03, 0.05, 0.08, 0.10, 0.15)
        print(f"{'target':>8s} | " + "  ".join(f"stop -{int(100 * s):>2d}%    " for s in stops))
        for tp in (0.05, 0.10, 0.15, 0.20, 0.30, None):
            cells = []
            for sl in stops:
                m, _ = run_window(px, Strategy("s", entry, ind.mom6, take_profit=tp, stop_loss=sl,
                                               max_hold=120), test, a)
                cells.append(f"{pct(m['cagr']):>7s} / {100 * m['win_rate']:2.0f}%")
            print(f"{('+%d%%' % int(100 * tp)) if tp else 'none':>8s} | " + "  ".join(cells))
        b = bench["TEST"]
        print(f"\nBenchmarks: SPY hold {pct(b['SPY hold']['cagr'])}/yr, "
              f"Universe hold {pct(b['Universe hold']['cagr'])}/yr")
        return

    head = (f"{'':27s}|{'TRAIN':^17s}|{'TEST':^58s}|\n"
            f"{'strategy':27s}| {'yearly':>7s} {'maxDD':>7s} | {'yearly':>7s} {'maxDD':>7s} "
            f"{'win%':>5s} {'trades':>6s} {'avgWin':>7s} {'avgLoss':>7s} {'streak':>6s} {'inv%':>4s} | verdict")
    print(head)
    print("-" * len(head.splitlines()[1]))
    for name in ("SPY hold", "Universe hold"):
        tr, te = bench["TRAIN"][name], bench["TEST"][name]
        print(f"{name:27s}| {pct(tr['cagr']):>7s} {pct(tr['max_dd']):>7s} | {pct(te['cagr']):>7s} "
              f"{pct(te['max_dd']):>7s} {'':5s} {'':6s} {'':7s} {'':7s} {'':6s} {'100':>4s} | benchmark")

    def row(name, tr, te, verdict):
        print(f"{name:27s}| {pct(tr['cagr']):>7s} {pct(tr['max_dd']):>7s} | {pct(te['cagr']):>7s} "
              f"{pct(te['max_dd']):>7s} {100 * te['win_rate']:5.0f} {te['trades']:6.0f} "
              f"{pct(te['avg_win']):>7s} {pct(te['avg_loss']):>7s} {te['losing_streak']:6.0f} "
              f"{100 * te['exposure']:4.0f} | {verdict}")

    runs = [(run_window(px, no_skill(ind, s), TRAIN, a)[0], run_window(px, no_skill(ind, s), test, a)[0])
            for s in range(a.seeds)]
    med = lambda k, w: float(np.median([r[w][k] for r in runs]))  # noqa: E731
    keys = ("cagr", "max_dd", "win_rate", "trades", "avg_win", "avg_loss", "losing_streak", "exposure")
    row("No-skill picks (+10/-5)", {k: med(k, 0) for k in keys}, {k: med(k, 1) for k in keys}, "benchmark")
    print("-" * len(head.splitlines()[1]))

    names = [n.strip() for n in a.only.split(",") if n.strip()] or list(STRATEGIES)
    for n in names:
        s = STRATEGIES[n](ind, a.slots)
        tr, te = run_window(px, s, TRAIN, a)[0], run_window(px, s, test, a)[0]
        beat = [w for w, m in (("TRAIN", tr), ("TEST", te))
                if m["cagr"] > bench[w]["Universe hold"]["cagr"]]
        verdict = {2: "PASS: beat universe hold in both", 0: "fail"}.get(
            len(beat), f"fail: beat it in {beat[0]} only" if beat else "fail")
        row(n, tr, te, verdict)

    print("\nyearly = compound return per year. maxDD = worst peak-to-bottom fall. streak = most losses in a row.")
    print("inv% = average share of the account invested. PASS needs a higher yearly return than")
    print("'Universe hold' in BOTH windows. Past results on today's surviving companies; not a forecast.")


if __name__ == "__main__":
    main()
