"""Filters to narrow scanned candidates to high-quality setups."""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone, timedelta

# Cache earnings results for 1 hour to avoid hammering yfinance
_earnings_cache: dict[str, tuple[float, bool]] = {}
_EARNINGS_TTL = 3600


def has_earnings_soon(symbol: str, days_ahead: int = 2) -> bool:
    """Return True if symbol has earnings within the next N calendar days.

    Blocks entry — a 20-30% earnings gap can wipe a day's gains instantly.
    Results cached for 1 hour. Fails safe (returns False) on any API error.
    """
    now_ts = time.time()
    cached = _earnings_cache.get(symbol)
    if cached and (now_ts - cached[0]) < _EARNINGS_TTL:
        return cached[1]

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            _earnings_cache[symbol] = (now_ts, False)
            return False

        # yfinance returns a dict; Earnings Date may be a list or single value
        dates_raw = cal.get("Earnings Date") if isinstance(cal, dict) else []
        if dates_raw is None:
            _earnings_cache[symbol] = (now_ts, False)
            return False

        if not isinstance(dates_raw, (list, tuple)):
            dates_raw = [dates_raw]

        today = datetime.now(timezone.utc).date()
        cutoff = today + timedelta(days=days_ahead)

        result = any(
            today <= (d.date() if hasattr(d, "date") else d) <= cutoff
            for d in dates_raw
        )
        _earnings_cache[symbol] = (now_ts, result)
        if result:
            logging.info("Earnings filter: %s has earnings within %d days — BUY blocked", symbol, days_ahead)
        return result
    except Exception as exc:
        logging.warning("Earnings check failed [%s]: %s — allowing trade", symbol, exc)
        _earnings_cache[symbol] = (now_ts, False)
        return False


class StockFilter:

    def trend_filter(self, price: float, ema: float) -> bool:
        """Price must be above EMA (confirmed uptrend)."""
        return price > ema

    def pullback_filter(self, price: float, ema: float, max_pct: float = 1.5) -> bool:
        """Price within 1.5% above EMA — healthy pullback, not extended."""
        if ema <= 0:
            return False
        pct_above = (price - ema) / ema * 100
        return 0.0 <= pct_above <= max_pct

    def volume_filter(self, current_vol: float, avg_vol: float, multiplier: float = 1.2) -> bool:
        """Current volume must exceed average × multiplier."""
        return avg_vol > 0 and current_vol >= avg_vol * multiplier

    def volatility_filter(self, day_change_pct: float, min_pct: float = 0.5, max_pct: float = 5.0) -> bool:
        """Active but not chaotic: moved 0.5–5% today."""
        return min_pct <= abs(day_change_pct) <= max_pct

    def sideways_filter(self, rsi: float) -> bool:
        """Exclude RSI 45–55 (no clear direction)."""
        return not (45.0 <= rsi <= 55.0)

    def rsi_buy_range(self, rsi: float) -> bool:
        """RSI 35–45: oversold pullback entering recovery zone."""
        return 35.0 <= rsi <= 45.0

    def apply_all_filters(self, candidates: list[dict], max_results: int = 8) -> list[dict]:
        """Run every filter and return top candidates sorted by RSI proximity to 40."""
        passed = []
        for c in candidates:
            sym = c.get("symbol", "?")
            price = c.get("price", 0.0)
            ema = c.get("ema", 0.0)
            rsi = c.get("rsi", 50.0)
            vol = c.get("volume", 0.0)
            avg_vol = c.get("avg_volume", 0.0)
            day_chg = c.get("day_change_pct", 0.0)

            checks = [
                (self.trend_filter(price, ema), "trend"),
                (self.pullback_filter(price, ema), "pullback"),
                (self.sideways_filter(rsi), "sideways"),
                (self.rsi_buy_range(rsi), "rsi_range"),
                (self.volatility_filter(day_chg), "volatility"),
            ]
            failed = [name for ok, name in checks if not ok]
            if failed:
                logging.debug("Filtered %s — failed: %s", sym, failed)
                continue
            passed.append(c)

        passed.sort(key=lambda x: abs(x.get("rsi", 50.0) - 40.0))
        result = passed[:max_results]
        logging.info("Filters: %d in → %d passed", len(candidates), len(result))
        return result
