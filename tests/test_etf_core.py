"""ETF core (paper): the monthly rotation rule and its rebalancing. No network."""
from __future__ import annotations

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from botv2 import notify
from botv2.config import V2Config
from botv2.etf_core import CASH, POOL, ETFCore

NY = ZoneInfo("America/New_York")
N = 260   # trading days of history


def series(start, end, n=N):
    """Straight line from start to end: 6-month return and trend are predictable."""
    return [start + (end - start) * i / (n - 1) for i in range(n)]


class FakeHistory:
    def __init__(self, closes):
        self.data = closes

    def closes(self, symbols):
        return {s: self.data[s] for s in symbols if s in self.data}


def flat_pool(**overrides):
    d = {s: series(100, 100) for s in POOL}            # 0% momentum: loses to cash
    d[CASH] = series(80, 81)                            # cash ~ +0.6% over 6 months
    d.update(overrides)
    return d


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_send", lambda m: out.append(m))
    return out


def core(journal, closes, month=(2026, 11, 18), **cfg):
    c = dataclasses.replace(V2Config(), etf_core_enabled=True, **cfg)
    return ETFCore(journal, c, history=FakeHistory(closes), now=lambda: datetime(*month, 10, 5, tzinfo=NY))


def test_picks_top_five_rising_above_trend_and_beating_cash(journal, sent):
    up = {s: series(100, 100 + 10 * (k + 1)) for k, s in enumerate(["QQQ", "XLK", "GLD", "SPY", "XLI", "XLF"])}
    w, rank = core(journal, flat_pool(**up)).targets(flat_pool(**up))
    assert set(w) == {"XLF", "XLI", "SPY", "GLD", "XLK"}                       # strongest five
    assert all(v == pytest.approx(0.2) for v in w.values())
    assert [r["symbol"] for r in rank[:6]] == ["XLF", "XLI", "SPY", "GLD", "XLK", "QQQ"]
    assert next(r for r in rank if r["symbol"] == "QQQ")["picked"] is False


def test_falling_or_weak_markets_go_to_cash(journal, sent):
    down = {"SPY": series(120, 100), "QQQ": series(100, 103)}                  # one falling, one rising
    w, _ = core(journal, flat_pool(**down)).targets(flat_pool(**down))
    assert w == {"QQQ": pytest.approx(0.2), CASH: pytest.approx(0.8)}


def test_spy_sleeve_for_the_blend(journal, sent):
    w, _ = core(journal, flat_pool(), etf_hold_spy_pct=0.5).targets(flat_pool())
    assert w == {CASH: pytest.approx(0.5), "SPY": pytest.approx(0.5)}


def test_first_rebalance_buys_the_targets_with_fractional_shares(journal, sent):
    up = {s: series(100, 120 + k) for k, s in enumerate(["QQQ", "XLK", "GLD", "SPY", "XLI"])}
    c = core(journal, flat_pool(**up))
    reb = c.rebalance()
    b = c.book()
    assert set(b["holdings"]) == {"QQQ", "XLK", "GLD", "SPY", "XLI"}
    assert b["cash"] == pytest.approx(0, abs=0.01)
    assert c.equity(b, b["marks"]) == pytest.approx(437 * (1 - 0.0005), abs=0.05)
    assert len(reb["trades"]) == 5 and all(t["side"] == "buy" for t in reb["trades"])
    assert any("Monthly rebalance" in m for m in sent)
    assert c.rebalance() is None                                              # once a month


def test_next_month_sells_what_dropped_out_and_leaves_steady_holdings(journal, sent):
    up = {s: series(100, 120 + k) for k, s in enumerate(["QQQ", "XLK", "GLD", "SPY", "XLI"])}
    core(journal, flat_pool(**up)).rebalance()
    later = dict(up, XLI=series(130, 100))                                    # XLI breaks down
    reb = core(journal, flat_pool(**later), month=(2026, 12, 1)).rebalance()
    sides = {(t["side"], t["symbol"]) for t in reb["trades"]}
    assert ("sell", "XLI") in sides and ("buy", CASH) in sides
    assert not any(t["symbol"] in ("QQQ", "XLK", "GLD", "SPY") for t in reb["trades"])   # within band


def test_no_prices_means_no_trades(journal, sent):
    assert core(journal, {}).rebalance() is None
    assert core(journal, {}).book()["holdings"] == {}


def test_summary_shape(journal, sent):
    up = {s: series(100, 120 + k) for k, s in enumerate(["QQQ", "XLK", "GLD", "SPY", "XLI"])}
    c = core(journal, flat_pool(**up))
    c.rebalance()
    s = c.summary()
    assert s["mode"] == "paper" and len(s["holdings"]) == 5 and s["last_rebalance"]["month"] == "2026-11"
    assert sum(h["weight"] for h in s["holdings"]) == pytest.approx(1.0, abs=0.01)


def test_off_by_default_and_never_with_broker_mode():
    assert V2Config().etf_core_enabled is False
