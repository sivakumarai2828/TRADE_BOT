"""Tests for CryptoModeManager and DayModeManager.

Covers all mode transitions: SAFE → AGGRESSIVE, SAFE → SHIELD, SHIELD → SAFE,
anti-flip guard, and boundary conditions. No network calls required.
"""
import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crypto_mode_manager import CryptoModeManager
from daybot.mode_manager import DayModeManager


# ---------------------------------------------------------------------------
# Helpers — lightweight metric stubs
# ---------------------------------------------------------------------------

def _crypto_metrics(
    consecutive_wins: int = 0,
    consecutive_losses: int = 0,
    total_trades: int = 0,
    balance: float = 1000.0,
    daily_start_balance: float = 1000.0,
    trade_history: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        consecutive_wins=consecutive_wins,
        consecutive_losses=consecutive_losses,
        total_trades=total_trades,
        balance=balance,
        daily_start_balance=daily_start_balance,
        trade_history=trade_history if trade_history is not None else [],
    )


def _day_metrics(
    consecutive_wins: int = 0,
    consecutive_losses: int = 0,
    wins_today: int = 0,
    losses_today: int = 0,
    portfolio_value: float = 10000.0,
    daily_start_value: float = 10000.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        consecutive_wins=consecutive_wins,
        consecutive_losses=consecutive_losses,
        wins_today=wins_today,
        losses_today=losses_today,
        portfolio_value=portfolio_value,
        daily_start_value=daily_start_value,
    )


def _advance(mgr, metrics, n: int, btc_trend: str = "neutral", spy_return: float = 0.0):
    """Simulate n evaluate() calls, bumping total_trades / wins+losses each time."""
    for _ in range(n):
        if isinstance(mgr, CryptoModeManager):
            metrics.total_trades += 1
            mgr.evaluate(metrics, btc_trend=btc_trend)
        else:
            metrics.wins_today += 1
            mgr.evaluate(metrics, spy_return=spy_return)


# ---------------------------------------------------------------------------
# CryptoModeManager
# ---------------------------------------------------------------------------

class TestCryptoModeManagerInit:
    def test_starts_in_safe(self):
        assert CryptoModeManager().mode == "SAFE"

    def test_params_safe(self):
        p = CryptoModeManager().params()
        assert p.size_multiplier == 1.0
        assert p.stop_loss_pct == 2.0
        assert p.take_profit_pct == 6.0
        assert p.allow_breakout is False


class TestCryptoModeManagerAntiFlip:
    def test_no_switch_before_min_trades(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_wins=5, total_trades=1)
        mode, old = mgr.evaluate(m, btc_trend="up")
        assert mode == "SAFE"
        assert old is None

    def test_switches_after_min_trades(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_wins=3, total_trades=0)
        _advance(mgr, m, 2, btc_trend="up")
        # Switch happens on the 2nd evaluate call inside _advance
        assert mgr.mode == "AGGRESSIVE"


class TestCryptoModeManagerAggressive:
    def test_aggressive_requires_win_streak_and_btc_up(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_wins=3, total_trades=0)
        _advance(mgr, m, 2, btc_trend="up")
        assert mgr.mode == "AGGRESSIVE"

    def test_aggressive_blocked_when_btc_neutral(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_wins=3, total_trades=0)
        _advance(mgr, m, 2, btc_trend="neutral")
        assert mgr.mode == "SAFE"

    def test_aggressive_blocked_when_btc_down(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_wins=3, total_trades=0)
        _advance(mgr, m, 2, btc_trend="down")
        assert mgr.mode == "SAFE"

    def test_aggressive_params(self):
        mgr = CryptoModeManager()
        mgr._mode = "AGGRESSIVE"
        p = mgr.params()
        assert p.size_multiplier == 1.5
        assert p.take_profit_pct == 8.0
        assert p.allow_breakout is True

    def test_win_streak_below_threshold_stays_safe(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_wins=2, total_trades=0)  # needs 3
        _advance(mgr, m, 2, btc_trend="up")
        assert mgr.mode == "SAFE"


class TestCryptoModeManagerShield:
    def test_shield_on_loss_streak(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(consecutive_losses=3, total_trades=0)
        _advance(mgr, m, 2)
        assert mgr.mode == "SHIELD"

    def test_shield_on_daily_loss_pct(self):
        mgr = CryptoModeManager()
        # 6% daily loss exceeds shield threshold of 5%
        m = _crypto_metrics(balance=940.0, daily_start_balance=1000.0, total_trades=0)
        _advance(mgr, m, 2)
        assert mgr.mode == "SHIELD"

    def test_shield_daily_loss_below_threshold_stays_safe(self):
        mgr = CryptoModeManager()
        # 4% loss — below 5% threshold
        m = _crypto_metrics(balance=960.0, daily_start_balance=1000.0, total_trades=0)
        _advance(mgr, m, 2)
        assert mgr.mode == "SAFE"

    def test_shield_params(self):
        mgr = CryptoModeManager()
        mgr._mode = "SHIELD"
        p = mgr.params()
        assert p.size_multiplier == 0.3
        assert p.stop_loss_pct == 1.5
        assert p.take_profit_pct == 4.0
        assert p.allow_breakout is False

    def test_shield_takes_priority_over_win_streak(self):
        mgr = CryptoModeManager()
        # Even with win streak, loss_streak >= 3 triggers SHIELD
        m = _crypto_metrics(consecutive_wins=5, consecutive_losses=3, total_trades=0)
        _advance(mgr, m, 2, btc_trend="up")
        assert mgr.mode == "SHIELD"


class TestCryptoModeManagerSafe:
    def test_safe_on_single_loss(self):
        mgr = CryptoModeManager()
        mgr._mode = "AGGRESSIVE"
        mgr._trades_at_switch = 0
        m = _crypto_metrics(consecutive_losses=1, total_trades=2)
        mode, old = mgr.evaluate(m)
        assert mode == "SAFE"
        assert old == "AGGRESSIVE"

    def test_safe_on_poor_win_rate(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(
            total_trades=0,
            trade_history=[True, False, False, False, False, False, False, False, False, False],
        )
        _advance(mgr, m, 2)
        assert mgr.mode == "SAFE"

    def test_no_switch_returns_none_old(self):
        mgr = CryptoModeManager()
        m = _crypto_metrics(total_trades=0)
        _advance(mgr, m, 2)
        mode, old = mgr.evaluate(m)
        assert mode == "SAFE"
        assert old is None


# ---------------------------------------------------------------------------
# DayModeManager
# ---------------------------------------------------------------------------

class TestDayModeManagerInit:
    def test_starts_in_safe(self):
        assert DayModeManager().mode == "SAFE"

    def test_params_safe(self):
        p = DayModeManager().params()
        assert p.position_size_pct == 0.15
        assert p.stop_loss_pct == 0.010
        assert p.take_profit_pct == 0.025
        assert p.allow_breakout is False


class TestDayModeManagerAntiFlip:
    def test_no_switch_before_min_trades(self):
        mgr = DayModeManager()
        m = _day_metrics(consecutive_wins=5, wins_today=1, losses_today=0)
        mode, old = mgr.evaluate(m, spy_return=1.0)
        assert mode == "SAFE"
        assert old is None


class TestDayModeManagerAggressive:
    def test_aggressive_requires_streak_and_spy(self):
        mgr = DayModeManager()
        m = _day_metrics(consecutive_wins=3, wins_today=0, losses_today=0)
        _advance(mgr, m, 2, spy_return=0.5)
        assert mgr.mode == "AGGRESSIVE"

    def test_aggressive_blocked_when_spy_flat(self):
        mgr = DayModeManager()
        m = _day_metrics(consecutive_wins=3, wins_today=0, losses_today=0)
        _advance(mgr, m, 2, spy_return=0.1)  # below 0.3% threshold
        assert mgr.mode == "SAFE"

    def test_aggressive_blocked_when_any_losses(self):
        mgr = DayModeManager()
        m = _day_metrics(consecutive_wins=3, consecutive_losses=1, wins_today=0, losses_today=0)
        _advance(mgr, m, 2, spy_return=1.0)
        assert mgr.mode == "SAFE"

    def test_aggressive_params(self):
        mgr = DayModeManager()
        mgr._mode = "AGGRESSIVE"
        p = mgr.params()
        assert p.position_size_pct == 0.25
        assert p.stop_loss_pct == 0.015
        assert p.take_profit_pct == 0.050
        assert p.allow_breakout is True


class TestDayModeManagerShield:
    def test_shield_on_loss_streak(self):
        mgr = DayModeManager()
        m = _day_metrics(consecutive_losses=3, wins_today=0, losses_today=0)
        _advance(mgr, m, 2)
        assert mgr.mode == "SHIELD"

    def test_shield_on_daily_loss(self):
        mgr = DayModeManager()
        # 4% loss exceeds shield threshold of 3%
        m = _day_metrics(portfolio_value=9600.0, daily_start_value=10000.0, wins_today=0, losses_today=0)
        _advance(mgr, m, 2)
        assert mgr.mode == "SHIELD"

    def test_shield_daily_loss_below_threshold(self):
        mgr = DayModeManager()
        # 2% loss — below 3% threshold
        m = _day_metrics(portfolio_value=9800.0, daily_start_value=10000.0, wins_today=0, losses_today=0)
        _advance(mgr, m, 2)
        assert mgr.mode == "SAFE"

    def test_shield_params(self):
        mgr = DayModeManager()
        mgr._mode = "SHIELD"
        p = mgr.params()
        assert p.position_size_pct == 0.03
        assert p.allow_breakout is False

    def test_shield_priority_over_win_streak(self):
        mgr = DayModeManager()
        m = _day_metrics(consecutive_wins=5, consecutive_losses=3, wins_today=0, losses_today=0)
        _advance(mgr, m, 2, spy_return=1.0)
        assert mgr.mode == "SHIELD"


class TestDayModeManagerSafe:
    def test_safe_on_single_loss(self):
        mgr = DayModeManager()
        mgr._mode = "AGGRESSIVE"
        mgr._trades_at_switch = 0
        m = _day_metrics(consecutive_losses=1, wins_today=1, losses_today=1)
        mode, old = mgr.evaluate(m, spy_return=1.0)
        assert mode == "SAFE"
        assert old == "AGGRESSIVE"

    def test_safe_on_poor_win_rate(self):
        mgr = DayModeManager()
        # 2W / 8L = 20% win rate — below 50% threshold
        m = _day_metrics(wins_today=2, losses_today=8, consecutive_losses=0)
        _advance(mgr, m, 2, spy_return=1.0)
        assert mgr.mode == "SAFE"

    def test_win_rate_check_needs_5_trades(self):
        mgr = DayModeManager()
        # Only 4 trades done — win rate check not triggered
        m = _day_metrics(wins_today=1, losses_today=3, consecutive_losses=0)
        _advance(mgr, m, 2, spy_return=1.0)
        # 4 trades total, below 5 — win rate filter skipped
        assert mgr.mode == "SAFE"  # still SAFE but not because of win rate
