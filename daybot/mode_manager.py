"""Adaptive Mode Manager for the Day Bot.

Switches between three modes every cycle based on live performance + SPY trend:

  SAFE       — default startup mode
               position 15%, SL 1.0%, TP 2.5%, pullback setups only
  AGGRESSIVE — hot streak + bullish market
               position 25%, SL 1.5%, TP 5.0%, breakouts allowed
  SHIELD     — losing streak or daily loss too deep
               position 3%,  SL 1.0%, TP 2.0%, only A+ pullback setups

Anti-flip guard: minimum 2 completed trades before any mode switch.
All thresholds are configurable via env vars (see load_mode_config).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

Mode = Literal["SAFE", "AGGRESSIVE", "SHIELD"]


@dataclass
class ModeParams:
    position_size_pct: float   # fraction of portfolio per trade
    stop_loss_pct: float        # fraction below entry
    take_profit_pct: float      # fraction above entry
    allow_breakout: bool        # permit Setup B (momentum breakout)
    label: str


_PARAMS: dict[str, ModeParams] = {
    "SAFE":       ModeParams(0.15, 0.010, 0.025, False, "SAFE"),
    "AGGRESSIVE": ModeParams(0.25, 0.015, 0.050, True,  "AGGRESSIVE"),
    "SHIELD":     ModeParams(0.03, 0.010, 0.020, False, "SHIELD"),
}


class DayModeManager:
    def __init__(self) -> None:
        self._mode: Mode = "SAFE"
        self._trades_at_switch: int = 0

        # Thresholds — override via env vars
        self._min_trades   = int(os.getenv("MODE_MIN_TRADES_BEFORE_SWITCH", "2"))
        self._agg_streak   = int(os.getenv("MODE_AGGRESSIVE_WIN_STREAK", "3"))
        self._agg_spy_min  = float(os.getenv("MODE_AGGRESSIVE_SPY_MIN_PCT", "0.3"))
        self._shield_loss  = int(os.getenv("MODE_SHIELD_LOSS_STREAK", "3"))
        self._shield_day   = float(os.getenv("MODE_SHIELD_DAILY_LOSS_PCT", "3.0"))
        self._safe_loss    = int(os.getenv("MODE_SAFE_LOSS_STREAK", "1"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, metrics, spy_return: float = 0.0) -> tuple[Mode, Mode | None]:
        """Evaluate metrics and return (current_mode, previous_mode_if_switched).

        Call once per trade cycle BEFORE generating any BUY signal.
        The returned previous_mode is non-None only when a switch just happened.
        """
        trades_done = metrics.wins_today + metrics.losses_today
        since_switch = trades_done - self._trades_at_switch

        if since_switch < self._min_trades:
            return self._mode, None

        new_mode = self._compute(metrics, spy_return)
        if new_mode != self._mode:
            old = self._mode
            self._mode = new_mode
            self._trades_at_switch = trades_done
            logging.info(
                "DayBot mode: %s → %s | streak_w=%d streak_l=%d spy=%.2f%%",
                old, new_mode,
                metrics.consecutive_wins, metrics.consecutive_losses, spy_return,
            )
            return new_mode, old

        return self._mode, None

    @property
    def mode(self) -> Mode:
        return self._mode

    def params(self) -> ModeParams:
        return _PARAMS[self._mode]

    # ------------------------------------------------------------------
    # Internal logic
    # ------------------------------------------------------------------

    def _compute(self, metrics, spy_return: float) -> Mode:
        # ---- SHIELD ----------------------------------------------------
        if metrics.consecutive_losses >= self._shield_loss:
            return "SHIELD"

        daily_pnl_pct = 0.0
        if metrics.daily_start_value > 0:
            daily_pnl_pct = (
                (metrics.portfolio_value - metrics.daily_start_value)
                / metrics.daily_start_value * 100
            )
        if daily_pnl_pct <= -self._shield_day:
            return "SHIELD"

        # ---- SAFE (any losing momentum) --------------------------------
        if metrics.consecutive_losses >= self._safe_loss:
            return "SAFE"

        trades_done = metrics.wins_today + metrics.losses_today
        if trades_done >= 5:
            wr = metrics.wins_today / trades_done * 100
            if wr < 50:
                return "SAFE"

        # ---- AGGRESSIVE (hot streak + bullish market) ------------------
        if (
            metrics.consecutive_wins >= self._agg_streak
            and metrics.consecutive_losses == 0
            and spy_return >= self._agg_spy_min
        ):
            return "AGGRESSIVE"

        return "SAFE"
