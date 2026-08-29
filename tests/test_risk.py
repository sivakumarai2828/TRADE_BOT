"""RiskEngine — the only wall between the model and an oversized position.

Boundary cases matter more than happy paths here: a gate that is off by one
comparison operator still looks correct in normal use.
"""
from __future__ import annotations

import pytest

EQUITY = 10_000.0
CASH = 10_000.0


# ── validate_buy: reward / risk ──────────────────────────────────
def test_rr_exactly_2R_accepted(risk):
    """R = 2.0 exactly must pass — the rule is >=, not >."""
    v = risk.validate_buy("X", price=100, stop=95, target=110,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.ok, v.reason


def test_rr_just_below_2R_rejected(risk):
    v = risk.validate_buy("X", price=100, stop=95, target=109.5,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "reward/risk" in v.reason


def test_rr_above_2R_accepted(risk):
    v = risk.validate_buy("X", price=100, stop=95, target=115,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.ok, v.reason


def test_target_at_entry_rejected(risk):
    v = risk.validate_buy("X", price=100, stop=95, target=100,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "target" in v.reason


def test_missing_target_rejected(risk):
    v = risk.validate_buy("X", price=100, stop=95, target=None,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "target" in v.reason


# ── validate_buy: direction and sanity ───────────────────────────
def test_stop_above_entry_rejected(risk):
    v = risk.validate_buy("X", price=100, stop=105, target=130,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "long only" in v.reason


def test_nonpositive_price_rejected(risk):
    v = risk.validate_buy("X", price=0, stop=-1, target=10,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok


# ── validate_buy: stop-distance bounds ───────────────────────────
def test_stop_too_tight_rejected(risk):
    """0.5% stop — below the 1% floor that stops fake stops."""
    v = risk.validate_buy("X", price=100, stop=99.5, target=102,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "too tight" in v.reason


def test_stop_at_1pct_boundary_accepted(risk):
    v = risk.validate_buy("X", price=100, stop=99, target=103,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.ok, v.reason


def test_stop_too_wide_rejected(risk):
    """16% stop — above the 15% ceiling."""
    v = risk.validate_buy("X", price=100, stop=84, target=140,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "too wide" in v.reason


def test_stop_at_15pct_boundary_accepted(risk):
    v = risk.validate_buy("X", price=100, stop=85, target=135,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.ok, v.reason


# ── validate_buy: portfolio limits ───────────────────────────────
def test_max_open_positions_blocks(risk, caps):
    v = risk.validate_buy("X", price=100, stop=95, target=110, equity=EQUITY,
                          cash=CASH, open_positions=caps.max_open_positions)
    assert not v.ok and "max open positions" in v.reason


def test_one_below_max_positions_allowed(risk, caps):
    v = risk.validate_buy("X", price=100, stop=95, target=110, equity=EQUITY,
                          cash=CASH, open_positions=caps.max_open_positions - 1)
    assert v.ok, v.reason


def test_daily_entry_limit_blocks(risk, journal, caps):
    for i in range(caps.max_new_entries_per_day):
        journal.open_trade("US", f"S{i}", 1, 100, 95, 110, "t")
    v = risk.validate_buy("X", price=100, stop=95, target=110,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "max new entries" in v.reason


# ── position sizing ──────────────────────────────────────────────
def test_size_limited_by_risk_budget(risk):
    """Wide stop -> the 2% risk budget binds, not the position cap.

    risk/share = 10, budget = 200  ->  20 shares ($2,000 = 20% of equity)
    """
    v = risk.validate_buy("X", price=100, stop=90, target=125,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.ok and v.qty == 20


def test_size_limited_by_position_cap(risk):
    """Tight stop -> risk budget would allow far more than 25% of equity.

    risk/share = 2 -> 100 shares by risk, but 25% cap = $2,500 = 25 shares.
    """
    v = risk.validate_buy("X", price=100, stop=98, target=110,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.ok and v.qty == 25


def test_size_limited_by_cash(risk):
    v = risk.validate_buy("X", price=100, stop=90, target=125,
                          equity=EQUITY, cash=550, open_positions=0)
    assert v.ok and v.qty == 5


def test_size_rounds_down_to_whole_shares(risk):
    v = risk.validate_buy("X", price=300, stop=270, target=390,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert v.qty == float(int(v.qty))


def test_zero_shares_rejected(risk):
    """Price far above what any cap allows -> rounds to 0, must reject."""
    v = risk.validate_buy("X", price=9_000, stop=8_500, target=10_100,
                          equity=EQUITY, cash=CASH, open_positions=0)
    assert not v.ok and "rounds to 0" in v.reason


def test_risk_never_exceeds_2pct_of_equity(risk):
    """Property: across many stop widths, modelled loss stays within budget."""
    for stop_pct in (0.02, 0.05, 0.08, 0.10, 0.12, 0.15):
        price, stop = 100.0, 100.0 * (1 - stop_pct)
        target = price + (price - stop) * 2.5
        v = risk.validate_buy("X", price, stop, target,
                              equity=EQUITY, cash=1e9, open_positions=0)
        if v.ok:
            assert v.qty * (price - stop) <= EQUITY * 0.02 + 1e-6


# ── check_halts: kill switch and daily halt ──────────────────────
def test_no_halt_when_healthy(risk):
    assert risk.check_halts(equity=10_000, day_pnl=0) is None


def test_kill_switch_at_exact_threshold(risk, journal):
    """-10% from peak triggers; the comparison is <=."""
    journal.kv_set("US_equity_peak", "10000")
    halt = risk.check_halts(equity=9_000, day_pnl=0)
    assert halt and "KILL SWITCH" in halt


def test_kill_switch_not_triggered_just_above(risk, journal):
    journal.kv_set("US_equity_peak", "10000")
    assert risk.check_halts(equity=9_001, day_pnl=0) is None


def test_kill_switch_flag_persists_after_recovery(risk, journal):
    """Once killed, recovery must not silently resume trading."""
    journal.kv_set("US_equity_peak", "10000")
    risk.check_halts(equity=9_000, day_pnl=0)
    halt = risk.check_halts(equity=12_000, day_pnl=0)
    assert halt and "kill-switched" in halt


def test_peak_ratchets_up(risk, journal):
    risk.check_halts(equity=10_000, day_pnl=0)
    risk.check_halts(equity=15_000, day_pnl=0)
    assert float(journal.kv_get("US_equity_peak")) == 15_000


def test_daily_halt_at_threshold(risk):
    halt = risk.check_halts(equity=10_000, day_pnl=-300)
    assert halt and "DAILY HALT" in halt


def test_daily_halt_not_triggered_just_above(risk):
    assert risk.check_halts(equity=10_000, day_pnl=-299) is None


def test_zero_equity_does_not_crash(risk):
    risk.check_halts(equity=0, day_pnl=-100)


# ── validate_adjust: the stop ratchet ────────────────────────────
def test_stop_may_move_up(risk):
    stop, target, note = risk.validate_adjust("X", 110, old_stop=95,
                                              new_stop=100, new_target=None)
    assert stop == 100 and not note


def test_stop_may_not_move_down(risk):
    stop, target, note = risk.validate_adjust("X", 110, old_stop=100,
                                              new_stop=95, new_target=None)
    assert stop is None and "only move UP" in note


def test_stop_at_or_above_price_rejected(risk):
    stop, target, note = risk.validate_adjust("X", 110, old_stop=95,
                                              new_stop=110, new_target=None)
    assert stop is None and ">= current price" in note


def test_non_numeric_stop_rejected(risk):
    stop, target, note = risk.validate_adjust("X", 110, old_stop=95,
                                              new_stop="abc", new_target=None)
    assert stop is None and "non-numeric" in note


def test_nan_stop_rejected(risk):
    stop, _, note = risk.validate_adjust("X", 110, old_stop=95,
                                         new_stop=float("nan"), new_target=None)
    assert stop is None and "non-numeric" in note


def test_negative_stop_rejected(risk):
    stop, _, note = risk.validate_adjust("X", 110, old_stop=95,
                                         new_stop=-5, new_target=None)
    assert stop is None


def test_target_below_price_rejected(risk):
    _, target, note = risk.validate_adjust("X", 110, old_stop=95,
                                           new_stop=None, new_target=105)
    assert target is None and "<= current price" in note


def test_target_below_new_stop_rejected(risk):
    _, target, note = risk.validate_adjust("X", 110, old_stop=95,
                                           new_stop=100, new_target=99)
    assert target is None


def test_adjust_with_no_prior_stop(risk):
    """old_stop None must not crash and must accept a sane new stop."""
    stop, _, _ = risk.validate_adjust("X", 110, old_stop=None,
                                      new_stop=100, new_target=None)
    assert stop == 100


# ── describe(): what the model is told ───────────────────────────
def test_describe_reports_live_caps(risk, caps):
    d = risk.describe()
    assert d["min_reward_risk_ratio"] == 2.0
    assert d["max_risk_per_trade_pct_of_equity"] == caps.max_risk_per_trade_pct
    assert d["max_position_pct_of_equity"] == caps.max_position_pct
