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

# BTC 4h EMA200 macro regime cache (single global — BTC drives all alts)
_btc_regime_cache: dict = {}
_BTC_REGIME_TTL = 7200  # refresh every 2 hours

# Claude availability tracker — block BUY entries when API repeatedly overloaded.
# Resets to 0 on any successful Claude call.
_claude_consecutive_failures: int = 0
_CLAUDE_FAILURE_THRESHOLD: int = 3  # block BUY after this many consecutive failures


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
        elif price_1h < sma_1h and rsi_1h < 42:
            # Only "down" when genuinely bearish — RSI<42 means actual selling pressure.
            # rsi<55 was too broad (fires in neutral market, blocks all bounces).
            trend = "down"
        else:
            trend = "neutral"

        _htf_cache[symbol] = {"trend": trend, "rsi": rsi_1h, "ts": _t.time()}
        logging.info("HTF [%s] 1h trend=%s rsi=%.1f", symbol, trend, rsi_1h)
        return trend
    except Exception as exc:
        logging.warning("HTF fetch failed [%s]: %s — defaulting to neutral", symbol, exc)
        return "neutral"


def _get_btc_regime() -> str:
    """BTC 4h EMA200 macro regime via yfinance: 'bull', 'bear', or 'neutral'.

    Cached 2h — 4h candles change slowly.
    bull  = BTC price > EMA200 × 1.01  → full trading allowed
    bear  = BTC price < EMA200 × 0.99  → block all new longs
    neutral = within 1% band          → allow dip-buys only
    Falls back to 'neutral' (non-blocking) on any error.
    """
    import time as _t
    if _btc_regime_cache.get("ts") and (_t.time() - _btc_regime_cache["ts"]) < _BTC_REGIME_TTL:
        return _btc_regime_cache["regime"]

    try:
        import yfinance as _yf
        df = _yf.download("BTC-USD", period="60d", interval="4h", progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return "neutral"

        # Flatten MultiIndex columns (yfinance ≥0.2.38)
        if hasattr(df.columns, "levels"):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        close = df["Close"].squeeze()
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        price = float(close.iloc[-1])

        if price > ema200 * 1.01:
            regime = "bull"
        elif price < ema200 * 0.99:
            regime = "bear"
        else:
            regime = "neutral"

        _btc_regime_cache.update({"regime": regime, "ts": _t.time(),
                                  "price": price, "ema200": ema200})
        logging.info("BTC 4h regime: %s (price=%.0f EMA200=%.0f)", regime, price, ema200)
        return regime
    except Exception as exc:
        logging.warning("BTC regime check failed: %s — defaulting to neutral", exc)
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

def _calculate_macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram — pure pandas, no ta library."""
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI(14), SMA(50), ATR(14), MACD(12,26,9), and 20-bar average volume columns."""

    if "close" not in df.columns:
        raise ValueError("DataFrame must contain a close column")

    result = df.copy()
    result["rsi"] = ta.momentum.RSIIndicator(close=result["close"], window=14).rsi()
    result["sma_50"] = ta.trend.SMAIndicator(close=result["close"], window=50).sma_indicator()
    result["atr"] = ta.volatility.AverageTrueRange(
        high=result["high"], low=result["low"], close=result["close"], window=14
    ).average_true_range()
    result["vol_avg_20"] = result["volume"].rolling(window=20).mean()
    macd_line, macd_sig, macd_hist = _calculate_macd(result["close"])
    result["macd"] = macd_line
    result["macd_signal"] = macd_sig
    result["macd_hist"] = macd_hist
    return result


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _rule_based_signal(rsi: float, price: float, sma: float,
                       oversold: float = 43.0, overbought: float = 70.0,
                       volume: float = 0.0, avg_volume: float = 0.0,
                       allow_breakout: bool = True,
                       macd_hist: float = 0.0) -> Signal:
    import datetime as _dt
    _hour_utc = _dt.datetime.now(_dt.timezone.utc).hour
    _is_overnight = 0 <= _hour_utc < 6  # low vol window UTC 00-06
    _vol_mult = 1.5 if _is_overnight else 2.0  # relaxed threshold overnight

    # Volume confirmation: 2× avg (1.5× overnight to handle thin crypto hours)
    vol_confirmed = avg_volume <= 0 or volume >= avg_volume * _vol_mult

    # MACD confirmation: histogram > 0 means short-term momentum turning bullish.
    # Guards Setup C (recovery) only — Setup A (deep dip) relies on RSI+volume alone.
    # Deep dips: MACD lags too much, RSI<oversold is already strong enough signal.
    macd_confirmed = macd_hist > 0

    # Setup A: Dip buy — RSI oversold, above SMA support, volume confirmed.
    # RSI floor at 35 prevents catching falling knives (RSI<35 = crash, not dip).
    # No MACD gate — at RSI 35-45 MACD still negative (lags), would block all dip entries.
    if 35.0 <= rsi < oversold and price > sma * 0.99 and vol_confirmed:
        return "BUY"

    # Setup C: Recovery — RSI emerging from oversold zone, price holding near SMA, MACD turning up.
    if oversold <= rsi <= 50.0 and price > sma * 0.98 and macd_confirmed:
        return "BUY"

    # Setup B: Momentum breakout — gated by allow_breakout (disabled in SAFE/SHIELD mode).
    if allow_breakout and 50.0 <= rsi <= 65.0 and price > sma * 1.001:
        return "BUY"

    # SELL: RSI overbought — no volume gate (overbought tops often have lower volume).
    if rsi > overbought:
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

    client = Anthropic(api_key=config.anthropic_api_key, timeout=20.0, max_retries=1)
    prompt = (
        f"{symbol} RSI is {rsi:.2f}, current price is ${price:,.2f}, "
        f"50-period SMA is ${sma:,.2f}. "
        f"Our strategy has three BUY setups: "
        f"(A) dip-buy: RSI < {oversold} and price > SMA×0.99; "
        f"(B) momentum breakout: RSI 50–65 and price > SMA×1.001; "
        f"(C) recovery bounce: RSI {oversold}–50 and price > SMA×0.98. "
        f"SELL when RSI > {overbought}. "
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
    # Strip markdown code fences Claude sometimes adds despite instructions
    if text.startswith("```"):
        text = text.split("```")[-2] if text.count("```") >= 2 else text
        text = text.lstrip("json").strip()

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
    global _claude_consecutive_failures
    symbol = symbol or config.symbol

    latest = df.dropna(subset=["rsi", "sma_50"]).iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi"])
    sma = float(latest["sma_50"])
    atr = float(latest["atr"]) if "atr" in latest and not pd.isna(latest["atr"]) else 0.0
    volume = float(latest["volume"]) if "volume" in latest else 0.0
    avg_volume = float(latest["vol_avg_20"]) if "vol_avg_20" in latest and not pd.isna(latest["vol_avg_20"]) else 0.0
    macd_hist = float(latest["macd_hist"]) if "macd_hist" in latest.index and not pd.isna(latest["macd_hist"]) else 0.0

    oversold = bot_state.settings.rsi_oversold
    overbought = bot_state.settings.rsi_overbought

    # Fetch 1h trend early — used for both breakout permission AND MTF filter below.
    # Cached 30 min so no extra API cost fetching here vs later.
    _htf_early = _get_htf_trend(exchange, symbol) if exchange is not None else "neutral"

    # Check mode manager for breakout permission (SAFE/SHIELD block Setup B).
    # Override: always allow breakout when 1h trend is confirmed uptrend — quality entry.
    _allow_breakout = True
    try:
        from api import _crypto_mode_manager
        if _crypto_mode_manager is not None:
            _allow_breakout = _crypto_mode_manager.params().allow_breakout
    except Exception:
        pass
    if not _allow_breakout and _htf_early == "up":
        _allow_breakout = True  # uptrend confirmed — Setup B has edge even in SAFE mode
        logging.info("Breakout override [%s]: 1h uptrend — Setup B allowed despite SAFE mode", symbol)

    rule_signal = _rule_based_signal(rsi=rsi, price=price, sma=sma,
                                     oversold=oversold, overbought=overbought,
                                     volume=volume, avg_volume=avg_volume,
                                     allow_breakout=_allow_breakout,
                                     macd_hist=macd_hist)

    # Claude availability gate — block BUY when AI has failed repeatedly.
    # Prevents bad entries when the AI confirmation layer is down (e.g. 529 overload).
    if rule_signal == "BUY" and _claude_consecutive_failures >= _CLAUDE_FAILURE_THRESHOLD:
        logging.warning(
            "Claude unavailable (%d consecutive failures) — BUY blocked for %s [AI gate closed]",
            _claude_consecutive_failures, symbol,
        )
        bot_state.add_log(
            "AI gate closed",
            f"Claude failed {_claude_consecutive_failures}x — no new BUY until API recovers",
            tone="neutral",
        )
        rule_signal = "HOLD"

    # BTC 4h EMA200 macro regime — block ALL new longs in bear market.
    # Bear = BTC price > 1% below 4h EMA200. Neutral/bull = allow.
    if rule_signal == "BUY":
        btc_regime = _get_btc_regime()
        if btc_regime == "bear":
            logging.info("BTC regime BEAR [%s] RSI=%.1f — BUY blocked (macro downtrend)", symbol, rsi)
            rule_signal = "HOLD"

    # Multi-timeframe filter: selectively block BUYs when 1h trend is bearish.
    # Rules:
    #   Setup B (breakout, RSI 50-65) — ALWAYS blocked in downtrend (fighting trend = bad)
    #   Setup A (deep dip, RSI < oversold) — ALLOWED in downtrend (bounce trades off extreme lows)
    #   Setup C (recovery, RSI 40-50) — ALLOWED in downtrend (RSI emerging = potential reversal)
    # SELL signals never blocked — exits always allowed.
    if rule_signal == "BUY" and exchange is not None:
        htf = _htf_early  # already fetched above — reuse cached result
        if htf == "down":
            # Confirmed downtrend — block ALL new longs (catching knives).
            logging.info("MTF filter [%s]: 1h downtrend RSI=%.1f — BUY blocked", symbol, rsi)
            rule_signal = "HOLD"
        elif htf == "neutral":
            # Neutral trend: allow Setup A (deep dip RSI<oversold) — bounces happen in sideways.
            # Block Setup B (breakout) and Setup C (recovery) — no trend confirmation.
            if rsi >= oversold:
                logging.info("MTF filter [%s]: 1h neutral RSI=%.1f — BUY blocked (no uptrend for C/B)", symbol, rsi)
                rule_signal = "HOLD"
            else:
                logging.info("MTF filter [%s]: 1h neutral RSI=%.1f — deep dip BUY allowed", symbol, rsi)
        else:
            logging.info("MTF filter [%s]: 1h uptrend RSI=%.1f — BUY allowed", symbol, rsi)

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
            # Successful call — reset failure counter.
            _claude_consecutive_failures = 0
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
            _claude_consecutive_failures += 1
            logging.exception("Claude decision failed (%d consecutive); final signal forced to HOLD: %s",
                              _claude_consecutive_failures, exc)
            claude_signal = "HOLD"
            bot_state.add_log("Claude error", str(exc)[:120], tone="negative")

    # Rule is primary. Claude is advisory:
    #   - Rule=HOLD → always HOLD regardless
    #   - Both agree → use that signal
    #   - Claude=HOLD (uncertain) → trust the rule
    #   - Claude=opposite with confidence >0.70 → strong veto, HOLD
    #   - Claude=opposite with confidence <=0.70 → weak disagreement, trust rule
    if rule_signal == "HOLD":
        final_action: Signal = "HOLD"
    elif claude_signal == rule_signal:
        final_action = rule_signal
    elif claude_signal == "HOLD":
        final_action = rule_signal  # Claude uncertain — rule wins
        logging.info("Claude uncertain (HOLD) for %s rule=%s — proceeding with rule", symbol, rule_signal)
    elif claude_confidence > 0.70:
        final_action = "HOLD"  # Claude strongly disagrees — veto
        logging.info("Claude strong veto for %s (conf=%.2f) rule=%s claude=%s — HOLD", symbol, claude_confidence, rule_signal, claude_signal)
    else:
        final_action = rule_signal  # Weak disagreement — rule wins
        logging.info("Claude weak disagreement for %s (conf=%.2f) — proceeding with rule %s", symbol, claude_confidence, rule_signal)
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
