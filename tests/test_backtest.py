"""Backtest engine — fills, exits and accounting on hand-made price bars.

A backtest that fills at prices the bot could never get is worse than no
backtest, so each fill rule is pinned on synthetic data (no network).
"""
from __future__ import annotations

import pandas as pd
import pytest

from botv2.backtest import Prices, Strategy, equal_weight, max_drawdown, metrics, simulate


def mk(bars: dict[str, list[tuple]]) -> Prices:
    """bars: ticker -> [(open, high, low, close), ...], one tuple per day."""
    n = len(next(iter(bars.values())))
    idx = pd.bdate_range("2024-01-01", periods=n)
    frames = {t: pd.DataFrame(b, index=idx, columns=["Open", "High", "Low", "Close"]).assign(Volume=1e6)
              for t, b in bars.items()}
    return Prices.from_frames(frames)


def strat(px, days=(0,), tickers=None, score=None, exit_days=None, **kw):
    """Entry signal on the given day numbers for the given tickers."""
    cols = px.close.columns
    e = pd.DataFrame(False, index=px.close.index, columns=cols)
    for t in (tickers or cols):
        e.iloc[list(days), cols.get_loc(t)] = True
    s = pd.DataFrame([[(score or {}).get(t, 1.0) for t in cols]] * len(e), index=e.index, columns=cols)
    x = None
    if exit_days:
        x = pd.DataFrame(False, index=e.index, columns=cols)
        x.iloc[list(exit_days), :] = True
    return Strategy("t", e, s, exit=x, **kw)


FLAT = (100, 100, 100, 100)
RUN = dict(capital=1000, slots=1, cost=0.0)


def one(res):
    assert len(res.trades) == 1
    return res.trades.iloc[0]


def test_signal_trades_at_next_open_not_same_day():
    px = mk({"A": [(50, 50, 50, 50), (100, 100, 100, 100), FLAT, FLAT]})
    t = one(simulate(px, strat(px), **RUN))
    assert t.entry_date == px.close.index[1] and t.entry_px == 100


def test_target_fills_at_target_price():
    px = mk({"A": [FLAT, FLAT, (100, 112, 99, 111), FLAT]})
    t = one(simulate(px, strat(px, take_profit=0.10, stop_loss=0.05), **RUN))
    assert t.reason == "target" and t.exit_px == pytest.approx(110) and t.ret == pytest.approx(0.10)


def test_stop_fills_at_stop_price():
    px = mk({"A": [FLAT, FLAT, (100, 101, 94, 96), FLAT]})
    t = one(simulate(px, strat(px, take_profit=0.10, stop_loss=0.05), **RUN))
    assert t.reason == "stop" and t.exit_px == pytest.approx(95) and t.ret == pytest.approx(-0.05)


def test_gap_below_stop_fills_at_open_worse_than_stop():
    px = mk({"A": [FLAT, FLAT, (88, 90, 87, 89), FLAT]})
    t = one(simulate(px, strat(px, take_profit=0.10, stop_loss=0.05), **RUN))
    assert t.reason == "stop_gap" and t.exit_px == 88 and t.ret == pytest.approx(-0.12)


def test_gap_above_target_fills_at_open():
    px = mk({"A": [FLAT, FLAT, (115, 116, 114, 115), FLAT]})
    t = one(simulate(px, strat(px, take_profit=0.10, stop_loss=0.05), **RUN))
    assert t.reason == "target_gap" and t.exit_px == 115


def test_stop_assumed_first_when_day_touches_both():
    px = mk({"A": [FLAT, FLAT, (100, 112, 94, 100), FLAT]})
    t = one(simulate(px, strat(px, take_profit=0.10, stop_loss=0.05), **RUN))
    assert t.reason == "stop"


def test_can_stop_out_on_the_entry_day():
    px = mk({"A": [FLAT, (100, 100, 94, 95), FLAT]})
    t = one(simulate(px, strat(px, stop_loss=0.05), **RUN))
    assert t.reason == "stop" and t.days == 0


def test_slots_filled_by_best_score():
    px = mk({t: [FLAT] * 4 for t in "ABC"})
    r = simulate(px, strat(px, score={"A": 1, "B": 3, "C": 2}), capital=900, slots=2, cost=0.0)
    assert set(r.trades.ticker) == {"B", "C"}
    assert r.trades.pnl.abs().max() == 0 and r.equity.iloc[-1] == pytest.approx(900)


def test_whole_shares_skip_what_the_slot_cannot_afford():
    px = mk({"A": [(200, 200, 200, 200)] * 4, "B": [FLAT] * 4})
    r = simulate(px, strat(px, score={"A": 2, "B": 1}), capital=437, slots=3, cost=0.0, whole_shares=True)
    assert list(r.trades.ticker) == ["B"]           # $145 slot: 0 x $200, 1 x $100
    assert r.exposure == pytest.approx(100 / 437, rel=0.01)


def test_sale_proceeds_settle_next_day():
    px = mk({"A": [FLAT] * 6, "B": [FLAT] * 6})
    s = strat(px, days=(0,), tickers=["A"], exit_days=(2,))
    s.entry.iloc[2, px.close.columns.get_loc("B")] = True      # B signals as A is sold
    assert set(simulate(px, s, **RUN).trades.ticker) == {"A"}                  # cash still unsettled
    assert set(simulate(px, s, settle_days=0, **RUN).trades.ticker) == {"A", "B"}


def test_trailing_stop_ratchets_up():
    px = mk({"A": [FLAT, FLAT, (100, 130, 100, 130), (130, 131, 110, 112), FLAT]})
    t = one(simulate(px, strat(px, stop_loss=0.08, trail=0.10), **RUN))
    assert t.reason == "stop" and t.exit_px == pytest.approx(117)             # 130 * 0.90


def test_max_hold_exits_at_close():
    px = mk({"A": [FLAT, FLAT, FLAT, (100, 104, 100, 103), FLAT]})
    t = one(simulate(px, strat(px, max_hold=2), **RUN))
    assert t.reason == "time" and t.exit_px == 103 and t.days == 2


def test_cooldown_blocks_reentry_after_a_loss():
    bars = [FLAT, (100, 100, 94, 95)] + [(95, 95, 95, 95)] * 5
    px = mk({"A": bars})
    every = tuple(range(6))
    assert len(simulate(px, strat(px, days=every, stop_loss=0.05), settle_days=0, **RUN).trades) > 1
    r = simulate(px, strat(px, days=every, stop_loss=0.05, cooldown=10), settle_days=0, **RUN)
    assert len(r.trades) == 1


def test_costs_charged_on_both_sides():
    px = mk({"A": [FLAT] * 5})
    t = one(simulate(px, strat(px, max_hold=1), capital=1000, slots=1, cost=0.001))
    assert t.ret == pytest.approx((1 - 0.001) / (1 + 0.001) - 1)


def test_no_signals_means_flat_equity():
    px = mk({"A": [FLAT, (100, 150, 50, 120), FLAT]})
    r = simulate(px, strat(px, days=()), **RUN)
    assert r.trades.empty and (r.equity == 1000).all()


def test_open_position_is_marked_but_not_counted_as_a_win():
    px = mk({"A": [FLAT, FLAT, (100, 120, 100, 120)]})
    r = simulate(px, strat(px), **RUN)
    assert bool(r.trades.iloc[0].open) and r.equity.iloc[-1] == pytest.approx(1200)
    assert metrics(r.equity, r.trades, 1000)["trades"] == 0


def test_metrics_and_drawdown():
    eq = pd.Series([100, 120, 90, 108.0])
    assert max_drawdown(eq) == pytest.approx(-0.25)
    tr = pd.DataFrame({"ret": [0.1, -0.05, -0.05, 0.1], "pnl": [10, -5, -5, 10], "days": [3] * 4,
                       "open": [False] * 4, "exit_date": pd.bdate_range("2024-01-01", periods=4)})
    m = metrics(eq, tr, 100)
    assert m["win_rate"] == 0.5 and m["losing_streak"] == 2 and m["profit_factor"] == pytest.approx(2.0)


def test_equal_weight_benchmark_averages_the_universe():
    px = mk({"A": [FLAT, (100, 110, 100, 110)], "B": [FLAT, (100, 100, 90, 90)]})
    assert equal_weight(px, None, None, 1000).iloc[-1] == pytest.approx(1000)
