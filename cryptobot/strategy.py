"""Rule-based signal generation for crypto markets — three distinct strategies."""
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
    macd: float
    macd_signal: float
    bb_upper: float
    bb_lower: float
    volume: float
    avg_volume: float
    strategy: str
    reason: str


# ---------------------------------------------------------------------------
# Strategy 1 — RSI + MACD Confluence (mean reversion + momentum)
# ---------------------------------------------------------------------------

def _strategy_confluence(
    symbol: str,
    price: float,
    ema: float,
    rsi: float,
    macd: float,
    macd_signal: float,
    macd_hist: float,
    macd_hist_prev: float,
    volume: float,
    avg_volume: float,
    has_position: bool,
) -> SignalResult:
    """RSI oversold + MACD histogram turning positive while above EMA-50."""
    hist_turning_positive = macd_hist > 0 and macd_hist_prev <= 0
    hist_turning_negative = macd_hist < 0 and macd_hist_prev >= 0

    # SELL — RSI overbought or MACD momentum flipping negative
    if has_position:
        if rsi > 68:
            return SignalResult(
                symbol, "SELL", price, ema, rsi, macd, macd_signal,
                0.0, 0.0, volume, avg_volume, "confluence",
                f"RSI {rsi:.1f} overbought (>68) — exit",
            )
        if hist_turning_negative:
            return SignalResult(
                symbol, "SELL", price, ema, rsi, macd, macd_signal,
                0.0, 0.0, volume, avg_volume, "confluence",
                "MACD histogram turned negative — momentum fading",
            )

    # BUY — RSI oversold + MACD histogram crossing zero + price above EMA support
    if (
        not has_position
        and rsi < 35
        and hist_turning_positive       # histogram crossed zero: negative → positive
        and price > ema                 # still above structural support
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, macd, macd_signal,
            0.0, 0.0, volume, avg_volume, "confluence",
            f"RSI {rsi:.1f} oversold + MACD histogram turning positive above EMA",
        )

    return SignalResult(
        symbol, "HOLD", price, ema, rsi, macd, macd_signal,
        0.0, 0.0, volume, avg_volume, "confluence",
        "No confluence — conditions not met",
    )


# ---------------------------------------------------------------------------
# Strategy 2 — Bollinger Band Bounce
# ---------------------------------------------------------------------------

def _strategy_bb_bounce(
    symbol: str,
    price: float,
    ema: float,
    rsi: float,
    macd: float,
    macd_signal: float,
    bb_upper: float,
    bb_middle: float,
    bb_lower: float,
    volume: float,
    avg_volume: float,
    has_position: bool,
) -> SignalResult:
    """Buy at lower BB with RSI confirmation; sell at middle or upper band."""
    vol_ok = avg_volume <= 0 or volume >= avg_volume * 1.3
    touching_lower = price <= bb_lower * 1.002   # within 0.2% of lower band
    at_middle = price >= bb_middle * 0.998        # reached or crossed middle SMA
    at_upper = price >= bb_upper * 0.998          # reached upper band

    # SELL — price recovered to middle or upper BB
    if has_position:
        if at_upper:
            return SignalResult(
                symbol, "SELL", price, ema, rsi, macd, macd_signal,
                bb_upper, bb_lower, volume, avg_volume, "bb_bounce",
                f"Price ${price:.2f} reached upper BB ${bb_upper:.2f} — full exit",
            )
        if at_middle:
            return SignalResult(
                symbol, "SELL", price, ema, rsi, macd, macd_signal,
                bb_upper, bb_lower, volume, avg_volume, "bb_bounce",
                f"Price ${price:.2f} at middle BB (SMA) ${bb_middle:.2f} — exit",
            )

    # BUY — price at lower BB, RSI confirms oversold, volume confirms interest
    if (
        not has_position
        and touching_lower
        and rsi < 40
        and vol_ok
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, macd, macd_signal,
            bb_upper, bb_lower, volume, avg_volume, "bb_bounce",
            f"Price ${price:.2f} at lower BB ${bb_lower:.2f} | RSI {rsi:.1f} | vol {volume/max(avg_volume,1):.1f}x",
        )

    # HOLD — between lower and middle BB without RSI confirmation
    return SignalResult(
        symbol, "HOLD", price, ema, rsi, macd, macd_signal,
        bb_upper, bb_lower, volume, avg_volume, "bb_bounce",
        "Price between bands — waiting for RSI confirmation or band touch",
    )


# ---------------------------------------------------------------------------
# Strategy 3 — Momentum Breakout
# ---------------------------------------------------------------------------

def _strategy_breakout(
    symbol: str,
    price: float,
    ema: float,
    rsi: float,
    macd: float,
    macd_signal: float,
    bb_upper: float,
    bb_lower: float,
    volume: float,
    avg_volume: float,
    has_position: bool,
) -> SignalResult:
    """Buy confirmed breakout above upper BB with MACD + volume; sell on reversal."""
    vol_strong = avg_volume <= 0 or volume >= avg_volume * 1.5
    above_upper = price > bb_upper           # confirmed breakout above band
    macd_positive = macd > macd_signal       # MACD still bullish
    back_inside = price < bb_upper * 0.995  # price fell back inside band

    # SELL — breakout failed (price back inside) or RSI extreme
    if has_position:
        if back_inside:
            return SignalResult(
                symbol, "SELL", price, ema, rsi, macd, macd_signal,
                bb_upper, bb_lower, volume, avg_volume, "breakout",
                f"Price ${price:.2f} back inside BB (${bb_upper:.2f}) — breakout failed",
            )
        if rsi > 78:
            return SignalResult(
                symbol, "SELL", price, ema, rsi, macd, macd_signal,
                bb_upper, bb_lower, volume, avg_volume, "breakout",
                f"RSI {rsi:.1f} extreme overbought (>78) — exit before reversal",
            )

    # BUY — price above upper BB, RSI in momentum zone, MACD bullish, strong volume
    if (
        not has_position
        and above_upper
        and 55 <= rsi <= 75              # momentum zone — not yet overbought
        and macd_positive                # MACD confirms upward direction
        and vol_strong                   # volume backs the move
    ):
        return SignalResult(
            symbol, "BUY", price, ema, rsi, macd, macd_signal,
            bb_upper, bb_lower, volume, avg_volume, "breakout",
            f"Breakout above BB ${bb_upper:.2f} | RSI {rsi:.1f} | MACD bullish | vol {volume/max(avg_volume,1):.1f}x",
        )

    return SignalResult(
        symbol, "HOLD", price, ema, rsi, macd, macd_signal,
        bb_upper, bb_lower, volume, avg_volume, "breakout",
        "No confirmed breakout — conditions not met",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_crypto_signal(
    symbol: str,
    price: float,
    ema: float,
    rsi: float,
    macd: float,
    macd_signal: float,
    bb_upper: float,
    bb_lower: float,
    volume: float,
    avg_volume: float,
    macd_hist: float = 0.0,
    macd_hist_prev: float = 0.0,
    bb_middle: float = 0.0,
    strategy: str = "confluence",
    has_position: bool = False,
) -> SignalResult:
    """Dispatch to the requested strategy and return a SignalResult."""
    if strategy == "confluence":
        return _strategy_confluence(
            symbol=symbol, price=price, ema=ema, rsi=rsi,
            macd=macd, macd_signal=macd_signal,
            macd_hist=macd_hist, macd_hist_prev=macd_hist_prev,
            volume=volume, avg_volume=avg_volume,
            has_position=has_position,
        )
    if strategy == "bb_bounce":
        return _strategy_bb_bounce(
            symbol=symbol, price=price, ema=ema, rsi=rsi,
            macd=macd, macd_signal=macd_signal,
            bb_upper=bb_upper, bb_middle=bb_middle, bb_lower=bb_lower,
            volume=volume, avg_volume=avg_volume,
            has_position=has_position,
        )
    if strategy == "breakout":
        return _strategy_breakout(
            symbol=symbol, price=price, ema=ema, rsi=rsi,
            macd=macd, macd_signal=macd_signal,
            bb_upper=bb_upper, bb_lower=bb_lower,
            volume=volume, avg_volume=avg_volume,
            has_position=has_position,
        )
    raise ValueError(f"Unknown strategy '{strategy}'. Choose: confluence | bb_bounce | breakout")
