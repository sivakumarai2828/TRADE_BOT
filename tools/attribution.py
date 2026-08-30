"""Which kind of trade actually produces the edge?

Groups closed trades by setup type, regime, sector and exit reason, and reports
expectancy in R for each. This is the report Phase 5 exists to feed: without it,
a parallel V2/V3 run produces a return number and no explanation.

Run on the VM, with the .env loaded so it reads the live journal:

    set -a && . ./.env && set +a && python3 tools/attribution.py
    python3 tools/attribution.py --market US
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, ".")

from botv2.config import load_config          # noqa: E402
from botv2.storage import make_journal        # noqa: E402


def _fmt(v, width=7, pct=False, plus=True):
    if v is None:
        return " " * (width - 1) + "-"
    sign = "+" if plus else ""
    return f"{v:{sign}{width}.2f}{'%' if pct else ''}"


def summarise(rows: list[dict]) -> dict:
    """Expectancy and friends for one group of closed trades."""
    n = len(rows)
    if not n:
        return {}
    pnls = [r.get("pnl") or 0 for r in rows]
    rs = [r["realized_r"] for r in rows if r.get("realized_r") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    return {
        "n": n,
        "win_pct": 100 * len(wins) / n,
        "avg_r": statistics.fmean(rs) if rs else None,
        "median_r": statistics.median(rs) if rs else None,
        "expectancy": sum(pnls) / n,
        "total": sum(pnls),
        "pf": (gross_win / gross_loss) if gross_loss else None,
        "n_with_r": len(rs),
    }


def table(title: str, groups: dict[str, list[dict]], currency: str) -> None:
    print(f"\n  {title}")
    print(f"    {'group':<22}{'N':>4}{'win%':>7}{'avg R':>8}{'med R':>8}"
          f"{'expect':>10}{'total':>11}{'PF':>7}")
    print("    " + "-" * 77)
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        s = summarise(groups[key])
        if not s:
            continue
        pf = f"{s['pf']:7.2f}" if s["pf"] is not None else "      -"
        print(f"    {str(key)[:22]:<22}{s['n']:>4}{s['win_pct']:>6.0f}%"
              f"{_fmt(s['avg_r'], 8)}{_fmt(s['median_r'], 8)}"
              f"{_fmt(s['expectancy'], 10)}{_fmt(s['total'], 11)}{pf}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["US", "INDIA", "BOTH"], default="BOTH")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()

    cfg = load_config()
    j = make_journal(cfg.db_path)
    markets = ["US", "INDIA"] if args.market == "BOTH" else [args.market]

    for market in markets:
        rows = j.recent_closed(market, args.limit)
        cur = "$" if market == "US" else "Rs"
        print("\n" + "=" * 81)
        print(f"  {market}  —  {len(rows)} closed trades   (amounts in {cur})")
        print("=" * 81)
        if not rows:
            print("  no closed trades yet")
            continue

        overall = summarise(rows)
        print(f"\n  overall: {overall['n']} trades, {overall['win_pct']:.0f}% win, "
              f"expectancy {overall['expectancy']:+.2f} per trade, "
              f"total {overall['total']:+.2f}")
        if overall["n_with_r"]:
            print(f"           avg {overall['avg_r']:+.2f}R over "
                  f"{overall['n_with_r']} trades with R recorded")
        else:
            print("           no realized_r yet — attribution starts with trades "
                  "opened after the V3 deploy")

        for label, field in (("BY SETUP", "setup_type"),
                             ("BY REGIME AT ENTRY", "market_regime"),
                             ("BY SECTOR", "sector"),
                             ("BY EXIT REASON", "exit_reason")):
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in rows:
                v = r.get(field)
                if field == "exit_reason" and v:
                    v = str(v).split(":")[0]          # collapse "ai_exit: ..."
                groups[v or "(unlabelled)"].append(r)
            if len(groups) > 1 or "(unlabelled)" not in groups:
                table(label, groups, cur)

        withr = [r for r in rows if r.get("realized_r") is not None]
        if withr:
            best = max(withr, key=lambda r: r["realized_r"])
            worst = min(withr, key=lambda r: r["realized_r"])
            print(f"\n  best  {best['symbol']:<14} {best['realized_r']:+.2f}R  "
                  f"({best.get('setup_type') or 'unlabelled'})")
            print(f"  worst {worst['symbol']:<14} {worst['realized_r']:+.2f}R  "
                  f"({worst.get('setup_type') or 'unlabelled'})")
    print()


if __name__ == "__main__":
    main()
