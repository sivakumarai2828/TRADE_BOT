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
    # vol_data_available: False when IEX feed returns 0 volume (known data gap)
    vol_data_available = avg_volume > 1 and volume > 0
    vol_rising = not vol_data_available or volume >= avg_volume * 1.2

    # --- SELL logic (only when position is open) ---
    if has_position:
        if rsi > 72:
            return SignalResult(symbol, "SELL", price, ema, rsi, volume, avg_volume,
                                trend, f"RSI {rsi:.1f} overbought (>72)")
        if price < ema * 0.995:
            return SignalResult(symbol, "SELL", price, ema, rsi, volume, avg_volume,
                                trend, "Price broke below EMA — trend reversal")

    # --- HOLD conditions ---
    if 49.0 <= rsi <= 51.0:
        return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                            trend, f"RSI {rsi:.1f} neutral zone (49–51) — no edge")
    if vol_data_available and not vol_rising and not has_position:
        return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                            trend, "Volume below average — no conviction")

    # --- BUY conditions ---
    # Setup A: Pullback dip — RSI recovering from oversold, price near EMA
    # Allow slight EMA undercut (-0.5%) on deep oversold (RSI<38) for bounce entries
    ema_low = -0.5 if rsi < 38.0 else 0.0
    if (
        ema_low <= pct_from_ema <= 3.0
        and 30.0 <= rsi <= 62.0
        and not has_position
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, volume, avg_volume, trend,
            f"RSI {rsi:.1f} pullback dip, {pct_from_ema:.1f}% from EMA",
        )

    # Setup B: Momentum breakout — strong uptrend, RSI rising
    if (
        price > ema
        and pct_from_ema > 3.0
        and 53.0 <= rsi <= 72.0
        and vol_rising
        and not has_position
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, volume, avg_volume, trend,
            f"RSI {rsi:.1f} momentum breakout, {pct_from_ema:.1f}% above EMA",
        )

    return SignalResult(symbol, "HOLD", price, ema, rsi, volume, avg_volume,
                        trend, "No setup — conditions not met")


def generate_orb_signal(
    symbol: str,
    price: float,
    orb_high: float,
    orb_low: float,
    volume: float,
    avg_volume: float,
    has_position: bool = False,
) -> SignalResult:
    """Opening Range Breakout — range = first 15 min (9:30–9:44 ET).

    BUY  : price breaks above range_high with volume ≥ 1.5× avg
    SELL : position stop — price drops back below range_low
    HOLD : no breakout yet, range too wide, or already fired
    Skip : range width > 2% of price (volatile open — no edge)
    """
    range_width_pct = (orb_high - orb_low) / orb_high * 100 if orb_high > 0 else 99.0
    vol_ok = avg_volume <= 1 or volume >= avg_volume * 1.5

    if has_position:
        if price < orb_low:
            return SignalResult(symbol, "SELL", price, orb_high, 50.0, volume, avg_volume,
                                "downtrend", f"ORB stop: price below range low ${orb_low:.2f}")
        return SignalResult(symbol, "HOLD", price, orb_high, 50.0, volume, avg_volume,
                            "uptrend", "ORB: position open")

    if range_width_pct > 2.0:
        return SignalResult(symbol, "HOLD", price, orb_high, 50.0, volume, avg_volume,
                            "neutral", f"ORB range {range_width_pct:.1f}% too wide — skip")

    if price > orb_high and vol_ok:
        return SignalResult(
            symbol, "BUY", price, orb_high, 60.0, volume, avg_volume, "uptrend",
            f"ORB breakout above ${orb_high:.2f} (range {range_width_pct:.1f}%, vol {volume/max(avg_volume,1):.1f}x)",
        )

    return SignalResult(symbol, "HOLD", price, orb_high, 50.0, volume, avg_volume,
                        "neutral", f"ORB: waiting for breakout above ${orb_high:.2f}")
