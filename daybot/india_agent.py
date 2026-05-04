"""Indian market (NSE) evening analysis — runs at 4:30 PM IST (11:00 AM UTC).

Uses yfinance for OHLCV data (15-min delayed). Claude Opus analyzes Nifty 50
stocks and returns entry zones (₹), stop levels, targets, and direction.
Results stored in day_state.india_* fields and persisted for next morning.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# Nifty 50 universe — top 40 liquid stocks with .NS suffix
INDIA_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
    "HCLTECH.NS", "SUNPHARMA.NS", "TITAN.NS", "WIPRO.NS", "ULTRACEMCO.NS",
    "ADANIENT.NS", "ADANIPORTS.NS", "POWERGRID.NS", "NTPC.NS", "ONGC.NS",
    "JSWSTEEL.NS", "TATACONSUM.NS", "TATASTEEL.NS", "TECHM.NS", "BPCL.NS",
    "BAJAJFINSV.NS", "CIPLA.NS", "DIVISLAB.NS", "DRREDDY.NS", "EICHERMOT.NS",
    "GRASIM.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "M&M.NS", "NESTLEIND.NS",
]

_NIFTY_INDEX = "^NSEI"


def _fetch_stock_data(symbol: str, period: str = "3mo") -> dict | None:
    """Fetch OHLCV + basic indicators for one NSE symbol via yfinance."""
    try:
        import yfinance as yf
        import pandas as pd

        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval="1d")
        if df is None or len(df) < 20:
            return None

        close = df["Close"]
        volume = df["Volume"]

        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))

        avg_vol = volume.rolling(20).mean()

        last = close.iloc[-1]
        return {
            "symbol": symbol,
            "display": symbol.replace(".NS", ""),
            "price": round(float(last), 2),
            "ema20": round(float(ema20.iloc[-1]), 2),
            "ema50": round(float(ema50.iloc[-1]), 2),
            "rsi": round(float(rsi.iloc[-1]), 1),
            "volume": int(volume.iloc[-1]),
            "avg_volume": int(avg_vol.iloc[-1]) if not pd.isna(avg_vol.iloc[-1]) else 0,
            "week_high": round(float(close.rolling(52).max().iloc[-1]), 2),
            "week_low": round(float(close.rolling(52).min().iloc[-1]), 2),
            "pct_from_ema20": round((float(last) - float(ema20.iloc[-1])) / float(ema20.iloc[-1]) * 100, 2),
            "1w_change": round((float(close.iloc[-1]) - float(close.iloc[-5])) / float(close.iloc[-5]) * 100, 2) if len(close) >= 5 else 0.0,
            "1m_change": round((float(close.iloc[-1]) - float(close.iloc[-21])) / float(close.iloc[-21]) * 100, 2) if len(close) >= 21 else 0.0,
        }
    except Exception as exc:
        logging.warning("India data fetch failed for %s: %s", symbol, exc)
        return None


def _fetch_nifty_trend() -> dict:
    """Fetch Nifty 50 index trend data."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(_NIFTY_INDEX)
        df = ticker.history(period="1mo", interval="1d")
        if df is None or len(df) < 5:
            return {"level": 0, "trend": "unknown", "1w_change": 0.0}
        close = df["Close"]
        ema20 = close.ewm(span=20, adjust=False).mean()
        level = float(close.iloc[-1])
        w_chg = (float(close.iloc[-1]) - float(close.iloc[-5])) / float(close.iloc[-5]) * 100
        trend = "uptrend" if close.iloc[-1] > ema20.iloc[-1] else "downtrend"
        return {"level": round(level, 2), "trend": trend, "1w_change": round(w_chg, 2)}
    except Exception as exc:
        logging.warning("Nifty index fetch failed: %s", exc)
        return {"level": 0, "trend": "unknown", "1w_change": 0.0}


def run_india_analysis(anthropic_api_key: str) -> dict:
    """Main entry: fetch data, run Claude analysis, store in day_state."""
    from .state import day_state

    logging.info("India analysis: fetching data for %d stocks", len(INDIA_UNIVERSE))

    nifty = _fetch_nifty_trend()
    stocks = []
    for sym in INDIA_UNIVERSE:
        data = _fetch_stock_data(sym)
        if data:
            stocks.append(data)

    if not stocks:
        logging.warning("India analysis: no stock data fetched — aborting")
        return {}

    # Sort by RSI for readability
    stocks.sort(key=lambda x: x["rsi"])

    result = _run_claude_analysis(anthropic_api_key, nifty, stocks)
    if not result:
        return {}

    # Store in shared state
    with day_state._lock:
        day_state.india_approved = result.get("approved", [])
        day_state.india_entry_zones = result.get("entry_zones", {})
        day_state.india_stop_levels = result.get("stop_levels", {})
        day_state.india_targets = result.get("targets", {})
        day_state.india_notes = result.get("notes", {})
        day_state.india_direction = result.get("direction", {})
        day_state.india_regime = result.get("regime", "")
        day_state.india_analysis_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_state.india_rank = result.get("rank", {})
        day_state.india_conviction = result.get("conviction", {})

    # Persist to Supabase
    _persist_india_results(result)

    # Send Telegram alert
    try:
        from telegram_notify import notify_india_suggestions
        notify_india_suggestions(
            approved=result.get("approved", []),
            entry_zones=result.get("entry_zones", {}),
            stop_levels=result.get("stop_levels", {}),
            targets=result.get("targets", {}),
            notes=result.get("notes", {}),
            regime=result.get("regime", ""),
            nifty_level=nifty["level"],
            nifty_trend=nifty["trend"],
        )
    except Exception as exc:
        logging.warning("India Telegram notify failed: %s", exc)

    logging.info("India analysis complete — %d picks, regime: %s", len(result.get("approved", [])), result.get("regime", ""))
    return result


def _run_claude_analysis(anthropic_api_key: str, nifty: dict, stocks: list[dict]) -> dict | None:
    """Call Claude Opus with stock data. Returns structured picks."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=anthropic_api_key)

        stock_lines = []
        for s in stocks:
            vol_flag = " [HIGH VOL]" if s["avg_volume"] > 0 and s["volume"] > s["avg_volume"] * 1.5 else ""
            stock_lines.append(
                f"{s['display']}: ₹{s['price']} | EMA20 ₹{s['ema20']} | RSI {s['rsi']} | "
                f"1W {s['1w_change']:+.1f}% | 1M {s['1m_change']:+.1f}%{vol_flag}"
            )

        prompt = f"""You are an expert Indian stock market analyst for NSE (National Stock Exchange).

Today's Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Nifty 50 Index:
- Level: {nifty['level']}
- Trend: {nifty['trend']}
- 1-Week Change: {nifty['1w_change']:+.1f}%

Stock Data (RSI lowest to highest):
{chr(10).join(stock_lines)}

Task: Identify the 5-8 best swing trade setups for tomorrow's NSE session.

Criteria:
- BUY setups: RSI 35-60, price near or recovering from EMA20, uptrend, strong sector
- SELL/SHORT: only if clearly overbought RSI>72 or breaking down below EMA50
- Skip stocks with RSI in 48-52 neutral zone
- Consider Nifty trend — in downtrend, prefer defensive + oversold bounces; in uptrend, prefer momentum
- Entry zones should be realistic for tomorrow's open (±0.5% from current price)
- Stops: 1.5-2.5% below entry for longs, above for shorts
- Targets: 3-5% from entry (minimum 2:1 risk/reward)

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "regime": "bullish|bearish|mixed",
  "approved": ["SYMBOL1", "SYMBOL2"],
  "direction": {{"SYMBOL1": "BUY", "SYMBOL2": "BUY"}},
  "entry_zones": {{"SYMBOL1": [low, high], "SYMBOL2": [low, high]}},
  "stop_levels": {{"SYMBOL1": stop_price, "SYMBOL2": stop_price}},
  "targets": {{"SYMBOL1": target_price, "SYMBOL2": target_price}},
  "notes": {{"SYMBOL1": "reason in 10 words max", "SYMBOL2": "reason"}},
  "rank": {{"SYMBOL1": 1, "SYMBOL2": 2, "SYMBOL3": 3}},
  "conviction": {{"SYMBOL1": "high", "SYMBOL2": "high", "SYMBOL3": "medium"}}
}}

IMPORTANT: rank and conviction are REQUIRED fields. rank 1 = strongest setup today (lowest rank = trade first). conviction must be exactly one of: high / medium / low.
Use bare symbol names (no .NS suffix) as JSON keys."""

        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            timeout=60,
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if Claude wrapped response
        if "```" in raw:
            import re
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            raw = m.group(1).strip() if m else re.sub(r"```[a-z]*", "", raw).strip()
        result = json.loads(raw)

        # Re-add .NS suffix to approved list for internal use
        ns_approved = [s + ".NS" if not s.endswith(".NS") else s for s in result.get("approved", [])]
        result["approved"] = ns_approved

        # Re-key dicts with .NS suffix
        for field in ("direction", "entry_zones", "stop_levels", "targets", "notes"):
            old = result.get(field, {})
            result[field] = {(k + ".NS" if not k.endswith(".NS") else k): v for k, v in old.items()}

        return result

    except json.JSONDecodeError as exc:
        logging.error("India analysis: Claude returned invalid JSON: %s", exc)
        return None
    except Exception as exc:
        logging.exception("India analysis: Claude call failed: %s", exc)
        return None


def _persist_india_results(result: dict) -> None:
    """Save India analysis to Supabase for restart recovery."""
    try:
        from persistence import _get_client
        c = _get_client()
        if not c:
            return
        c.table("india_analysis").upsert({
            "analysis_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "regime": result.get("regime", ""),
            "approved": json.dumps(result.get("approved", [])),
            "entry_zones": json.dumps(result.get("entry_zones", {})),
            "stop_levels": json.dumps(result.get("stop_levels", {})),
            "targets": json.dumps(result.get("targets", {})),
            "notes": json.dumps(result.get("notes", {})),
            "direction": json.dumps(result.get("direction", {})),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="analysis_date").execute()
    except Exception as exc:
        logging.warning("India analysis persist failed: %s", exc)


def load_india_results_from_db() -> None:
    """Restore last India analysis from Supabase on server restart."""
    try:
        from persistence import _get_client
        from .state import day_state
        c = _get_client()
        if not c:
            return
        res = c.table("india_analysis").select("*").order("analysis_date", desc=True).limit(1).execute()
        if not res.data:
            return
        row = res.data[0]
        with day_state._lock:
            day_state.india_approved = json.loads(row.get("approved", "[]"))
            day_state.india_entry_zones = json.loads(row.get("entry_zones", "{}"))
            day_state.india_stop_levels = json.loads(row.get("stop_levels", "{}"))
            day_state.india_targets = json.loads(row.get("targets", "{}"))
            day_state.india_notes = json.loads(row.get("notes", "{}"))
            day_state.india_direction = json.loads(row.get("direction", "{}"))
            day_state.india_regime = row.get("regime", "")
            day_state.india_analysis_date = row.get("analysis_date", "")
        logging.info("India analysis restored from DB: %s", row.get("analysis_date"))
    except Exception as exc:
        logging.warning("India analysis DB restore failed: %s", exc)
