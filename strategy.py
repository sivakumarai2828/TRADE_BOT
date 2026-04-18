"""Market data fetching, technical indicators, and signal generation.

generate_signal() now returns a SignalResult dataclass that contains all
information the REST API and the trading dashboard need (confidence,
trend, explanation, etc.) instead of a bare string.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import pandas as pd
import requests
import ta
from anthropic import Anthropic

from config import BotConfig
from state import bot_state


Signal = Literal["BUY", "SELL", "HOLD"]


@dataclass
class SignalResult:
    action: Signal
    confidence: int       # 0-100
    rsi: float
    price: float
    sma: float
    trend: str            # "Uptrend" | "Downtrend" | "Neutral"
    explanation: str
    rule_signal: Signal
    claude_signal: Signal


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

_ALPACA_TF_MAP = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "1h": "1Hour", "1d": "1Day"}


def _fetch_alpaca_candles(exchange, symbol: str, timeframe: str, limit: int) -> list:
    """Fetch candles directly from Alpaca data API using explicit date range."""
    tf = _ALPACA_TF_MAP.get(timeframe, "5Min")
    end = datetime.now(timezone.utc)
    minutes = limit * int(tf.replace("Min", "").replace("Hour", "60").replace("Day", "1440"))
    start = end - timedelta(minutes=minutes + 60)
    resp = requests.get(
        "https://data.alpaca.markets/v1beta3/crypto/us/bars",
        params={"symbols": symbol, "timeframe": tf, "limit": limit,
                "start": start.isoformat(), "end": end.isoformat()},
        headers={"APCA-API-KEY-ID": exchange.apiKey, "APCA-API-SECRET-KEY": exchange.secret},
        timeout=10,
    )
    resp.raise_for_status()
    bars = resp.json().get("bars", {}).get(symbol, [])
    return [[int(pd.Timestamp(b["t"]).timestamp() * 1000),
             b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]


def get_market_data(
    exchange,
    symbol: str,
    timeframe: str = "1m",
    limit: int = 100,
) -> pd.DataFrame:
    """Fetch recent OHLCV candles and return them as a pandas DataFrame."""

    if exchange.id == "alpaca":
        candles = _fetch_alpaca_candles(exchange, symbol, timeframe, limit)
    else:
        candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    if not candles:
        raise RuntimeError(f"No market data returned for {symbol}")

    frame = pd.DataFrame(
        candles,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)

    numeric_columns = ["open", "high", "low", "close", "volume"]
    frame[numeric_columns] = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=numeric_columns)

    if len(frame) < 50:
        raise RuntimeError(f"Need at least 50 valid candles, got {len(frame)}")

    return frame


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI(14) and SMA(50) columns to the DataFrame."""

    if "close" not in df.columns:
        raise ValueError("DataFrame must contain a close column")

    result = df.copy()
    result["rsi"] = ta.momentum.RSIIndicator(close=result["close"], window=14).rsi()
    result["sma_50"] = ta.trend.SMAIndicator(close=result["close"], window=50).sma_indicator()
    return result


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _rule_based_signal(rsi: float, price: float, sma: float,
                       oversold: float = 30.0, overbought: float = 70.0) -> Signal:
    # Allow price within 0.1% below SMA to handle minor fluctuations.
    if rsi < oversold and price > sma * 0.999:
        return "BUY"
    if rsi > overbought:
        return "SELL"
    return "HOLD"


def _claude_signal(config: BotConfig, rsi: float, price: float, sma: float,
                   oversold: float = 30.0, overbought: float = 70.0) -> Signal:
    """Ask Claude for a strict BUY/SELL/HOLD answer.

    Uses a cached system prompt (ephemeral cache_control) to avoid re-sending
    the static instruction block on every call.
    """

    if not config.anthropic_api_key:
        logging.warning("ANTHROPIC_API_KEY is missing; Claude signal defaults to HOLD")
        return "HOLD"

    client = Anthropic(api_key=config.anthropic_api_key)
    prompt = (
        f"BTC RSI is {rsi:.2f}, current price is ${price:,.2f}, "
        f"50-period SMA is ${sma:,.2f}. "
        f"Our strategy buys when RSI < {oversold} and price > SMA, "
        f"sells when RSI > {overbought}. "
        "Should I BUY, SELL, or HOLD?"
    )

    response = client.messages.create(
        model=config.anthropic_model,
        max_tokens=16,
        temperature=0,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a crypto trading signal validator. "
                    "Given RSI, price, and SMA values, respond with exactly ONE word: "
                    "BUY, SELL, or HOLD. No punctuation, no explanation, nothing else."
                ),
                # Cache this static system block — avoids re-tokenising on every tick.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    decision = text.strip().upper()

    if "BUY" in decision:
        return "BUY"
    if "SELL" in decision:
        return "SELL"
    return "HOLD"


def _compute_confidence(action: Signal, rsi: float) -> int:
    """Scale confidence 70-100 for actionable signals based on RSI distance."""
    if action == "BUY":
        # Deeper below 30 → more confident (max at rsi=0).
        return min(100, int(70 + (30 - rsi) / 30 * 30))
    if action == "SELL":
        # Higher above 70 → more confident (max at rsi=100).
        return min(100, int(70 + (rsi - 70) / 30 * 30))
    return 0


def _compute_trend(price: float, sma: float) -> str:
    if price > sma * 1.001:
        return "Uptrend"
    if price < sma * 0.999:
        return "Downtrend"
    return "Neutral"


def _build_explanation(
    action: Signal,
    rule_signal: Signal,
    claude_signal: Signal,
    rsi: float,
    price: float,
    sma: float,
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> str:
    if action == "BUY":
        return (
            f"RSI {rsi:.1f} is below the oversold threshold ({oversold}) and price "
            f"${price:,.0f} is above the 50 SMA (${sma:,.0f}). "
            "Rule engine and Claude both agree on a long entry."
        )
    if action == "SELL":
        return (
            f"RSI {rsi:.1f} is above the overbought threshold ({overbought}). "
            "Rule engine and Claude both agree this is a good exit point."
        )
    if rule_signal != claude_signal:
        return (
            f"Rule engine says {rule_signal} but Claude suggests {claude_signal}. "
            "Conflicting signals — holding to avoid a low-confidence trade."
        )
    return (
        f"RSI {rsi:.1f} is in neutral territory ({oversold}–{overbought}) with price near the SMA. "
        "No clear entry or exit condition met."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_signal(df: pd.DataFrame, config: BotConfig, symbol: str = None) -> SignalResult:
    """Generate the final trading signal for the given symbol.

    A trade signal is actionable only when the rule-based strategy and Claude
    agree. Disagreement returns HOLD by design.

    Side-effect: writes the result into bot_state so the API can serve it.
    """
    symbol = symbol or config.symbol

    latest = df.dropna(subset=["rsi", "sma_50"]).iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma = float(latest["sma_50"])

    oversold = bot_state.settings.rsi_oversold
    overbought = bot_state.settings.rsi_overbought

    rule_signal = _rule_based_signal(rsi=rsi, price=price, sma=sma,
                                     oversold=oversold, overbought=overbought)

    try:
        claude_signal = _claude_signal(config=config, rsi=rsi, price=price, sma=sma,
                                       oversold=oversold, overbought=overbought)
    except Exception as exc:
        logging.exception("Claude decision failed; final signal forced to HOLD: %s", exc)
        claude_signal = "HOLD"
        bot_state.add_log("Claude error", str(exc)[:120], tone="negative")

    final_action: Signal = rule_signal if rule_signal == claude_signal else "HOLD"
    confidence = _compute_confidence(final_action, rsi)
    trend = _compute_trend(price, sma)
    explanation = _build_explanation(final_action, rule_signal, claude_signal, rsi, price, sma,
                                      oversold=oversold, overbought=overbought)

    logging.info(
        "Signal | price=%s rsi=%s sma_50=%s rule=%s claude=%s final=%s confidence=%s%%",
        Decimal(str(round(price, 2))),
        Decimal(str(round(rsi, 2))),
        Decimal(str(round(sma, 2))),
        rule_signal,
        claude_signal,
        final_action,
        confidence,
    )

    result = SignalResult(
        action=final_action,
        confidence=confidence,
        rsi=rsi,
        price=price,
        sma=sma,
        trend=trend,
        explanation=explanation,
        rule_signal=rule_signal,
        claude_signal=claude_signal,
    )

    # Persist to shared state so the API can read it immediately.
    bot_state.update_signal(
        symbol=symbol,
        action=result.action,
        confidence=result.confidence,
        rsi=result.rsi,
        price=result.price,
        sma=result.sma,
        trend=result.trend,
        explanation=result.explanation,
        rule_signal=result.rule_signal,
        claude_signal=result.claude_signal,
    )

    tone = "positive" if final_action == "BUY" else "negative" if final_action == "SELL" else "neutral"
    bot_state.add_log(
        "Signal generated",
        f"{final_action} (rule={rule_signal}, claude={claude_signal}, confidence={confidence}%)",
        tone=tone,
    )

    return result
