"""Options evening pre-analysis — runs at 8 PM ET to prepare next-day options picks.

Claude acts as orchestrator with tools:
  - get_iv_environment   : VIX + IV/RV ratio per symbol → expensive/cheap premium?
  - get_market_regime    : SPY/QQQ daily trend → directional bias
  - get_stock_technicals : RSI, trend, momentum for each options symbol
  - get_earnings_calendar: avoid symbols with earnings within 2 days (IV crush)

Output saved to options_state:
  approved    → which symbols to trade tomorrow
  direction   → CALL | PUT per symbol
  strike_pct  → target OTM % (4-12%) per symbol
  conviction  → high | medium | low
  iv_regime   → whether premium is cheap/expensive overall
  notes       → reasoning per symbol
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import anthropic

from .blueprint import SYMBOLS
from .state import options_state

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_get_iv_environment() -> dict:
    """Fetch VIX and per-symbol IV vs realized vol. Tells if options cheap/expensive."""
    try:
        import yfinance as yf

        # VIX as overall fear gauge
        vix = yf.Ticker("^VIX").history(period="5d")
        vix_level = float(vix["Close"].iloc[-1]) if not vix.empty else 20.0

        if vix_level < 15:
            iv_regime = "low"
        elif vix_level < 20:
            iv_regime = "normal"
        elif vix_level < 30:
            iv_regime = "high"
        else:
            iv_regime = "extreme"

        # Per-symbol IV vs RV
        symbol_iv = {}
        for sym in SYMBOLS:
            try:
                ticker = yf.Ticker(sym)
                exps = ticker.options
                if not exps:
                    continue
                # Use nearest expiry for IV sample
                chain = ticker.option_chain(exps[0])
                atm_calls = chain.calls.copy()
                hist = ticker.history(period="35d")
                if hist.empty or len(hist) < 15:
                    continue
                current_price = float(hist["Close"].iloc[-1])
                # Pick near-ATM call
                atm_calls["dist"] = abs(atm_calls["strike"] - current_price)
                nearest = atm_calls.nsmallest(3, "dist")
                avg_iv = float(nearest["impliedVolatility"].mean()) if not nearest.empty else 0.0
                # RV: 20-day realized vol
                returns = hist["Close"].pct_change().dropna()
                rv = float(returns.std() * (252 ** 0.5)) if len(returns) >= 10 else 0.0
                iv_rv_ratio = round(avg_iv / rv, 2) if rv > 0 else 0.0
                symbol_iv[sym] = {
                    "iv_pct": round(avg_iv * 100, 1),
                    "rv_pct": round(rv * 100, 1),
                    "iv_rv_ratio": iv_rv_ratio,
                    "premium_status": "cheap" if iv_rv_ratio < 1.2 else "fair" if iv_rv_ratio < 1.8 else "expensive",
                    "price": round(current_price, 2),
                }
            except Exception as se:
                logging.debug("IV fetch failed [%s]: %s", sym, se)

        return {
            "vix": round(vix_level, 1),
            "iv_regime": iv_regime,
            "symbols": symbol_iv,
        }
    except Exception as exc:
        logging.warning("tool_get_iv_environment failed: %s", exc)
        return {"vix": 20.0, "iv_regime": "normal", "symbols": {}, "error": str(exc)}


def _tool_get_market_regime() -> dict:
    """SPY/QQQ 20-day daily bars → market trend and bias."""
    try:
        import yfinance as yf

        result = {}
        for sym in ["SPY", "QQQ"]:
            hist = yf.Ticker(sym).history(period="30d")
            if hist.empty or len(hist) < 10:
                continue
            closes = list(hist["Close"])
            price = closes[-1]
            ma20 = sum(closes[-20:]) / min(20, len(closes))
            ret5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
            ret20d = (closes[-1] - closes[0]) / closes[0] * 100
            result[sym] = {
                "price": round(price, 2),
                "above_ma20": price > ma20,
                "ret_5d_pct": round(ret5d, 2),
                "ret_20d_pct": round(ret20d, 2),
            }

        spy = result.get("SPY", {})
        if spy.get("above_ma20") and spy.get("ret_5d_pct", 0) > 0.5:
            regime = "trending_up"
        elif not spy.get("above_ma20") or spy.get("ret_5d_pct", 0) < -1.0:
            regime = "trending_down"
        else:
            regime = "sideways"

        result["regime"] = regime
        return result
    except Exception as exc:
        logging.warning("tool_get_market_regime failed: %s", exc)
        return {"regime": "sideways", "error": str(exc)}


def _tool_get_stock_technicals(symbols: list[str]) -> list[dict]:
    """RSI, EMA position, momentum, volume for each symbol."""
    try:
        import yfinance as yf
        import pandas as pd

        results = []
        for sym in symbols:
            try:
                hist = yf.Ticker(sym).history(period="60d")
                if hist.empty or len(hist) < 20:
                    continue
                closes = hist["Close"]
                volumes = hist["Volume"]

                # RSI-14
                delta = closes.diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss
                rsi = float(100 - 100 / (1 + rs.iloc[-1]))

                # EMA20 / EMA50
                ema20 = float(closes.ewm(span=20).mean().iloc[-1])
                ema50 = float(closes.ewm(span=50).mean().iloc[-1])
                price = float(closes.iloc[-1])

                # Volume vs 20d avg
                vol_ratio = float(volumes.iloc[-1] / volumes.rolling(20).mean().iloc[-1])

                # 5-day return
                ret5d = float((closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5] * 100)

                trend = "up" if price > ema20 > ema50 else "down" if price < ema20 < ema50 else "mixed"

                results.append({
                    "symbol": sym,
                    "price": round(price, 2),
                    "rsi": round(rsi, 1),
                    "ema20": round(ema20, 2),
                    "ema50": round(ema50, 2),
                    "trend": trend,
                    "vol_ratio": round(vol_ratio, 2),
                    "ret_5d_pct": round(ret5d, 2),
                    "overbought": rsi > 70,
                    "oversold": rsi < 35,
                })
            except Exception as se:
                logging.debug("Technicals failed [%s]: %s", sym, se)
        return results
    except Exception as exc:
        logging.warning("tool_get_stock_technicals failed: %s", exc)
        return []


_ETFS = {"SPY", "QQQ", "IWM", "GLD", "TLT", "XLF", "XLE", "XLK"}  # ETFs have no earnings

def _tool_get_earnings_calendar(symbols: list[str]) -> dict:
    """Flag symbols with earnings within next 2 days — avoid IV crush."""
    result = {}
    try:
        import yfinance as yf
        from datetime import date

        today = date.today()
        for sym in symbols:
            if sym in _ETFS:
                result[sym] = {"earnings_soon": False}
                continue
            try:
                cal = yf.Ticker(sym).calendar
                if cal is None:
                    result[sym] = {"earnings_soon": False}
                    continue
                # calendar may be dict or DataFrame
                if hasattr(cal, "to_dict"):
                    cal = cal.to_dict()
                earn_date = cal.get("Earnings Date")
                if earn_date:
                    if isinstance(earn_date, (list, tuple)):
                        earn_date = earn_date[0]
                    if hasattr(earn_date, "date"):
                        earn_date = earn_date.date()
                    days_away = (earn_date - today).days
                    result[sym] = {
                        "earnings_soon": 0 <= days_away <= 2,
                        "earnings_date": str(earn_date),
                        "days_away": days_away,
                    }
                else:
                    result[sym] = {"earnings_soon": False}
            except Exception:
                result[sym] = {"earnings_soon": False}
    except Exception as exc:
        logging.warning("tool_get_earnings_calendar failed: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Tool schema for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_iv_environment",
        "description": (
            "Get VIX level and per-symbol IV/RV ratio. Tells you whether options premiums "
            "are cheap (IV < 1.2× RV = buy options) or expensive (IV > 1.8× RV = avoid). "
            "Call this FIRST to set the overall options-trading bias for tomorrow."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_market_regime",
        "description": (
            "Get SPY/QQQ 20-day trend. Trending up = bias toward calls. "
            "Trending down = bias toward puts. Sideways = higher conviction bar needed."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_stock_technicals",
        "description": (
            "Get RSI, EMA trend, volume for specific symbols. Use to identify: "
            "overbought (RSI>70, trend=up → good PUT candidate), "
            "oversold (RSI<35, trend=down → good CALL candidate). "
            "Call with the symbols you want to evaluate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of symbols to analyze",
                }
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "get_earnings_calendar",
        "description": (
            "Check if any symbols have earnings within 2 days. "
            "NEVER trade options on earnings stocks — IV crush destroys premium."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Symbols to check for upcoming earnings",
                }
            },
            "required": ["symbols"],
        },
    },
]


# ---------------------------------------------------------------------------
# Batch analysis — pre-fetch all data in Python, then one request per symbol.
# 50% cheaper than standard API, all symbols analysed in parallel.
# Falls back to agentic loop if batch fails or times out.
# ---------------------------------------------------------------------------

_BATCH_SYMBOL_SYSTEM = """You are an expert options trader analysing a single stock for tomorrow.
Small account ($200-600). Budget: $200 max per contract. PAPER trading.

Given pre-fetched market data, decide:
- Should we trade this symbol tomorrow? (yes/no)
- CALL or PUT?
- Strike OTM%: 4-12% (4-6% high conviction, 7-10% medium)
- Conviction: high | medium | low (if low → don't trade)

Rules:
- earnings_soon=true → NEVER trade (IV crush risk)
- iv_rv_ratio > 1.8 → avoid (premium too expensive)
- CALL: RSI < 38 OR (RSI < 50 AND trend=up AND ret_5d > 0)
- PUT: RSI > 68 OR (RSI > 55 AND trend=down AND ret_5d < 0)
- Match market regime (no calls in trending_down market, no puts in trending_up)

Output ONLY valid JSON, no markdown:
{"trade": true/false, "direction": "CALL"/"PUT", "strike_pct_otm": 5.0,
 "conviction": "high"/"medium"/"low", "reason": "one sentence"}"""


def _run_batch_analysis(client: anthropic.Anthropic, trade_date: str) -> dict:
    """Pre-fetch all data in Python, submit one Batch API request per symbol.

    Returns merged result dict in same format as agentic loop output.
    Batch API is 50% cheaper than standard — all symbols processed in parallel.
    """
    import time

    logging.info("Options batch analysis: pre-fetching market data for %d symbols…", len(SYMBOLS))

    # ── Step 1: Fetch all data in Python (no LLM cost) ───────────────────────
    iv_data    = _tool_get_iv_environment()    # VIX + per-symbol IV/RV
    regime_data = _tool_get_market_regime()    # SPY/QQQ trend
    tech_data  = _tool_get_stock_technicals(SYMBOLS)   # RSI, EMA, momentum
    earn_data  = _tool_get_earnings_calendar(SYMBOLS)  # earnings within 2d

    tech_by_sym = {t["symbol"]: t for t in tech_data}
    market_regime = regime_data.get("regime", "sideways")
    iv_regime = iv_data.get("iv_regime", "normal")
    vix = iv_data.get("vix", 20.0)

    # ── Step 2: Build one batch request per symbol ────────────────────────────
    # System prompt cached with 1h TTL — all requests in the batch share the cache.
    cached_system = [{"type": "text", "text": _BATCH_SYMBOL_SYSTEM,
                      "cache_control": {"type": "ephemeral", "ttl": "1h"}}]

    requests = []
    for sym in SYMBOLS:
        tech   = tech_by_sym.get(sym, {})
        iv_sym = iv_data.get("symbols", {}).get(sym, {})
        earn   = earn_data.get(sym, {})

        user_content = (
            f"Symbol: {sym}\n"
            f"Market regime: {market_regime} | VIX: {vix:.1f} | IV regime: {iv_regime}\n"
            f"Technicals: RSI={tech.get('rsi', 50):.1f} trend={tech.get('trend','mixed')} "
            f"ret_5d={tech.get('ret_5d_pct', 0):.1f}% vol_ratio={tech.get('vol_ratio', 1):.2f}\n"
            f"IV: iv_pct={iv_sym.get('iv_pct', 0):.1f}% rv_pct={iv_sym.get('rv_pct', 0):.1f}% "
            f"iv_rv_ratio={iv_sym.get('iv_rv_ratio', 0):.2f} status={iv_sym.get('premium_status', 'unknown')}\n"
            f"Earnings soon: {earn.get('earnings_soon', False)}\n\n"
            "Should we trade this symbol tomorrow? Output JSON."
        )
        requests.append({
            "custom_id": f"opts-{sym}-{trade_date}",
            "params": {
                "model": "claude-opus-4-8",
                "max_tokens": 300,
                "system": cached_system,
                "messages": [{"role": "user", "content": user_content}],
            },
        })

    # ── Step 3: Submit batch ──────────────────────────────────────────────────
    logging.info("Options batch: submitting %d symbol requests…", len(requests))
    batch = client.messages.batches.create(requests=requests)
    logging.info("Options batch submitted: id=%s", batch.id)

    # ── Step 4: Poll until complete (max 20 min) ──────────────────────────────
    deadline = time.time() + 20 * 60
    while time.time() < deadline:
        status = client.messages.batches.retrieve(batch.id)
        if status.processing_status == "ended":
            break
        logging.info("Options batch %s: status=%s — waiting 30s…", batch.id, status.processing_status)
        time.sleep(30)
    else:
        logging.warning("Options batch timed out after 20 min — falling back to agentic loop")
        return {}

    # ── Step 5: Collect and merge results ─────────────────────────────────────
    approved, direction, strike_pct, conviction, notes = [], {}, {}, {}, {}

    for result in client.messages.batches.results(batch.id):
        sym = result.custom_id.split("-")[1]   # opts-{sym}-{date} → sym
        if result.result.type != "succeeded":
            logging.warning("Batch result failed for %s: %s", sym, result.result.type)
            continue
        try:
            text = "".join(b.text for b in result.result.message.content if hasattr(b, "text"))
            start, end = text.find("{"), text.rfind("}") + 1
            data = json.loads(text[start:end]) if start >= 0 and end > start else {}
            if data.get("trade") and data.get("conviction", "low") != "low":
                approved.append(sym)
                direction[sym]   = data.get("direction", "CALL")
                strike_pct[sym]  = float(data.get("strike_pct_otm", 6.0))
                conviction[sym]  = data.get("conviction", "medium")
                notes[sym]       = data.get("reason", "")
                logging.info("Batch result %s: %s %s%.0f%% [%s]",
                             sym, direction[sym], "", strike_pct[sym], conviction[sym])
        except Exception as exc:
            logging.warning("Batch result parse failed [%s]: %s", sym, exc)

    logging.info("Options batch analysis done: %d/%d symbols approved — %s",
                 len(approved), len(SYMBOLS), ", ".join(approved))

    return {
        "approved": approved,
        "direction": direction,
        "strike_pct_otm": strike_pct,
        "conviction": conviction,
        "iv_regime": iv_regime,
        "regime": market_regime,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

def run_options_evening_analysis(anthropic_api_key: str) -> None:
    """Claude-powered evening analysis for options bot. Saves results to options_state."""
    from datetime import date
    import calendar as cal_mod

    today = date.today()
    # Find next trading day (skip weekends)
    next_day = today + timedelta(days=1)
    while next_day.weekday() >= 5:  # Sat=5, Sun=6
        next_day += timedelta(days=1)
    trade_date = str(next_day)

    logging.info("Options evening analysis: starting for %s", trade_date)
    options_state.add_log("Evening", f"Options pre-analysis starting for {trade_date}…", "neutral")

    client = anthropic.Anthropic(api_key=anthropic_api_key)

    # ── Try Batch API first (50% cheaper, parallel) ───────────────────────────
    try:
        result = _run_batch_analysis(client, trade_date)
        if result.get("approved"):
            logging.info("Options batch analysis succeeded — skipping agentic loop")
            _save_evening_result(result, trade_date)
            return
        logging.warning("Options batch returned no approved symbols — falling back to agentic loop")
    except Exception as exc:
        logging.warning("Options batch analysis failed (%s) — falling back to agentic loop", exc)

    # ── Fallback: original agentic loop ──────────────────────────────────────
    logging.info("Options evening analysis: using agentic loop (batch unavailable)")

    system_prompt = """You are an expert options trader preparing tomorrow's trade plan.
You have a small options account ($200-600 range). Budget per trade: $200 max per contract.
You trade PAPER options — this is a 2-week monitored test.
Universe: SPY, QQQ, AAPL, AMD, MSFT (these are the only symbols available).

Your job: decide which 1-3 symbols to trade tomorrow, CALL or PUT, and at what OTM%.

Decision framework:
1. If IV expensive (IV/RV > 1.8×) → avoid buying options (theta kills you)
2. If IV cheap (IV/RV < 1.2×) → good time to buy options
3. CALL setup: RSI oversold (<35), price near support, trend turning up
4. PUT setup: RSI overbought (>70), price near resistance, trend turning down
5. Match market regime: don't fight the trend (calls in bear = hard)
6. Never trade earnings stocks (IV crush)

Strike target:
- High conviction: 4-6% OTM (closer to money, higher delta)
- Medium conviction: 7-10% OTM (cheaper, more leverage)
- Low conviction: SKIP the symbol

Output ONLY valid JSON, no markdown:
{
  "approved": ["AAPL", "MSFT"],
  "direction": {"AAPL": "CALL", "MSFT": "PUT"},
  "strike_pct_otm": {"AAPL": 5.0, "MSFT": 8.0},
  "conviction": {"AAPL": "high", "MSFT": "medium"},
  "iv_regime": "low",
  "regime": "sideways",
  "notes": {
    "AAPL": "RSI 32 oversold, support at 315, IV cheap at 0.9× RV",
    "MSFT": "RSI 84 overbought, extended +12% week, IV/RV 1.1 acceptable"
  }
}

RULES:
- Every symbol in approved MUST have direction, strike_pct_otm, conviction, notes
- strike_pct_otm must be between 4.0 and 12.0
- direction must be CALL or PUT (uppercase)
- conviction must be high, medium, or low
- If iv_regime is extreme, approved can be empty [] (skip all)
- Prefer 1-2 symbols max — quality over quantity"""

    messages = [
        {
            "role": "user",
            "content": (
                f"Analyse the universe for tomorrow's options session ({trade_date}).\n"
                f"Universe: {', '.join(SYMBOLS)}\n"
                "Start by checking IV environment and market regime, then evaluate technicals. "
                "Finally output your JSON trade plan."
            ),
        }
    ]

    result = {}
    max_rounds = 8
    # Wrap system_prompt with 1h cache — same prompt sent across all agentic loop iterations.
    # Cache writes cost 2× for first call, reads cost 0.1× — breaks even after 2nd iteration.
    _cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    for round_num in range(max_rounds):
        response = client.messages.create(
            model="claude-opus-4-8",          # upgraded from 4-7 — same price, newer training
            max_tokens=4000,
            thinking={"type": "adaptive"},    # adaptive thinking — deep on complex, fast on simple
            output_config={"effort": "high"}, # high effort for conviction scoring + IV analysis
            system=_cached_system,
            tools=TOOLS,
            messages=messages,
        )

        # Collect assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract JSON from final text block — try multiple parse strategies
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    # Strategy 1: clean JSON block
                    try:
                        start = text.find("{")
                        end = text.rfind("}") + 1
                        if start >= 0 and end > start:
                            result = json.loads(text[start:end])
                            break
                    except json.JSONDecodeError:
                        pass
                    # Strategy 2: strip markdown fences
                    try:
                        import re as _re
                        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
                        if m:
                            result = json.loads(m.group(1))
                            break
                    except (json.JSONDecodeError, AttributeError):
                        pass
            break

        if response.stop_reason != "tool_use":
            break

        # Handle tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            logging.info("Options evening agent: calling tool %s", tool_name)

            if tool_name == "get_iv_environment":
                tool_out = _tool_get_iv_environment()
            elif tool_name == "get_market_regime":
                tool_out = _tool_get_market_regime()
            elif tool_name == "get_stock_technicals":
                tool_out = _tool_get_stock_technicals(tool_input.get("symbols", SYMBOLS))
            elif tool_name == "get_earnings_calendar":
                tool_out = _tool_get_earnings_calendar(tool_input.get("symbols", SYMBOLS))
            else:
                tool_out = {"error": f"Unknown tool: {tool_name}"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(tool_out),
            })

        messages.append({"role": "user", "content": tool_results})

    _save_evening_result(result, trade_date)


def _save_evening_result(result: dict, trade_date: str) -> None:
    """Validate and persist evening analysis result to options_state. Shared by batch + agentic paths."""
    approved = [s for s in result.get("approved", []) if s in SYMBOLS]
    direction = {s: v for s, v in result.get("direction", {}).items() if s in approved}
    strike_pct = {s: float(v) for s, v in result.get("strike_pct_otm", {}).items() if s in approved}
    conviction = {s: v for s, v in result.get("conviction", {}).items() if s in approved}
    notes = {s: v for s, v in result.get("notes", {}).items() if s in approved}

    # Remove symbols missing required fields
    approved = [s for s in approved if s in direction and s in strike_pct and s in conviction]

    if not approved:
        logging.warning("Options evening analysis returned no approved symbols")
        options_state.add_log("Evening", "Options analysis: no symbols approved — will scan all tomorrow", "warning")
        return

    with options_state._lock:
        options_state.evening_approved = approved
        options_state.evening_direction = direction
        options_state.evening_strike_pct = strike_pct
        options_state.evening_conviction = conviction
        options_state.evening_iv_regime = result.get("iv_regime", "normal")
        options_state.evening_regime = result.get("regime", "sideways")
        options_state.evening_notes = notes
        options_state.evening_analysis_date = trade_date

    options_state._save()

    summary_parts = [
        f"{s} {direction.get(s,'?')} {strike_pct.get(s,0):.0f}%OTM [{conviction.get(s,'?')}]"
        for s in approved
    ]
    iv_reg = result.get("iv_regime", "?")
    regime = result.get("regime", "?")
    options_state.add_log(
        "Evening",
        f"Options plan for {trade_date} | IV:{iv_reg} market:{regime} | " + " | ".join(summary_parts),
        "positive",
    )
    logging.info("Options evening analysis done: %d symbols for %s — %s",
                 len(approved), trade_date, ", ".join(summary_parts))
