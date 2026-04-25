"""Evening sub-agent analysis — runs at 8 PM ET to prepare next-day watchlist.

Claude acts as an orchestrator with tools:
  - get_market_regime     : SPY/QQQ/VIX daily bars → market trend classification
  - get_sector_performance: sector ETF returns → which sectors are leading/lagging
  - get_stock_technicals  : RSI, EMA position, volume trend for each stock
  - get_news_sentiment    : Alpaca news API → recent headlines + sentiment per symbol
  - get_earnings_calendar : yfinance → flag stocks reporting tomorrow

Claude decides which tools to call, synthesises everything, and outputs a ranked
watchlist with entry zones and risk flags — saved to Supabase evening_watchlist.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import anthropic

from .scanner import STOCK_UNIVERSE
from .state import day_state

# ---------------------------------------------------------------------------
# Tool implementations — called when Claude requests them
# ---------------------------------------------------------------------------

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLB", "XLC", "XLU", "XLRE"]
REGIME_SYMBOLS = ["SPY", "QQQ", "VIX"]


def _alpaca_client(api_key: str, secret_key: str):
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(api_key, secret_key)


def tool_get_market_regime(api_key: str, secret_key: str) -> dict:
    """Fetch 20-day daily bars for SPY, QQQ, VIX and classify market regime."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = _alpaca_client(api_key, secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        req = StockBarsRequest(
            symbol_or_symbols=["SPY", "QQQ"],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex",
        )
        bars = client.get_stock_bars(req)
        result = {}
        for sym in ["SPY", "QQQ"]:
            sym_bars = bars.data.get(sym, [])
            if len(sym_bars) >= 5:
                closes = [float(b.close) for b in sym_bars[-20:]]
                pct_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
                pct_20d = (closes[-1] - closes[0]) / closes[0] * 100 if len(closes) >= 20 else 0
                above_20d_avg = closes[-1] > sum(closes) / len(closes)
                result[sym] = {
                    "price": round(closes[-1], 2),
                    "5d_return_pct": round(pct_5d, 2),
                    "20d_return_pct": round(pct_20d, 2),
                    "above_20d_ma": above_20d_avg,
                }
        regime = "trending_up"
        spy = result.get("SPY", {})
        if spy.get("5d_return_pct", 0) < -1.5 or not spy.get("above_20d_ma", True):
            regime = "trending_down"
        elif abs(spy.get("5d_return_pct", 0)) < 0.5:
            regime = "sideways"
        result["regime"] = regime
        return result
    except Exception as exc:
        logging.warning("tool_get_market_regime failed: %s", exc)
        return {"regime": "unknown", "error": str(exc)}


def tool_get_sector_performance(api_key: str, secret_key: str) -> dict:
    """Fetch 5-day returns for major sector ETFs."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = _alpaca_client(api_key, secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=10)
        req = StockBarsRequest(
            symbol_or_symbols=SECTOR_ETFS,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex",
        )
        bars = client.get_stock_bars(req)
        result = {}
        for sym in SECTOR_ETFS:
            sym_bars = bars.data.get(sym, [])
            if len(sym_bars) >= 2:
                closes = [float(b.close) for b in sym_bars]
                pct = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else (closes[-1] - closes[0]) / closes[0] * 100
                result[sym] = round(pct, 2)
        # Sort by performance
        sorted_sectors = sorted(result.items(), key=lambda x: x[1], reverse=True)
        return {
            "leading": [s[0] for s in sorted_sectors[:3]],
            "lagging": [s[0] for s in sorted_sectors[-3:]],
            "returns": result,
        }
    except Exception as exc:
        logging.warning("tool_get_sector_performance failed: %s", exc)
        return {"leading": [], "lagging": [], "returns": {}, "error": str(exc)}


def tool_get_stock_technicals(api_key: str, secret_key: str, symbols: list[str]) -> list[dict]:
    """Fetch 20-day daily bars, compute RSI-14 and EMA-20 for each symbol."""
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        import pandas as pd

        client = _alpaca_client(api_key, secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=40)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="iex",
        )
        bars = client.get_stock_bars(req)
        results = []
        for sym in symbols:
            sym_bars = bars.data.get(sym, [])
            if len(sym_bars) < 5:
                continue
            closes = [float(b.close) for b in sym_bars]
            volumes = [float(b.volume) for b in sym_bars]

            # EMA-20
            ema = closes[0]
            k = 2 / (20 + 1)
            for c in closes[1:]:
                ema = c * k + ema * (1 - k)

            # RSI-14
            if len(closes) >= 15:
                deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                gains = [d if d > 0 else 0 for d in deltas[-14:]]
                losses = [-d if d < 0 else 0 for d in deltas[-14:]]
                avg_gain = sum(gains) / 14
                avg_loss = sum(losses) / 14
                rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
            else:
                rsi = 50.0

            avg_vol = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else sum(volumes) / len(volumes)
            vol_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
            price = closes[-1]
            day_change = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0.0
            pct_above_ema = (price - ema) / ema * 100 if ema > 0 else 0.0

            results.append({
                "symbol": sym,
                "price": round(price, 2),
                "rsi": round(rsi, 1),
                "ema20": round(ema, 2),
                "pct_above_ema": round(pct_above_ema, 2),
                "volume_ratio": vol_ratio,
                "day_change_pct": round(day_change, 2),
            })
        return results
    except Exception as exc:
        logging.warning("tool_get_stock_technicals failed: %s", exc)
        return []


def tool_get_news_sentiment(api_key: str, secret_key: str, symbols: list[str]) -> dict:
    """Fetch last 24h news headlines for symbols via Alpaca News API."""
    try:
        from alpaca.data.historical import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(api_key=api_key, secret_key=secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        req = NewsRequest(
            symbols=symbols,
            start=start,
            end=end,
            limit=50,
            include_content=False,
        )
        news = client.get_news(req)
        result = {}
        for article in (news.news if hasattr(news, "news") else []):
            headline = article.headline or ""
            for sym in (article.symbols or []):
                if sym not in result:
                    result[sym] = []
                result[sym].append(headline)
        # Trim to 3 headlines per symbol
        return {sym: headlines[:3] for sym, headlines in result.items()}
    except Exception as exc:
        logging.warning("tool_get_news_sentiment failed: %s", exc)
        return {}


def tool_get_earnings_calendar(symbols: list[str]) -> dict:
    """Check which symbols have earnings in the next 2 days via yfinance."""
    result = {}
    for sym in symbols:
        try:
            import yfinance as yf
            ticker = yf.Ticker(sym)
            cal = ticker.calendar
            if cal is None:
                result[sym] = False
                continue
            dates_raw = cal.get("Earnings Date") if isinstance(cal, dict) else []
            if not dates_raw:
                result[sym] = False
                continue
            if not isinstance(dates_raw, (list, tuple)):
                dates_raw = [dates_raw]
            today = datetime.now(timezone.utc).date()
            cutoff = today + timedelta(days=2)
            has_earnings = any(
                today <= (d.date() if hasattr(d, "date") else d) <= cutoff
                for d in dates_raw
            )
            result[sym] = has_earnings
            time.sleep(0.3)  # rate limit
        except Exception:
            result[sym] = False
    return result


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_market_regime",
        "description": (
            "Fetch SPY and QQQ 20-day daily data and classify the current market regime "
            "as trending_up, trending_down, or sideways. Call this first to understand "
            "the macro backdrop before analysing individual stocks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_sector_performance",
        "description": (
            "Fetch 5-day returns for major sector ETFs (XLK, XLF, XLE, XLV, etc). "
            "Use this to identify which sectors are leading and which are lagging, "
            "so you can favour stocks in strong sectors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_stock_technicals",
        "description": (
            "Fetch 20-day daily bars and compute RSI-14, EMA-20, volume ratio, and "
            "day change for a list of stock symbols. Use this to assess technical setups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of stock ticker symbols to analyse (max 33).",
                }
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "get_news_sentiment",
        "description": (
            "Fetch the last 24 hours of news headlines for a list of symbols via "
            "Alpaca News API. Use this to detect negative catalysts (lawsuits, "
            "downgrades, executive departures) or positive catalysts (partnerships, "
            "beats, upgrades) that should affect your ranking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of stock ticker symbols to fetch news for.",
                }
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "get_earnings_calendar",
        "description": (
            "Check which symbols have earnings announcements in the next 2 days. "
            "Stocks reporting earnings tomorrow are high-risk and should be excluded "
            "from the approved watchlist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of stock ticker symbols to check for earnings.",
                }
            },
            "required": ["symbols"],
        },
    },
]

_SYSTEM = """\
You are an expert pre-market stock analyst preparing a watchlist for an automated day trading bot.

Your goal: select the best 8-12 stocks from the universe for tomorrow's intraday trading session.

Workflow:
1. Call get_market_regime to understand the macro backdrop.
2. Call get_sector_performance to identify leading and lagging sectors.
3. Call get_stock_technicals with all symbols to assess technical setups.
4. Call get_news_sentiment for your top candidates to check for negative catalysts.
5. Call get_earnings_calendar for your top candidates to filter out earnings risk.
6. Synthesise all data and output your final watchlist.

Selection criteria:
- RSI 32-52 (oversold recovery or healthy pullback — not extended)
- Price within 2% above EMA-20 (pullback to support, not overextended)
- Volume ratio > 1.0 (normal or above-average interest)
- In a leading or neutral sector (avoid lagging sectors in downtrend)
- No earnings in next 2 days
- No major negative news catalysts
- Favour stocks in trending_up regime; be more selective in sideways; minimal in trending_down

Output ONLY a JSON object in this exact format — no markdown, no explanation:
{
  "regime": "trending_up",
  "approved": ["NVDA", "MSFT", "AAPL"],
  "skip": ["TSLA", "BABA"],
  "risk_flags": {"TSLA": "earnings tomorrow", "BABA": "negative news"},
  "entry_zones": {"NVDA": [480.0, 495.0], "MSFT": [415.0, 422.0]},
  "stop_levels": {"NVDA": 476.0, "MSFT": 412.0},
  "targets": {"NVDA": 508.0, "MSFT": 430.0},
  "direction": {"NVDA": "BUY", "MSFT": "BUY"},
  "notes": {"NVDA": "strong momentum, RSI cooling to 42, volume 1.4x avg"}
}

stop_levels: 1-1.5% below entry zone low (intraday stop).
targets: 2.5-4% above entry zone high (intraday target).
direction: BUY for long setups, SELL for short/avoid setups.
"""


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_evening_analysis(
    anthropic_api_key: str,
    alpaca_api_key: str,
    alpaca_secret_key: str,
) -> dict:
    """Run the full Claude sub-agent evening analysis. Returns the parsed result dict."""
    trade_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    logging.info("Evening analysis starting for trade date %s", trade_date)
    day_state.add_log("Evening", f"Sub-agent analysis starting for {trade_date}…", "neutral")

    if not anthropic_api_key:
        logging.warning("Evening analysis: no Anthropic key — skipping")
        return {}

    client = anthropic.Anthropic(api_key=anthropic_api_key, timeout=120.0, max_retries=1)
    messages = [
        {
            "role": "user",
            "content": (
                f"Please analyse the stock universe and prepare the watchlist for tomorrow ({trade_date}).\n"
                f"Universe: {', '.join(STOCK_UNIVERSE)}"
            ),
        }
    ]

    result = {}
    max_iterations = 10

    for iteration in range(max_iterations):
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant response
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract final JSON from text blocks
            for block in response.content:
                if hasattr(block, "text") and block.text.strip().startswith("{"):
                    try:
                        result = json.loads(block.text.strip())
                    except json.JSONDecodeError:
                        # Try to extract JSON from within text
                        text = block.text.strip()
                        start = text.find("{")
                        end = text.rfind("}") + 1
                        if start >= 0 and end > start:
                            try:
                                result = json.loads(text[start:end])
                            except Exception:
                                pass
            break

        if response.stop_reason != "tool_use":
            break

        # Execute tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            logging.info("Evening agent calling tool: %s %s", tool_name, tool_input)

            try:
                if tool_name == "get_market_regime":
                    output = tool_get_market_regime(alpaca_api_key, alpaca_secret_key)
                elif tool_name == "get_sector_performance":
                    output = tool_get_sector_performance(alpaca_api_key, alpaca_secret_key)
                elif tool_name == "get_stock_technicals":
                    output = tool_get_stock_technicals(
                        alpaca_api_key, alpaca_secret_key,
                        tool_input.get("symbols", STOCK_UNIVERSE),
                    )
                elif tool_name == "get_news_sentiment":
                    output = tool_get_news_sentiment(
                        alpaca_api_key, alpaca_secret_key,
                        tool_input.get("symbols", []),
                    )
                elif tool_name == "get_earnings_calendar":
                    output = tool_get_earnings_calendar(tool_input.get("symbols", []))
                else:
                    output = {"error": f"Unknown tool: {tool_name}"}
            except Exception as exc:
                output = {"error": str(exc)}
                logging.warning("Tool %s failed: %s", tool_name, exc)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output),
            })

        messages.append({"role": "user", "content": tool_results})

    # Validate and store result
    approved = result.get("approved", [])
    approved = [s for s in approved if s in STOCK_UNIVERSE]
    if not approved:
        logging.warning("Evening analysis returned no approved stocks — using scanner fallback")
        day_state.add_log("Evening", "No approved stocks from agent — watchlist empty", "warning")
        return result

    # Save to state and Supabase
    with day_state._lock:
        day_state.evening_approved = approved
        day_state.evening_entry_zones = result.get("entry_zones", {})
        day_state.evening_risk_flags = result.get("risk_flags", {})
        day_state.evening_regime = result.get("regime", "unknown")
        day_state.evening_notes = result.get("notes", {})
        day_state.evening_analysis_date = trade_date
        day_state.evening_stop_levels = result.get("stop_levels", {})
        day_state.evening_targets = result.get("targets", {})
        day_state.evening_direction = result.get("direction", {})

    from .evening_db import save_evening_analysis
    save_evening_analysis(trade_date, result)

    approved_str = ", ".join(approved[:8]) + ("…" if len(approved) > 8 else "")
    day_state.add_log(
        "Evening",
        f"✅ {len(approved)} stocks approved for {trade_date}: {approved_str}",
        "positive",
    )
    logging.info("Evening analysis complete — approved: %s", approved)

    # Send Telegram summary
    _notify_evening_summary(trade_date, result)
    return result


def _notify_evening_summary(trade_date: str, result: dict) -> None:
    try:
        import os, requests as req
        approved = result.get("approved", [])
        regime = result.get("regime", "unknown")
        notes = result.get("notes", {})
        risk_flags = result.get("risk_flags", {})
        skipped = result.get("skip", [])

        regime_icon = {"trending_up": "📈", "trending_down": "📉", "sideways": "↔️"}.get(regime, "❓")
        lines = [
            f"🌙 <b>Evening Analysis — {trade_date}</b>",
            f"━━━━━━━━━━━━━━━",
            f"{regime_icon} Market regime: <b>{regime.replace('_', ' ').title()}</b>",
            f"✅ Approved ({len(approved)}): <b>{', '.join(approved)}</b>",
        ]
        if skipped:
            lines.append(f"❌ Skipped: {', '.join(skipped[:5])}{'…' if len(skipped) > 5 else ''}")
        if risk_flags:
            for sym, flag in list(risk_flags.items())[:3]:
                lines.append(f"⚠️ {sym}: {flag}")
        entry_zones = result.get("entry_zones", {})
        stop_levels = result.get("stop_levels", {})
        targets = result.get("targets", {})
        if approved:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("📋 <b>Tomorrow's Picks</b>")
            for sym in approved[:6]:
                ez = entry_zones.get(sym, [])
                sl = stop_levels.get(sym)
                tp = targets.get(sym)
                note = notes.get(sym, "")
                entry_str = f"${ez[0]:.2f}–${ez[1]:.2f}" if len(ez) == 2 else "—"
                sl_str = f"${sl:.2f}" if sl else "—"
                tp_str = f"${tp:.2f}" if tp else "—"
                lines.append(
                    f"  <b>{sym}</b> | Entry: {entry_str} | SL: {sl_str} | Target: {tp_str}"
                )
                if note:
                    lines.append(f"  ↳ {note}")
        elif notes:
            lines.append("━━━━━━━━━━━━━━━")
            for sym, note in list(notes.items())[:4]:
                lines.append(f"• {sym}: {note}")

        token = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML"},
                timeout=5,
            )
    except Exception as exc:
        logging.warning("Evening Telegram notify failed: %s", exc)
