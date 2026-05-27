"""Options bot state — positions, metrics, logs.

Persists balance + cumulative stats to JSON so restarts don't wipe 2-week paper track record.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

_STATE_FILE = os.path.join(os.path.dirname(__file__), "options_state.json")


@dataclass
class OptionsPosition:
    symbol: str              # underlying: NVDA / TSLA / SPY etc.
    contract_symbol: str     # OPRA: NVDA260523C00120000
    option_type: str         # call / put
    strike: float
    expiry: str              # YYYY-MM-DD
    qty: int                 # contracts (1 each)
    entry_premium: float     # per share (cost = × 100)
    current_premium: float
    sl_price: float          # 50% loss
    tp_price: float          # 200% gain = 3x
    entry_time: str
    pnl: float = 0.0
    pnl_pct: float = 0.0
    highest_premium: float = 0.0
    tp1_hit: bool = False    # True after 50% partial sold at +100%


@dataclass
class OptionsMetrics:
    # ── Daily (reset at midnight) ──
    wins_today: int = 0
    losses_today: int = 0
    daily_pnl: float = 0.0
    daily_loss_halted: bool = False
    daily_start_balance: float = 1000.0

    # ── Cumulative (persisted across restarts) ──
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    win_rate: float = 0.0
    balance: float = 1000.0
    peak_balance: float = 1000.0
    total_pnl: float = 0.0

    # ── Session meta ──
    mode: str = "SAFE"
    monitor_start_date: str = ""   # set once, tracks when 2-week period began
    last_trade_date: str = ""


class OptionsState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.positions: dict[str, OptionsPosition] = {}
        self.metrics = OptionsMetrics()
        self.running = False
        self._logs: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load persisted metrics from JSON. Called on startup."""
        if not os.path.exists(_STATE_FILE):
            # First run — set monitor start date
            self.metrics.monitor_start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._save()
            return
        try:
            with open(_STATE_FILE) as f:
                data = json.load(f)
            m = self.metrics
            m.balance            = float(data.get("balance", 1000.0))
            m.peak_balance       = float(data.get("peak_balance", m.balance))
            m.total_trades       = int(data.get("total_trades", 0))
            m.total_wins         = int(data.get("total_wins", 0))
            m.total_losses       = int(data.get("total_losses", 0))
            m.total_pnl          = float(data.get("total_pnl", 0.0))
            m.win_rate           = float(data.get("win_rate", 0.0))
            m.monitor_start_date = data.get("monitor_start_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            m.last_trade_date    = data.get("last_trade_date", "")
            m.daily_start_balance = m.balance  # reset daily baseline to current balance
            logging.info(
                "Options state loaded: balance=$%.2f trades=%d wins=%d losses=%d since=%s",
                m.balance, m.total_trades, m.total_wins, m.total_losses, m.monitor_start_date,
            )
        except Exception as exc:
            logging.warning("Options state load failed: %s — starting fresh", exc)

    def _save(self) -> None:
        """Persist cumulative metrics to JSON. Call after every trade close."""
        try:
            m = self.metrics
            data = {
                "balance":            m.balance,
                "peak_balance":       m.peak_balance,
                "total_trades":       m.total_trades,
                "total_wins":         m.total_wins,
                "total_losses":       m.total_losses,
                "total_pnl":          m.total_pnl,
                "win_rate":           m.win_rate,
                "monitor_start_date": m.monitor_start_date,
                "last_trade_date":    m.last_trade_date,
                "saved_at":           datetime.now(timezone.utc).isoformat(),
            }
            with open(_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logging.warning("Options state save failed: %s", exc)

    def record_trade_close(self, pnl: float) -> None:
        """Call after every trade close to update cumulative stats + persist."""
        with self._lock:
            m = self.metrics
            m.total_trades  += 1
            m.total_pnl      = round(m.total_pnl + pnl, 2)
            m.last_trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if pnl >= 0:
                m.total_wins += 1
            else:
                m.total_losses += 1
            total = m.total_wins + m.total_losses
            m.win_rate = round(m.total_wins / total * 100, 1) if total > 0 else 0.0
            m.peak_balance = max(m.peak_balance, m.balance)
        self._save()

    def add_log(self, type_: str, message: str, tone: str = "neutral") -> None:
        with self._lock:
            entry = {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "type": type_,
                "message": message,
                "tone": tone,
            }
            self._logs.insert(0, entry)
            if len(self._logs) > 100:
                self._logs.pop()

    @property
    def logs(self) -> list[dict]:
        with self._lock:
            return list(self._logs)

    def daily_reset(self) -> None:
        """Reset daily counters at midnight. Preserves cumulative stats."""
        with self._lock:
            m = self.metrics
            m.wins_today = 0
            m.losses_today = 0
            m.daily_pnl = 0.0
            m.daily_loss_halted = False
            m.daily_start_balance = m.balance
        self.add_log("Daily reset", "Daily counters reset — cumulative stats preserved", "neutral")

    def summary(self) -> dict:
        """Return 2-week monitoring summary."""
        m = self.metrics
        elapsed_days = 0
        if m.monitor_start_date:
            try:
                start = datetime.strptime(m.monitor_start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                elapsed_days = (datetime.now(timezone.utc) - start).days
            except Exception:
                pass
        return {
            "monitor_start": m.monitor_start_date,
            "elapsed_days": elapsed_days,
            "balance": m.balance,
            "peak_balance": m.peak_balance,
            "total_pnl": m.total_pnl,
            "pnl_pct": round((m.balance - 1000.0) / 1000.0 * 100, 1),
            "total_trades": m.total_trades,
            "total_wins": m.total_wins,
            "total_losses": m.total_losses,
            "win_rate": m.win_rate,
        }


options_state = OptionsState()
