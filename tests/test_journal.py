"""Journal — the trade ledger of record."""
from __future__ import annotations

import pytest


def test_open_trade_returns_id_and_appears_open(journal):
    tid = journal.open_trade("US", "X", 10, 100, 95, 115, "thesis")
    assert tid
    assert [t["symbol"] for t in journal.open_trades("US")] == ["X"]


def test_close_trade_computes_pnl(journal):
    tid = journal.open_trade("US", "X", 10, 100, 95, 115, "t")
    assert journal.close_trade(tid, 110, "take_profit") is True
    closed = journal.recent_closed("US")[0]
    assert closed["pnl"] == pytest.approx(100.0)
    assert closed["pnl_pct"] == pytest.approx(10.0)


def test_close_trade_twice_returns_false(journal):
    """Atomic guard against double-closing the same row."""
    tid = journal.open_trade("US", "X", 10, 100, 95, 115, "t")
    assert journal.close_trade(tid, 110, "first") is True
    assert journal.close_trade(tid, 120, "second") is False


def test_closed_trade_leaves_open_list(journal):
    tid = journal.open_trade("US", "X", 10, 100, 95, 115, "t")
    journal.close_trade(tid, 110, "done")
    assert journal.open_trades("US") == []


def test_markets_are_isolated(journal):
    journal.open_trade("US", "AAPL", 1, 100, 95, 115, "t")
    journal.open_trade("INDIA", "TITAN.NS", 1, 100, 95, 115, "t")
    assert len(journal.open_trades("US")) == 1
    assert len(journal.open_trades("INDIA")) == 1


def test_stats_on_empty_book_does_not_divide_by_zero(journal):
    s = journal.stats("US")
    assert s["closed_trades"] == 0
    assert s["win_rate_pct"] is None


def test_stats_computes_win_rate_and_averages(journal):
    for exit_px, in ((110,), (110,), (90,)):
        tid = journal.open_trade("US", "X", 10, 100, 95, 115, "t")
        journal.close_trade(tid, exit_px, "r")
    s = journal.stats("US")
    assert s["closed_trades"] == 3
    assert s["win_rate_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["avg_win"] == pytest.approx(100.0)
    assert s["avg_loss"] == pytest.approx(-100.0)


def test_entries_today_counts_new_opens(journal):
    assert journal.entries_today("US") == 0
    journal.open_trade("US", "X", 1, 100, 95, 115, "t")
    journal.open_trade("US", "Y", 1, 100, 95, 115, "t")
    assert journal.entries_today("US") == 2


def test_entries_today_is_per_market(journal):
    journal.open_trade("US", "X", 1, 100, 95, 115, "t")
    assert journal.entries_today("INDIA") == 0


def test_update_levels_changes_stop_and_target(journal):
    tid = journal.open_trade("US", "X", 1, 100, 95, 115, "t")
    journal.update_levels(tid, 99, 120)
    pos = journal.open_trades("US")[0]
    assert pos["stop"] == 99 and pos["target"] == 120


def test_update_levels_ignores_none(journal):
    tid = journal.open_trade("US", "X", 1, 100, 95, 115, "t")
    journal.update_levels(tid, 99, None)
    pos = journal.open_trades("US")[0]
    assert pos["stop"] == 99 and pos["target"] == 115


def test_kv_roundtrip_and_default(journal):
    assert journal.kv_get("missing", "fallback") == "fallback"
    journal.kv_set("k", "v1")
    assert journal.kv_get("k") == "v1"
    journal.kv_set("k", "v2")
    assert journal.kv_get("k") == "v2"


def test_memo_returns_latest(journal):
    journal.save_memo("US", "self_review", "older")
    journal.save_memo("US", "self_review", "newer")
    assert journal.latest_memo("US") == "newer"


def test_memo_absent_returns_none(journal):
    assert journal.latest_memo("US") is None
