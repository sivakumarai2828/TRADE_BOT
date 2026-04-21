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
        if rsi > 65:
            return SignalResult(symbol, "SELL", price, ema, rsi, volume, avg_volume,
                                trend, f"RSI {rsi:.1f} overbought (>65)")
        if price < ema:
            return SignalResult(symbol, "SELL", price, ema, rsi, volume, avg_volume,
                                trend, "Price crossed below EMA — trend break")

    # --- HOLD conditions ---
    if 45.0 <= rsi <= 55.0:
        return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                            trend, f"RSI {rsi:.1f} neutral zone (45–55) — no edge")
    if not vol_rising and not has_position:
        return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                            trend, "Volume below average — no conviction")

    # --- BUY conditions ---
    if (
        price > ema
        and 0.0 <= pct_from_ema <= 1.5
        and 35.0 <= rsi <= 45.0
        and vol_rising
        and not has_position
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, volume, avg_volume, trend,
            f"RSI {rsi:.1f} pullback, {pct_from_ema:.1f}% above EMA, volume up",
        )

    return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                        trend, "No setup — conditions not met")
