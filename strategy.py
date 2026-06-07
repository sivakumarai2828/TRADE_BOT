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
    """BTC 4h EMA50 macro regime via yfinance: 'bull', 'bear', or 'neutral'.

    Cached 2h — 4h candles change slowly.
    bull    = BTC price > EMA50 × 1.02  → full trading allowed
    bear    = BTC price < EMA50 × 0.97  → block all new longs
    neutral = within 2% band           → allow dip-buys only (Setup A/C)

    EMA50 (not EMA200) used — captures current ~2-week macro trend without
    distortion from peaks/crashes 2+ months ago. Falls back to 'neutral' on error.
    """
    import time as _t
    if _btc_regime_cache.get("ts") and (_t.time() - _btc_regime_cache["ts"]) < _BTC_REGIME_TTL:
        return _btc_regime_cache["regime"]

    try:
        import yfinance as _yf
        df = _yf.download("BTC-USD", period="30d", interval="4h", progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return "neutral"

        # Flatten MultiIndex columns (yfinance ≥0.2.38)
        if hasattr(df.columns, "levels"):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        close = df["Close"].squeeze()
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        price = float(close.iloc[-1])

        if price > ema50 * 1.02:
            regime = "bull"
        elif price < ema50 * 0.97:
            regime = "bear"
        else:
            regime = "neutral"

        _btc_regime_cache.update({"regime": regime, "ts": _t.time(),
                                  "price": price, "ema50": ema50})
        logging.info("BTC 4h regime: %s (price=%.0f EMA50=%.0f)", regime, price, ema50)
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
    """Add RSI(14), SMA(50), ATR(14), MACD(12,26,9), ADX(14), and 20-bar average volume columns."""

    if "close" not in df.columns:
        raise ValueError("DataFrame must contain a close column")

    result = df.copy()
    result["rsi"] = ta.momentum.RSIIndicator(close=result["close"], window=14).rsi()
    result["sma_20"] = ta.trend.SMAIndicator(close=result["close"], window=20).sma_indicator()
    result["sma_50"] = ta.trend.SMAIndicator(close=result["close"], window=50).sma_indicator()
    result["atr"] = ta.volatility.AverageTrueRange(
        high=result["high"], low=result["low"], close=result["close"], window=14
    ).average_true_range()
    result["vol_avg_20"] = result["volume"].rolling(window=20).mean()
    macd_line, macd_sig, macd_hist = _calculate_macd(result["close"])
    result["macd"] = macd_line
    result["macd_signal"] = macd_sig
    result["macd_hist"] = macd_hist
    # ADX(14) — measures trend strength. >20 = trending, <20 = ranging/choppy.
    adx_ind = ta.trend.ADXIndicator(
        high=result["high"], low=result["low"], close=result["close"], window=14
    )
    result["adx"] = adx_ind.adx()
    return result


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _rule_based_signal(rsi: float, price: float, sma: float,
                       oversold: float = 38.0, overbought: float = 70.0,
                       volume: float = 0.0, avg_volume: float = 0.0,
                       allow_breakout: bool = True,
                       macd_hist: float = 0.0,
                       adx: float = 0.0) -> Signal:
    import datetime as _dt
    _hour_utc = _dt.datetime.now(_dt.timezone.utc).hour
    # Dead zone: 22:00–06:00 UTC — Asia low-volume hours, worst signal quality.
    # Setup A (deep dip, RSI<oversold) stays active 24/7 — panic dips happen any hour.
    # Setup B and C blocked in dead zone — no momentum/recovery trades in thin market.
    _is_dead_zone = _hour_utc >= 22 or _hour_utc < 6
    _vol_mult = 1.5 if _is_dead_zone else 2.0  # relaxed volume threshold in thin hours

    # Volume confirmation: 2× avg (1.5× in thin hours)
    vol_confirmed = avg_volume <= 0 or volume >= avg_volume * _vol_mult

    # MACD confirmation: histogram > 0 means short-term momentum turning bullish.
    # Guards Setup C (recovery) only — Setup A (deep dip) relies on RSI+volume alone.
    # Deep dips: MACD lags too much, RSI<oversold is already strong enough signal.
    macd_confirmed = macd_hist > 0

    # ADX gate: ADX < 20 = ranging/choppy market, momentum/recovery trades fail.
    # Setup A (deep dip) allowed even in ranging markets — panic dips are real regardless.
    # Setup B and C need trend energy (ADX ≥ 20) to produce follow-through.
    adx_trending = adx <= 0 or adx >= 20.0  # adx=0 means not computed — allow through

    # Setup A: Dip buy — RSI oversold, above SMA support, volume confirmed.
    # RSI floor at 35 prevents catching falling knives (RSI<35 = crash, not dip).
    # No MACD, ADX, or session gate — deep dips valid 24/7 in any regime.
    if 35.0 <= rsi < oversold and price > sma * 0.99 and vol_confirmed:
        return "BUY"

    # Setup C: Recovery — RSI emerging from oversold zone, price holding near SMA, MACD turning up.
    # Blocked in dead zone (22–06 UTC) and ranging market (ADX < 20).
    if not _is_dead_zone and adx_trending and oversold <= rsi <= 50.0 and price > sma * 0.98 and macd_confirmed:
        return "BUY"

    # Setup B: Momentum breakout — gated by allow_breakout (disabled in SAFE/SHIELD mode).
    # Blocked in dead zone (22–06 UTC) and ranging market (ADX < 20).
    if not _is_dead_zone and adx_trending and allow_breakout and 50.0 <= rsi <= 65.0 and price > sma * 1.001:
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
    sma_20 = float(latest["sma_20"]) if "sma_20" in latest.index and not pd.isna(latest["sma_20"]) else 0.0
    atr = float(latest["atr"]) if "atr" in latest and not pd.isna(latest["atr"]) else 0.0
    volume = float(latest["volume"]) if "volume" in latest else 0.0
    avg_volume = float(latest["vol_avg_20"]) if "vol_avg_20" in latest and not pd.isna(latest["vol_avg_20"]) else 0.0
    macd_hist = float(latest["macd_hist"]) if "macd_hist" in latest.index and not pd.isna(latest["macd_hist"]) else 0.0
    adx = float(latest["adx"]) if "adx" in latest.index and not pd.isna(latest["adx"]) else 0.0

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
    # SAFE mode only: price > SMA can override the breakout block (momentum confirmed).
    # SHIELD mode: NO override — SHIELD is emergency, breakouts always blocked.
    _mode = "SAFE"
    try:
        from api import _crypto_mode_manager
        if _crypto_mode_manager is not None:
            _mode = _crypto_mode_manager.mode
    except Exception:
        pass
    if not _allow_breakout and _mode != "SHIELD" and price > sma * 1.001:
        _allow_breakout = True
        logging.info("Breakout override [%s]: price %.4f > SMA*1.001 — Setup B allowed (SAFE mode only)", symbol, price)

    rule_signal = _rule_based_signal(rsi=rsi, price=price, sma=sma,
                                     oversold=oversold, overbought=overbought,
                                     volume=volume, avg_volume=avg_volume,
                                     allow_breakout=_allow_breakout,
                                     macd_hist=macd_hist,
                                     adx=adx)

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

    # BTC 4h macro regime gate:
    #   bear    → block ALL longs on all symbols
    #   neutral → block altcoin longs entirely
    #             block BTC Setup C (recovery RSI 38-50) — only deep dip (Setup A, RSI<38) allowed
    #             Rationale: Setup C in ranging market = buying a bounce that reverses. All May 27-28
    #             losses were Setup C entries in NEUTRAL. Deep dips (Setup A) still valid.
    #   bull    → allow all symbols, all setups
    if rule_signal == "BUY":
        btc_regime = _get_btc_regime()
        if btc_regime == "bear":
            logging.info("BTC regime BEAR [%s] RSI=%.1f — BUY blocked (macro downtrend)", symbol, rsi)
            rule_signal = "HOLD"
        elif btc_regime == "neutral":
            if symbol not in ("BTC/USD", "BTC/USDT", "BTCUSD"):
                if rsi >= 30.0:
                    # Allow only extreme panic dips (RSI<30) on alts in neutral BTC market.
                    # Mid-range alts in choppy BTC = noise entries. But RSI<30 panic = real reversal.
                    logging.info("BTC regime NEUTRAL [%s] RSI=%.1f — altcoin BUY blocked (need RSI<30 extreme dip)", symbol, rsi)
                    rule_signal = "HOLD"
                else:
                    logging.info("BTC regime NEUTRAL [%s] RSI=%.1f < 30 — extreme panic dip allowed in choppy market", symbol, rsi)
            elif rsi >= oversold:
                # NEUTRAL + BTC + RSI >= oversold threshold → Setup C or B territory.
                # Only Setup A (deep dip, RSI < oversold) allowed in ranging market.
                # Setup C (recovery bounce RSI 38-50) in neutral = buying mid-range in a chop = losing trade.
                logging.info(
                    "BTC regime NEUTRAL [BTC] RSI=%.1f — only deep-dip (RSI<%.0f) allowed in choppy market, blocking Setup C/B",
                    rsi, oversold,
                )
                rule_signal = "HOLD"

    # Per-symbol trend regime — SMA20 vs SMA50 cross.
    # SMA20 < SMA50 = short-term momentum below long-term = local downtrend.
    # Block Setup B breakouts + Setup C recoveries in downtrend (only allow Setup A deep dips).
    # Avoids entering trend-following trades against the current symbol's own momentum.
    if rule_signal == "BUY" and sma_20 > 0 and sma > 0:
        if sma_20 < sma * 0.995:  # SMA20 clearly below SMA50 (0.5% gap = confirmed cross)
            if rsi >= oversold:    # RSI not deep oversold — allow Setup A bounce off extreme lows
                logging.info(
                    "Symbol regime [%s]: SMA20(%.2f) < SMA50(%.2f) — local downtrend, BUY blocked (RSI=%.1f)",
                    symbol, sma_20, sma, rsi,
                )
                rule_signal = "HOLD"

    # Multi-timeframe filter: selectively block BUYs when 1h trend is bearish.
    # Rules:
    #   1h downtrend : RSI < 30 only (extreme panic dip)
    #   1h neutral   : RSI < 35 only (deep dip, not mid-range noise)
    #   1h uptrend   : RSI < 42 only (still need real oversold, not RSI 42-46 mid-range)
    #   Setup B breakout in neutral: price > SMA × 1.001 still allowed (momentum confirmed)
    # Root cause: rsi_oversold=45 + old MTF floors let bot buy at RSI 38-45 in downtrend.
    # Fix: lower all floors so only genuinely oversold entries are allowed.
    # SELL signals never blocked — exits always allowed.
    if rule_signal == "BUY" and exchange is not None:
        htf = _htf_early  # already fetched above — reuse cached result
        if htf == "down":
            # Confirmed 1h downtrend — only extreme panic dips (RSI < 30).
            if rsi >= 30.0:
                logging.info("MTF filter [%s]: 1h downtrend RSI=%.1f ≥ 30 — BUY blocked (not extreme enough)", symbol, rsi)
                rule_signal = "HOLD"
            else:
                logging.info("MTF filter [%s]: 1h downtrend RSI=%.1f < 30 — extreme panic dip BUY allowed", symbol, rsi)
        elif htf == "neutral":
            # Neutral/choppy market:
            # Allow Setup B breakout (RSI 50+ with price above SMA — momentum there)
            # Require RSI < 35 for dip buys (RSI 35-45 in chop = noise, not real dip)
            if rsi >= oversold and price > sma * 1.001:
                logging.info("MTF filter [%s]: 1h neutral RSI=%.1f price above SMA — Setup B breakout allowed", symbol, rsi)
            elif rsi >= 35.0:
                logging.info("MTF filter [%s]: 1h neutral RSI=%.1f ≥ 35 (chop) — BUY blocked (tighter floor)", symbol, rsi)
                rule_signal = "HOLD"
            else:
                logging.info("MTF filter [%s]: 1h neutral RSI=%.1f < 35 — deep dip BUY allowed", symbol, rsi)
        else:
            # 1h uptrend — still require RSI < 42 (RSI 42-46 in mid-range = not a real dip)
            if rsi >= 42.0 and rsi < oversold:
                logging.info("MTF filter [%s]: 1h uptrend RSI=%.1f ≥ 42 (mid-range) — BUY blocked (not oversold enough)", symbol, rsi)
                rule_signal = "HOLD"
            else:
                logging.info("MTF filter [%s]: 1h uptrend RSI=%.1f — BUY allowed", symbol, rsi)

    # Volume gate — Setup B breakout (RSI 50-65) requires real volume confirmation.
    # Deep dip Setup A (RSI < 35) allowed even on low volume — panic dips dry up volume first.
    # Using hardcoded 35 floor so extreme dips always pass regardless of oversold setting.
    if rule_signal == "BUY" and rsi >= 35 and avg_volume > 0:
        if volume < avg_volume * 1.5:
            logging.info("Volume gate [%s]: vol=%.0f < 1.5×avg=%.0f — breakout not confirmed, HOLD",
                         symbol, volume, avg_volume)
            rule_signal = "HOLD"
        else:
            logging.info("Volume gate [%s]: vol=%.0f ≥ 1.5×avg=%.0f — volume confirmed ✓", symbol, volume, avg_volume)

    # --- Rule-based confidence scoring (replaces Claude haiku) ---
    # Claude haiku analysis (14 days, 54 signals) showed formulaic responses:
    # it mapped RSI→confidence tiers (0.72/0.82/0.85) without adding new information.
    # ADX, session block, MTF filters already provide stronger quality gates.
    # Rule-based scoring uses ALL available indicator data: RSI depth, ADX strength,
    # volume confirmation, and SMA distance — more signal than haiku ever had.
    def _rule_based_confidence() -> tuple[Signal, float, str]:
        if rule_signal == "HOLD":
            return "HOLD", 0.0, "rule=HOLD"
        if rule_signal == "SELL":
            sell_conf = min(0.95, 0.70 + (rsi - overbought) / 100)
            return "SELL", round(sell_conf, 2), f"RSI {rsi:.1f} overbought"
        # BUY confidence — composite of RSI depth, ADX, volume, SMA proximity
        conf = 0.60  # base: rule engine already vetted this
        # RSI depth below oversold → stronger signal
        rsi_depth = max(0.0, oversold - rsi) / oversold  # 0→1
        conf += rsi_depth * 0.20
        # ADX strength → trend conviction
        if adx >= 25:  conf += 0.10
        elif adx >= 20: conf += 0.05
        # Volume confirmation
        if avg_volume > 0:
            vol_mult = volume / avg_volume
            if vol_mult >= 2.0:   conf += 0.10
            elif vol_mult >= 1.5: conf += 0.05
        # SMA proximity — price close to SMA = better risk/reward
        sma_dist = abs(price - sma) / sma if sma > 0 else 0.0
        if sma_dist < 0.01: conf += 0.05  # within 1% of SMA
        reason = (f"RSI={rsi:.1f} ADX={adx:.1f} vol={volume/max(avg_volume,1):.1f}x "
                  f"conf={min(conf,1.0):.2f}")
        return "BUY", round(min(conf, 1.0), 2), reason

    claude_signal, claude_confidence, claude_reason = _rule_based_confidence()
    _claude_consecutive_failures = 0  # no LLM = no failures

    # Rule-based confidence always agrees with rule signal — final = rule signal.
    # Confidence threshold: BUY requires score ≥ 0.65 (base 0.60 + at least one confirming factor).
    if rule_signal == "HOLD":
        final_action: Signal = "HOLD"
    elif rule_signal == "SELL":
        final_action = "SELL"  # exits always allowed
    elif claude_confidence >= 0.65:
        final_action = "BUY"
        logging.info("Rule-based BUY confirmed [%s]: conf=%.2f %s", symbol, claude_confidence, claude_reason)
    else:
        final_action = "HOLD"
        logging.info("Rule-based BUY skipped [%s]: conf=%.2f < 0.65 — weak setup %s", symbol, claude_confidence, claude_reason)
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
        logging.info("Signal reasoning: %s", claude_reason)

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
