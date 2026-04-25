"""Morning options suggestions — picks 1-3 intraday options setups from the day's watchlist.

Runs at 9:15 AM ET after pre-market confirms the watchlist.
Uses yfinance for options chain + VIX. Claude Sonnet picks the best setup per symbol.
Suggestions only — user trades manually on Robinhood.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

# Liquid symbols we allow options on (need tight bid/ask + high OI)
OPTIONABLE = {
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "SPY", "QQQ", "NFLX", "CRM", "JPM", "BAC", "V", "MA",
}

MAX_PICKS = 3  # max options suggestions per day


def run_options_analysis(anthropic_api_key: str) -> list[dict]:
    """Main entry. Returns list of option pick dicts, saves to day_state + Supabase + Telegram."""
    from .state import day_state

    watchlist = day_state.premarket_approved or day_state.evening_approved
    if not watchlist:
        logging.warning("Options picker: no watchlist available — skipping")
        return []

    candidates = [s for s in watchlist if s in OPTIONABLE][:6]
    if not candidates:
        logging.warning("Options picker: no optionable symbols in watchlist %s", watchlist)
        return []

    vix = _get_vix()
    regime = day_state.evening_regime or "unknown"
    direction_map = day_state.evening_direction  # symbol → BUY/SELL from evening agent

    picks = []
    for symbol in candidates:
        if len(picks) >= MAX_PICKS:
            break
        try:
            pick = _analyze_symbol(
                symbol=symbol,
                anthropic_api_key=anthropic_api_key,
                vix=vix,
                regime=regime,
                direction=direction_map.get(symbol, "BUY"),
                entry_zone=day_state.evening_entry_zones.get(symbol, []),
                notes=day_state.evening_notes.get(symbol, ""),
            )
            if pick:
                picks.append(pick)
        except Exception as exc:
            logging.warning("Options picker failed for %s: %s", symbol, exc)

    if not picks:
        logging.info("Options picker: no strong setups found today")
        return []

    trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with day_state._lock:
        day_state.options_picks = picks
        day_state.options_picks_date = trade_date

    _save_to_supabase(trade_date, picks)
    _notify_telegram(picks, vix, regime)
    logging.info("Options picker: %d picks for %s", len(picks), trade_date)
    return picks


# ---------------------------------------------------------------------------
# Per-symbol analysis
# ---------------------------------------------------------------------------

def _analyze_symbol(
    symbol: str,
    anthropic_api_key: str,
    vix: float,
    regime: str,
    direction: str,
    entry_zone: list,
    notes: str,
) -> Optional[dict]:
    chain_data = _get_options_chain(symbol)
    if not chain_data:
        return None

    import anthropic, json
    client = anthropic.Anthropic(api_key=anthropic_api_key, timeout=25.0, max_retries=1)

    prompt = f"""You are an options trading advisor. Analyze this setup and recommend one options trade.

Symbol: {symbol}
Direction bias: {direction} (from overnight analysis)
Market regime: {regime}
VIX: {vix:.1f} ({"high — options expensive, prefer spreads or smaller size" if vix > 25 else "normal — buying options reasonable"})
Evening analysis notes: {notes or "none"}
Entry zone from analysis: {entry_zone}

Available options (nearest weekly expiry):
{json.dumps(chain_data, indent=2)}

Rules:
- Only recommend BUYING a call (if bullish) or put (if bearish). No selling/spreads.
- Pick ATM or 1 strike OTM for best risk/reward.
- Skip if bid/ask spread > 15% of mid price (poor liquidity).
- Skip if open interest < 500 (illiquid).
- Skip if VIX > 30 and this is not SPY/QQQ (individual stocks too risky in panic).
- Entry: use mid price between bid and ask.
- Target: 80-100% gain on the option (not 2x underlying).
- Underlying stop: the stock/ETF price level where the thesis is broken.

Respond ONLY with JSON (or null if no good setup):
{{
  "symbol": "{symbol}",
  "option_type": "call",
  "strike": 880.0,
  "expiry": "2026-05-02",
  "entry_price": 8.50,
  "target_price": 16.00,
  "underlying_stop": 865.0,
  "open_interest": 2840,
  "iv": 0.42,
  "reason": "one sentence"
}}"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.lower() == "null" or not text.startswith("{"):
            logging.info("Options picker: Claude skipped %s — %s", symbol, text[:80])
            return None
        pick = json.loads(text)
        pick["asset_type"] = "option"
        return pick
    except Exception as exc:
        logging.warning("Options Claude call failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _get_vix() -> float:
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(period="1d")
        return float(vix["Close"].iloc[-1]) if not vix.empty else 18.0
    except Exception:
        return 18.0


def _get_options_chain(symbol: str) -> Optional[list[dict]]:
    """Fetch nearest weekly expiry chain, return ATM ± 3 strikes as list of dicts."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        exps = ticker.options
        if not exps:
            return None

        # Pick expiry 5-14 DTE (weekly)
        today = datetime.now(timezone.utc).date()
        target = None
        for exp in exps:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if 4 <= dte <= 14:
                target = exp
                break
        if not target:
            target = exps[0]  # fallback: nearest expiry

        chain = ticker.option_chain(target)
        hist = ticker.history(period="1d")
        if hist.empty:
            return None
        current_price = float(hist["Close"].iloc[-1])

        rows = []
        for opt_type, df in [("call", chain.calls), ("put", chain.puts)]:
            # ATM ± 3 strikes
            df = df.copy()
            df["dist"] = abs(df["strike"] - current_price)
            nearest = df.nsmallest(6, "dist")
            for _, row in nearest.iterrows():
                mid = (float(row.get("bid", 0)) + float(row.get("ask", 0))) / 2
                rows.append({
                    "type": opt_type,
                    "strike": float(row["strike"]),
                    "expiry": target,
                    "bid": round(float(row.get("bid", 0)), 2),
                    "ask": round(float(row.get("ask", 0)), 2),
                    "mid": round(mid, 2),
                    "volume": int(row.get("volume", 0) or 0),
                    "open_interest": int(row.get("openInterest", 0) or 0),
                    "iv": round(float(row.get("impliedVolatility", 0)), 3),
                })

        return rows if rows else None
    except Exception as exc:
        logging.warning("Options chain fetch failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Persistence + notifications
# ---------------------------------------------------------------------------

def _save_to_supabase(trade_date: str, picks: list[dict]) -> None:
    try:
        from persistence import _get_client
        c = _get_client()
        if not c:
            return
        c.table("options_suggestions").upsert({
            "trade_date": trade_date,
            "picks": picks,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:
        logging.warning("Options suggestions Supabase save failed: %s", exc)


def _notify_telegram(picks: list[dict], vix: float, regime: str) -> None:
    try:
        from telegram_notify import notify_options_suggestions
        notify_options_suggestions(picks, vix, regime)
    except Exception as exc:
        logging.warning("Options Telegram notify failed: %s", exc)
