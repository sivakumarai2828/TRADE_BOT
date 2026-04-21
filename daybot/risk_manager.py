"""Risk management — trade gates, position sizing, daily loss limit."""
from __future__ import annotations
import logging
from datetime import date


class RiskManager:
    def __init__(
        self,
        max_trades_per_day: int = 3,
        max_concurrent: int = 2,
        position_size_pct: float = 0.05,
        max_daily_loss_pct: float = 0.03,
    ) -> None:
        self.max_trades_per_day = max_trades_per_day
        self.max_concurrent = max_concurrent
        self.position_size_pct = position_size_pct
        self.max_daily_loss_pct = max_daily_loss_pct

        self._trades_today: int = 0
        self._daily_start_value: float = 0.0
        self._daily_loss_halted: bool = False
        self._active_symbols: set[str] = set()
        self._date: date = date.today()

    def reset_daily(self, portfolio_value: float) -> None:
        self._trades_today = 0
        self._daily_start_value = portfolio_value
        self._daily_loss_halted = False
        self._active_symbols.clear()
        self._date = date.today()
        logging.info("Risk reset — daily start value: $%.2f", portfolio_value)

    def _auto_reset_if_new_day(self, portfolio_value: float) -> None:
        if date.today() != self._date:
            self.reset_daily(portfolio_value)

    def can_trade(self, symbol: str, portfolio_value: float) -> tuple[bool, str]:
        self._auto_reset_if_new_day(portfolio_value)
        if self._daily_loss_halted:
            return False, "Daily loss limit hit — trading paused for today"
        if self._trades_today >= self.max_trades_per_day:
            return False, f"Max {self.max_trades_per_day} trades/day reached"
        if len(self._active_symbols) >= self.max_concurrent:
            return False, f"Max {self.max_concurrent} concurrent positions active"
        if symbol in self._active_symbols:
            return False, f"Duplicate — already holding {symbol}"
        return True, "ok"

    def check_daily_loss(self, current_value: float) -> bool:
        """Returns True (and halts) if daily loss exceeds threshold."""
        if self._daily_start_value <= 0:
            return False
        loss_pct = (self._daily_start_value - current_value) / self._daily_start_value
        if loss_pct >= self.max_daily_loss_pct:
            self._daily_loss_halted = True
            logging.warning("Daily loss limit: %.1f%% — halting", loss_pct * 100)
            return True
        return False

    def calculate_position_size(self, portfolio_value: float, price: float, state=None) -> int:
        """Delegate to state for mode-aware sizing; fallback to fixed pct."""
        if state is not None:
            return state.calculate_position_size(portfolio_value, price)
        if price <= 0:
            return 1
        qty = int(portfolio_value * self.position_size_pct / price)
        return max(1, qty)

    def register_trade(self, symbol: str) -> None:
        self._trades_today += 1
        self._active_symbols.add(symbol)

    def deregister_trade(self, symbol: str) -> None:
        self._active_symbols.discard(symbol)

    @property
    def is_halted(self) -> bool:
        return self._daily_loss_halted

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def active_symbols(self) -> set[str]:
        return set(self._active_symbols)
