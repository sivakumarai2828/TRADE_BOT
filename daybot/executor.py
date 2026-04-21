"""Alpaca order execution with retry logic — paper trading only."""
from __future__ import annotations
import logging
import time
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


class TradeExecutor:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True, budget: float = 0.0) -> None:
        self._client = TradingClient(api_key, secret_key, paper=paper)
        self._budget = budget  # if > 0, cap reported portfolio value to this amount

    def _with_retry(self, fn, retries: int = 3):
        for attempt in range(retries):
            try:
                return fn()
            except Exception as exc:
                wait = 2 ** attempt
                logging.warning("Attempt %d/%d failed: %s — retrying in %ds",
                                attempt + 1, retries, exc, wait)
                if attempt < retries - 1:
                    time.sleep(wait)
        raise RuntimeError(f"All {retries} attempts failed")

    def place_buy_order(self, symbol: str, qty: int) -> dict:
        def _do():
            req = MarketOrderRequest(
                symbol=symbol, qty=qty,
                side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            logging.info("BUY submitted: %s qty=%d id=%s", symbol, qty, order.id)
            return {"id": str(order.id), "symbol": symbol, "qty": qty, "side": "buy"}
        return self._with_retry(_do)

    def place_sell_order(self, symbol: str, qty: int) -> dict:
        def _do():
            req = MarketOrderRequest(
                symbol=symbol, qty=qty,
                side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            logging.info("SELL submitted: %s qty=%d id=%s", symbol, qty, order.id)
            return {"id": str(order.id), "symbol": symbol, "qty": qty, "side": "sell"}
        return self._with_retry(_do)

    def close_all_positions(self) -> None:
        self._with_retry(lambda: self._client.close_all_positions(cancel_orders=True))
        logging.info("All positions closed via Alpaca")

    def get_open_positions(self) -> list:
        return self._with_retry(lambda: self._client.get_all_positions())

    def get_portfolio_value(self) -> float:
        account = self._with_retry(lambda: self._client.get_account())
        value = float(account.portfolio_value)
        if self._budget > 0:
            value = min(value, self._budget)
        return value

    def get_cash(self) -> float:
        account = self._with_retry(lambda: self._client.get_account())
        cash = float(account.cash)
        if self._budget > 0:
            cash = min(cash, self._budget)
        return cash

    def is_market_open(self) -> bool:
        clock = self._with_retry(lambda: self._client.get_clock())
        return bool(clock.is_open)
