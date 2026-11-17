"""Options lab — would buying calls beat buying the shares on the same signals?

For each strategy in tools/strategy_lab.py: run the share version, then replay
every one of its trades as an at-the-money call (botv2/options_bt.py), on the
TRAIN (2018-22) and TEST (2023-now) windows. Option prices are MODELED with
Black-Scholes from each stock's realized volatility; see options_bt.py.

  python3 tools/options_lab.py
  python3 tools/options_lab.py --premium 0.10 --spread 0.05 --iv 1.3
  python3 tools/options_lab.py --capital 437

Gate for Phase 2: the call version must beat BOTH the share version and
holding every stock, in BOTH windows, without a deeper worst drop than the
share version, under the default (middle-of-the-road) cost assumptions.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategy_lab as L  # noqa: E402
from botv2.backtest import equal_weight, load_prices, metrics, simulate  # noqa: E402
from botv2.options_bt import OptionRules, simulate_calls  # noqa: E402

PICK = ["trend_hold_50dma", "trail_breakout", "fixed_10_5_breakout", "fixed_10_5_strongest",
        "rotation_monthly", "dip_rsi2"]


def pct(x):
    return f"{100 * x:+.1f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--slots", type=int, default=3)
    ap.add_argument("--premium", type=float, default=0.05, help="premium per trade, share of equity")
    ap.add_argument("--spread", type=float, default=0.03, help="cost per side, share of option price")
    ap.add_argument("--iv", type=float, default=1.15, help="implied vol / realized vol")
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    px_all = load_prices(L.US_UNIVERSE + [L.BENCH], L.DATA_START, None)
    spy, px = px_all.close[L.BENCH], px_all.drop([L.BENCH])
    ind = L.Ind(px, spy)
    test = (L.TEST_START, str(px.close.index[-1].date()))
    wins = {"2018-22": L.TRAIN, "2023-26": test}
    rules = OptionRules(premium_pct=a.premium, max_open=a.slots, half_spread=a.spread, iv_premium=a.iv)
    hold = {w: metrics(equal_weight(px, *rng, a.capital))["cagr"] for w, rng in wins.items()}

    print(f"US universe, ${a.capital:,.0f}. Shares: {a.slots} slots of 1/{a.slots} each. "
          f"Calls: {a.premium:.0%} premium per trade, max {a.slots} open, "
          f"spread {a.spread:.0%}/side, implied vol = {a.iv:g} x realized.")
    print("Option prices are MODELED (Black-Scholes), not real quotes.\n")
    print(f"{'hold every stock':38s} 2018-22 {pct(hold['2018-22']):>7s}/yr   2023-26 {pct(hold['2023-26']):>7s}/yr\n")
    head = (f"{'strategy':24s}{'version':8s}| {'2018-22':>8s} {'maxDD':>7s} | {'2023-26':>8s} {'maxDD':>7s} "
            f"{'win%':>5s} {'trades':>6s} {'skipped':>7s} | verdict")
    print(head); print("-" * len(head))

    names = [n.strip() for n in a.only.split(",") if n.strip()] or PICK
    rows = []
    for name in names:
        strat = L.STRATEGIES[name](ind, a.slots)
        res = {}
        for w, rng in wins.items():
            sh = simulate(px, strat, rng[0], rng[1], a.capital, a.slots, 0.0005)
            ms = metrics(sh.equity, sh.trades, a.capital)
            op = simulate_calls(px, sh.trades, rng[0], rng[1], a.capital, rules)
            closed = op.trades[~op.trades.reason.isin(["open", "unaffordable"])]
            mo = metrics(op.equity, None, a.capital)
            mo.update(win_rate=float((closed.ret > 0).mean()) if len(closed) else 0.0,
                      trades=len(closed), skipped=int((op.trades.reason == "unaffordable").sum()),
                      avg_win=float(closed.ret[closed.ret > 0].mean()) if (closed.ret > 0).any() else 0.0,
                      avg_loss=float(closed.ret[closed.ret <= 0].mean()) if (closed.ret <= 0).any() else 0.0,
                      reasons=closed.reason.str.split(":").str[0].value_counts().to_dict())
            res[w] = (ms, mo)
        for label, k in (("shares", 0), ("calls", 1)):
            tr, te = res["2018-22"][k], res["2023-26"][k]
            verdict = ""
            if k == 1:
                beats = all(res[w][1]["cagr"] > max(res[w][0]["cagr"], hold[w]) for w in wins)
                safer = all(res[w][1]["max_dd"] >= res[w][0]["max_dd"] for w in wins)
                verdict = "PASS" if beats and safer else "fail: " + (
                    "beat shares+hold in both but deeper drop" if beats else "did not beat shares and hold in both windows")
            print(f"{name if k == 0 else '':24s}{label:8s}| {pct(tr['cagr']):>8s} {pct(tr['max_dd']):>7s} | "
                  f"{pct(te['cagr']):>8s} {pct(te['max_dd']):>7s} {100 * te.get('win_rate', 0):5.0f} "
                  f"{te.get('trades', 0):6d} {te.get('skipped', 0) if k else '':>7} | {verdict}")
        mo = res["2023-26"][1]
        print(f"{'':24s}{'':8s}  2023-26 calls: avg win {pct(mo['avg_win'])}, avg loss {pct(mo['avg_loss'])}, "
              f"exits {mo['reasons']}")
        rows.append((name, res))
    print("\nwin%, trades and avg win/loss are per trade, 2023-26. maxDD = worst fall from a peak.")
    print("Calls keep most of the account in cash (at most "
          f"{a.premium * a.slots:.0%} in premium), so compare yearly return and worst drop, not win rate.")


if __name__ == "__main__":
    main()
