"""Tests for strategy.py — rule-based signal logic and indicator calculations.

No network calls, no API keys needed. All tests use synthetic OHLCV data.
"""
import numpy as np
import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy import _rule_based_signal, calculate_indicators


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 100, base_price: float = 50000.0, trend: float = 0.0) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame with n bars.

    trend > 0 = rising prices, trend < 0 = falling prices.
    """
    rng = np.random.default_rng(42)
    closes = base_price + trend * np.arange(n) + rng.normal(0, base_price * 0.005, n)
    closes = np.maximum(closes, 1.0)
    noise = base_price * 0.002
    highs = closes + rng.uniform(0, noise, n)
    lows = closes - rng.uniform(0, noise, n)
    opens = closes + rng.normal(0, noise / 2, n)
    volumes = rng.uniform(1000, 5000, n)
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    })


# ---------------------------------------------------------------------------
# calculate_indicators
# ---------------------------------------------------------------------------

class TestCalculateIndicators:
    def test_adds_required_columns(self):
        df = _make_ohlcv(100)
        result = calculate_indicators(df)
        assert "rsi" in result.columns
        assert "sma_50" in result.columns
        assert "atr" in result.columns
        assert "vol_avg_20" in result.columns

    def test_rsi_range(self):
        df = calculate_indicators(_make_ohlcv(100))
        valid = df["rsi"].dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_sma_50_needs_50_bars(self):
        df = calculate_indicators(_make_ohlcv(100))
        # First 49 rows should be NaN for SMA-50
        assert df["sma_50"].iloc[:49].isna().all()
        assert df["sma_50"].iloc[49:].notna().all()

    def test_sma_50_value_correct(self):
        df = _make_ohlcv(100)
        result = calculate_indicators(df)
        expected = df["close"].iloc[:50].mean()
        assert abs(result["sma_50"].iloc[49] - expected) < 0.01

    def test_vol_avg_20_rolling(self):
        df = calculate_indicators(_make_ohlcv(100))
        assert df["vol_avg_20"].iloc[:19].isna().all()
        assert df["vol_avg_20"].iloc[19:].notna().all()

    def test_raises_without_close_column(self):
        df = pd.DataFrame({"open": [1, 2], "high": [2, 3], "low": [0.5, 1], "volume": [100, 200]})
        with pytest.raises(ValueError, match="close"):
            calculate_indicators(df)

    def test_does_not_modify_original(self):
        df = _make_ohlcv(100)
        original_cols = set(df.columns)
        calculate_indicators(df)
        assert set(df.columns) == original_cols


# ---------------------------------------------------------------------------
# _rule_based_signal — Setup A: dip buy
# ---------------------------------------------------------------------------

class TestRuleBasedSignalDipBuy:
    """RSI < oversold (38) and price > sma * 0.99"""

    def test_dip_buy_triggers(self):
        assert _rule_based_signal(rsi=30.0, price=50000, sma=50200) == "BUY"

    def test_dip_buy_exact_threshold(self):
        # RSI exactly at oversold boundary should NOT trigger (< not <=)
        assert _rule_based_signal(rsi=38.0, price=50000, sma=50200) == "HOLD"

    def test_dip_buy_rsi_just_below(self):
        assert _rule_based_signal(rsi=37.9, price=50000, sma=50200) == "BUY"

    def test_dip_buy_price_too_far_below_sma(self):
        # price < sma * 0.99 — price crashed, not a dip buy setup
        assert _rule_based_signal(rsi=30.0, price=49000, sma=50200) == "HOLD"

    def test_dip_buy_price_at_sma_boundary(self):
        # condition is price > sma * 0.99 (strict), so exactly equal → HOLD
        sma = 50000.0
        assert _rule_based_signal(rsi=30.0, price=sma * 0.99, sma=sma) == "HOLD"
        assert _rule_based_signal(rsi=30.0, price=sma * 0.99 + 0.01, sma=sma) == "BUY"

    def test_dip_buy_blocked_on_low_volume(self):
        assert _rule_based_signal(
            rsi=30.0, price=50000, sma=50200,
            volume=1000, avg_volume=1000,  # exactly 1.0x — below 1.2x threshold
        ) == "HOLD"

    def test_dip_buy_passes_with_sufficient_volume(self):
        assert _rule_based_signal(
            rsi=30.0, price=50000, sma=50200,
            volume=1200, avg_volume=1000,  # exactly 1.2x
        ) == "BUY"

    def test_dip_buy_no_volume_data_skips_check(self):
        # avg_volume=0 means no data — volume filter skipped
        assert _rule_based_signal(rsi=30.0, price=50000, sma=50200, volume=0, avg_volume=0) == "BUY"


# ---------------------------------------------------------------------------
# _rule_based_signal — Setup B: momentum breakout
# ---------------------------------------------------------------------------

class TestRuleBasedSignalBreakout:
    """RSI in 50–65 range and price > sma * 1.001"""

    def test_breakout_triggers(self):
        assert _rule_based_signal(rsi=57.0, price=50100, sma=50000) == "BUY"

    def test_breakout_rsi_low_boundary(self):
        assert _rule_based_signal(rsi=50.0, price=50100, sma=50000) == "BUY"

    def test_breakout_rsi_high_boundary(self):
        assert _rule_based_signal(rsi=65.0, price=50100, sma=50000) == "BUY"

    def test_breakout_rsi_just_above_range(self):
        # RSI 65.1 is outside Setup B range — not a breakout signal
        assert _rule_based_signal(rsi=65.1, price=50100, sma=50000) == "HOLD"

    def test_breakout_rsi_just_below_range(self):
        assert _rule_based_signal(rsi=49.9, price=50100, sma=50000) == "HOLD"

    def test_breakout_price_at_sma(self):
        # price must be > sma * 1.001 — exactly at SMA is not enough
        sma = 50000.0
        assert _rule_based_signal(rsi=57.0, price=sma, sma=sma) == "HOLD"

    def test_breakout_price_just_above_threshold(self):
        sma = 50000.0
        assert _rule_based_signal(rsi=57.0, price=sma * 1.0011, sma=sma) == "BUY"

    def test_breakout_blocked_on_low_volume(self):
        assert _rule_based_signal(
            rsi=57.0, price=50100, sma=50000,
            volume=500, avg_volume=1000,
        ) == "HOLD"


# ---------------------------------------------------------------------------
# _rule_based_signal — SELL
# ---------------------------------------------------------------------------

class TestRuleBasedSignalSell:
    def test_sell_triggers_above_overbought(self):
        assert _rule_based_signal(rsi=75.0, price=50000, sma=48000) == "SELL"

    def test_sell_at_exact_threshold(self):
        # rsi > overbought (70), not >=
        assert _rule_based_signal(rsi=70.0, price=50000, sma=48000) == "HOLD"

    def test_sell_just_above_threshold(self):
        assert _rule_based_signal(rsi=70.1, price=50000, sma=48000) == "SELL"

    def test_sell_blocked_on_low_volume(self):
        assert _rule_based_signal(
            rsi=75.0, price=50000, sma=48000,
            volume=100, avg_volume=1000,
        ) == "HOLD"

    def test_sell_passes_with_sufficient_volume(self):
        assert _rule_based_signal(
            rsi=75.0, price=50000, sma=48000,
            volume=1200, avg_volume=1000,
        ) == "SELL"


# ---------------------------------------------------------------------------
# _rule_based_signal — HOLD (neutral zone)
# ---------------------------------------------------------------------------

class TestRuleBasedSignalHold:
    def test_neutral_rsi_holds(self):
        # RSI in 38–50 gap — no setup applies
        assert _rule_based_signal(rsi=44.0, price=50000, sma=49000) == "HOLD"

    def test_rsi_between_setups(self):
        # RSI between 65 and 70 — above breakout, below overbought
        assert _rule_based_signal(rsi=67.0, price=50000, sma=49000) == "HOLD"

    def test_custom_thresholds_respected(self):
        # With oversold=25, RSI=30 should be HOLD (30 >= 25 is false, wait — 30 > 25, so...)
        # Actually oversold threshold: rsi < oversold, so rsi=30 < oversold=25 is False → HOLD
        assert _rule_based_signal(rsi=30.0, price=50000, sma=50200, oversold=25.0) == "HOLD"

    def test_custom_overbought_threshold(self):
        # With overbought=80, RSI=75 should be HOLD
        assert _rule_based_signal(rsi=75.0, price=50000, sma=48000, overbought=80.0) == "HOLD"
        assert _rule_based_signal(rsi=81.0, price=50000, sma=48000, overbought=80.0) == "SELL"
