"""Monitor open positions and trigger stop-loss / take-profit exits."""
from __future__ import annotations
import functools
import logging
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from .db import save_trade as db_save_trade
from .executor import TradeExecutor
from .risk_manager import RiskManager
from .state import DayBotState, DayPosition


def _patch_timeout(client, seconds: int = 10):
    orig = client._session.request

    @functools.wraps(orig)
    def _req(method, url, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return orig(method, url, **kwargs)

    client._session.request = _req


class PositionMonitor:
    def __init__(
        self,
        data_client: StockHistoricalDataClient,
        executor: TradeExecutor,
        risk_manager: RiskManager,
        state: DayBotState,
        stop_loss_pct: float = 0.01,
        take_profit_pct: float = 0.025,
    ) -> None:
        _patch_timeout(data_client, 10)
        self._data = data_client
        self._executor = executor
        self._risk = risk_manager
        self._state = state
        self._sl_pct = stop_loss_pct
        self._tp_pct = take_profit_pct

    def _latest_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trades = self._data.get_stock_latest_trade(req)
        return float(trades[symbol].price)

    def _close(self, symbol: str, pos: DayPosition, reason: str) -> None:
        try:
            self._executor.place_sell_order(symbol, pos.qty)
            pnl = (pos.current_price - pos.entry_price) * pos.qty
            pnl_pct = (pos.current_price - pos.entry_price) / pos.entry_price * 100
            tone = "positive" if pnl >= 0 else "negative"
            self._state.add_log(
                "Closed",
                f"{symbol} @ ${pos.current_price:.2f} | {reason} | PnL ${pnl:+.2f} ({pnl_pct:+.1f}%)",
                tone,
            )
            with self._state._lock:
                self._state.positions.pop(symbol, None)
                m = self._state.metrics
                m.daily_pnl += round(pnl, 2)
                if m.daily_start_value > 0:
                    m.daily_pnl_pct = round(m.daily_pnl / m.daily_start_value * 100, 2)
            self._risk.deregister_trade(symbol)
            self._state.record_trade_result(pnl)
            db_save_trade(
                symbol=symbol, entry_price=pos.entry_price,
                exit_price=pos.current_price, qty=pos.qty,
                pnl=pnl, pnl_pct=pnl_pct, exit_reason=reason,
                ai_confidence=getattr(pos, "_ai_confidence", 0.0),
                ai_reason=getattr(pos, "_ai_reason", ""),
                weekly_context=getattr(pos, "_weekly_context", None),
            )
            logging.info("Closed %s | %s | pnl=%+.2f", symbol, reason, pnl)
        except Exception as exc:
            logging.error("Failed to close %s: %s", symbol, exc)

    def monitor_positions(self) -> None:
        for symbol in list(self._state.positions.keys()):
            pos = self._state.positions.get(symbol)
            if pos is None:
                continue
            try:
                price = self._latest_price(symbol)
                pnl = (price - pos.entry_price) * pos.qty
                pnl_pct = (price - pos.entry_price) / pos.entry_price * 100

                with self._state._lock:
                    if symbol in self._state.positions:
                        self._state.positions[symbol].current_price = price
                        self._state.positions[symbol].pnl = round(pnl, 2)
                        self._state.positions[symbol].pnl_pct = round(pnl_pct, 2)

                logging.info("Monitor %s | $%.2f | SL $%.2f TP $%.2f | pnl %+.2f%%",
                             symbol, price, pos.stop_loss, pos.take_profit, pnl_pct)

                if price <= pos.stop_loss:
                    self._close(symbol, pos, "stop_loss")
                elif price >= pos.take_profit:
                    self._close(symbol, pos, "take_profit")

            except Exception as exc:
                logging.warning("Monitor error [%s]: %s", symbol, exc)
