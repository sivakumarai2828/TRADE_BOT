"""Thread-safe shared state for the day trading bot."""
from __future__ import annotations
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass
class DayPosition:
    symbol: str
    qty: int
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class DaySignal:
    symbol: str
    action: str = "HOLD"
    rsi: float = 0.0
    price: float = 0.0
    ema: float = 0.0
    ai_confidence: float = 0.0
    ai_reason: str = ""
    rule_reason: str = ""
    timestamp: str = ""


@dataclass
class DayLogEntry:
    time: str
    type: str
    message: str
    tone: str = "neutral"


@dataclass
class DayMetrics:
    portfolio_value: float = 0.0
    cash: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    daily_start_value: float = 0.0
    daily_loss_halted: bool = False
    market_open: bool = False
    # Compounding / house money
    trade_mode: str = "compound"        # "fixed" | "compound" | "house_money"
    position_size_pct: float = 0.05     # runtime size (may shrink during shield)
    profit_pool: float = 0.0            # cumulative realised profit (house money source)
    # Shield
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    shield_active: bool = False
    pre_shield_mode: str = "compound"   # mode to restore when shield lifts
    # Adaptive mode
    current_mode: str = "SAFE"          # SAFE | AGGRESSIVE | SHIELD


class DayBotState:
    """Central, lock-protected state for the day trading bot."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.running: bool = False
        self.positions: dict[str, DayPosition] = {}
        self.signals: dict[str, DaySignal] = {}
        self.watchlist: list[str] = []
        self.logs: list[DayLogEntry] = []
        self.metrics: DayMetrics = DayMetrics()
        # Pre-market analysis results
        self.premarket_approved: list[str] = []
        self.premarket_time: str = ""
        # Evening sub-agent analysis results (set at 8 PM ET, used next day)
        self.evening_approved: list[str] = []
        self.evening_entry_zones: dict[str, list[float]] = {}
        self.evening_risk_flags: dict[str, str] = {}
        self.evening_regime: str = ""
        self.evening_notes: dict[str, str] = {}
        self.evening_analysis_date: str = ""
        self.evening_stop_levels: dict[str, float] = {}   # symbol → stop price
        self.evening_targets: dict[str, float] = {}       # symbol → target price
        self.evening_direction: dict[str, str] = {}       # symbol → BUY/SELL
        # Options picks (set at 9:15 AM ET)
        self.options_picks: list[dict] = []
        self.options_picks_date: str = ""

        # India (NSE) evening analysis results (set at 4:30 PM IST, used next morning)
        self.india_approved: list[str] = []
        self.india_entry_zones: dict[str, list[float]] = {}
        self.india_stop_levels: dict[str, float] = {}
        self.india_targets: dict[str, float] = {}
        self.india_notes: dict[str, str] = {}
        self.india_direction: dict[str, str] = {}
        self.india_regime: str = ""
        self.india_analysis_date: str = ""

        # Shield thresholds (configurable at runtime via /daybot/settings)
        self.shield_loss_streak: int = 2      # activate after N consecutive losses
        self.shield_recovery_wins: int = 2    # deactivate after N consecutive wins
        self.shield_size_pct: float = 0.01    # shrunken position size during shield
        self.normal_size_pct: float = 0.05    # normal position size

    # ------------------------------------------------------------------
    # Trade result recording — updates streak counters and shield state
    # ------------------------------------------------------------------

    def record_trade_result(self, pnl: float) -> None:
        """Call after every closed trade. Manages streak and shield."""
        with self._lock:
            m = self.metrics
            if pnl > 0:
                m.wins_today += 1
                m.consecutive_losses = 0
                m.consecutive_wins += 1
                m.profit_pool = round(m.profit_pool + pnl, 2)
                # Shield recovery: N consecutive wins → lift shield
                if m.shield_active and m.consecutive_wins >= self.shield_recovery_wins:
                    m.shield_active = False
                    m.trade_mode = m.pre_shield_mode
                    m.position_size_pct = self.normal_size_pct
                    self._add_log_unlocked(
                        "Shield", f"Shield lifted after {m.consecutive_wins} wins — back to {m.trade_mode} mode", "positive"
                    )
            else:
                m.losses_today += 1
                m.consecutive_wins = 0
                m.consecutive_losses += 1
                # Shield activation: N consecutive losses → reduce size
                if not m.shield_active and m.consecutive_losses >= self.shield_loss_streak:
                    m.shield_active = True
                    m.pre_shield_mode = m.trade_mode
                    m.trade_mode = "house_money"
                    m.position_size_pct = self.shield_size_pct
                    self._add_log_unlocked(
                        "Shield", f"Shield ON after {m.consecutive_losses} losses — position size → {self.shield_size_pct*100:.0f}%", "warning"
                    )

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_position_size(self, portfolio_value: float, price: float) -> int:
        """Return share qty based on current trade mode."""
        with self._lock:
            m = self.metrics
            mode = m.trade_mode

            if mode == "house_money":
                # Only risk profit already earned; if no profit yet, use 1% safety size
                available = m.profit_pool if m.profit_pool > 0 else portfolio_value * 0.01
                dollar_size = available * m.position_size_pct
            elif mode == "compound":
                # Re-invest: size off current portfolio value
                dollar_size = portfolio_value * m.position_size_pct
            else:
                # Fixed: always use the stored size_pct against starting portfolio
                dollar_size = m.daily_start_value * m.position_size_pct

            qty = int(dollar_size / price) if price > 0 else 1
            return max(1, qty)

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _add_log_unlocked(self, log_type: str, message: str, tone: str = "neutral") -> None:
        entry = DayLogEntry(
            time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
            type=log_type,
            message=message,
            tone=tone,
        )
        self.logs.insert(0, entry)
        self.logs = self.logs[:100]

    def add_log(self, log_type: str, message: str, tone: str = "neutral") -> None:
        with self._lock:
            self._add_log_unlocked(log_type, message, tone)

    def set_signal(self, sig: DaySignal) -> None:
        with self._lock:
            sig.timestamp = datetime.now(timezone.utc).isoformat()
            self.signals[sig.symbol] = sig

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "watchlist": self.watchlist,
                "positions": {s: asdict(p) for s, p in self.positions.items()},
                "signals": {s: asdict(sig) for s, sig in self.signals.items()},
                "logs": [asdict(lg) for lg in self.logs[:30]],
                "metrics": asdict(self.metrics),
            }


day_state = DayBotState()
