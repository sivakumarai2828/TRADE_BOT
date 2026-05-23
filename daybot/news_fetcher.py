"""News and earnings context fetcher — yfinance-based, cached, never raises.

Used by options_picker and ai_validator to give AI models real-world context
before making trading decisions. Prevents entries on bad-news dips and
options trades before earnings IV crush.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

# Cache: symbol → (timestamp, news_str, earnings_str)
_news_cache: dict[str, tuple[float, str, str]] = {}
_NEWS_TTL = 1800  # 30 minutes — news doesn't change every minute


def get_news_summary(symbol: str, max_items: int = 5) -> str:
    """Return recent news headlines for symbol as a plain string.

    Returns empty string on any error — never raises.
    Cached 30 minutes to avoid hammering yfinance on every signal cycle.
    """
    cached = _news_cache.get(symbol)
    if cached and (time.time() - cached[0]) < _NEWS_TTL:
        return cached[1]

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        news = ticker.news or []

        lines = []
        for item in news[:max_items]:
            title = item.get("title", "")
            pub_ts = item.get("providerPublishTime", 0)
            if title:
                if pub_ts:
                    age_h = (time.time() - pub_ts) / 3600
                    age_str = f"{age_h:.0f}h ago" if age_h < 48 else f"{age_h/24:.0f}d ago"
                    lines.append(f"• [{age_str}] {title}")
                else:
                    lines.append(f"• {title}")

        result = "\n".join(lines) if lines else "No recent news found."
        _update_cache(symbol, news_str=result)
        logging.debug("News fetched for %s: %d items", symbol, len(lines))
        return result

    except Exception as exc:
        logging.warning("News fetch failed for %s: %s", symbol, exc)
        return ""


def get_earnings_date(symbol: str) -> Optional[str]:
    """Return next earnings date as 'YYYY-MM-DD' string, or None if unknown.

    Cached alongside news. Never raises.
    """
    cached = _news_cache.get(symbol)
    if cached and (time.time() - cached[0]) < _NEWS_TTL:
        return cached[2] if cached[2] else None

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            return None

        # calendar is a dict with 'Earnings Date' key (list of Timestamps)
        earnings_dates = cal.get("Earnings Date", [])
        if not earnings_dates:
            return None

        today = datetime.now(timezone.utc).date()
        for ed in earnings_dates:
            try:
                ed_date = ed.date() if hasattr(ed, "date") else datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
                if ed_date >= today:
                    result = str(ed_date)
                    _update_cache(symbol, earnings_str=result)
                    return result
            except Exception:
                continue
        return None

    except Exception as exc:
        logging.warning("Earnings date fetch failed for %s: %s", symbol, exc)
        return None


def get_news_and_earnings(symbol: str, max_items: int = 5) -> tuple[str, Optional[str]]:
    """Fetch both news summary and earnings date in one call (shares cache).

    Returns (news_str, earnings_date_str_or_None).
    """
    # Fetch both together so cache is populated for both calls
    news = get_news_summary(symbol, max_items=max_items)
    earnings = get_earnings_date(symbol)
    return news, earnings


def earnings_within_days(symbol: str, days: int = 3) -> bool:
    """Return True if next earnings date is within `days` calendar days."""
    ed_str = get_earnings_date(symbol)
    if not ed_str:
        return False
    try:
        ed_date = datetime.strptime(ed_str, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        return (ed_date - today).days <= days
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _update_cache(symbol: str, news_str: str = "", earnings_str: str = "") -> None:
    existing = _news_cache.get(symbol)
    if existing:
        _, prev_news, prev_earn = existing
        news_str = news_str or prev_news
        earnings_str = earnings_str or prev_earn
    _news_cache[symbol] = (time.time(), news_str, earnings_str)
