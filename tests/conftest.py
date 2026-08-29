"""Shared fixtures for the risk-layer test suite.

Journal opens a fresh connection per call, so an in-memory SQLite database
would be discarded between operations — every fixture uses a real file under
pytest's tmp_path instead.
"""
from __future__ import annotations

import pytest

from botv2.config import HardCaps
from botv2.journal import Journal
from botv2.ledger import PaperLedger
from botv2.risk import RiskEngine


@pytest.fixture
def journal(tmp_path):
    """Empty SQLite journal on a throwaway file."""
    return Journal(str(tmp_path / "test.db"))


@pytest.fixture
def caps():
    """The caps actually deployed on the VM, not the dataclass defaults."""
    return HardCaps(
        max_position_pct=0.25,
        max_risk_per_trade_pct=0.02,
        max_open_positions=8,
        max_new_entries_per_day=5,
        daily_loss_halt_pct=0.03,
        max_drawdown_kill_pct=0.10,
        min_stop_distance_pct=0.01,
        max_stop_distance_pct=0.15,
    )


@pytest.fixture
def risk(caps, journal):
    return RiskEngine(caps, journal, "US")


@pytest.fixture
def ledger(journal):
    return PaperLedger(journal, "US", 10_000.0)


@pytest.fixture
def fixed_price(monkeypatch):
    """Pin data.fetch_price so ledger tests never touch the network.

    Usage:  set_price(100.0)  ->  every fetch returns 100.0
            set_price(None)   ->  simulates a data outage
    """
    from botv2 import data

    state = {"px": 100.0}

    def _fake(symbol):
        return state["px"]

    monkeypatch.setattr(data, "fetch_price", _fake)

    def set_price(px):
        state["px"] = px

    return set_price
