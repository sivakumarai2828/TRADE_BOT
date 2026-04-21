"""Technical indicators — pure pandas, no external TA library needed."""
from __future__ import annotations
import pandas as pd


def calculate_ema(prices: pd.Series, period: int = 50) -> pd.Series:
    return prices.ewm(span=period, adjust=False).mean()


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("inf"))
    return 100 - (100 / (1 + rs))


def get_volume_avg(volumes: pd.Series, period: int = 20) -> float:
    return float(volumes.tail(period).mean())


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP from the start of the current trading day.

    Typical price × volume, cumulated from the first bar of the day.
    Resets each calendar day so intraday positioning stays accurate.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]
    # Group by date so VWAP resets at market open each day
    date_key = pd.to_datetime(df.index).date if hasattr(df.index, "__iter__") else pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df.index).dt.date
    try:
        cum_tp_vol = tp_vol.groupby(date_key).cumsum()
        cum_vol = df["volume"].groupby(date_key).cumsum()
    except Exception:
        cum_tp_vol = tp_vol.cumsum()
        cum_vol = df["volume"].cumsum()
    return cum_tp_vol / cum_vol.replace(0, float("nan"))


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ema_50, rsi, vol_avg, vwap columns to an OHLCV DataFrame."""
    result = df.copy()
    result["ema_50"] = calculate_ema(result["close"])
    result["rsi"] = calculate_rsi(result["close"])
    result["vol_avg"] = result["volume"].rolling(20).mean()
    result["vwap"] = calculate_vwap(result)
    return result
