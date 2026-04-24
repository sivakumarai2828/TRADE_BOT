"""Market scanner — filters a curated liquid stock universe by volume and movement."""
from __future__ import annotations
import functools
import logging
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest


def _patch_timeout(client, seconds: int = 10):
    orig = client._session.request

    @functools.wraps(orig)
    def _req(method, url, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return orig(method, url, **kwargs)

    client._session.request = _req

# Curated universe of highly liquid, actively traded US stocks
STOCK_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "NFLX", "CRM", "SHOP", "SQ", "COIN", "PLTR", "RBLX", "SNAP",
    "UBER", "ABNB", "SOFI", "RIVN", "NIO", "BABA", "PYPL", "HOOD",
    "JPM", "BAC", "GS", "V", "MA", "XLF", "SPY", "QQQ", "IWM",
]


class MarketScanner:
    def __init__(self, api_key: str, secret_key: str) -> None:
        self._client = StockHistoricalDataClient(api_key, secret_key)
        _patch_timeout(self._client, 10)

    def _get_snapshots(self, symbols: list[str]) -> dict:
        try:
            req = StockSnapshotRequest(symbol_or_symbols=symbols)
            return self._client.get_stock_snapshot(req)
        except Exception as exc:
            logging.warning("Snapshot fetch failed: %s", exc)
            return {}

    def get_gap_stocks(self, snapshots: dict, min_gap_pct: float = 1.0) -> list[str]:
        result = []
        for sym, snap in snapshots.items():
            try:
                prev = float(snap.prev_daily_bar.close)
                curr = float(snap.daily_bar.open)
                if prev > 0 and abs(curr - prev) / prev * 100 >= min_gap_pct:
                    result.append(sym)
            except Exception:
                pass
        return result

    def get_top_movers(self, snapshots: dict, min_move_pct: float = 1.5) -> list[str]:
        result = []
        for sym, snap in snapshots.items():
            try:
                prev = float(snap.prev_daily_bar.close)
                curr = float(snap.daily_bar.close)
                if prev > 0 and abs(curr - prev) / prev * 100 >= min_move_pct:
                    result.append(sym)
            except Exception:
                pass
        return result

    def get_high_volume_stocks(self, snapshots: dict, multiplier: float = 1.5) -> list[str]:
        result = []
        for sym, snap in snapshots.items():
            try:
                today_vol = float(snap.daily_bar.volume)
                # Estimate avg daily volume from minute bar × 390 trading minutes
                minute_vol = float(snap.minute_bar.volume)
                est_avg = minute_vol * 390
                if est_avg > 0 and today_vol >= est_avg * multiplier:
                    result.append(sym)
            except Exception:
                pass
        return result

    def run_scan(self) -> list[str]:
        """Return up to 15 candidate symbols from the universe."""
        snapshots = self._get_snapshots(STOCK_UNIVERSE)
        if not snapshots:
            logging.warning("Scanner got no data — using default universe")
            return STOCK_UNIVERSE[:12]

        candidates: set[str] = set()
        candidates.update(self.get_gap_stocks(snapshots))
        candidates.update(self.get_top_movers(snapshots))
        candidates.update(self.get_high_volume_stocks(snapshots))
        candidates.update(["SPY", "QQQ"])  # always include market ETFs

        # If filters returned too few, pad with liquid defaults so bot has enough to work with
        if len(candidates) < 6:
            logging.warning("Scanner: only %d candidates after filters — padding with defaults", len(candidates))
            for sym in ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "AMD", "GOOGL"]:
                candidates.add(sym)
                if len(candidates) >= 12:
                    break

        result = list(candidates)[:15]
        logging.info("Scanner: %d candidates — %s", len(result), result)
        return result
