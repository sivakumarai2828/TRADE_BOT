"""Thread-safe shared state for the trading bot.

All modules (execution, strategy, api) read and write through this single
shared object so the Flask API always reflects the current bot state without
any race conditions.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


PAPER_INITIAL_USDT = 10_000.0


@dataclass
class SignalData:
    symbol: str = "BTC/USDT"
    action: str = "HOLD"
    confidence: int = 0
    rsi: float = 0.0
    price: float = 0.0
    sma: float = 0.0
    trend: str = "\u2014"
    explanation: str = "Bot not started yet."
    rule_signal: str = "HOLD"
    claude_signal: str = "HOLD"
    timestamp: str = ""


@dataclass
class PositionData:
    symbol: str
    amount: float
    entry: float
    current: float
    pnl: float
    pnl_pct: float
    stop_loss: float
    take_profit: float
    highest_price: float
    is_house_trade: bool = False


@dataclass
class LogEntry:
    time: str
    type: str
    message: str
    tone: str


@dataclass
class Metrics:
    balance: float = PAPER_INITIAL_USDT
    balance_detail: str = "Paper trading"
    pnl: float = 0.0
    pnl_pct: float = 0.0
    active_trades: int = 0
    risk_exposure: float = 0.0
    risk_exposure_pct: float = 0.0
    paper_usdt: float = PAPER_INITIAL_USDT
    paper_holdings: dict = field(default_factory=dict)
    principal: float = PAPER_INITIAL_USDT
    profit_pool: float = 0.0
    house_trade_active: bool = False
    # Auto-Shield tracking
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    peak_balance: float = PAPER_INITIAL_USDT
    shield_active: bool = False
    pre_shield_mode: str = "fixed"
    trade_history: list = field(default_factory=list)  # last 20 bools: True=win


@dataclass
class BotSettings:
    trade_size_usdt: float = 50.0
    trade_size_mode: str = "fixed"
    trade_size_pct: float = 50.0
    stop_loss_pct: float = 2.0
    take_profit_pct: float = 5.0
    polling_seconds: int = 60
    auto_mode: bool = True
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    house_profit_threshold: float = 2.0
    house_take_profit_pct: float = 15.0
    house_stop_loss_pct: float = 50.0
    active_symbols: list = field(default_factory=lambda: ["BTC/USD"])
    # Auto-Shield settings
    shield_enabled: bool = True
    shield_loss_streak: int = 5        # consecutive losses to trigger
    shield_winrate_min: float = 40.0   # win rate % below which shield triggers
    shield_drawdown_pct: float = 10.0  # % drop from peak balance to trigger
    shield_recovery_winrate: float = 55.0  # win rate % to deactivate shield


class BotState:
    """Central, lock-protected state container."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.running: bool = False
        self.paper_mode: bool = True
        self.exchange_name: str = "—"
        self.signals: dict[str, SignalData] = {}
        self.positions: dict[str, Optional[PositionData]] = {}
        self.logs: list[LogEntry] = []
        self.metrics: Metrics = Metrics()
        self.settings: BotSettings = BotSettings()
        self._last_prices: dict[str, float] = {}

    def add_log(self, log_type: str, message: str, tone: str = "neutral") -> None:
        with self._lock:
            entry = LogEntry(
                time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                type=log_type,
                message=message,
                tone=tone,
            )
            self.logs.insert(0, entry)
            self.logs = self.logs[:100]

    def update_signal(self, symbol: str, **kwargs) -> None:
        with self._lock:
            if symbol not in self.signals:
                self.signals[symbol] = SignalData(symbol=symbol)
            sig = self.signals[symbol]
            for k, v in kwargs.items():
                if hasattr(sig, k):
                    setattr(sig, k, v)
            sig.timestamp = datetime.now(timezone.utc).isoformat()

    def get_position(self, symbol: str) -> Optional[PositionData]:
        with self._lock:
            return self.positions.get(symbol)

    def set_position(self, symbol: str, pos: Optional[PositionData]) -> None:
        with self._lock:
            self.positions[symbol] = pos
            self._recalc_exposure()

    def update_metrics(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.metrics, k):
                    setattr(self.metrics, k, v)

    def update_settings(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.settings, k):
                    setattr(self.settings, k, v)

    def refresh_paper_balance(self, symbol: str, current_price: float) -> None:
        with self._lock:
            self._last_prices[symbol] = current_price
            total = self.metrics.paper_usdt
            detail_parts = [f"${self.metrics.paper_usdt:,.2f} USDT"]
            for base_cur, amount in self.metrics.paper_holdings.items():
                price = self._last_prices.get(f"{base_cur}/USDT", 0)
                total += amount * price
                if amount > 0:
                    detail_parts.append(f"{amount:.6f} {base_cur}")
            self.metrics.balance = round(total, 2)
            principal = self.metrics.principal or PAPER_INITIAL_USDT
            gained = total - principal
            self.metrics.pnl = round(gained, 2)
            self.metrics.pnl_pct = round(gained / principal * 100, 2)
            self.metrics.balance_detail = "Paper: " + " + ".join(detail_parts)

    def record_trade_result(self, pnl: float) -> None:
        """Record win/loss and auto-activate or deactivate shield."""
        is_win = pnl > 0
        shield_msg = None

        with self._lock:
            m = self.metrics
            s = self.settings

            m.total_trades += 1
            if is_win:
                m.win_count += 1
                m.consecutive_losses = 0
                m.consecutive_wins += 1
            else:
                m.loss_count += 1
                m.consecutive_losses += 1
                m.consecutive_wins = 0

            m.trade_history.append(is_win)
            if len(m.trade_history) > 20:
                m.trade_history = m.trade_history[-20:]

            recent = m.trade_history[-20:]
            m.win_rate = round(sum(recent) / len(recent) * 100, 1) if recent else 0.0

            if m.balance > m.peak_balance:
                m.peak_balance = m.balance

            if s.shield_enabled and not m.shield_active:
                reason = self._shield_trigger_reason()
                if reason:
                    m.pre_shield_mode = s.trade_size_mode
                    s.trade_size_mode = "house_money"
                    m.shield_active = True
                    shield_msg = ("shield_on", reason)
            elif m.shield_active:
                if self._shield_can_recover():
                    s.trade_size_mode = m.pre_shield_mode
                    m.shield_active = False
                    m.consecutive_losses = 0
                    shield_msg = ("shield_off", m.pre_shield_mode)

        if shield_msg:
            if shield_msg[0] == "shield_on":
                self.add_log("🛡 Auto-Shield ON", f"Switched to House Money — {shield_msg[1]}", tone="warning")
                from telegram_notify import notify_shield_on
                notify_shield_on(shield_msg[1])
            else:
                self.add_log("✅ Auto-Shield OFF", f"Market recovered — back to {shield_msg[1]} mode", tone="positive")
                from telegram_notify import notify_shield_off
                notify_shield_off(shield_msg[1])

    def _shield_trigger_reason(self) -> Optional[str]:
        """Return reason string if shield should activate, else None. Called within lock."""
        m, s = self.metrics, self.settings
        if m.consecutive_losses >= s.shield_loss_streak:
            return f"{m.consecutive_losses} consecutive losses"
        if len(m.trade_history) >= 10:
            recent = m.trade_history[-20:]
            wr = sum(recent) / len(recent) * 100
            if wr < s.shield_winrate_min:
                return f"Win rate {wr:.0f}% below {s.shield_winrate_min:.0f}% threshold"
        if m.peak_balance > 0:
            drawdown = (m.peak_balance - m.balance) / m.peak_balance * 100
            if drawdown >= s.shield_drawdown_pct:
                return f"Balance dropped {drawdown:.1f}% from peak ${m.peak_balance:,.2f}"
        return None

    def _shield_can_recover(self) -> bool:
        """Return True if win rate has recovered enough to deactivate shield. Called within lock."""
        m, s = self.metrics, self.settings
        if len(m.trade_history) < 10:
            return False
        recent = m.trade_history[-10:]
        wr = sum(recent) / len(recent) * 100
        return wr >= s.shield_recovery_winrate and m.consecutive_losses == 0

    def _recalc_exposure(self) -> None:
        active = [(s, p) for s, p in self.positions.items() if p is not None]
        self.metrics.active_trades = len(active)
        total_risk = sum(p.current * p.amount for _, p in active)
        self.metrics.risk_exposure = round(total_risk, 2)
        if self.metrics.balance > 0:
            self.metrics.risk_exposure_pct = round(total_risk / self.metrics.balance * 100, 2)
        else:
            self.metrics.risk_exposure_pct = 0.0

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "paper_mode": self.paper_mode,
                "exchange_name": self.exchange_name,
                "signals": {s: asdict(sig) for s, sig in self.signals.items()},
                "positions": {s: asdict(p) for s, p in self.positions.items() if p is not None},
                "logs": [asdict(lg) for lg in self.logs[:30]],
                "metrics": asdict(self.metrics),
                "settings": asdict(self.settings),
            }


bot_state = BotState()
