"""Pre-market analysis — 9:00 AM ET price confirmation of the evening watchlist.

If an evening sub-agent analysis exists for today, this module:
  1. Loads the approved list from last night
  2. Fetches current pre-market snapshot to confirm price is still near the entry zone
  3. Drops any symbol whose pre-market price has moved far outside the entry zone
  4. Stores the confirmed approved list in day_state

If no evening analysis exists (first run, weekend, agent failed), falls back to the
original Claude one-shot ranking from the full universe.
"""
from __future__ import annotations

import functools
import json
import logging
from datetime import datetime, timezone

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
    """Confirm or build the approved watchlist at 9:00 AM ET."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Reload evening analysis from Supabase if state was lost (e.g. service restart) ---
    if not (day_state.evening_analysis_date == today and day_state.evening_approved):
        try:
            from .evening_db import load_evening_analysis
            row = load_evening_analysis(today)
            if row and row.get("approved"):
                logging.info("Pre-market: reloaded evening analysis from Supabase for %s", today)
                with day_state._lock:
                    day_state.evening_approved = row["approved"]
                    day_state.evening_entry_zones = row.get("entry_zones") or {}
                    day_state.evening_risk_flags = row.get("risk_flags") or {}
                    day_state.evening_regime = row.get("regime", "unknown")
                    day_state.evening_notes = row.get("notes") or {}
                    day_state.evening_analysis_date = today
        except Exception as exc:
            logging.warning("Pre-market: Supabase reload failed: %s", exc)

    # --- Try to use last night's evening analysis first ---
    if day_state.evening_analysis_date == today and day_state.evening_approved:
        logging.info("Pre-market: using evening analysis for %s (%d stocks)", today, len(day_state.evening_approved))
        confirmed = _confirm_with_premarket_prices(
            day_state.evening_approved,
            day_state.evening_entry_zones,
            alpaca_api_key,
            alpaca_secret_key,
        )
        with day_state._lock:
            day_state.premarket_approved = confirmed
            day_state.premarket_time = datetime.now(timezone.utc).isoformat()

        day_state.add_log(
            "Pre-market",
            f"Confirmed {len(confirmed)} stocks from evening analysis: {', '.join(confirmed[:8])}{'…' if len(confirmed) > 8 else ''}",
            "positive",
        )
        logging.info("Pre-market confirmed: %s", confirmed)
        return confirmed

    # --- Fallback: full one-shot Claude analysis ---
    logging.info("Pre-market analysis (fallback): analysing %d stocks", len(STOCK_UNIVERSE))
    day_state.add_log("Pre-market", f"No evening analysis — running full scan of {len(STOCK_UNIVERSE)} stocks…", "neutral")

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


def _confirm_with_premarket_prices(
    candidates: list[str],
    entry_zones: dict[str, list[float]],
    api_key: str,
    secret_key: str,
    max_deviation_pct: float = 3.0,
) -> list[str]:
    """Drop symbols whose pre-market price has moved >3% outside the entry zone."""
    snapshots = _fetch_all_snapshots(api_key, secret_key)
    if not snapshots:
        return candidates  # can't confirm, trust evening analysis

    confirmed = []
    for sym in candidates:
        snap = snapshots.get(sym)
        if snap is None:
            confirmed.append(sym)  # no data, keep it
            continue
        try:
            price = float(snap.daily_bar.close)
            zone = entry_zones.get(sym)
            if zone and len(zone) == 2:
                low, high = zone
                # Allow up to max_deviation_pct above the high (momentum still valid)
                upper_limit = high * (1 + max_deviation_pct / 100)
                lower_limit = low * (1 - max_deviation_pct / 100)
                if price > upper_limit:
                    logging.info("Pre-market: %s dropped — price $%.2f too far above entry zone [%.2f–%.2f]", sym, price, low, high)
                    day_state.add_log("Pre-market", f"{sym} removed — gapped up ${price:.2f} above entry zone", "warning")
                    continue
                if price < lower_limit:
                    logging.info("Pre-market: %s dropped — price $%.2f below entry zone [%.2f–%.2f]", sym, price, low, high)
                    day_state.add_log("Pre-market", f"{sym} removed — price ${price:.2f} broke below entry zone", "warning")
                    continue
            confirmed.append(sym)
        except Exception:
            confirmed.append(sym)

    return confirmed if confirmed else candidates


def _patch_timeout(client, seconds: int = 10):
    orig = client._session.request

    @functools.wraps(orig)
    def _req(method, url, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return orig(method, url, **kwargs)

    client._session.request = _req


def _fetch_all_snapshots(api_key: str, secret_key: str) -> dict:
    import threading
    result_box = [{}]
    exc_box = [None]

    def _run():
        try:
            result_box[0] = _fetch_snapshots_impl(api_key, secret_key)
        except Exception as e:
            exc_box[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=15)
    if t.is_alive():
        logging.warning("Pre-market snapshot timed out")
        return {}
    if exc_box[0]:
        logging.warning("Pre-market snapshot failed: %s", exc_box[0])
        return {}
    return result_box[0]


def _fetch_snapshots_impl(api_key: str, secret_key: str) -> dict:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest
    client = StockHistoricalDataClient(api_key, secret_key)
    _patch_timeout(client, 10)
    req = StockSnapshotRequest(symbol_or_symbols=STOCK_UNIVERSE)
    return client.get_stock_snapshot(req)


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
    return summaries[:20]


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
        client = Anthropic(api_key=api_key, timeout=30.0, max_retries=1)
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

        notes = parsed.get("notes", {})
        for sym, note in list(notes.items())[:5]:
            logging.info("Pre-market [%s]: %s", sym, note)

        return approved if approved else [s["symbol"] for s in summaries[:12]]

    except Exception as exc:
        logging.warning("Pre-market Claude call failed: %s", exc)
        return [s["symbol"] for s in summaries[:12]]
