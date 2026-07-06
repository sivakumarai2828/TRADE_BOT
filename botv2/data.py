"""Market data layer (yfinance, daily bars).

Indicators here are CONTEXT for the AI, not trade gates. Nothing in this
module decides anything.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger("botv2.data")


def _safe(x) -> float | None:
    try:
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else round(f, 4)
    except Exception:
        return None


def fetch_snapshot(symbol: str, period: str = "1y") -> dict | None:
    """One symbol -> rich daily-bar snapshot dict for the AI prompt."""
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
        if df is None or len(df) < 60:
            return None

        close, vol = df["Close"], df["Volume"]
        high, low = df["High"], df["Low"]
        last = float(close.iloc[-1])

        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean() if len(close) >= 200 else None
        ema20 = close.ewm(span=20, adjust=False).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))

        tr = (high - low).combine((high - close.shift()).abs(), max).combine(
            (low - close.shift()).abs(), max
        )
        atr14 = tr.rolling(14).mean()

        def ret(days: int) -> float | None:
            if len(close) <= days:
                return None
            return _safe((last / float(close.iloc[-1 - days]) - 1) * 100)

        hi52 = float(high.tail(252).max()) if len(high) >= 252 else float(high.max())
        lo52 = float(low.tail(252).min()) if len(low) >= 252 else float(low.min())

        vol20 = vol.rolling(20).mean()
        vol50 = vol.rolling(50).mean()

        return {
            "symbol": symbol,
            "price": _safe(last),
            "sma50": _safe(sma50.iloc[-1]),
            "sma200": _safe(sma200.iloc[-1]) if sma200 is not None else None,
            "ema20": _safe(ema20.iloc[-1]),
            "rsi14": _safe(rsi.iloc[-1]),
            "atr14": _safe(atr14.iloc[-1]),
            "ret_5d_pct": ret(5),
            "ret_1m_pct": ret(21),
            "ret_3m_pct": ret(63),
            "ret_6m_pct": ret(126),
            "pct_off_52w_high": _safe((last / hi52 - 1) * 100),
            "pct_above_52w_low": _safe((last / lo52 - 1) * 100),
            "vol20_vs_vol50": _safe(float(vol20.iloc[-1]) / float(vol50.iloc[-1]))
            if float(vol50.iloc[-1] or 0) > 0 else None,
            "last_5_closes": [_safe(c) for c in close.tail(5).tolist()],
        }
    except Exception as exc:
        log.warning("snapshot failed for %s: %s", symbol, exc)
        return None


def fetch_price(symbol: str) -> float | None:
    """Latest executable price: 5-minute intraday bar, daily-bar fallback.

    Used for fills AND stop/target monitoring, so it must reflect the
    current session, not yesterday's close (review issue #1).
    """
    try:
        import yfinance as yf

        t = yf.Ticker(symbol)
        df = t.history(period="1d", interval="5m")
        if df is None or df.empty:
            df = t.history(period="5d", interval="1d")
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as exc:
        log.warning("price fetch failed for %s: %s", symbol, exc)
        return None


def fetch_market_context(benchmarks: list[str]) -> list[dict]:
    out = []
    for b in benchmarks:
        snap = fetch_snapshot(b, period="6mo")
        if snap:
            out.append(
                {k: snap[k] for k in (
                    "symbol", "price", "sma50", "rsi14",
                    "ret_5d_pct", "ret_1m_pct", "ret_3m_pct",
                )}
            )
    return out
