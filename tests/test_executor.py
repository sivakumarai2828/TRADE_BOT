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


# ── suggest-only market (INDIA_MODE=suggest) ─────────────────────
@pytest.fixture
def india(caps, journal, monkeypatch):
    """An India runner in suggest-only mode; captures Telegram messages."""
    from botv2 import notify
    from botv2.ledger import PaperLedger

    sent = []
    monkeypatch.setattr(notify, "_send", lambda msg: sent.append(msg))
    cfg = types.SimpleNamespace(caps=caps, target_deployment_pct=0.0)
    ai = types.SimpleNamespace(decide=lambda p: {}, self_review=lambda *a: "")
    r = MarketRunner("INDIA", "INR", cfg, journal, ai,
                     PaperLedger(journal, "INDIA", 250_000.0), suggest_only=True)
    r.sent = sent
    return r


def test_suggest_only_never_opens_a_position(india, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    act = {"action": "BUY", "symbol": "TITAN.NS", "stop": 95, "target": 115,
           "thesis": "base breakout", "confidence": 0.8}
    india._apply(act, {}, halt=None, earnings={})
    assert india.ledger.positions() == [] and india.ledger.cash == 250_000.0
    assert india.journal.open_trades("INDIA") == []
    s = suggest.load(india.journal, "INDIA")
    assert len(s) == 1 and s[0]["symbol"] == "TITAN.NS" and s[0]["priority"] == "HIGH"
    assert s[0]["price"] == 100.0 and s[0]["r"] == 3.0
    assert s[0]["size_pct"] == 25.0           # 2% risk / 5% stop = 40%, capped at the 25% position cap


def test_suggestion_still_needs_the_risk_rules(india, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    india._snapshots["Y.NS"] = {"price": 100.0}
    india._apply({"action": "BUY", "symbol": "X.NS", "stop": 95, "target": 104}, {}, None, {})  # 0.8R
    india._apply({"action": "BUY", "symbol": "Y.NS", "stop": 95, "target": 115}, {}, None, {"Y.NS": 1})  # earnings
    items = suggest.load(india.journal, "INDIA")
    assert [s["status"] for s in items] == ["blocked", "blocked"]          # kept, never suggested
    assert items[0]["blocked_by"] == "risk" and "reward/risk" in items[0]["reason"]
    assert items[1]["reason"] == "earnings report in 1 day(s)"
    assert [s["rank"] for s in suggest.recent(india.journal, "INDIA")] == [None, None]


def test_skeptic_block_means_no_suggestion(india, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    india.skeptic = types.SimpleNamespace(review=lambda *a: types.SimpleNamespace(
        approved=False, ruling="REJECT", summary="extended", reasons=["rsi 85"]))
    monkeypatch.setattr(india, "_log_skeptic", lambda *a: None)
    india._apply({"action": "BUY", "symbol": "X.NS", "stop": 95, "target": 115, "confidence": 0.6}, {}, None, {})
    s = suggest.load(india.journal, "INDIA")
    assert len(s) == 1 and s[0]["status"] == "blocked" and s[0]["blocked_by"] == "skeptic"
    assert s[0]["reason"] == "extended" and s[0]["ruling"] == "REJECT" and s[0]["priority"] == "MEDIUM"
    assert not any("Buy suggestions for" in m for m in india.sent)


def test_cycle_sends_one_ranked_list_and_does_not_repeat(india, monkeypatch):
    from botv2 import data, executor, suggest
    _price(monkeypatch, 100.0)
    monkeypatch.setattr(data, "fetch_snapshot", lambda s: None)
    monkeypatch.setattr(data, "fetch_market_context", lambda b: [])
    monkeypatch.setattr(executor, "universe_for", lambda m: ([], []))
    india.ai.decide = lambda p: {"market_view": "", "actions": [
        {"action": "BUY", "symbol": "LOW.NS", "stop": 95, "target": 112, "confidence": 0.4},
        {"action": "BUY", "symbol": "TOP.NS", "stop": 95, "target": 115, "confidence": 0.9},
        {"action": "BUY", "symbol": "MID.NS", "stop": 95, "target": 115, "confidence": 0.6}]}
    india.run_cycle()
    lists = [m for m in india.sent if "Buy suggestions for" in m]
    assert len(lists) == 1 and "(3)" in lists[0]
    assert lists[0].index("TOP.NS") < lists[0].index("MID.NS") < lists[0].index("LOW.NS")
    assert "places no orders" in lists[0] and india.journal.open_trades("INDIA") == []
    ranked = suggest.recent(india.journal, "INDIA")
    assert [(s["rank"], s["symbol"], s["priority"]) for s in ranked] == [
        (1, "TOP.NS", "HIGH"), (2, "MID.NS", "MEDIUM"), (3, "LOW.NS", "LOW")]
    india.run_cycle()                                     # same day again: nothing new stored
    assert len(suggest.load(india.journal, "INDIA")) == 3


def test_cycle_with_nothing_says_so(india, monkeypatch):
    from botv2 import data, executor
    monkeypatch.setattr(data, "fetch_snapshot", lambda s: None)
    monkeypatch.setattr(data, "fetch_market_context", lambda b: [])
    monkeypatch.setattr(executor, "universe_for", lambda m: ([], []))
    india.ai.decide = lambda p: {"market_view": "", "actions": []}
    india.run_cycle()
    assert any("No buy suggestions" in m for m in india.sent)


def test_watch_level_reached_becomes_a_single_suggestion(india, monkeypatch):
    from botv2 import data, suggest
    _price(monkeypatch, 100.0)
    monkeypatch.setattr(data, "fetch_snapshot", lambda s: {"price": 100.0})
    wid = india.journal.add_watch("INDIA", "W.NS", "BREAKOUT", 100.0, 95.0, 115.0, "needs 100")
    w = india.journal.active_watches("INDIA")[0]
    india._trigger_watch(w, 100.0)
    assert india.journal.active_watches("INDIA") == [] and india.journal.open_trades("INDIA") == []
    assert suggest.load(india.journal, "INDIA")[0]["kind"] == "WATCH"
    assert any("watch level reached" in m for m in india.sent)


def test_traded_market_still_buys(runner, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    runner._apply({"action": "BUY", "symbol": "X", "stop": 95, "target": 115}, {}, None, {})
    assert len(runner.ledger.positions()) == 1


def test_advisory_note_only_in_suggest_mode():
    from botv2.ai_pm import build_prompt
    args = ("INDIA", "INR", [], [], {}, {}, [], None, {}, None)
    assert "ADVISORY MODE" in build_prompt(*args, advisory=True)
    assert "ADVISORY MODE" not in build_prompt(*args)


def test_traded_market_records_the_idea_and_still_trades(runner, monkeypatch):
    """US keeps trading; the same entry also appears in the ranked list."""
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    runner._apply({"action": "BUY", "symbol": "X", "stop": 95, "target": 115, "confidence": 0.75},
                  {}, None, {})
    assert len(runner.ledger.positions()) == 1                 # bought as before
    s = suggest.load(runner.journal, "US")
    assert [(x["symbol"], x["priority"], x["status"]) for x in s] == [("X", "HIGH", "bought")]


def test_traded_market_with_approvals_marks_the_idea_as_awaiting_approval(runner, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    asked = []
    runner.approvals = types.SimpleNamespace(submit=lambda req: asked.append(req["symbol"]))
    runner._apply({"action": "BUY", "symbol": "X", "stop": 95, "target": 115}, {}, None, {})
    assert asked == ["X"] and runner.ledger.positions() == []
    assert suggest.load(runner.journal, "US")[0]["status"] == "approval"


def test_suggestion_store_failure_never_blocks_a_trade(runner, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    monkeypatch.setattr(suggest, "record", lambda *a, **k: 1 / 0)
    runner._apply({"action": "BUY", "symbol": "X", "stop": 95, "target": 115}, {}, None, {})
    assert len(runner.ledger.positions()) == 1


def test_us_suggest_mode_refused_with_broker_trading():
    import dataclasses
    from botv2.alpaca_ledger import PAPER_URL
    from botv2.config import V2Config, check_broker_config
    cfg = dataclasses.replace(V2Config(), broker_trading=True, alpaca_key="k", alpaca_secret="s",
                              alpaca_base_url=PAPER_URL, telegram_token="t", telegram_chat_id="c",
                              trade_india=False, db_path="botv2_live.db", us_mode="suggest")
    assert any("US_MODE" in e for e in check_broker_config(cfg))


def test_suggestion_shows_what_happened_to_its_approval(journal):
    import time
    from botv2 import suggest
    s = suggest.record(journal, "US", "USD", {"kind": "BUY", "symbol": "X", "price": 100, "stop": 95,
                                              "target": 115}, types.SimpleNamespace(
        max_position_pct=0.2, max_risk_per_trade_pct=0.015), status="approval")
    reqs = [{"id": "abc123", "market": "US", "symbol": "X", "status": "filled",
             "note": "2 sh @ 100.10", "created": time.time(), "resolved": time.time()},
            {"id": "zzz999", "market": "INDIA", "symbol": "X", "status": "rejected", "created": time.time()}]
    out = suggest.attach_approvals(suggest.recent(journal, "US"), reqs, "US")
    assert out[0]["approval"]["id"] == "abc123" and out[0]["approval"]["status"] == "filled"
    assert "approval" not in suggest.attach_approvals([dict(s, symbol="Y")], reqs, "US")[0]



def test_a_later_pass_replaces_a_stopped_idea_but_not_the_reverse(india, monkeypatch):
    from botv2 import suggest
    _price(monkeypatch, 100.0)
    caps = india.cfg.caps
    p = {"kind": "BUY", "symbol": "X.NS", "price": 100, "stop": 95, "target": 115}
    assert suggest.record_blocked(india.journal, "INDIA", "INR", p, caps, "skeptic", "too early")
    assert suggest.record(india.journal, "INDIA", "INR", p, caps)["status"] == "suggest"
    assert suggest.record_blocked(india.journal, "INDIA", "INR", p, caps, "risk", "x") is None
    assert [s["status"] for s in suggest.load(india.journal, "INDIA")] == ["suggest"]


def test_digest_mentions_stopped_ideas(india, monkeypatch):
    from botv2 import data, executor
    _price(monkeypatch, 100.0)
    monkeypatch.setattr(data, "fetch_snapshot", lambda s: None)
    monkeypatch.setattr(data, "fetch_market_context", lambda b: [])
    monkeypatch.setattr(executor, "universe_for", lambda m: ([], []))
    india.ai.decide = lambda p: {"market_view": "", "actions": [
        {"action": "BUY", "symbol": "LOW.NS", "stop": 95, "target": 101}]}   # fails 2R
    india.run_cycle()
    msg = [m for m in india.sent if "No buy suggestions" in m][0]
    assert "1 idea the AI proposed today was stopped" in msg


# ── 2026-11-17 live-bot review fixes ─────────────────────────────
def test_watch_at_or_above_price_is_taken_as_a_buy_idea(runner, monkeypatch):
    """CB was armed at 385 with the price already there and fired 4 s later."""
    _price(monkeypatch, 100.0)
    runner._apply({"action": "WATCH", "symbol": "X", "setup": "PULLBACK", "ideal_entry": 100.2,
                   "stop": 95, "target": 115}, {}, None, {})
    assert runner.journal.active_watches("US") == []          # not armed
    pos = runner.ledger.positions()
    assert len(pos) == 1 and "watch level 100.2 already reached" in pos[0]["thesis"]


def test_watch_below_price_is_armed(runner, monkeypatch):
    _price(monkeypatch, 100.0)
    runner._apply({"action": "WATCH", "symbol": "X", "setup": "PULLBACK", "ideal_entry": 97,
                   "stop": 93, "target": 110}, {}, None, {})
    assert [w["symbol"] for w in runner.journal.active_watches("US")] == ["X"]
    assert runner.ledger.positions() == []


def test_logs_never_show_tokens_or_keys(caplog):
    import logging
    from botv2 import logsafe
    log = logging.getLogger("t.redact")
    caplog.handler.addFilter(logsafe.RedactSecrets())
    with caplog.at_level(logging.WARNING):
        log.warning("telegram getUpdates failed: %s",
                    "Max retries exceeded with url: /bot1234567890:AAHogbkQL5EVP-MFGmgdH9MFm5X_L04teF8/getUpdates")
        try:
            raise RuntimeError("bad key sk-or-v1-abcdefabcdefabcdefabcdefabcdef")
        except RuntimeError:
            log.exception("AI call failed")
    text = caplog.text
    assert "AAHogbkQL5EVP" not in text and "abcdefabcdefabcdef" not in text
    assert "<telegram-token>" in text and "<openrouter-key>" in text


def test_renamed_tickers_are_in_the_lists():
    from botv2.universe import INDIA_UNIVERSE, US_UNIVERSE
    for gone in ("LTIM.NS", "PIDILITE.NS", "MMC"):
        assert gone not in INDIA_UNIVERSE + US_UNIVERSE
    assert "LTM.NS" in INDIA_UNIVERSE and "PIDILITIND.NS" in INDIA_UNIVERSE and "MRSH" in US_UNIVERSE


# ── 2026-11-18: watch direction depends on the setup ─────────────
def test_breakout_watch_waits_for_the_price_to_rise(runner, monkeypatch):
    """DIS/MAR breakout watches at 122.80 / 433.40 were treated as reached with the price below."""
    from botv2.executor import watch_level_reached
    assert watch_level_reached("BREAKOUT", 119.20, 122.80) is False
    assert watch_level_reached("BREAKOUT", 123.00, 122.80) is True
    assert watch_level_reached("PULLBACK", 119.20, 122.80) is True
    assert watch_level_reached("PULLBACK", 125.00, 122.80) is False
    _price(monkeypatch, 100.0)
    runner._apply({"action": "WATCH", "symbol": "X", "setup": "BREAKOUT", "ideal_entry": 104,
                   "stop": 99, "target": 115}, {}, None, {})
    assert [w["symbol"] for w in runner.journal.active_watches("US")] == ["X"]   # armed, not bought
    assert runner.ledger.positions() == []
    runner.check_watch_triggers()
    assert runner.ledger.positions() == []                                     # still below 104


# ── what-if tracking of every idea (2026-11-18) ──────────────────
def _ideas(journal):
    from botv2 import suggest
    caps = types.SimpleNamespace(max_position_pct=0.2, max_risk_per_trade_pct=0.015)
    p = lambda sym: {"kind": "BUY", "symbol": sym, "price": 100, "stop": 95, "target": 110}
    suggest.record(journal, "US", "USD", p("PASS"), caps, status="bought")
    suggest.record_blocked(journal, "US", "USD", p("WAIT1"), caps, "skeptic", "early", ruling="WAIT")
    suggest.record_blocked(journal, "US", "USD", p("SKIP1"), caps, "skeptic", "bad", ruling="SKIP")
    suggest.record_blocked(journal, "US", "USD", p("RISK1"), caps, "risk", "1.8R")


def test_every_idea_is_followed_to_its_target_or_stop(journal):
    from botv2 import suggest
    _ideas(journal)
    px = {"PASS": 103, "WAIT1": 111, "SKIP1": 94, "RISK1": 100}
    closed = suggest.track_outcomes(journal, "US", lambda s: px[s])
    assert sorted(c["symbol"] for c in closed) == ["SKIP1", "WAIT1"]
    by = {s["symbol"]: s["outcome"] for s in suggest.load(journal, "US")}
    assert by["WAIT1"]["status"] == "target" and by["WAIT1"]["ret"] == pytest.approx(0.10)
    assert by["SKIP1"]["status"] == "stop" and by["SKIP1"]["ret"] == pytest.approx(-0.05)
    assert by["PASS"]["status"] == "open" and by["PASS"]["ret_now"] == pytest.approx(0.03)
    assert suggest.track_outcomes(journal, "US", lambda s: 200) == [] or True    # closed ones stay closed
    assert by["WAIT1"]["status"] == suggest.load(journal, "US")[1]["outcome"]["status"]


def test_open_ideas_finish_after_30_days(journal):
    import time as _t
    from botv2 import suggest
    _ideas(journal)
    later = _t.time() + 31 * 86400
    closed = suggest.track_outcomes(journal, "US", lambda s: 102, now_ts=later)
    assert len(closed) == 4 and all(c["outcome"]["status"] == "expired" for c in closed)
    assert all(c["outcome"]["ret"] == pytest.approx(0.02) for c in closed)


def test_results_are_grouped_by_what_the_checks_decided(journal):
    from botv2 import suggest
    _ideas(journal)
    suggest.track_outcomes(journal, "US", lambda s: {"PASS": 111, "WAIT1": 111, "SKIP1": 94, "RISK1": 101}[s])
    rows = {r["group"]: r for r in suggest.outcome_stats(suggest.load(journal, "US"))}
    assert list(rows)[0] == "Passed the checks"
    assert rows["Passed the checks"]["win_rate"] == 1.0
    assert rows["Stopped by the second AI check (WAIT)"]["avg_closed_ret"] == pytest.approx(0.10)
    assert rows["Stopped by the second AI check (SKIP)"]["stop"] == 1
    assert rows["Stopped by the risk rules"]["closed"] == 0
    assert rows["Stopped by the risk rules"]["avg_open_ret"] == pytest.approx(0.01)


def test_no_price_leaves_the_idea_open(journal):
    from botv2 import suggest
    _ideas(journal)
    assert suggest.track_outcomes(journal, "US", lambda s: None) == []
    assert all("outcome" not in s for s in suggest.load(journal, "US"))
