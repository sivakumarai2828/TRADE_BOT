"""MarketRunner — where model actions meet the risk layer.

These are the regression tests for bugs that actually reached production.
"""
from __future__ import annotations

import types

import pytest

from botv2.executor import MarketRunner


@pytest.fixture
def runner(caps, journal, ledger, monkeypatch):
    """A MarketRunner with the network and Telegram stubbed out."""
    from botv2 import notify

    monkeypatch.setattr(notify, "_send", lambda msg: None)
    cfg = types.SimpleNamespace(caps=caps, target_deployment_pct=0.0)
    ai = types.SimpleNamespace(decide=lambda p: {}, self_review=lambda *a: "")
    return MarketRunner("US", "USD", cfg, journal, ai, ledger, alpaca=None)


def _price(monkeypatch, px):
    from botv2 import data

    monkeypatch.setattr(data, "fetch_price", lambda s: px)


# ── BUY gating in _apply ─────────────────────────────────────────
def test_buy_blocked_within_earnings_window(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 115}
    runner._apply(act, {}, halt=None, earnings={"X": 2})
    assert runner.ledger.positions() == []


def test_buy_allowed_outside_earnings_window(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 115}
    runner._apply(act, {}, halt=None, earnings={"X": 4})
    assert len(runner.ledger.positions()) == 1


def test_buy_allowed_when_earnings_unknown(runner, monkeypatch):
    """None means 'no data', and must not freeze the book."""
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 115}
    runner._apply(act, {}, halt=None, earnings={"X": None})
    assert len(runner.ledger.positions()) == 1


def test_buy_blocked_at_earnings_boundary(runner, monkeypatch):
    """days_to_earnings == 3 is inside the block (<=)."""
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 115}
    runner._apply(act, {}, halt=None, earnings={"X": 3})
    assert runner.ledger.positions() == []


def test_buy_blocked_while_halt_active(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 115}
    runner._apply(act, {}, halt="DAILY HALT: ...", earnings={})
    assert runner.ledger.positions() == []


def test_buy_skipped_when_already_held(runner, monkeypatch):
    """No pyramiding."""
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 115}
    runner._apply(act, {"X": {"symbol": "X"}}, halt=None, earnings={})
    assert runner.ledger.positions() == []


def test_buy_below_2R_rejected_by_risk_layer(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "stop": 95, "target": 105}
    runner._apply(act, {}, halt=None, earnings={})
    assert runner.ledger.positions() == []


def test_malformed_action_does_not_raise(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner._apply({"action": "BUY"}, {}, halt=None, earnings={})
    runner._apply({}, {}, halt=None, earnings={})


# ── monitor_positions ────────────────────────────────────────────
def test_stop_triggers_exit(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    _price(monkeypatch, 94.0)
    runner.monitor_positions()
    assert runner.ledger.positions() == []


def test_target_triggers_exit(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    _price(monkeypatch, 116.0)
    runner.monitor_positions()
    assert runner.ledger.positions() == []


def test_position_held_between_stop_and_target(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    _price(monkeypatch, 105.0)
    runner.monitor_positions()
    assert len(runner.ledger.positions()) == 1


def test_stale_price_does_not_trigger_false_stop(runner, monkeypatch):
    """Regression for 2026-07-16.

    With the stop trailed above entry, a failed price fetch made last_price
    fall back to entry — which read as 'stop hit' and closed a winning trade.
    """
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=130, thesis="t")
    pos = runner.ledger.positions()[0]
    runner.journal.update_levels(pos["id"], 105, 130)   # trail stop above entry
    _price(monkeypatch, None)                            # data outage
    runner.monitor_positions()
    assert len(runner.ledger.positions()) == 1, "stale price closed a live position"


def test_kill_switch_flattens_book(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    runner.journal.kv_set("US_equity_peak", "1000000")   # force a deep drawdown
    runner.monitor_positions()
    assert runner.ledger.positions() == []


# ── SELL / ADJUST ────────────────────────────────────────────────
def test_sell_closes_position(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    pos = runner.ledger.positions()[0]
    runner._apply({"action": "SELL", "symbol": "X", "reason": "broke"},
                  {"X": pos}, halt=None, earnings={})
    assert runner.ledger.positions() == []


def test_duplicate_sell_in_one_plan_is_guarded(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    pos = runner.ledger.positions()[0]
    by_symbol = {"X": pos}
    act = {"action": "SELL", "symbol": "X", "reason": "r"}
    cash_before = runner.ledger.cash
    runner._apply(act, by_symbol, halt=None, earnings={})
    cash_once = runner.ledger.cash
    runner._apply(act, by_symbol, halt=None, earnings={})
    assert runner.ledger.cash == cash_once > cash_before


def test_adjust_raises_stop(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    pos = runner.ledger.positions()[0]
    runner._apply({"action": "ADJUST", "symbol": "X", "stop": 98},
                  {"X": pos}, halt=None, earnings={})
    assert runner.ledger.positions()[0]["stop"] == 98


def test_adjust_cannot_lower_stop(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=95, target=115, thesis="t")
    pos = runner.ledger.positions()[0]
    runner._apply({"action": "ADJUST", "symbol": "X", "stop": 90},
                  {"X": pos}, halt=None, earnings={})
    assert runner.ledger.positions()[0]["stop"] == 95
