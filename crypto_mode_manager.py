"""Adaptive Mode Manager for the Crypto Bot.

Switches between three modes every cycle based on live performance + BTC 4h trend:

  SAFE       — default startup mode
               trade_size ×1.0, SL 2.0%, TP 6.0%, dip-buy only
  AGGRESSIVE — hot streak + BTC trending up
               trade_size ×1.5, SL 2.0%, TP 8.0%, breakouts allowed
  SHIELD     — losing streak or daily loss too deep
               trade_size ×0.3, SL 1.5%, TP 4.0%, dip-buy only

Anti-flip guard: minimum 2 completed trades before any mode switch.
All thresholds configurable via env vars.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

Mode = Literal["SAFE", "AGGRESSIVE", "SHIELD"]


@dataclass
class ModeParams:
    size_multiplier: float   # applied to base trade_size_usdt
    stop_loss_pct: float      # %
    take_profit_pct: float    # %
    allow_breakout: bool      # permit Setup B (momentum breakout)
    label: str


_PARAMS: dict[str, ModeParams] = {
    "SAFE":       ModeParams(1.0, 2.0, 6.0, False, "SAFE"),
    "AGGRESSIVE": ModeParams(1.5, 2.0, 8.0, True,  "AGGRESSIVE"),
    "SHIELD":     ModeParams(0.3, 1.5, 4.0, False, "SHIELD"),
}


class CryptoModeManager:
    def __init__(self) -> None:
        self._mode: Mode = "SAFE"
        self._trades_at_switch: int = 0

        self._min_trades  = int(os.getenv("MODE_MIN_TRADES_BEFORE_SWITCH", "2"))
        self._agg_streak  = int(os.getenv("MODE_AGGRESSIVE_WIN_STREAK", "3"))
        self._shield_loss = int(os.getenv("MODE_SHIELD_LOSS_STREAK", "3"))
        self._shield_day  = float(os.getenv("MODE_SHIELD_DAILY_LOSS_PCT", "5.0"))
        self._safe_loss   = int(os.getenv("MODE_SAFE_LOSS_STREAK", "1"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, metrics, btc_trend: str = "neutral") -> tuple[Mode, Mode | None]:
        """Return (current_mode, previous_mode_if_switched).

        btc_trend: 'up' | 'down' | 'neutral'  (from HTF 4h check)
        """
        trades_done = metrics.total_trades
        since_switch = trades_done - self._trades_at_switch

        if since_switch < self._min_trades:
            return self._mode, None

        new_mode = self._compute(metrics, btc_trend)
        if new_mode != self._mode:
            old = self._mode
            self._mode = new_mode
            self._trades_at_switch = trades_done
            logging.info(
                "CryptoBot mode: %s → %s | streak_w=%d streak_l=%d btc=%s",
                old, new_mode,
                metrics.consecutive_wins, metrics.consecutive_losses, btc_trend,
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

    def _compute(self, metrics, btc_trend: str) -> Mode:
        # ---- SHIELD ----------------------------------------------------
        if metrics.consecutive_losses >= self._shield_loss:
            return "SHIELD"

        daily_loss_pct = 0.0
        if metrics.daily_start_balance > 0:
            daily_loss_pct = (
                (metrics.daily_start_balance - metrics.balance)
                / metrics.daily_start_balance * 100
            )
        if daily_loss_pct >= self._shield_day:
            return "SHIELD"

        # ---- SAFE (any losing momentum) --------------------------------
        if metrics.consecutive_losses >= self._safe_loss:
            return "SAFE"

        if len(metrics.trade_history) >= 10:
            recent = metrics.trade_history[-10:]
            wr = sum(recent) / len(recent) * 100
            if wr < 50:
                return "SAFE"

        # ---- AGGRESSIVE (hot streak + BTC trending up) -----------------
        if (
            metrics.consecutive_wins >= self._agg_streak
            and btc_trend == "up"
        ):
            return "AGGRESSIVE"

        return "SAFE"
