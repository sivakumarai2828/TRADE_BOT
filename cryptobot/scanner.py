"""Crypto market scanner — fetches OHLCV and filters curated pairs by volume/movement."""
from __future__ import annotations
import logging
import ccxt
import pandas as pd

# Curated liquid crypto pairs — health-checked on every build_watchlist() call
CRYPTO_UNIVERSE = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "AVAX/USDT",
]

# Minimum 24h volume in USD to consider a pair liquid enough to trade
_MIN_VOLUME_USD = 10_000_000  # $10M


class CryptoScanner:
    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        secret: str = "",
    ) -> None:
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"ccxt does not support exchange '{exchange_id}'")
        self._exchange: ccxt.Exchange = exchange_class(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
                "timeout": 10_000,  # 10-second hard timeout on all API calls
            }
        )
        self._exchange_id = exchange_id

    # ------------------------------------------------------------------
    # OHLCV
    # ------------------------------------------------------------------

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 100,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles and return as a clean DataFrame."""
        _supported = {"1m", "5m", "15m", "1h", "4h", "1d"}
        if timeframe not in _supported:
            logging.warning("Unsupported timeframe '%s' — defaulting to '1h'", timeframe)
            timeframe = "1h"
        try:
            raw = self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if not raw:
                logging.warning("get_ohlcv: no data returned for %s [%s]", symbol, timeframe)
                return pd.DataFrame()
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df[["open", "high", "low", "close", "volume"]] = df[
                ["open", "high", "low", "close", "volume"]
            ].apply(pd.to_numeric, errors="coerce")
            df = df.dropna()
            return df.reset_index(drop=True)
        except Exception as exc:
            logging.warning("get_ohlcv failed [%s %s]: %s", symbol, timeframe, exc)
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def build_watchlist(self) -> list[str]:
        """Return curated pairs that pass the $10M 24h volume health check."""
        watchlist: list[str] = []
        for symbol in CRYPTO_UNIVERSE:
            try:
                ticker = self._exchange.fetch_ticker(symbol)
                volume_usd = float(ticker.get("quoteVolume") or 0)
                if volume_usd >= _MIN_VOLUME_USD:
                    watchlist.append(symbol)
                    logging.info(
                        "Watchlist: %s included — 24h vol $%.0fM",
                        symbol, volume_usd / 1_000_000,
                    )
                else:
                    logging.warning(
                        "Watchlist: %s excluded — 24h vol $%.0fM < $10M",
                        symbol, volume_usd / 1_000_000,
                    )
            except Exception as exc:
                logging.warning("Watchlist: skipping %s — fetch error: %s", symbol, exc)
        logging.info("Watchlist built: %s", watchlist)
        return watchlist

    # ------------------------------------------------------------------
    # Top movers
    # ------------------------------------------------------------------

    def get_top_movers(self, min_change_pct: float = 3.0) -> list[str]:
        """Return curated pairs with absolute 24h change >= min_change_pct."""
        movers: list[str] = []
        for symbol in CRYPTO_UNIVERSE:
            try:
                ticker = self._exchange.fetch_ticker(symbol)
                change_pct = float(ticker.get("percentage") or 0)
                if abs(change_pct) >= min_change_pct:
                    movers.append(symbol)
                    logging.info(
                        "Top mover: %s — 24h change %.1f%%", symbol, change_pct
                    )
            except Exception as exc:
                logging.warning("get_top_movers: skipping %s — %s", symbol, exc)
        return movers
