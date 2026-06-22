"""Central Capital Manager — tracks all 3 bots with mixed paper/real mode.

2-Week Monitoring Setup:
    Day Bot      $1,000   PAPER  — Alpaca paper trading, safe to run now
    Options Bot  $1,000   PAPER  — Alpaca paper trading, safe to run now
    Crypto Bot   REAL     REAL   — Live exchange, real money (use TRADE_SIZE_USDT wisely)

Safety rules:
    Global daily stop-loss  -15% of real capital → ALL bots pause until next day
    Per-bot stop-loss        -20% of bot allocation → that bot pauses
    Weekly rebalance         every Monday 9:00 AM ET — shift toward best performer
    Monthly withdrawal       withdraw 20% of profits when monthly PnL > 10%
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

_STATE_FILE = os.path.join(os.path.dirname(__file__), "capital_state.json")

BotName = Literal["day", "options", "crypto"]

# Default allocation percentages
_DEFAULT_ALLOC = {"day": 0.20, "options": 0.50, "crypto": 0.30}

TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "5000"))
GLOBAL_DAILY_STOP_PCT = float(os.getenv("GLOBAL_DAILY_STOP_PCT", "0.15"))   # -15% total
BOT_DAILY_STOP_PCT    = float(os.getenv("BOT_DAILY_STOP_PCT",    "0.20"))   # -20% per bot
MONTHLY_WITHDRAW_PCT  = float(os.getenv("MONTHLY_WITHDRAW_PCT",  "0.20"))   # withdraw 20% of profit
MONTHLY_PROFIT_GATE   = float(os.getenv("MONTHLY_PROFIT_GATE",   "0.10"))   # only withdraw if >10% monthly gain
REBALANCE_ENABLED     = os.getenv("CAPITAL_REBALANCE", "true").lower() in ("1", "true", "yes")


@dataclass
class BotAllocation:
    name: str
    allocated: float          # current capital allocated to this bot
    initial: float            # starting capital for this bot
    mode: str = "paper"       # "paper" or "real" — real money flag
    realized_pnl: float = 0.0        # cumulative closed-trade P&L
    unrealized_pnl: float = 0.0      # open position estimate
    daily_start: float = 0.0         # balance at start of today
    daily_pnl: float = 0.0           # today's P&L
    trades_today: int = 0
    wins_total: int = 0
    losses_total: int = 0
    paused: bool = False              # True when bot-level stop triggered
    paused_reason: str = ""

    @property
    def balance(self) -> float:
        # allocated is already maintained as initial + realized_pnl (see record_trade),
        # so balance == allocated. Adding realized_pnl again double-counted every gain/loss
        # (balance = initial + 2*realized_pnl), distorting daily/global halt math.
        return self.allocated

    @property
    def win_rate(self) -> float:
        total = self.wins_total + self.losses_total
        return round(self.wins_total / total * 100, 1) if total > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 2)


@dataclass
class CapitalState:
    total_capital: float = TOTAL_CAPITAL
    allocations: dict[str, BotAllocation] = field(default_factory=dict)
    global_halted: bool = False
    global_halt_reason: str = ""
    daily_start_total: float = 0.0
    monthly_start_total: float = 0.0
    monthly_withdrawn: float = 0.0
    last_rebalance_date: str = ""
    last_reset_date: str = ""
    created_at: str = ""


class CapitalManager:
    """Thread-safe capital allocator for 3 trading bots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = CapitalState()
        self._load()
        self._ensure_bots_initialized()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    # Per-bot fixed amounts and modes (2-week monitoring setup)
    _BOT_DEFAULTS = {
        "day":     {"amount": 1000.0, "mode": "paper"},
        "options": {"amount": 1000.0, "mode": "paper"},
        "crypto":  {"amount": 0.0,    "mode": "real"},   # 0 = fetch real balance from exchange
    }

    def _ensure_bots_initialized(self) -> None:
        """Create bot allocations if they don't exist yet.
        For real-money bots, always sync balance from env var if set."""
        allocs = self._state.allocations
        today = date.today().isoformat()

        for bot, cfg in self._BOT_DEFAULTS.items():
            if bot not in allocs:
                if cfg["mode"] == "real":
                    amount = float(os.getenv("CRYPTO_REAL_BALANCE", "0"))
                else:
                    amount = cfg["amount"]
                allocs[bot] = BotAllocation(
                    name=bot,
                    allocated=amount,
                    initial=amount,
                    mode=cfg["mode"],
                    daily_start=amount,
                )
            elif cfg["mode"] == "real":
                # Always re-sync real-money balance from env var on startup
                # so setting CRYPTO_REAL_BALANCE takes effect without manual JSON edits
                env_balance = float(os.getenv("CRYPTO_REAL_BALANCE", "0"))
                if env_balance > 0 and allocs[bot].allocated == 0.0:
                    allocs[bot].allocated = env_balance
                    allocs[bot].initial = env_balance
                    allocs[bot].daily_start = env_balance
                    logging.info("Crypto real balance synced from env: $%.2f", env_balance)

        if not self._state.daily_start_total:
            self._state.daily_start_total = self._state.total_capital
        if not self._state.monthly_start_total:
            self._state.monthly_start_total = self._state.total_capital
        if not self._state.created_at:
            self._state.created_at = datetime.now(timezone.utc).isoformat()
        if not self._state.last_reset_date:
            self._state.last_reset_date = today

        # Auto-heal missed midnight reset: if last_reset_date is stale (bot was
        # down at midnight), clear any global halt so bots aren't stuck indefinitely.
        if self._state.last_reset_date and self._state.last_reset_date != today:
            logging.info(
                "Startup: stale reset date %s → running daily_reset() to clear any stale halt",
                self._state.last_reset_date,
            )
            self.daily_reset()
            return  # daily_reset() calls _save() — skip the one below

        self._save()

    # ------------------------------------------------------------------
    # Public API — called by each bot
    # ------------------------------------------------------------------

    def get_allocation(self, bot: BotName) -> float:
        """Return current capital allocated to this bot."""
        with self._lock:
            a = self._state.allocations.get(bot)
            return round(a.allocated, 2) if a else 0.0

    def get_trade_size(self, bot: BotName, pct_of_allocation: float = 0.10) -> float:
        """Return max position size for one trade (default 10% of bot allocation)."""
        alloc = self.get_allocation(bot)
        return round(alloc * pct_of_allocation, 2)

    def is_halted(self, bot: BotName | None = None) -> bool:
        """True if global halt is active, or if the specific bot is paused."""
        with self._lock:
            if self._state.global_halted:
                return True
            if bot:
                a = self._state.allocations.get(bot)
                return bool(a and a.paused)
            return False

    def halt_reason(self, bot: BotName | None = None) -> str:
        with self._lock:
            if self._state.global_halted:
                return self._state.global_halt_reason
            if bot:
                a = self._state.allocations.get(bot)
                if a and a.paused:
                    return a.paused_reason
            return ""

    def record_trade(
        self,
        bot: BotName,
        pnl: float,
        unrealized: float = 0.0,
    ) -> None:
        """Call after every closed trade. pnl is the dollar P&L of the trade."""
        with self._lock:
            a = self._state.allocations.get(bot)
            if not a:
                return
            a.realized_pnl = round(a.realized_pnl + pnl, 2)
            a.daily_pnl    = round(a.daily_pnl + pnl, 2)
            a.unrealized_pnl = unrealized
            a.trades_today += 1
            if pnl >= 0:
                a.wins_total += 1
            else:
                a.losses_total += 1

            # Update bot-level allocation balance
            a.allocated = round(a.initial + a.realized_pnl, 2)

            # Check per-bot daily stop
            self._check_bot_stop(a)
            # Check global daily stop
            self._check_global_stop()

        self._save()

    def update_unrealized(self, bot: BotName, unrealized: float) -> None:
        """Update unrealized P&L (open positions). Called each monitor cycle."""
        with self._lock:
            a = self._state.allocations.get(bot)
            if a:
                a.unrealized_pnl = round(unrealized, 2)
                self._check_global_stop()
        self._save()

    def daily_reset(self) -> None:
        """Reset daily counters. Call at market open or midnight."""
        with self._lock:
            total = self._total_balance()
            self._state.daily_start_total = round(total, 2)
            self._state.global_halted = False
            self._state.global_halt_reason = ""
            self._state.last_reset_date = date.today().isoformat()
            for a in self._state.allocations.values():
                a.daily_start = round(a.allocated, 2)
                a.daily_pnl = 0.0
                a.unrealized_pnl = 0.0   # clear yesterday's open position estimates
                a.trades_today = 0
                a.paused = False
                a.paused_reason = ""
            logging.info(
                "Capital daily reset — total balance: $%.2f "
                "(Day $%.2f | Options $%.2f | Crypto $%.2f)",
                total,
                self._state.allocations.get("day", BotAllocation("day", 0, 0)).allocated,
                self._state.allocations.get("options", BotAllocation("options", 0, 0)).allocated,
                self._state.allocations.get("crypto", BotAllocation("crypto", 0, 0)).allocated,
            )
        self._save()
        self._notify_daily_reset()

    def rebalance(self, force: bool = False) -> dict:
        """Shift capital toward best-performing bot. Returns rebalance summary."""
        with self._lock:
            today = date.today().isoformat()
            if not force and self._state.last_rebalance_date == today:
                return {"skipped": True, "reason": "Already rebalanced today"}

            allocs = self._state.allocations
            total = self._total_balance()

            # Compute performance score per bot — win rate × profit magnitude.
            # Two bots with equal win rate but different profit sizes get different scores.
            scores: dict[str, float] = {}
            for name, a in allocs.items():
                total_trades = a.wins_total + a.losses_total
                if total_trades >= 5:
                    pnl_factor = max(0.5, min(2.0, 1.0 + a.realized_pnl / 500))
                    score = a.win_rate * pnl_factor
                else:
                    score = 50.0  # neutral score before enough data
                scores[name] = score

            # Adjust allocation weights proportionally to performance (±5% max shift, faster 0.5×).
            base = _DEFAULT_ALLOC.copy()
            total_score = sum(scores.values()) or 1.0
            for name in base:
                perf_pct = scores[name] / total_score
                base_pct = _DEFAULT_ALLOC[name]
                shift = (perf_pct - base_pct) * 0.50   # 0.5× for faster rebalancing
                shift = max(-0.05, min(0.05, shift))
                base[name] = round(base_pct + shift, 4)

            # Normalize so they sum to 1.0
            total_pct = sum(base.values())
            new_alloc = {k: round(v / total_pct, 4) for k, v in base.items()}

            summary = {
                "date": today,
                "total_balance": round(total, 2),
                "before": {},
                "after": {},
                "scores": scores,
            }

            for name, a in allocs.items():
                old = a.allocated
                new = round(total * new_alloc[name], 2)
                summary["before"][name] = round(old, 2)
                summary["after"][name] = new
                a.allocated = new
                a.initial = new        # reset baseline for next period
                a.realized_pnl = 0.0   # baseline reset → realized restarts from the new allocation
                a.daily_start = new    # redistribution is NOT a daily loss — rebaseline per-bot

            # Rebalancing only MOVES capital between bots; total is unchanged, so it must
            # not register as a daily loss. Rebaseline the global daily start to the post-
            # rebalance total (otherwise a bot losing allocation looks like a loss → false halt).
            self._state.daily_start_total = round(total, 2)
            self._state.last_rebalance_date = today
            logging.info("Capital rebalanced: %s", summary)

        self._save()
        self._notify_rebalance(summary)
        return summary

    def check_monthly_withdrawal(self) -> dict | None:
        """Withdraw 20% of profits if monthly gain > 10%. Returns withdrawal info or None."""
        with self._lock:
            total = self._total_balance()
            monthly_gain = total - self._state.monthly_start_total
            monthly_gain_pct = monthly_gain / self._state.monthly_start_total if self._state.monthly_start_total > 0 else 0

            if monthly_gain_pct < MONTHLY_PROFIT_GATE:
                return None

            withdrawal = round(monthly_gain * MONTHLY_WITHDRAW_PCT, 2)
            if withdrawal <= 0:
                return None

            # Reduce each bot's allocation proportionally
            for a in self._state.allocations.values():
                proportion = a.allocated / total if total > 0 else 0
                reduction = round(withdrawal * proportion, 2)
                a.allocated = max(0, round(a.allocated - reduction, 2))
                a.initial = a.allocated

            self._state.monthly_withdrawn = round(self._state.monthly_withdrawn + withdrawal, 2)
            self._state.monthly_start_total = self._total_balance()

            result = {
                "withdrawn": withdrawal,
                "monthly_gain": round(monthly_gain, 2),
                "monthly_gain_pct": round(monthly_gain_pct * 100, 1),
                "total_withdrawn_lifetime": self._state.monthly_withdrawn,
                "remaining_capital": round(self._total_balance(), 2),
            }
            logging.info("Monthly withdrawal: $%.2f (gain was %.1f%%)", withdrawal, monthly_gain_pct * 100)

        self._save()
        self._notify_withdrawal(result)
        return result

    # ------------------------------------------------------------------
    # Status / reporting
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Full capital status for API endpoint."""
        with self._lock:
            allocs = self._state.allocations
            total = self._total_balance()
            daily_pnl = total - self._state.daily_start_total
            monthly_pnl = total - self._state.monthly_start_total

            bots = {}
            for name, a in allocs.items():
                bots[name] = {
                    "allocated":      round(a.allocated, 2),
                    "initial":        round(a.initial, 2),
                    "balance":        round(a.balance, 2),
                    "mode":           a.mode,
                    "realized_pnl":   round(a.realized_pnl, 2),
                    "unrealized_pnl": round(a.unrealized_pnl, 2),
                    "total_pnl":      round(a.total_pnl, 2),
                    "daily_pnl":      round(a.daily_pnl, 2),
                    "trades_today":   a.trades_today,
                    "wins":           a.wins_total,
                    "losses":         a.losses_total,
                    "win_rate":       a.win_rate,
                    "paused":         a.paused,
                    "paused_reason":  a.paused_reason,
                    "allocation_pct": round(a.allocated / total * 100, 1) if total > 0 else 0,
                }

            return {
                "total_capital":        TOTAL_CAPITAL,
                "total_balance":        round(total, 2),
                "daily_pnl":            round(daily_pnl, 2),
                "daily_pnl_pct":        round(daily_pnl / self._state.daily_start_total * 100, 1) if self._state.daily_start_total > 0 else 0,
                "monthly_pnl":          round(monthly_pnl, 2),
                "monthly_pnl_pct":      round(monthly_pnl / self._state.monthly_start_total * 100, 1) if self._state.monthly_start_total > 0 else 0,
                "monthly_withdrawn":    round(self._state.monthly_withdrawn, 2),
                "total_pnl":            round(total - TOTAL_CAPITAL, 2),
                "total_pnl_pct":        round((total - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 1),
                "global_halted":        self._state.global_halted,
                "global_halt_reason":   self._state.global_halt_reason,
                "last_rebalance":       self._state.last_rebalance_date,
                "last_reset":           self._state.last_reset_date,
                "created_at":           self._state.created_at,
                "bots":                 bots,
                "target_6m":            50000,
                "progress_pct":         round((total - TOTAL_CAPITAL) / (50000 - TOTAL_CAPITAL) * 100, 2) if total > TOTAL_CAPITAL else 0,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _total_balance(self) -> float:
        return sum(
            a.allocated + a.realized_pnl + a.unrealized_pnl
            for a in self._state.allocations.values()
        )

    def _check_bot_stop(self, a: BotAllocation) -> None:
        """Pause individual bot if its daily loss exceeds threshold."""
        if a.paused or a.daily_start <= 0:
            return
        loss_pct = -a.daily_pnl / a.daily_start if a.daily_start > 0 else 0
        if loss_pct >= BOT_DAILY_STOP_PCT:
            a.paused = True
            a.paused_reason = f"Daily loss {loss_pct*100:.1f}% exceeds {BOT_DAILY_STOP_PCT*100:.0f}% limit"
            logging.warning("[%s] Bot paused: %s", a.name.upper(), a.paused_reason)
            self._notify_bot_halt(a)

    def _check_global_stop(self) -> None:
        """Halt ALL bots if total portfolio drops -15% intraday."""
        if self._state.global_halted or self._state.daily_start_total <= 0:
            return
        total = self._total_balance()
        loss_pct = (self._state.daily_start_total - total) / self._state.daily_start_total
        if loss_pct >= GLOBAL_DAILY_STOP_PCT:
            self._state.global_halted = True
            self._state.global_halt_reason = (
                f"Global daily loss {loss_pct*100:.1f}% (${self._state.daily_start_total - total:.0f}) "
                f"exceeds {GLOBAL_DAILY_STOP_PCT*100:.0f}% limit — all bots halted until tomorrow"
            )
            logging.critical("GLOBAL HALT: %s", self._state.global_halt_reason)
            self._notify_global_halt(total)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(_STATE_FILE):
            return
        try:
            with open(_STATE_FILE) as f:
                data = json.load(f)
            s = self._state
            s.total_capital         = float(data.get("total_capital", TOTAL_CAPITAL))
            s.global_halted         = bool(data.get("global_halted", False))
            s.global_halt_reason    = data.get("global_halt_reason", "")
            s.daily_start_total     = float(data.get("daily_start_total", 0))
            s.monthly_start_total   = float(data.get("monthly_start_total", 0))
            s.monthly_withdrawn     = float(data.get("monthly_withdrawn", 0))
            s.last_rebalance_date   = data.get("last_rebalance_date", "")
            s.last_reset_date       = data.get("last_reset_date", "")
            s.created_at            = data.get("created_at", "")

            for name, bd in data.get("allocations", {}).items():
                default_mode = self._BOT_DEFAULTS.get(name, {}).get("mode", "paper")
                s.allocations[name] = BotAllocation(
                    name          = name,
                    allocated     = float(bd.get("allocated", 0)),
                    initial       = float(bd.get("initial", 0)),
                    mode          = bd.get("mode", default_mode),
                    realized_pnl  = float(bd.get("realized_pnl", 0)),
                    unrealized_pnl= float(bd.get("unrealized_pnl", 0)),
                    daily_start   = float(bd.get("daily_start", 0)),
                    daily_pnl     = float(bd.get("daily_pnl", 0)),
                    trades_today  = int(bd.get("trades_today", 0)),
                    wins_total    = int(bd.get("wins_total", 0)),
                    losses_total  = int(bd.get("losses_total", 0)),
                    paused        = bool(bd.get("paused", False)),
                    paused_reason = bd.get("paused_reason", ""),
                )
            logging.info(
                "Capital state loaded — total: $%.2f (Day $%.2f | Options $%.2f | Crypto $%.2f)",
                sum(a.allocated for a in s.allocations.values()),
                s.allocations.get("day", BotAllocation("day", 0, 0)).allocated,
                s.allocations.get("options", BotAllocation("options", 0, 0)).allocated,
                s.allocations.get("crypto", BotAllocation("crypto", 0, 0)).allocated,
            )
        except Exception as exc:
            logging.warning("Capital state load failed: %s — starting fresh", exc)

    def _save(self) -> None:
        try:
            s = self._state
            data = {
                "total_capital":      s.total_capital,
                "global_halted":      s.global_halted,
                "global_halt_reason": s.global_halt_reason,
                "daily_start_total":  s.daily_start_total,
                "monthly_start_total": s.monthly_start_total,
                "monthly_withdrawn":  s.monthly_withdrawn,
                "last_rebalance_date": s.last_rebalance_date,
                "last_reset_date":    s.last_reset_date,
                "created_at":         s.created_at,
                "saved_at":           datetime.now(timezone.utc).isoformat(),
                "allocations": {
                    name: {
                        "allocated":      a.allocated,
                        "initial":        a.initial,
                        "mode":           a.mode,
                        "realized_pnl":   a.realized_pnl,
                        "unrealized_pnl": a.unrealized_pnl,
                        "daily_start":    a.daily_start,
                        "daily_pnl":      a.daily_pnl,
                        "trades_today":   a.trades_today,
                        "wins_total":     a.wins_total,
                        "losses_total":   a.losses_total,
                        "paused":         a.paused,
                        "paused_reason":  a.paused_reason,
                    }
                    for name, a in s.allocations.items()
                },
            }
            with open(_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logging.warning("Capital state save failed: %s", exc)

    # ------------------------------------------------------------------
    # Telegram notifications
    # ------------------------------------------------------------------

    def _notify(self, msg: str) -> None:
        try:
            from telegram_notify import _send
            _send(msg)
        except Exception:
            pass

    def _notify_global_halt(self, current_total: float) -> None:
        loss = self._state.daily_start_total - current_total
        self._notify(
            f"🚨 <b>GLOBAL CAPITAL HALT</b>\n"
            f"Daily loss: <b>${loss:.0f}</b> ({loss/self._state.daily_start_total*100:.1f}%)\n"
            f"Threshold: {GLOBAL_DAILY_STOP_PCT*100:.0f}% = ${self._state.daily_start_total*GLOBAL_DAILY_STOP_PCT:.0f}\n"
            f"ALL 3 BOTS PAUSED until tomorrow\n"
            f"Portfolio: <b>${current_total:.2f}</b>"
        )

    def _notify_bot_halt(self, a: BotAllocation) -> None:
        self._notify(
            f"⚠️ <b>{a.name.upper()} BOT PAUSED</b>\n"
            f"Daily loss: <b>${abs(a.daily_pnl):.2f}</b> ({abs(a.daily_pnl)/a.daily_start*100:.1f}%)\n"
            f"Threshold: {BOT_DAILY_STOP_PCT*100:.0f}% = ${a.daily_start*BOT_DAILY_STOP_PCT:.0f}\n"
            f"Bot paused until tomorrow"
        )

    def _notify_daily_reset(self) -> None:
        s = self._state
        total = sum(a.allocated for a in s.allocations.values())
        day_a   = s.allocations.get("day", BotAllocation("day", 0, 0))
        opts_a  = s.allocations.get("options", BotAllocation("options", 0, 0))
        cryp_a  = s.allocations.get("crypto", BotAllocation("crypto", 0, 0))
        self._notify(
            f"🌅 <b>Daily Capital Reset</b>\n"
            f"Total: <b>${total:.2f}</b> | Target: $50,000\n"
            f"Day Bot:     ${day_a.allocated:.0f}\n"
            f"Options Bot: ${opts_a.allocated:.0f}\n"
            f"Crypto Bot:  ${cryp_a.allocated:.0f}\n"
            f"Progress: {(total-TOTAL_CAPITAL)/(50000-TOTAL_CAPITAL)*100:.1f}% to $50K goal"
        )

    def _notify_rebalance(self, summary: dict) -> None:
        b = summary.get("before", {})
        a = summary.get("after", {})
        self._notify(
            f"⚖️ <b>Weekly Capital Rebalance</b>\n"
            f"Total: ${summary.get('total_balance', 0):.2f}\n"
            f"Day:     ${b.get('day', 0):.0f} → ${a.get('day', 0):.0f}\n"
            f"Options: ${b.get('options', 0):.0f} → ${a.get('options', 0):.0f}\n"
            f"Crypto:  ${b.get('crypto', 0):.0f} → ${a.get('crypto', 0):.0f}"
        )

    def _notify_withdrawal(self, info: dict) -> None:
        self._notify(
            f"💰 <b>Monthly Profit Withdrawal</b>\n"
            f"Withdrawn: <b>${info['withdrawn']:.2f}</b>\n"
            f"Monthly gain: ${info['monthly_gain']:.2f} (+{info['monthly_gain_pct']:.1f}%)\n"
            f"Remaining capital: ${info['remaining_capital']:.2f}\n"
            f"Total withdrawn lifetime: ${info['total_withdrawn_lifetime']:.2f}"
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
capital_manager = CapitalManager()
