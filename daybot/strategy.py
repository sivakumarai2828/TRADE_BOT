"""Rule-based signal generation for intraday trading."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

Signal = Literal["BUY", "SELL", "HOLD"]


@dataclass
class SignalResult:
    symbol: str
    action: Signal
    price: float
    ema: float
    rsi: float
    volume: float
    avg_volume: float
    trend: str
    reason: str


def generate_signal(
    symbol: str,
    price: float,
    ema: float,
    rsi: float,
    volume: float,
    avg_volume: float,
    has_position: bool = False,
) -> SignalResult:
    trend = "uptrend" if price > ema else "downtrend"
    pct_from_ema = (price - ema) / ema * 100 if ema > 0 else 0.0
    vol_rising = avg_volume > 0 and volume >= avg_volume * 1.2

    # --- SELL logic (only when position is open) ---
    if has_position:
        if rsi > 72:
            return SignalResult(symbol, "SELL", price, ema, rsi, volume, avg_volume,
                                trend, f"RSI {rsi:.1f} overbought (>72)")
        if price < ema * 0.995:
            return SignalResult(symbol, "SELL", price, ema, rsi, volume, avg_volume,
                                trend, "Price broke below EMA — trend reversal")

    # --- HOLD conditions ---
    if 45.0 <= rsi <= 55.0:
        return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                            trend, f"RSI {rsi:.1f} neutral zone (45–55) — no edge")
    if not vol_rising and not has_position:
        return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                            trend, "Volume below average — no conviction")

    # --- BUY conditions ---
    # Setup A: Pullback dip — RSI recovering from oversold, price near EMA with volume
    if (
        price > ema
        and 0.0 <= pct_from_ema <= 3.0
        and 30.0 <= rsi <= 52.0
        and vol_rising
        and not has_position
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, volume, avg_volume, trend,
            f"RSI {rsi:.1f} pullback dip, {pct_from_ema:.1f}% above EMA, volume up",
        )

    # Setup B: Momentum breakout — strong uptrend, RSI rising with high volume
    if (
        price > ema
        and pct_from_ema > 3.0
        and 55.0 <= rsi <= 70.0
        and vol_rising
        and not has_position
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, volume, avg_volume, trend,
            f"RSI {rsi:.1f} momentum breakout, {pct_from_ema:.1f}% above EMA, volume surge",
        )

    return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                        trend, "No setup — conditions not met")
