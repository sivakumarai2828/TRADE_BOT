"""Dashboard control plane: flags, command queue, API auth."""
from __future__ import annotations

import json
import types

import pytest

from botv2 import control
from botv2.executor import MarketRunner


@pytest.fixture
def runner(caps, journal, ledger, monkeypatch):
    from botv2 import data, notify
    monkeypatch.setattr(notify, "_send", lambda m: None)
    monkeypatch.setattr(data, "fetch_price", lambda s: 100.0)
    cfg = types.SimpleNamespace(caps=caps, target_deployment_pct=0.0,
                                skeptic_enabled=True, approval_mode="telegram")
    ai = types.SimpleNamespace(decide=lambda p: {"actions": [], "market_view": ""},
                               self_review=lambda *a: "")
    r = MarketRunner("US", "USD", cfg, journal, ai, ledger)
    r.cfg = cfg
    return r


def test_enqueue_validates():
    j = types.SimpleNamespace(kv_get=lambda k, d=None: None, kv_set=lambda k, v: None)
    with pytest.raises(ValueError):
        control.enqueue(j, "rm -rf", "US")
    with pytest.raises(ValueError):
        control.enqueue(j, "close", "US")            # no symbol
    with pytest.raises(ValueError):
        control.enqueue(j, "flatten", "MARS")


def test_close_command(runner, journal):
    runner.ledger.buy("X", 5, 95.0, 110.0, "t")
    control.enqueue(journal, "close", "US", "X")
    control.drain(journal, {"US": runner})
    assert runner.ledger.positions() == []
    assert control.results(journal)[-1]["ok"]
    assert json.loads(journal.kv_get("ctl_queue")) == []


def test_close_unknown_symbol_reports_failure(runner, journal):
    control.enqueue(journal, "close", "US", "NOPE")
    control.drain(journal, {"US": runner})
    assert not control.results(journal)[-1]["ok"]


def test_flatten_command(runner, journal):
    runner.ledger.buy("X", 5, 95.0, 110.0, "t")
    runner.ledger.buy("Y", 5, 95.0, 110.0, "t")
    control.enqueue(journal, "flatten", "US")
    control.drain(journal, {"US": runner})
    assert runner.ledger.positions() == []


def test_run_cycle_refused_while_paused(runner, journal):
    control.set_paused(journal, "US", True)
    control.enqueue(journal, "run_cycle", "US")
    control.drain(journal, {"US": runner})
    res = control.results(journal)[-1]
    assert not res["ok"] and "paused" in res["note"]


def test_pause_stops_watch_triggers(runner, journal):
    journal.add_watch("US", "X", "PULLBACK", 100.0, 95.0, 115.0, "dip")
    control.set_paused(journal, "US", True)
    control.apply_flags(journal, {"US": runner}, None, None, runner.cfg)
    runner.check_watch_triggers()
    assert runner.ledger.positions() == []
    assert len(journal.active_watches("US")) == 1


def test_pause_does_not_stop_exits(runner, journal, monkeypatch):
    from botv2 import data
    runner.ledger.buy("X", 5, 95.0, 110.0, "t")
    control.set_paused(journal, "US", True)
    control.apply_flags(journal, {"US": runner}, None, None, runner.cfg)
    monkeypatch.setattr(data, "fetch_price", lambda s: 94.0)
    runner.monitor_positions()
    assert runner.ledger.positions() == []


def test_gate_flags_override_config(runner, journal):
    sk, desk = object(), object()
    control.apply_flags(journal, {"US": runner}, sk, desk, runner.cfg)
    assert runner.skeptic is sk and runner.approvals is desk
    control.set_gate(journal, "skeptic", False)
    control.set_gate(journal, "approval", False)
    control.apply_flags(journal, {"US": runner}, sk, desk, runner.cfg)
    assert runner.skeptic is None and runner.approvals is None


def test_approve_without_desk_fails_cleanly(runner, journal):
    control.enqueue(journal, "approve", arg="abc123")
    control.drain(journal, {"US": runner}, desk=None)
    assert not control.results(journal)[-1]["ok"]


# ── API auth ─────────────────────────────────────────────────────
@pytest.fixture
def client(journal, monkeypatch):
    import api_v2
    monkeypatch.setattr(api_v2, "JOURNAL", journal)
    return api_v2.app.test_client()


def test_controls_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    r = client.post("/api/control/flag", json={"flag": "paused", "market": "US", "value": True},
                    headers={"X-Dashboard-Token": ""})
    assert r.status_code == 403


def test_wrong_token_rejected(client, monkeypatch, journal):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    r = client.post("/api/control/flag", json={"flag": "paused", "market": "US", "value": True},
                    headers={"X-Dashboard-Token": "nope"})
    assert r.status_code == 401 and not control.is_paused(journal, "US")


def test_right_token_pauses_and_queues(client, monkeypatch, journal):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    h = {"X-Dashboard-Token": "s3cret"}
    assert client.post("/api/control/flag", json={"flag": "paused", "market": "US", "value": True},
                       headers=h).status_code == 200
    assert control.is_paused(journal, "US")
    r = client.post("/api/control/cmd", json={"cmd": "close", "market": "US", "arg": "X"}, headers=h)
    assert r.status_code == 200
    assert json.loads(journal.kv_get("ctl_queue"))[0]["arg"] == "X"


def test_bad_command_rejected(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    r = client.post("/api/control/cmd", json={"cmd": "shell"}, headers={"X-Dashboard-Token": "s3cret"})
    assert r.status_code == 400


def test_state_endpoint_reads(client):
    r = client.get("/api/control")
    assert r.status_code == 200 and "markets" in r.get_json()


def test_reset_kill_switch(runner, journal):
    journal.kv_set("US_killed", "1")
    journal.kv_set("US_equity_peak", "6000")       # equity now 5000: 16% under
    assert runner.risk.check_halts(runner.ledger.equity(), 0) is not None
    control.enqueue(journal, "reset_kill", "US")
    control.drain(journal, {"US": runner})
    assert control.results(journal)[-1]["ok"]
    assert journal.kv_get("US_killed") == "0"
    assert runner.risk.check_halts(runner.ledger.equity(), 0) is None   # does not re-trip
