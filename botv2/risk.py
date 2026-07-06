"""Hard risk enforcement — the only part of V2 the AI cannot influence.

Every AI decision passes through validate_buy()/validate_adjust()/check_halts()
before any order is placed. Violations are clamped or rejected, never negotiated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import HardCaps
from .journal import Journal

log = logging.getLogger("botv2.risk")


@dataclass
class BuyVerdict:
    ok: bool
    qty: float = 0.0
    reason: str = ""


class RiskEngine:
    MIN_REWARD_RISK = 2.0  # target must be >= 2x the stop distance

    def __init__(self, caps: HardCaps, journal: Journal, market: str):
        self.caps = caps
        self.journal = journal
        self.market = market

    # ── kill switch & daily halt ─────────────────────────────────
    def check_halts(self, equity: float, day_pnl: float) -> str | None:
        """Returns a halt reason, or None if trading may proceed."""
        peak_key = f"{self.market}_equity_peak"
        peak = float(self.journal.kv_get(peak_key, "0") or 0)
        if equity > peak:
            self.journal.kv_set(peak_key, str(equity))
            peak = equity

        if peak > 0 and equity <= peak * (1 - self.caps.max_drawdown_kill_pct):
            self.journal.kv_set(f"{self.market}_killed", "1")
            return (f"KILL SWITCH: equity {equity:.2f} is "
                    f"{self.caps.max_drawdown_kill_pct:.0%} below peak {peak:.2f}. "
                    f"All positions will be flattened; bot halted until manual reset.")

        if self.journal.kv_get(f"{self.market}_killed") == "1":
            return "Bot previously kill-switched. Manual reset required (kv key *_killed)."

        if equity > 0 and day_pnl / equity <= -self.caps.daily_loss_halt_pct:
            return (f"DAILY HALT: day P&L {day_pnl:.2f} exceeds "
                    f"-{self.caps.daily_loss_halt_pct:.0%} of equity. No new buys today.")
        return None

    # ── per-trade validation ─────────────────────────────────────
    def validate_buy(self, symbol: str, price: float, stop: float, target: float | None,
                     equity: float, cash: float, open_positions: int) -> BuyVerdict:
        caps = self.caps

        if price <= 0 or stop <= 0:
            return BuyVerdict(False, reason="invalid price/stop")
        if stop >= price:
            return BuyVerdict(False, reason=f"{symbol}: stop {stop} must be below entry {price} (long only)")

        # 2R asymmetry enforced in code, not just in the prompt (review issue #2).
        if target is None or target <= price:
            return BuyVerdict(False, reason=f"{symbol}: target must be above entry")
        rr = (target - price) / (price - stop)
        if rr < self.MIN_REWARD_RISK:
            return BuyVerdict(False, reason=f"{symbol}: reward/risk {rr:.2f}R < {self.MIN_REWARD_RISK}R minimum")

        stop_dist = (price - stop) / price
        if stop_dist < caps.min_stop_distance_pct:
            return BuyVerdict(False, reason=f"{symbol}: stop too tight ({stop_dist:.2%} < {caps.min_stop_distance_pct:.0%})")
        if stop_dist > caps.max_stop_distance_pct:
            return BuyVerdict(False, reason=f"{symbol}: stop too wide ({stop_dist:.2%} > {caps.max_stop_distance_pct:.0%})")

        if open_positions >= caps.max_open_positions:
            return BuyVerdict(False, reason=f"max open positions ({caps.max_open_positions}) reached")

        if self.journal.entries_today(self.market) >= caps.max_new_entries_per_day:
            return BuyVerdict(False, reason=f"max new entries/day ({caps.max_new_entries_per_day}) reached")

        # Risk-based sizing, then clamp by position cap and cash.
        max_loss = equity * caps.max_risk_per_trade_pct
        qty_by_risk = max_loss / (price - stop)
        qty_by_pos_cap = (equity * caps.max_position_pct) / price
        qty_by_cash = cash / price
        qty = min(qty_by_risk, qty_by_pos_cap, qty_by_cash)

        # Whole shares for stocks (both US and India universes here are stocks).
        qty = float(int(qty))
        if qty < 1:
            return BuyVerdict(False, reason=f"{symbol}: size rounds to 0 shares within risk caps")

        return BuyVerdict(True, qty=qty,
                          reason=f"sized {qty:.0f} sh (risk {max_loss:.2f}, stop dist {stop_dist:.2%})")

    def validate_adjust(self, symbol: str, current_price: float, old_stop: float | None,
                        new_stop: float | None, new_target: float | None
                        ) -> tuple[float | None, float | None, str]:
        """Sanitize an ADJUST action. Returns (stop, target, note); invalid parts -> None."""
        notes = []
        if new_stop is not None:
            try:
                new_stop = float(new_stop)
                if new_stop != new_stop or new_stop <= 0:   # NaN / negative
                    raise ValueError
            except (TypeError, ValueError):
                new_stop = None
                notes.append(f"{symbol}: non-numeric stop rejected")
            if new_stop is not None and old_stop and new_stop < float(old_stop):
                notes.append(f"{symbol}: stop may only move UP ({old_stop} -> {new_stop} rejected)")
                new_stop = None
            if new_stop is not None and new_stop >= current_price:
                notes.append(f"{symbol}: stop {new_stop} >= current price {current_price} rejected")
                new_stop = None
        if new_target is not None:
            try:
                new_target = float(new_target)
                if new_target != new_target or new_target <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                new_target = None
                notes.append(f"{symbol}: non-numeric target rejected")
            if new_target is not None and new_target <= current_price:
                notes.append(f"{symbol}: target {new_target} <= current price rejected")
                new_target = None
            if new_target is not None and new_stop is not None and new_target <= new_stop:
                notes.append(f"{symbol}: target <= stop rejected")
                new_target = None
        return new_stop, new_target, "; ".join(notes)

    def describe(self) -> dict:
        """Caps as shown to the AI in its prompt."""
        c = self.caps
        return {
            "max_position_pct_of_equity": c.max_position_pct,
            "max_risk_per_trade_pct_of_equity": c.max_risk_per_trade_pct,
            "max_open_positions": c.max_open_positions,
            "max_new_entries_per_day": c.max_new_entries_per_day,
            "daily_loss_halt_pct": c.daily_loss_halt_pct,
            "max_drawdown_kill_switch_pct": c.max_drawdown_kill_pct,
            "stop_distance_range_pct": [c.min_stop_distance_pct, c.max_stop_distance_pct],
            "min_reward_risk_ratio": self.MIN_REWARD_RISK,
            "long_only": c.long_only,
            "note": "Orders violating these are clamped or rejected by code. Size positions so they fit.",
        }
