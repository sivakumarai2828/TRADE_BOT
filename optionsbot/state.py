"""Options bot state — positions, metrics, logs."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class OptionsPosition:
    symbol: str              # underlying: SPY / QQQ
    contract_symbol: str     # OPRA: SPY260523C00520000
    option_type: str         # call / put
    strike: float
    expiry: str              # YYYY-MM-DD
    qty: int                 # contracts (1 each)
    entry_premium: float     # per share (cost = × 100)
    current_premium: float
    sl_price: float          # 50% of entry_premium
    tp_price: float          # 200% of entry_premium (100% gain)
    entry_time: str
    pnl: float = 0.0
    pnl_pct: float = 0.0
    highest_premium: float = 0.0


@dataclass
class OptionsMetrics:
    wins_today: int = 0
    losses_today: int = 0
    daily_pnl: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    mode: str = "SAFE"
    daily_loss_halted: bool = False
    daily_start_balance: float = 500.0
    balance: float = 500.0


class OptionsState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.positions: dict[str, OptionsPosition] = {}
        self.metrics = OptionsMetrics()
        self.running = False
        self._logs: list[dict] = []

    def add_log(self, type_: str, message: str, tone: str = "neutral") -> None:
        with self._lock:
            entry = {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "type": type_,
                "message": message,
                "tone": tone,
            }
            self._logs.insert(0, entry)
            if len(self._logs) > 50:
                self._logs.pop()

    @property
    def logs(self) -> list[dict]:
        with self._lock:
            return list(self._logs)


options_state = OptionsState()
