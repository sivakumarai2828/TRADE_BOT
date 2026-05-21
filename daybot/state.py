"""Thread-safe shared state for the day trading bot."""
from __future__ import annotations
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass
class DayPosition:
    symbol: str
    qty: float  # fractional shares supported by Alpaca
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    pnl: float = 0.0
    pnl_pct: float = 0.0
    # Partial profit tracking
    tp1: float = 0.0           # price to close 50% — entry + 1.5x risk
    tp1_hit: bool = False      # True once 50% sold at TP1
    highest_price: float = 0.0 # tracks peak since TP1 hit (for trailing stop)
    atr: float = 0.0           # ATR at entry — used for dynamic SL/TP


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
    portfolio_value: float = 1000.0
    cash: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    daily_start_value: float = 1000.0
    daily_loss_halted: bool = False
    market_open: bool = False
    # Compounding / house money
    trade_mode: str = "compound"        # "fixed" | "compound" | "house_money"
    position_size_pct: float = 0.25     # runtime size (may shrink during shield)
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
        self.india_rank: dict[str, int] = {}
        self.india_conviction: dict[str, str] = {}

        # Shield thresholds (configurable at runtime via /daybot/settings)
        self.shield_loss_streak: int = 2      # activate after N consecutive losses
        self.shield_recovery_wins: int = 2    # deactivate after N consecutive wins
        self.shield_size_pct: float = 0.01    # shrunken position size during shield
        self.normal_size_pct: float = 0.25    # normal position size (25% per trade)

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

    @staticmethod
    def _dynamic_pct(portfolio_value: float, starting_value: float) -> float:
        """25% base, scales up as portfolio grows (same logic as crypto bot)."""
        if starting_value <= 0:
            return 0.25
        ratio = portfolio_value / starting_value
        if ratio >= 3.0:   return 0.35
        if ratio >= 2.0:   return 0.30
        if ratio >= 1.5:   return 0.28
        if ratio >= 1.25:  return 0.26
        return 0.25

    def calculate_position_size(self, portfolio_value: float, price: float) -> float:
        """Return fractional share qty based on current trade mode."""
        with self._lock:
            m = self.metrics
            mode = m.trade_mode

            if mode == "house_money":
                # Shield mode: block new trades entirely if no profit to risk
                if m.profit_pool <= 0:
                    return 0.0   # caller checks qty < 0.001 → skips trade
                dollar_size = m.profit_pool * m.position_size_pct
            elif mode == "compound":
                # Dynamic 25% base — scales up as portfolio grows with profits
                pct = self._dynamic_pct(portfolio_value, m.daily_start_value or 1000.0)
                dollar_size = portfolio_value * pct
            else:
                # Fixed: always use the stored size_pct against starting portfolio
                dollar_size = m.daily_start_value * m.position_size_pct

            # Alpaca minimum order = $1 — enforce before calculating qty
            dollar_size = max(dollar_size, 1.0)

            # Fractional shares — no forced integer minimum
            qty = round(dollar_size / price, 6) if price > 0 else 0.0
            return max(0.001, qty)

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
                "evening_approved": self.evening_approved,
                "evening_analysis_date": self.evening_analysis_date,
                "evening_regime": self.evening_regime,
                "india_approved": self.india_approved,
                "india_analysis_date": self.india_analysis_date,
            }


day_state = DayBotState()
