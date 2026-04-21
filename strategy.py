"""Market data fetching, technical indicators, and signal generation.

generate_signal() now returns a SignalResult dataclass that contains all
information the REST API and the trading dashboard need (confidence,
trend, explanation, etc.) instead of a bare string.
"""

from __future__ import annotations

import json
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
from persistence import load_claude_cache, save_claude_cache_entry
from state import bot_state


Signal = Literal["BUY", "SELL", "HOLD"]

# 1-hour trend cache per symbol: {symbol: {"trend": str, "rsi": float, "ts": float}}
_htf_cache: dict[str, dict] = {}
_HTF_TTL = 1800  # refresh every 30 minutes


def _get_htf_trend(exchange, symbol: str) -> str:
    """Return 1-hour trend for symbol: 'up', 'down', or 'neutral'.

    Cached for 30 minutes — no point re-fetching on every 1-minute cycle.
    Falls back to 'neutral' (non-blocking) on any error.
    """
    import time as _t
    cached = _htf_cache.get(symbol, {})
    if cached and (_t.time() - cached.get("ts", 0)) < _HTF_TTL:
        return cached["trend"]

    try:
        if exchange.id == "alpaca":
            candles = _fetch_alpaca_candles(exchange, symbol, "1h", 60)
        else:
            candles = exchange.fetch_ohlcv(symbol, "1h", limit=60)

        if not candles or len(candles) < 14:
            return "neutral"

        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        df["rsi_1h"] = ta.momentum.RSIIndicator(close=df["close"], window=14).rsi()
        df["sma_1h"] = ta.trend.SMAIndicator(close=df["close"], window=20).sma_indicator()
        latest = df.dropna(subset=["rsi_1h", "sma_1h"]).iloc[-1]

        rsi_1h = float(latest["rsi_1h"])
        price_1h = float(latest["close"])
        sma_1h = float(latest["sma_1h"])

        if price_1h > sma_1h and rsi_1h > 45:
            trend = "up"
        elif price_1h < sma_1h and rsi_1h < 55:
            trend = "down"
        else:
            trend = "neutral"

        _htf_cache[symbol] = {"trend": trend, "rsi": rsi_1h, "ts": _t.time()}
        logging.info("HTF [%s] 1h trend=%s rsi=%.1f", symbol, trend, rsi_1h)
        return trend
    except Exception as exc:
        logging.warning("HTF fetch failed [%s]: %s — defaulting to neutral", symbol, exc)
        return "neutral"


# Per-symbol cache: stores last values sent to Claude to detect meaningful changes.
# Schema: {symbol: {"rsi", "price", "rule_signal", "claude_signal", "claude_confidence",
#                    "claude_reason", "called_at"}}
# Seeded from Supabase on import so server restarts don't cause a cold-start spike.
_last_claude_input: dict[str, dict] = load_claude_cache()
logging.info("Claude signal cache loaded: %d symbol(s)", len(_last_claude_input))


@dataclass
class SignalResult:
    action: Signal
    confidence: int       # 0-100
    rsi: float
    price: float
    sma: float
    atr: float            # ATR(14) — used for dynamic stop-loss in execution
    trend: str            # "Uptrend" | "Downtrend" | "Neutral"
    explanation: str
    rule_signal: Signal
    claude_signal: Signal
    claude_confidence: float  # 0.0-1.0 from Claude JSON response
    claude_reason: str        # Claude's reasoning text


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
    """Add RSI(14), SMA(50), ATR(14), and 20-bar average volume columns."""

    if "close" not in df.columns:
        raise ValueError("DataFrame must contain a close column")

    result = df.copy()
    result["rsi"] = ta.momentum.RSIIndicator(close=result["close"], window=14).rsi()
    result["sma_50"] = ta.trend.SMAIndicator(close=result["close"], window=50).sma_indicator()
    result["atr"] = ta.volatility.AverageTrueRange(
        high=result["high"], low=result["low"], close=result["close"], window=14
    ).average_true_range()
    result["vol_avg_20"] = result["volume"].rolling(window=20).mean()
    return result


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _rule_based_signal(rsi: float, price: float, sma: float,
                       oversold: float = 38.0, overbought: float = 70.0,
                       volume: float = 0.0, avg_volume: float = 0.0) -> Signal:
    # Volume confirmation: require at least 1.2x average volume for BUY/SELL.
    # Weak signals on low volume are likely noise — skip them.
    vol_confirmed = avg_volume <= 0 or volume >= avg_volume * 1.2

    # Setup A: Dip buy — RSI oversold with price near SMA support
    if rsi < oversold and price > sma * 0.99 and vol_confirmed:
        return "BUY"

    # Setup B: Momentum breakout — RSI rising in bullish zone, price above SMA
    if 50.0 <= rsi <= 65.0 and price > sma * 1.001 and vol_confirmed:
        return "BUY"

    if rsi > overbought and vol_confirmed:
        return "SELL"
    return "HOLD"


def _claude_signal(config: BotConfig, rsi: float, price: float, sma: float,
                   oversold: float = 30.0, overbought: float = 70.0,
                   symbol: str = "BTC/USD") -> tuple[Signal, float, str]:
    """Ask Claude for a structured JSON signal with confidence score.

    Returns (decision, confidence, reason). Confidence gate of 0.65 is applied
    in generate_signal() — low-confidence responses are treated as HOLD.
    Uses a cached system prompt (ephemeral cache_control) to avoid re-sending
    the static instruction block on every call.
    Uses claude-haiku-4-5 for cost efficiency (~3x cheaper than Sonnet).
    """

    if not config.anthropic_api_key:
        logging.warning("ANTHROPIC_API_KEY is missing; Claude signal defaults to HOLD")
        return "HOLD", 0.0, "No API key"

    client = Anthropic(api_key=config.anthropic_api_key)
    prompt = (
        f"{symbol} RSI is {rsi:.2f}, current price is ${price:,.2f}, "
        f"50-period SMA is ${sma:,.2f}. "
        f"Our strategy buys when RSI < {oversold} and price is near or above SMA (within 1%), "
        f"sells when RSI > {overbought}. "
        "Respond with a JSON object only — no markdown, no extra text."
    )

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=100,
        temperature=0,
        system=[
            {
                "type": "text",
                "text": (
                    "You are a crypto trading signal validator. "
                    "Given market indicators, respond with ONLY a JSON object with these exact fields: "
                    "\"decision\" (BUY, SELL, or HOLD), "
                    "\"confidence\" (float 0.0 to 1.0), "
                    "\"reason\" (one short sentence). "
                    "Example: {\"decision\": \"BUY\", \"confidence\": 0.78, \"reason\": \"Oversold RSI with price near SMA support\"}"
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()

    try:
        parsed = json.loads(text)
        raw_decision = str(parsed.get("decision", "HOLD")).strip().upper()
        confidence = float(parsed.get("confidence", 0.5))
        reason = str(parsed.get("reason", ""))
    except (json.JSONDecodeError, ValueError, TypeError):
        # Fallback: treat as plain-text word if JSON parse fails
        logging.warning("Claude returned non-JSON: %s", text[:80])
        raw_decision = text.upper()
        confidence = 0.5
        reason = text[:100]

    if "BUY" in raw_decision:
        signal: Signal = "BUY"
    elif "SELL" in raw_decision:
        signal = "SELL"
    else:
        signal = "HOLD"

    return signal, confidence, reason


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

def generate_signal(df: pd.DataFrame, config: BotConfig, symbol: str = None,
                    exchange=None) -> SignalResult:
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
    atr = float(latest["atr"]) if "atr" in latest and not pd.isna(latest["atr"]) else 0.0
    volume = float(latest["volume"]) if "volume" in latest else 0.0
    avg_volume = float(latest["vol_avg_20"]) if "vol_avg_20" in latest and not pd.isna(latest["vol_avg_20"]) else 0.0

    oversold = bot_state.settings.rsi_oversold
    overbought = bot_state.settings.rsi_overbought

    rule_signal = _rule_based_signal(rsi=rsi, price=price, sma=sma,
                                     oversold=oversold, overbought=overbought,
                                     volume=volume, avg_volume=avg_volume)

    # Multi-timeframe filter: block BUY when 1h trend is bearish.
    # SELL signals are not blocked — exits should always be allowed.
    if rule_signal == "BUY" and exchange is not None:
        htf = _get_htf_trend(exchange, symbol)
        if htf == "down":
            logging.info("MTF filter [%s]: 1h trend=down — BUY overridden to HOLD", symbol)
            rule_signal = "HOLD"

    claude_confidence = 0.0
    claude_reason = ""
    last = _last_claude_input.get(symbol, {})

    # --- Cost optimisation: skip Claude call when it won't change the outcome ---
    # 1. Rule is HOLD → Claude can't flip the final signal (requires agreement).
    # 2. Rule matches last cycle AND RSI/price barely moved → reuse cached response.
    _rsi_delta = abs(rsi - last.get("rsi", rsi + 999))
    _price_delta_pct = abs(price - last.get("price", 0)) / max(last.get("price", price), 1) * 100

    # Timestamp gate: if last real call was < 10 minutes ago with the same rule_signal, reuse.
    _called_at_str = last.get("called_at", "")
    _age_minutes = float("inf")
    if _called_at_str:
        try:
            _called_at = datetime.fromisoformat(_called_at_str.replace("Z", "+00:00"))
            _age_minutes = (datetime.now(timezone.utc) - _called_at).total_seconds() / 60
        except Exception:
            pass

    _reuse_cache = (
        last
        and last.get("rule_signal") == rule_signal
        and (
            _age_minutes < 10                              # called within last 10 min
            or (_rsi_delta < 2.0 and _price_delta_pct < 0.3)  # or conditions barely moved
        )
    )

    if rule_signal == "HOLD":
        # No point asking Claude — final can only be HOLD regardless.
        claude_signal: Signal = "HOLD"
        claude_reason = "Skipped (rule=HOLD)"
        logging.debug("Claude skipped for %s — rule signal is HOLD", symbol)
    elif _reuse_cache:
        # Conditions barely changed; reuse the last Claude response.
        claude_signal = last.get("claude_signal", "HOLD")
        claude_confidence = last.get("claude_confidence", 0.0)
        claude_reason = last.get("claude_reason", "") + " [cached]"
        logging.debug(
            "Claude reused cache for %s — RSI Δ=%.2f price Δ=%.2f%%",
            symbol, _rsi_delta, _price_delta_pct,
        )
    else:
        try:
            claude_signal, claude_confidence, claude_reason = _claude_signal(
                config=config, rsi=rsi, price=price, sma=sma,
                oversold=oversold, overbought=overbought, symbol=symbol,
            )
            # Require confidence >= 0.55 — low-confidence responses count as HOLD.
            if claude_confidence < 0.55 and claude_signal != "HOLD":
                logging.info(
                    "Claude signal %s overridden to HOLD — confidence %.2f < 0.65 | reason: %s",
                    claude_signal, claude_confidence, claude_reason,
                )
                claude_signal = "HOLD"
            # Update in-memory and Supabase cache after a real API call.
            _now = datetime.now(timezone.utc).isoformat()
            _last_claude_input[symbol] = {
                "rsi": rsi,
                "price": price,
                "rule_signal": rule_signal,
                "claude_signal": claude_signal,
                "claude_confidence": claude_confidence,
                "claude_reason": claude_reason,
                "called_at": _now,
            }
            save_claude_cache_entry(
                symbol=symbol, rsi=rsi, price=price,
                rule_signal=rule_signal, claude_signal=claude_signal,
                claude_confidence=claude_confidence, claude_reason=claude_reason,
            )
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
        "Signal | price=%s rsi=%s sma_50=%s rule=%s claude=%s(conf=%.2f) final=%s confidence=%s%%",
        Decimal(str(round(price, 2))),
        Decimal(str(round(rsi, 2))),
        Decimal(str(round(sma, 2))),
        rule_signal,
        claude_signal,
        claude_confidence,
        final_action,
        confidence,
    )
    if claude_reason:
        logging.info("Claude reasoning: %s", claude_reason)

    result = SignalResult(
        action=final_action,
        confidence=confidence,
        rsi=rsi,
        price=price,
        sma=sma,
        atr=atr,
        trend=trend,
        explanation=explanation,
        rule_signal=rule_signal,
        claude_signal=claude_signal,
        claude_confidence=claude_confidence,
        claude_reason=claude_reason,
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
        claude_confidence=result.claude_confidence,
        claude_reason=result.claude_reason,
    )

    tone = "positive" if final_action == "BUY" else "negative" if final_action == "SELL" else "neutral"
    bot_state.add_log(
        "Signal generated",
        f"{final_action} (rule={rule_signal}, claude={claude_signal}, confidence={confidence}%)",
        tone=tone,
    )

    return result
