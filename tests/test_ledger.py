"""PaperLedger — where money actually moves.

Fees, the double-sell guard and the price_stale flag all have live P&L
consequences, so each is pinned here.
"""
from __future__ import annotations

import pytest

US_FEE = 0.0005  # 0.05% per side


def test_buy_applies_entry_fee(ledger, fixed_price):
    fixed_price(100.0)
    fill = ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    assert fill is not None
    assert fill["fill"] == pytest.approx(100.0 * (1 + US_FEE), rel=1e-9)


def test_buy_decrements_cash_by_cost(ledger, fixed_price):
    fixed_price(100.0)
    before = ledger.cash
    fill = ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    assert ledger.cash == pytest.approx(before - fill["fill"] * 10, abs=0.01)


def test_buy_shrinks_qty_when_cash_short(ledger, fixed_price):
    fixed_price(100.0)
    ledger._set_cash(550)
    fill = ledger.buy("X", qty=100, stop=95, target=115, thesis="t")
    assert fill["qty"] == 5


def test_buy_returns_none_when_cash_below_one_share(ledger, fixed_price):
    fixed_price(100.0)
    ledger._set_cash(50)
    assert ledger.buy("X", qty=10, stop=95, target=115, thesis="t") is None


def test_buy_skipped_when_price_unavailable(ledger, fixed_price):
    fixed_price(None)
    assert ledger.buy("X", qty=10, stop=95, target=115, thesis="t") is None


def test_sell_applies_exit_fee(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    fixed_price(120.0)
    pos = ledger.positions()[0]
    fill = ledger.sell(pos, "take_profit")
    assert fill["fill"] == pytest.approx(120.0 * (1 - US_FEE), rel=1e-9)


def test_sell_credits_cash(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    fixed_price(120.0)
    before = ledger.cash
    fill = ledger.sell(ledger.positions()[0], "take_profit")
    assert ledger.cash == pytest.approx(before + fill["fill"] * 10, abs=0.01)


def test_double_sell_is_refused(ledger, fixed_price):
    """The guard that stops one position being sold twice."""
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    pos = ledger.positions()[0]
    assert ledger.sell(pos, "first") is not None
    cash_after_first = ledger.cash
    assert ledger.sell(pos, "second") is None
    assert ledger.cash == cash_after_first


def test_sell_skipped_when_price_unavailable(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    pos = ledger.positions()[0]
    fixed_price(None)
    assert ledger.sell(pos, "stop_loss") is None


def test_price_stale_flag_set_on_fetch_failure(ledger, fixed_price):
    """The 2026-07-16 false-stop fix: a failed fetch must be visible."""
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    fixed_price(None)
    pos = ledger.positions()[0]
    assert pos["price_stale"] is True
    assert pos["last_price"] == pytest.approx(pos["entry"], abs=0.01)


def test_price_stale_false_when_fetch_works(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    assert ledger.positions()[0]["price_stale"] is False


def test_unrealized_pnl_tracks_price(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    fixed_price(110.0)
    pos = ledger.positions()[0]
    assert pos["unrealized_pnl"] == pytest.approx((110.0 - pos["entry"]) * 10, abs=0.01)


def test_equity_is_cash_plus_market_value(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    fixed_price(110.0)
    assert ledger.equity() == pytest.approx(ledger.cash + 110.0 * 10, abs=0.02)


def test_day_pnl_zero_on_first_call_of_new_day(ledger, fixed_price):
    fixed_price(100.0)
    assert ledger.day_pnl() == 0.0


def test_day_pnl_measures_move_from_day_start(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=10, stop=95, target=115, thesis="t")
    ledger.day_pnl()                      # sets the day baseline
    fixed_price(110.0)
    assert ledger.day_pnl() == pytest.approx(100.0, abs=1.0)


def test_flatten_all_closes_every_position(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("A", qty=5, stop=95, target=115, thesis="t")
    ledger.buy("B", qty=5, stop=95, target=115, thesis="t")
    fills = ledger.flatten_all("kill_switch")
    assert len(fills) == 2
    assert ledger.positions() == []


def test_duplicate_buy_refused(ledger, fixed_price):
    """Same symbol twice must not open a second position, even if the caller
    forgot its own bookkeeping."""
    fixed_price(100.0)
    assert ledger.buy("X", qty=5, stop=95, target=115, thesis="t") is not None
    cash_after_first = ledger.cash
    assert ledger.buy("X", qty=5, stop=95, target=115, thesis="t") is None
    assert ledger.cash == cash_after_first
    assert len(ledger.positions()) == 1


def test_buy_allowed_again_after_position_closed(ledger, fixed_price):
    fixed_price(100.0)
    ledger.buy("X", qty=5, stop=95, target=115, thesis="t")
    ledger.sell(ledger.positions()[0], "done")
    assert ledger.buy("X", qty=5, stop=95, target=115, thesis="t") is not None


def test_duplicate_guard_is_per_market(journal, fixed_price):
    from botv2.ledger import PaperLedger
    fixed_price(100.0)
    us = PaperLedger(journal, "US", 10_000.0)
    india = PaperLedger(journal, "INDIA", 10_000.0)
    assert us.buy("X", 5, 95, 115, "t") is not None
    assert india.buy("X", 5, 95, 115, "t") is not None
