"""Option backtest helpers: pricing, contract choice and exits on hand-made prices."""
from __future__ import annotations

import pandas as pd
import pytest

from botv2.backtest import Prices
from botv2.options_bt import (OptionRules, atm_strike, bs_call, pick_expiry, simulate_calls,
                              third_friday)


def test_black_scholes_textbook_value():
    # S=100, K=100, T=1y, r=5%, vol=20% -> 10.45 (standard textbook figure)
    assert bs_call(100, 100, 1.0, 0.05, 0.20) == pytest.approx(10.45, abs=0.01)


def test_call_at_expiry_is_intrinsic():
    assert bs_call(110, 100, 0, 0.04, 0.3) == 10 and bs_call(90, 100, 0, 0.04, 0.3) == 0


def test_strikes_and_expiries():
    assert atm_strike(43.4) == 43 and atm_strike(187.4) == 185 and atm_strike(512) == 510
    assert third_friday(2026, 11) == pd.Timestamp("2026-11-20")
    exp = pick_expiry(pd.Timestamp("2026-11-17"))
    assert exp == pd.Timestamp("2026-12-18") and 30 <= (exp - pd.Timestamp("2026-11-17")).days <= 60


def _px(closes):
    idx = pd.bdate_range("2025-01-02", periods=len(closes))
    f = pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1e6}, index=idx)
    return Prices.from_frames({"X": f}), idx


def _trade(idx, entry, exit_, ret=0.0, reason="signal"):
    return pd.DataFrame([{"ticker": "X", "entry_date": idx[entry], "exit_date": idx[exit_], "entry_px": 100,
                          "exit_px": 100, "ret": ret, "pnl": 0, "days": exit_ - entry, "reason": reason,
                          "open": False}])


RULES = OptionRules(premium_pct=0.5, half_spread=0.0, min_tick=0.0, iv_premium=1.0, vol_floor=0.3)


def test_call_doubles_and_takes_profit_before_the_stock_exit():
    closes = [100.0] * 40 + [100.0, 108, 116, 125, 135] + [135.0] * 20
    px, idx = _px(closes)
    r = simulate_calls(px, _trade(idx, 40, 60), str(idx[30].date()), str(idx[-1].date()), 10_000, RULES)
    t = r.trades.iloc[0]
    assert t.reason == "target" and t.ret >= 1.0 and t.exit_date < idx[60]


def test_stock_exit_closes_the_call():
    px, idx = _px([100.0] * 60)
    r = simulate_calls(px, _trade(idx, 40, 45, reason="stop"), str(idx[30].date()), str(idx[-1].date()), 10_000, RULES)
    t = r.trades.iloc[0]
    assert t.reason == "stock_exit:stop" and t.exit_date == idx[45] and t.ret < 0     # time value lost


def test_falling_stock_hits_the_option_stop():
    closes = [100.0] * 40 + [100.0, 95, 90, 85, 80] + [80.0] * 20
    px, idx = _px(closes)
    r = simulate_calls(px, _trade(idx, 40, 60), str(idx[30].date()), str(idx[-1].date()), 10_000, RULES)
    assert r.trades.iloc[0].reason == "stop" and r.trades.iloc[0].ret <= -0.5


def test_spread_and_whole_contracts():
    px, idx = _px([100.0] * 60)
    small = simulate_calls(px, _trade(idx, 40, 50), str(idx[30].date()), str(idx[-1].date()), 437,
                           OptionRules(premium_pct=0.05))
    assert small.trades.iloc[0].reason == "unaffordable"              # $22 buys no $100-stock call
    costly = simulate_calls(px, _trade(idx, 40, 41), str(idx[30].date()), str(idx[-1].date()), 100_000,
                            OptionRules(premium_pct=0.05, half_spread=0.05))
    assert costly.trades.iloc[0].ret < -0.09                          # ~5% in, ~5% out, one day
