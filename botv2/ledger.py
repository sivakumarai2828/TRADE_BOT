"""Virtual paper ledger (SQLite-backed via Journal kv + trades tables).

Used for INDIA (no broker API) and as US fallback when Alpaca keys are absent.
Fills happen at the latest yfinance price (5-min intraday bar). Good enough
for daily-bar swing paper trading; not for intraday simulation.
"""

from __future__ import annotations

import logging
import os

from . import data
from .journal import Journal

log = logging.getLogger("botv2.ledger")


def _trade_cost(market: str) -> float:
    """Per-side trading cost (fees + slippage) as a fraction of price.

    Defaults: US 0.05%/side (commission-free broker, mostly slippage);
    INDIA 0.15%/side (brokerage + STT + exchange charges + GST + stamp duty).
    Override via env: US_COST_PCT / INDIA_COST_PCT.
    """
    default = "0.0015" if market == "INDIA" else "0.0005"
    return float(os.getenv(f"{market}_COST_PCT", default))


class PaperLedger:
    def __init__(self, journal: Journal, market: str, start_cash: float):
        self.journal = journal
        self.market = market
        if journal.kv_get(f"{market}_cash") is None:
            journal.kv_set(f"{market}_cash", str(start_cash))
            journal.kv_set(f"{market}_start_cash", str(start_cash))

    # ── state ────────────────────────────────────────────────────
    @property
    def cash(self) -> float:
        return float(self.journal.kv_get(f"{self.market}_cash", "0"))

    def _set_cash(self, v: float) -> None:
        self.journal.kv_set(f"{self.market}_cash", str(round(v, 2)))

    def positions(self) -> list[dict]:
        """Open trades enriched with live price and unrealized P&L.

        price_stale=True means the live fetch failed and last_price is just
        the entry price — callers must not make stop/target decisions on it.
        """
        out = []
        for t in self.journal.open_trades(self.market):
            px = data.fetch_price(t["symbol"])
            stale = px is None
            if stale:
                px = t["entry"]
            out.append({
                **t,
                "price_stale": stale,
                "last_price": round(px, 2),
                "market_value": round(px * t["qty"], 2),
                "unrealized_pnl": round((px - t["entry"]) * t["qty"], 2),
                "unrealized_pnl_pct": round((px / t["entry"] - 1) * 100, 2),
            })
        return out

    def equity(self) -> float:
        return round(self.cash + sum(p["market_value"] for p in self.positions()), 2)

    def day_pnl(self) -> float:
        """True day P&L including unrealized moves: current equity minus
        start-of-day equity (review issue #4). Day boundary is market-local."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Kolkata" if self.market == "INDIA" else "America/New_York")
        today = datetime.now(tz).strftime("%Y-%m-%d")
        key_day = f"{self.market}_day_key"
        key_eq = f"{self.market}_day_start_equity"

        if self.journal.kv_get(key_day) != today:
            self.journal.kv_set(key_day, today)
            self.journal.kv_set(key_eq, str(self.equity()))
            return 0.0
        start = float(self.journal.kv_get(key_eq, str(self.equity())))
        return round(self.equity() - start, 2)

    # ── orders ───────────────────────────────────────────────────
    def buy(self, symbol: str, qty: float, stop: float, target: float, thesis: str) -> dict | None:
        px = data.fetch_price(symbol)
        if px is None:
            log.warning("no price for %s, buy skipped", symbol)
            return None
        px = round(px * (1 + _trade_cost(self.market)), 4)  # fees+slippage on entry
        cost = px * qty
        if cost > self.cash:
            qty = float(int(self.cash / px))
            if qty < 1:
                return None
            cost = px * qty
        trade_id = self.journal.open_trade(self.market, symbol, qty, px, stop, target, thesis)
        self._set_cash(self.cash - cost)
        return {"trade_id": trade_id, "symbol": symbol, "qty": qty, "fill": px}

    def sell(self, trade: dict, reason: str) -> dict | None:
        px = data.fetch_price(trade["symbol"])
        if px is None:
            return None
        px = round(px * (1 - _trade_cost(self.market)), 4)  # fees+slippage on exit
        if not self.journal.close_trade(trade["id"], px, reason):
            log.warning("sell %s skipped — trade %s already closed (double-sell guard)",
                        trade["symbol"], trade["id"])
            return None
        self._set_cash(self.cash + px * trade["qty"])
        return {"symbol": trade["symbol"], "qty": trade["qty"], "fill": px,
                "pnl": round((px - trade["entry"]) * trade["qty"], 2)}

    def flatten_all(self, reason: str) -> list[dict]:
        return [r for t in self.journal.open_trades(self.market)
                if (r := self.sell(t, reason))]
