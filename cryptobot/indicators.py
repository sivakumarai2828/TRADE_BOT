"""Crypto-specific technical indicators — pure pandas/numpy, no external TA library."""
from __future__ import annotations
import numpy as np
import pandas as pd
from daybot.indicators import (
    add_indicators,
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    calculate_vwap,
    get_volume_avg,
)

__all__ = [
    "calculate_ema",
    "calculate_rsi",
    "get_volume_avg",
    "calculate_vwap",
    "calculate_atr",
    "add_indicators",
    "calculate_macd",
    "calculate_bollinger_bands",
    "calculate_stochastic",
    "add_crypto_indicators",
]


def calculate_macd(
    prices: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram via EMA crossover."""
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(
    prices: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Upper, middle (SMA), and lower Bollinger Bands."""
    middle = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std(ddof=0)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def calculate_stochastic(
    df: pd.DataFrame,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Stochastic oscillator %K (raw) and %D (signal) lines."""
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k_line = (df["close"] - low_min) / denom * 100
    d_line = k_line.rolling(window=d_period).mean()
    return k_line, d_line


def add_crypto_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all base + crypto indicators to an OHLCV DataFrame."""
    result = add_indicators(df)

    macd_line, signal_line, histogram = calculate_macd(result["close"])
    result["macd"] = macd_line
    result["macd_signal"] = signal_line
    result["macd_hist"] = histogram

    bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(result["close"])
    result["bb_upper"] = bb_upper
    result["bb_middle"] = bb_middle
    result["bb_lower"] = bb_lower

    stoch_k, stoch_d = calculate_stochastic(result)
    result["stoch_k"] = stoch_k
    result["stoch_d"] = stoch_d

    return result
