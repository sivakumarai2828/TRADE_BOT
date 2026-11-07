"""Desk gates (branch v3): Skeptic and Telegram per-trade approval."""
from __future__ import annotations

import json
import time
import types

import pytest

from botv2.approval import ApprovalDesk
from botv2.executor import MarketRunner
from botv2.skeptic import Skeptic, parse_verdict

BUY = {"action": "BUY", "symbol": "X", "setup": "PULLBACK", "stop": 95,
       "target": 115, "thesis": "test 3R"}


class FakeBackend:
    """Stands in for PortfolioManagerAI._call."""

    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, 0

    def _call(self, system, user, max_tokens):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.reply


def ruling(r):
    return json.dumps({"reasons": ["a", "b", "c"], "ruling": r, "summary": f"says {r}"})


class FakeHttp:
    """Records Telegram calls; getUpdates returns queued updates once."""

    def __init__(self):
        self.posts, self.updates, self._next_id = [], [], 100

    def post(self, url, json=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], json))
        self._next_id += 1
        return types.SimpleNamespace(json=lambda: {"result": {"message_id": self._next_id}})

    def get(self, url, params=None, timeout=None):
        ups, self.updates = self.updates, []
        return types.SimpleNamespace(json=lambda: {"result": ups})

    def tap(self, action, req_id, chat="42", update_id=1):
        self.updates.append({"update_id": update_id, "callback_query": {
            "id": "cb", "data": f"{action}:{req_id}",
            "message": {"chat": {"id": int(chat)}}}})


@pytest.fixture
def price(monkeypatch):
    from botv2 import data
    state = {"px": 100.0}
    monkeypatch.setattr(data, "fetch_price", lambda s: state["px"])
    monkeypatch.setattr(data, "fetch_snapshot", lambda s, **kw: {"days_to_earnings": 30})
    return state


@pytest.fixture
def make_runner(caps, journal, ledger, monkeypatch):
    from botv2 import notify
    sent = []
    monkeypatch.setattr(notify, "_send", lambda msg: sent.append(msg))

    def _make(skeptic_reply=None, skeptic_exc=None, approvals=False):
        cfg = types.SimpleNamespace(caps=caps, target_deployment_pct=0.0)
        ai = types.SimpleNamespace(decide=lambda p: {}, self_review=lambda *a: "")
        sk = None
        if skeptic_reply or skeptic_exc:
            sk = Skeptic(FakeBackend(skeptic_reply, skeptic_exc))
        desk = None
        if approvals:
            desk = ApprovalDesk(journal, "TOKEN", "42", timeout_min=90,
                                max_drift_pct=0.015, http=FakeHttp())
        r = MarketRunner("US", "USD", cfg, journal, ai, ledger, alpaca=None,
                         skeptic=sk, approvals=desk)
        r._regime_label = "BULL"
        r.sent = sent
        if desk:
            desk.runners = {"US": r}
        return r
    return _make


# ── Skeptic verdict parsing ──────────────────────────────────────
def test_parse_valid_ruling():
    v = parse_verdict({"reasons": ["x"], "ruling": "buy", "summary": "ok"})
    assert v.ruling == "BUY" and v.approved


@pytest.mark.parametrize("bad", [{"ruling": "MAYBE"}, {}, ["BUY"], None])
def test_parse_invalid_fails_closed(bad):
    v = parse_verdict(bad)
    assert v.ruling == "SKIP" and not v.approved


def test_skeptic_api_error_fails_closed():
    v = Skeptic(FakeBackend(exc=RuntimeError("503"))).review(
        "US", "USD", {"symbol": "X", "price": 100, "stop": 95, "target": 115}, None, "BULL")
    assert v.ruling == "SKIP" and "503" in v.error


def test_skeptic_garbage_reply_fails_closed():
    v = Skeptic(FakeBackend("I think it's fine")).review(
        "US", "USD", {"symbol": "X", "price": 100, "stop": 95, "target": 115}, None, "BULL")
    assert v.ruling == "SKIP"


# ── Skeptic as a gate ────────────────────────────────────────────
def test_no_gates_behaves_like_v2(make_runner, price):
    r = make_runner()
    r._apply(dict(BUY), {}, halt=None, earnings={})
    assert len(r.ledger.positions()) == 1


def test_skeptic_buy_lets_entry_through(make_runner, price):
    r = make_runner(skeptic_reply=ruling("BUY"))
    r._apply(dict(BUY), {}, halt=None, earnings={})
    assert len(r.ledger.positions()) == 1


@pytest.mark.parametrize("verdict", ["WAIT", "SKIP"])
def test_skeptic_wait_or_skip_blocks(make_runner, price, verdict):
    r = make_runner(skeptic_reply=ruling(verdict))
    r._apply(dict(BUY), {}, halt=None, earnings={})
    assert r.ledger.positions() == []
    assert any("Skeptic " + verdict in m for m in r.sent)


def test_skeptic_not_called_when_risk_rejects(make_runner, price):
    """Code caps run first, so a doomed order never costs an AI call."""
    r = make_runner(skeptic_reply=ruling("BUY"))
    r._apply(dict(BUY, target=101), {}, halt=None, earnings={})   # 0.2R
    assert r.skeptic.backend.calls == 0


def test_skeptic_rulings_are_logged(make_runner, price, journal):
    r = make_runner(skeptic_reply=ruling("SKIP"))
    r._apply(dict(BUY), {}, halt=None, earnings={})
    log_ = json.loads(journal.kv_get("US_skeptic_log"))
    assert log_[-1]["symbol"] == "X" and log_[-1]["ruling"] == "SKIP"


def test_skeptic_block_on_watch_cancels_watch(make_runner, price, journal):
    r = make_runner(skeptic_reply=ruling("WAIT"))
    journal.add_watch("US", "X", "PULLBACK", 100.0, 95.0, 115.0, "dip buy")
    r.check_watch_triggers()
    assert r.ledger.positions() == []
    assert journal.active_watches("US") == []


# ── Approval: submit ─────────────────────────────────────────────
def test_approval_holds_entry_until_tapped(make_runner, price):
    r = make_runner(approvals=True)
    by_symbol = {}
    r._apply(dict(BUY), by_symbol, halt=None, earnings={})
    assert r.ledger.positions() == []
    assert len(r.approvals.pending()) == 1
    assert by_symbol["X"]["pending"]                 # counts toward caps this cycle
    method, payload = r.approvals.http.posts[-1]
    assert method == "sendMessage" and "reply_markup" in payload


def test_no_duplicate_pending_for_same_symbol(make_runner, price):
    r = make_runner(approvals=True)
    r._apply(dict(BUY), {}, halt=None, earnings={})
    r._apply(dict(BUY), {}, halt=None, earnings={})
    assert len(r.approvals.pending()) == 1


def test_skeptic_runs_before_approval(make_runner, price):
    r = make_runner(skeptic_reply=ruling("SKIP"), approvals=True)
    r._apply(dict(BUY), {}, halt=None, earnings={})
    assert r.approvals.pending() == []


# ── Approval: decisions via Telegram ─────────────────────────────
def _submit(r):
    r._apply(dict(BUY), {}, halt=None, earnings={})
    return r.approvals.pending()[0]["id"]


def test_approve_tap_fills(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    r.approvals.http.tap("ap", rid)
    r.approvals.poll()
    assert len(r.ledger.positions()) == 1
    assert r.approvals.pending() == []


def test_skip_tap_does_not_fill(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    r.approvals.http.tap("rj", rid)
    r.approvals.poll()
    assert r.ledger.positions() == []
    assert r.approvals.pending() == []


def test_tap_from_other_chat_ignored(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    r.approvals.http.tap("ap", rid, chat="999")
    r.approvals.poll()
    assert r.ledger.positions() == []
    assert len(r.approvals.pending()) == 1


def test_update_offset_advances(make_runner, price, journal):
    r = make_runner(approvals=True)
    rid = _submit(r)
    r.approvals.http.tap("rj", rid, update_id=77)
    r.approvals.poll()
    assert journal.kv_get("telegram_update_offset") == "78"


def test_text_command_approve(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    r.approvals.http.updates.append({"update_id": 5, "message": {
        "chat": {"id": 42}, "text": f"/approve {rid}"}})
    r.approvals.poll()
    assert len(r.ledger.positions()) == 1


# ── Approval: revalidation at fill time ──────────────────────────
def test_price_ran_away_cancels(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    price["px"] = 102.0                               # +2% > 1.5% drift limit
    msg = r.approvals.decide(rid, approve=True)
    assert r.ledger.positions() == [] and "ran" in msg


def test_price_below_stop_cancels(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    price["px"] = 94.0
    r.approvals.decide(rid, approve=True)
    assert r.ledger.positions() == []


def test_small_dip_still_fills(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    price["px"] = 99.0
    r.approvals.decide(rid, approve=True)
    assert len(r.ledger.positions()) == 1


def test_halt_at_approval_cancels(make_runner, price, monkeypatch):
    r = make_runner(approvals=True)
    rid = _submit(r)
    monkeypatch.setattr(r.risk, "check_halts", lambda e, d: "DAILY LOSS HALT")
    r.approvals.decide(rid, approve=True)
    assert r.ledger.positions() == []


def test_expired_request_cannot_fill(make_runner, price, journal):
    r = make_runner(approvals=True)
    rid = _submit(r)
    reqs = json.loads(journal.kv_get("approval_requests"))
    reqs[0]["created"] = time.time() - 91 * 60
    journal.kv_set("approval_requests", json.dumps(reqs))
    msg = r.approvals.decide(rid, approve=True)
    assert r.ledger.positions() == [] and "expired" in msg


def test_poll_expires_stale(make_runner, price, journal):
    r = make_runner(approvals=True)
    _submit(r)
    reqs = json.loads(journal.kv_get("approval_requests"))
    reqs[0]["created"] = time.time() - 91 * 60
    journal.kv_set("approval_requests", json.dumps(reqs))
    r.approvals.poll()
    assert r.approvals.pending() == []


def test_double_tap_fills_once(make_runner, price):
    r = make_runner(approvals=True)
    rid = _submit(r)
    r.approvals.decide(rid, approve=True)
    msg = r.approvals.decide(rid, approve=True)
    assert len(r.ledger.positions()) == 1 and "already" in msg


# ── Approval with WATCH and REPLACE ──────────────────────────────
def test_watch_trigger_goes_to_approval_then_resolves(make_runner, price, journal):
    r = make_runner(approvals=True)
    journal.add_watch("US", "X", "PULLBACK", 100.0, 95.0, 115.0, "dip buy")
    r.check_watch_triggers()
    assert r.ledger.positions() == []
    rid = r.approvals.pending()[0]["id"]
    r.check_watch_triggers()                          # next 20-min tick: no re-ping
    assert len(r.approvals.pending()) == 1
    r.approvals.decide(rid, approve=True)
    assert len(r.ledger.positions()) == 1
    assert journal.active_watches("US") == []


def test_skipped_watch_is_cancelled(make_runner, price, journal):
    r = make_runner(approvals=True)
    journal.add_watch("US", "X", "PULLBACK", 100.0, 95.0, 115.0, "dip buy")
    r.check_watch_triggers()
    rid = r.approvals.pending()[0]["id"]
    r.approvals.decide(rid, approve=False)
    assert journal.active_watches("US") == []


def test_replace_keeps_old_until_approved(make_runner, price, journal):
    r = make_runner(approvals=True)
    r.ledger.buy("OLD", 10, 90.0, 110.0, "old thesis")
    old = r.ledger.positions()[0]
    journal_ts = time.time() - 10 * 86400
    old["ts_open"] = journal_ts
    old["planned_r"] = 1.0
    act = {"action": "REPLACE", "symbol": "X", "replace": "OLD",
           "stop": 95, "target": 115, "thesis": "better"}
    r._apply(act, {"OLD": old}, halt=None, earnings={})
    assert [p["symbol"] for p in r.ledger.positions()] == ["OLD"]
    rid = r.approvals.pending()[0]["id"]
    assert r.approvals.decide(rid, approve=True).startswith("Filled")
    assert [p["symbol"] for p in r.ledger.positions()] == ["X"]
