"""Pre-market analysis — Claude ranks all 33 stocks before market opens.

Runs automatically at 9:00 AM ET every weekday via the scheduler.
Produces an approved watchlist stored in day_state so the trading loop
only enters positions Claude has pre-approved for today.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic

from .scanner import STOCK_UNIVERSE
from .state import day_state

_SYSTEM = (
    "You are a pre-market stock analyst. Given a list of stocks with their 4-week "
    "performance data and technical indicators, rank and select the best candidates "
    "for intraday trading today. Focus on: clear uptrends, healthy pullbacks to EMA, "
    "rising volume, and avoid stocks near strong resistance or in downtrends. "
    "Respond ONLY with a JSON object — no markdown. "
    'Format: {"approved": ["NVDA","MSFT"], "skip": ["TSLA","BABA"], '
    '"notes": {"NVDA": "strong trend, RSI cooling", "TSLA": "near resistance, choppy"}}'
)


def run_premarket_analysis(anthropic_api_key: str, alpaca_api_key: str, alpaca_secret_key: str) -> list[str]:
    """Fetch data for all stocks, ask Claude to rank them, store approved list."""
    logging.info("Pre-market analysis starting for %d stocks", len(STOCK_UNIVERSE))
    day_state.add_log("Pre-market", f"Analysing {len(STOCK_UNIVERSE)} stocks…", "neutral")

    snapshots = _fetch_all_snapshots(alpaca_api_key, alpaca_secret_key)
    if not snapshots:
        logging.warning("Pre-market: no snapshot data — using full universe")
        day_state.add_log("Pre-market", "No data — using full universe", "warning")
        return STOCK_UNIVERSE[:15]

    stock_summaries = _build_summaries(snapshots)
    approved = _ask_claude(anthropic_api_key, stock_summaries)

    with day_state._lock:
        day_state.premarket_approved = approved
        day_state.premarket_time = datetime.now(timezone.utc).isoformat()

    day_state.add_log(
        "Pre-market",
        f"Approved {len(approved)} stocks: {', '.join(approved[:8])}{'…' if len(approved) > 8 else ''}",
        "positive",
    )
    logging.info("Pre-market approved: %s", approved)
    return approved


def _fetch_all_snapshots(api_key: str, secret_key: str) -> dict:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        client = StockHistoricalDataClient(api_key, secret_key)
        req = StockSnapshotRequest(symbol_or_symbols=STOCK_UNIVERSE)
        return client.get_stock_snapshot(req)
    except Exception as exc:
        logging.warning("Pre-market snapshot failed: %s", exc)
        return {}


def _build_summaries(snapshots: dict) -> list[dict]:
    summaries = []
    for sym, snap in snapshots.items():
        try:
            prev_close = float(snap.prev_daily_bar.close)
            curr_price = float(snap.daily_bar.close)
            day_change = (curr_price - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
            today_vol = float(snap.daily_bar.volume)
            minute_vol = float(snap.minute_bar.volume)
            est_avg_vol = minute_vol * 390
            vol_ratio = round(today_vol / est_avg_vol, 2) if est_avg_vol > 0 else 1.0

            summaries.append({
                "symbol": sym,
                "price": round(curr_price, 2),
                "day_change_pct": round(day_change, 2),
                "volume_ratio": vol_ratio,
            })
        except Exception:
            pass
    summaries.sort(key=lambda x: abs(x.get("day_change_pct", 0)), reverse=True)
    return summaries[:20]  # top 20 most active to Claude


def _ask_claude(api_key: str, summaries: list[dict]) -> list[str]:
    if not api_key:
        return [s["symbol"] for s in summaries[:12]]

    lines = []
    for s in summaries:
        direction = "▲" if s["day_change_pct"] > 0 else "▼"
        lines.append(
            f"{s['symbol']}: ${s['price']:.2f} {direction}{abs(s['day_change_pct']):.1f}% "
            f"vol_ratio={s['volume_ratio']:.1f}x"
        )
    stock_list = "\n".join(lines)

    prompt = (
        f"Today's pre-market data ({datetime.now(timezone.utc).strftime('%Y-%m-%d')}):\n\n"
        f"{stock_list}\n\n"
        "Select the best 8–12 stocks for intraday trading today. "
        "Approve stocks with clear momentum and volume. "
        "Skip stocks that are overextended, too volatile, or lack direction."
    )

    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            temperature=0,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        parsed = json.loads(text)
        approved = [s for s in parsed.get("approved", []) if s in STOCK_UNIVERSE]

        # Log Claude's notes per stock
        notes = parsed.get("notes", {})
        for sym, note in list(notes.items())[:5]:
            logging.info("Pre-market [%s]: %s", sym, note)

        return approved if approved else [s["symbol"] for s in summaries[:12]]

    except Exception as exc:
        logging.warning("Pre-market Claude call failed: %s", exc)
        return [s["symbol"] for s in summaries[:12]]
