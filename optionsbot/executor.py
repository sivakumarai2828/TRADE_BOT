"""Alpaca options order execution."""
from __future__ import annotations

import logging


def buy_contract(
    contract_symbol: str,
    qty: int,
    api_key: str,
    secret_key: str,
    paper: bool = True,
) -> bool:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        tc = TradingClient(api_key, secret_key, paper=paper)
        order = MarketOrderRequest(
            symbol=contract_symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        result = tc.submit_order(order)
        logging.info("Options BUY submitted: %s qty=%d id=%s", contract_symbol, qty, result.id)
        return True
    except Exception as exc:
        logging.error("Options BUY failed %s: %s", contract_symbol, exc)
        return False


def sell_contract(
    contract_symbol: str,
    qty: int,
    api_key: str,
    secret_key: str,
    paper: bool = True,
) -> bool:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        tc = TradingClient(api_key, secret_key, paper=paper)
        order = MarketOrderRequest(
            symbol=contract_symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            position_intent="sell_to_close",
        )
        result = tc.submit_order(order)
        logging.info("Options SELL submitted: %s qty=%d id=%s", contract_symbol, qty, result.id)
        return True
    except Exception as exc:
        logging.error("Options SELL failed %s: %s", contract_symbol, exc)
        return False
