"""V3 features: attribution (Phase 2), WATCH (Phase 3), REPLACE (Phase 4)."""
from __future__ import annotations

import time
import types

import pytest

from botv2.executor import MarketRunner, classify_regime
from botv2.universe import sector_for


@pytest.fixture
def runner(caps, journal, ledger, monkeypatch):
    from botv2 import notify

    monkeypatch.setattr(notify, "_send", lambda msg: None)
    cfg = types.SimpleNamespace(caps=caps, target_deployment_pct=0.0)
    ai = types.SimpleNamespace(decide=lambda p: {}, self_review=lambda *a: "")
    r = MarketRunner("US", "USD", cfg, journal, ai, ledger, alpaca=None)
    r._regime_label = "BULL"
    r._snapshots = {"X": {"symbol": "X", "price": 100.0, "rsi14": 58.0,
                          "atr14": 2.5, "vol20_vs_vol50": 1.4,
                          "rs_3m_vs_benchmark": 12.0, "stage2_uptrend": True,
                          "sma200": 80.0, "days_to_earnings": 30}}
    return r


def _price(monkeypatch, px):
    from botv2 import data

    monkeypatch.setattr(data, "fetch_price", lambda s: px)


def _snapshot(monkeypatch, snap):
    from botv2 import data

    monkeypatch.setattr(data, "fetch_snapshot", lambda s, **kw: snap)


# ══ Phase 2: attribution ═════════════════════════════════════════
def test_regime_classifier():
    bull = [{"symbol": "SPY", "price": 100, "sma50": 90, "ret_3m_pct": 12}]
    bear = [{"symbol": "SPY", "price": 80, "sma50": 90, "ret_3m_pct": -12}]
    chop = [{"symbol": "SPY", "price": 80, "sma50": 90, "ret_3m_pct": 1}]
    neut = [{"symbol": "SPY", "price": 100, "sma50": 90, "ret_3m_pct": 1}]
    assert classify_regime(bull) == "BULL"
    assert classify_regime(bear) == "BEAR"
    assert classify_regime(chop) == "CHOPPY"
    assert classify_regime(neut) == "NEUTRAL"
    assert classify_regime([]) == "UNKNOWN"


def test_sector_map_covers_universe():
    from botv2.universe import INDIA_UNIVERSE, US_UNIVERSE

    assert [s for s in US_UNIVERSE + INDIA_UNIVERSE if sector_for(s) is None] == []


def test_buy_records_attribution(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "setup": "PULLBACK",
           "stop": 90, "target": 125, "thesis": "t"}
    runner._apply(act, {}, halt=None, earnings={})
    row = runner.journal.open_trades("US")[0]
    assert row["setup_type"] == "PULLBACK"
    assert row["market_regime"] == "BULL"
    assert row["rsi_entry"] == 58.0
    assert row["stage2_entry"] in (1, True)
    assert row["planned_r"] == pytest.approx(2.5, abs=0.05)
    assert row["initial_stop"] == 90


def test_invalid_setup_label_stored_as_none(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "X", "setup": "SCALP",
           "stop": 90, "target": 125, "thesis": "t"}
    runner._apply(act, {}, halt=None, earnings={})
    assert runner.journal.open_trades("US")[0]["setup_type"] is None


def test_initial_stop_survives_ratchet(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=90, target=125, thesis="t", meta={})
    pos = runner.ledger.positions()[0]
    runner.journal.update_levels(pos["id"], 105, 125)
    row = runner.journal.open_trades("US")[0]
    assert row["stop"] == 105 and row["initial_stop"] == 90


def test_realized_r_measured_from_initial_stop(runner, monkeypatch):
    """Entry 100, initial stop 90 -> 1R = 10. Exit at 120 is +2R."""
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=90, target=125, thesis="t", meta={})
    pos = runner.ledger.positions()[0]
    runner.journal.update_levels(pos["id"], 108, 125)      # ratchet the stop up
    _price(monkeypatch, 120.0)
    runner._exit(runner.ledger.positions()[0], "take_profit")
    closed = runner.journal.recent_closed("US")[0]
    assert closed["realized_r"] == pytest.approx(2.0, abs=0.05)


def test_realized_r_negative_on_loss(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=90, target=125, thesis="t", meta={})
    _price(monkeypatch, 90.0)
    runner._exit(runner.ledger.positions()[0], "stop_loss")
    assert runner.journal.recent_closed("US")[0]["realized_r"] < 0


# ══ Phase 3: WATCH ═══════════════════════════════════════════════
def test_watch_created(runner, monkeypatch):
    act = {"action": "WATCH", "symbol": "X", "setup": "BREAKOUT",
           "ideal_entry": 100, "stop": 90, "target": 125, "thesis": "waiting"}
    runner._apply(act, {}, halt=None, earnings={})
    w = runner.journal.active_watches("US")
    assert len(w) == 1 and w[0]["symbol"] == "X" and w[0]["setup"] == "BREAKOUT"


def test_watch_rejected_below_2R(runner):
    act = {"action": "WATCH", "symbol": "X", "ideal_entry": 100,
           "stop": 90, "target": 110, "thesis": "t"}          # 1.0R
    runner._apply(act, {}, halt=None, earnings={})
    assert runner.journal.active_watches("US") == []


def test_watch_rejected_when_levels_out_of_order(runner):
    act = {"action": "WATCH", "symbol": "X", "ideal_entry": 100,
           "stop": 110, "target": 125, "thesis": "t"}
    runner._apply(act, {}, halt=None, earnings={})
    assert runner.journal.active_watches("US") == []


def test_watch_rejected_when_already_held(runner, monkeypatch):
    act = {"action": "WATCH", "symbol": "X", "ideal_entry": 100,
           "stop": 90, "target": 125, "thesis": "t"}
    runner._apply(act, {"X": {"symbol": "X"}}, halt=None, earnings={})
    assert runner.journal.active_watches("US") == []


def test_new_watch_supersedes_old(runner):
    for tgt in (125, 130):
        runner._apply({"action": "WATCH", "symbol": "X", "ideal_entry": 100,
                       "stop": 90, "target": tgt, "thesis": "t"},
                      {}, halt=None, earnings={})
    active = runner.journal.active_watches("US")
    assert len(active) == 1 and active[0]["target"] == 130


def test_watch_expires(runner, journal):
    journal.add_watch("US", "X", "BREAKOUT", 100, 90, 125, "t", expiry_days=-1)
    assert journal.expire_watches("US") == 1
    assert journal.active_watches("US") == []


def test_watch_triggers_and_buys(runner, monkeypatch, journal):
    journal.add_watch("US", "X", "PULLBACK", 100, 90, 125, "t")
    _price(monkeypatch, 99.0)                                   # at the level
    _snapshot(monkeypatch, runner._snapshots["X"])
    runner.check_watch_triggers()
    assert [p["symbol"] for p in runner.ledger.positions()] == ["X"]
    assert journal.active_watches("US") == []


def test_watch_does_not_trigger_above_level(runner, monkeypatch, journal):
    journal.add_watch("US", "X", "PULLBACK", 100, 90, 125, "t")
    _price(monkeypatch, 108.0)
    runner.check_watch_triggers()
    assert runner.ledger.positions() == []
    assert len(journal.active_watches("US")) == 1


def test_watch_cancelled_when_earnings_arrive(runner, monkeypatch, journal):
    journal.add_watch("US", "X", "PULLBACK", 100, 90, 125, "t")
    _price(monkeypatch, 99.0)
    snap = dict(runner._snapshots["X"], days_to_earnings=1)
    _snapshot(monkeypatch, snap)
    runner.check_watch_triggers()
    assert runner.ledger.positions() == []
    assert journal.active_watches("US") == []


def test_watch_cancelled_when_trend_broken(runner, monkeypatch, journal):
    journal.add_watch("US", "X", "PULLBACK", 100, 90, 125, "t")
    _price(monkeypatch, 99.0)
    snap = dict(runner._snapshots["X"], price=70.0, sma200=80.0)
    _snapshot(monkeypatch, snap)
    runner.check_watch_triggers()
    assert runner.ledger.positions() == []


def test_watch_not_triggered_during_halt(runner, monkeypatch, journal):
    journal.add_watch("US", "X", "PULLBACK", 100, 90, 125, "t")
    _price(monkeypatch, 99.0)
    _snapshot(monkeypatch, runner._snapshots["X"])
    runner.check_watch_triggers(halt="DAILY HALT")
    assert runner.ledger.positions() == []
    assert len(journal.active_watches("US")) == 1


def test_direct_buy_cancels_pending_watch(runner, monkeypatch, journal):
    journal.add_watch("US", "X", "PULLBACK", 100, 90, 125, "t")
    _price(monkeypatch, 100.0)
    runner._apply({"action": "BUY", "symbol": "X", "setup": "PULLBACK",
                   "stop": 90, "target": 125, "thesis": "t"},
                  {}, halt=None, earnings={})
    assert journal.active_watches("US") == []


# ══ Phase 4: REPLACE ═════════════════════════════════════════════
def _open_old(runner, monkeypatch, *, age_days=10, stop=90, entry_px=100.0):
    _price(monkeypatch, entry_px)
    runner.ledger.buy("OLD", 10, stop=stop, target=125, thesis="t", meta={})
    row = runner.journal.open_trades("US")[0]
    with runner.journal._conn() as c:
        c.execute("UPDATE trades SET ts_open=? WHERE id=?",
                  (time.time() - age_days * 86400, row["id"]))
    return runner.ledger.positions()[0]


def test_replace_swaps_weakest_holding(runner, monkeypatch):
    old = _open_old(runner, monkeypatch)
    _price(monkeypatch, 100.0)
    runner._apply({"action": "REPLACE", "symbol": "X", "replace": "OLD",
                   "setup": "PULLBACK", "stop": 90, "target": 140, "thesis": "better"},
                  {"OLD": old}, halt=None, earnings={})
    assert [p["symbol"] for p in runner.ledger.positions()] == ["X"]


def test_replace_refused_when_old_is_risk_free(runner, monkeypatch):
    """Stop at/above entry means the trade cannot lose — never give it up."""
    old = _open_old(runner, monkeypatch, stop=90)
    runner.journal.update_levels(old["id"], 101, 125)      # ratchet above entry
    old = runner.ledger.positions()[0]
    _price(monkeypatch, 100.0)
    runner._apply({"action": "REPLACE", "symbol": "X", "replace": "OLD",
                   "stop": 90, "target": 140, "thesis": "t"},
                  {"OLD": old}, halt=None, earnings={})
    assert "OLD" in [p["symbol"] for p in runner.ledger.positions()]


def test_replace_refused_when_position_too_young(runner, monkeypatch):
    old = _open_old(runner, monkeypatch, age_days=1)
    _price(monkeypatch, 100.0)
    runner._apply({"action": "REPLACE", "symbol": "X", "replace": "OLD",
                   "stop": 90, "target": 140, "thesis": "t"},
                  {"OLD": old}, halt=None, earnings={})
    assert "OLD" in [p["symbol"] for p in runner.ledger.positions()]


def test_replace_refused_without_material_edge(runner, monkeypatch):
    """Old is 2.5R; new must clear 3.0R. 2.6R is not enough."""
    old = _open_old(runner, monkeypatch)
    _price(monkeypatch, 100.0)
    runner._apply({"action": "REPLACE", "symbol": "X", "replace": "OLD",
                   "stop": 90, "target": 126, "thesis": "t"},
                  {"OLD": old}, halt=None, earnings={})
    assert "OLD" in [p["symbol"] for p in runner.ledger.positions()]


def test_replace_refused_for_unknown_holding(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner._apply({"action": "REPLACE", "symbol": "X", "replace": "NOPE",
                   "stop": 90, "target": 140, "thesis": "t"},
                  {}, halt=None, earnings={})
    assert runner.ledger.positions() == []


def test_replace_refused_during_halt(runner, monkeypatch):
    old = _open_old(runner, monkeypatch)
    _price(monkeypatch, 100.0)
    runner._apply({"action": "REPLACE", "symbol": "X", "replace": "OLD",
                   "stop": 90, "target": 140, "thesis": "t"},
                  {"OLD": old}, halt="DAILY HALT", earnings={})
    assert "OLD" in [p["symbol"] for p in runner.ledger.positions()]


def test_hold_is_a_noop(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner.ledger.buy("X", 10, stop=90, target=125, thesis="t", meta={})
    runner._apply({"action": "HOLD", "symbol": "X", "reason": "intact"},
                  {"X": runner.ledger.positions()[0]}, halt=None, earnings={})
    assert len(runner.ledger.positions()) == 1
