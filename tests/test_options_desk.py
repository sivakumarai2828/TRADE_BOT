"""Options desk (paper): contract choice, approval routing, fills and exits.

Every test uses FakeChains: no network, no broker. The desk never places a
broker order, so these tests also pin that money only moves in the paper
options book (OPT_cash) and the journal.
"""
from __future__ import annotations

import dataclasses
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from botv2 import notify, suggest
from botv2.approval import ApprovalDesk
from botv2.config import V2Config
from botv2.options_desk import MARKET, OptionsDesk, occ_parts

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 18)
EXP = "2026-12-18"           # 30 days out
EXP_FAR = "2027-01-15"


def occ(root, exp, strike):
    return f"{root}{exp[2:4]}{exp[5:7]}{exp[8:10]}C{int(strike * 1000):08d}"


class FakeChains:
    def __init__(self, spot=55.0):
        self.spot_px = spot
        self.rows = {EXP: [], EXP_FAR: []}

    def add(self, strike, bid, ask, oi=1000, exp=EXP, root="INTC"):
        self.rows[exp].append({"contractSymbol": occ(root, exp, strike), "strike": strike, "bid": bid,
                               "ask": ask, "lastPrice": ask, "volume": 10, "openInterest": oi})
        return self

    def expiries(self, symbol):
        return ["2026-11-20", EXP, EXP_FAR]

    def calls(self, symbol, expiry):
        return [dict(r) for r in self.rows.get(expiry, [])]

    def spot(self, symbol):
        return self.spot_px

    def set(self, contract, bid, ask):
        for rows in self.rows.values():
            for r in rows:
                if r["contractSymbol"] == contract:
                    r.update(bid=bid, ask=ask)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_send", lambda m: out.append(m))
    return out


@pytest.fixture
def cfg():
    return dataclasses.replace(V2Config(), options_enabled=True)


def make_desk(journal, cfg, chains, day=TODAY):
    d = OptionsDesk(journal, cfg, chains=chains, now=lambda: datetime(day.year, day.month, day.day, 10, 5, tzinfo=NY))
    ad = ApprovalDesk(journal, "", "", timeout_min=390, http=types.SimpleNamespace(
        post=lambda *a, **k: types.SimpleNamespace(json=lambda: {"result": {"message_id": 1}})))
    ad.options, d.approvals = d, ad
    return d, ad


def idea(journal, symbol="INTC", status="approval", conf=0.8, monkeypatch=None):
    caps = V2Config().caps
    return suggest.record(journal, "US", "USD", {"kind": "BUY", "symbol": symbol, "price": 55, "stop": 52,
                                                 "target": 62, "confidence": conf, "thesis": "breakout"},
                          caps, status=status)


# ── choosing a contract ──────────────────────────────────────────
def test_picks_the_nearest_affordable_strike_above_the_price(journal, cfg, sent):
    ch = FakeChains(55.0).add(50, 6.0, 6.2).add(57.5, 3.8, 4.0).add(62.5, 0.95, 1.0).add(65, 0.40, 0.44).add(67.5, 0.20, 0.22)
    d, _ = make_desk(journal, cfg, ch)
    q, why = d.pick_contract("INTC")
    assert q["strike"] == 65 and q["ask"] == 0.44 and q["expiry"] == EXP      # 57.5 and 62.5 cost > $50
    assert q["otm_pct"] == pytest.approx(18.2, abs=0.1) and why == ""


def test_refuses_far_out_and_penny_contracts(journal, cfg, sent):
    far = FakeChains(55.0).add(57.5, 3.8, 4.0).add(77.5, 0.30, 0.33)        # +41%: lottery ticket
    q, why = make_desk(journal, cfg, far)[0].pick_contract("INTC")
    assert q is None and "more than 25% above" in why
    penny = FakeChains(55.0).add(57.5, 3.8, 4.0).add(60, 0.03, 0.05)
    q, why = make_desk(journal, cfg, penny)[0].pick_contract("INTC")
    assert q is None and "under $0.10" in why


def test_skips_illiquid_and_wide_quotes(journal, cfg, sent):
    ch = (FakeChains(55.0).add(62.5, 0.40, 0.45, oi=20)        # too few open contracts
          .add(65, 0.10, 0.40)                                 # spread 120% of mid
          .add(67.5, 0.20, 0.22))
    d, _ = make_desk(journal, cfg, ch)
    assert d.pick_contract("INTC")[0]["strike"] == 67.5


def test_no_contract_explains_why(journal, cfg, sent):
    d, _ = make_desk(journal, cfg, FakeChains(55.0).add(57.5, 3.8, 4.0))
    q, why = d.pick_contract("INTC")
    assert q is None and why == "no strike fits the budget"


# ── proposals and approval ───────────────────────────────────────
def test_each_passed_idea_gets_one_option_proposal(journal, cfg, sent):
    idea(journal, "INTC")
    suggest.record_blocked(journal, "US", "USD", {"symbol": "AMD", "price": 100, "stop": 95, "target": 115},
                           cfg.caps, "skeptic", "too early")                 # stopped ideas get none
    d, ad = make_desk(journal, cfg, FakeChains(55.0).add(65, 0.30, 0.33))
    assert d.propose_from_today() == 1
    req = ad.pending()[0]
    assert req["kind"] == "OPTION" and req["symbol"] == "INTC" and req["contracts"] == 1
    assert req["cost"] == pytest.approx(33.0) and req["sell_at"] == pytest.approx(0.66)
    text = ad._request_text(req)
    assert "OPTION (PAPER)" in text and "can lose all of it" in text and "$33.00" in text
    assert d.propose_from_today() == 0                                       # no duplicate ping


def test_stock_and_option_requests_for_the_same_symbol_both_wait(journal, cfg, sent):
    idea(journal, "INTC")
    d, ad = make_desk(journal, cfg, FakeChains(55.0).add(65, 0.30, 0.33))
    ad.submit({"kind": "BUY", "market": "US", "currency": "USD", "symbol": "INTC", "price": 55,
               "stop": 52, "target": 62, "qty": 3})
    d.propose_from_today()
    assert sorted(r["kind"] for r in ad.pending()) == ["BUY", "OPTION"]


def test_approve_fills_on_paper_at_the_ask(journal, cfg, sent):
    idea(journal, "INTC")
    ch = FakeChains(55.0).add(65, 0.30, 0.33)
    d, ad = make_desk(journal, cfg, ch)
    d.propose_from_today()
    rid = ad.pending()[0]["id"]
    ch.set(occ("INTC", EXP, 65), 0.31, 0.35)
    msg = ad.decide(rid, approve=True, source="web")
    assert msg.startswith("Filled INTC") and "$0.35" in msg
    t = journal.open_trades(MARKET)[0]
    assert t["symbol"] == occ("INTC", EXP, 65) and t["qty"] == 100 and t["entry"] == 0.35
    assert t["target"] == pytest.approx(0.70) and d.cash == pytest.approx(500 - 35)


def test_approve_cancels_if_the_price_jumped_or_no_longer_fits(journal, cfg, sent):
    idea(journal, "INTC")
    ch = FakeChains(55.0).add(65, 0.30, 0.33)
    d, ad = make_desk(journal, cfg, ch)
    d.propose_from_today()
    rid = ad.pending()[0]["id"]
    ch.set(occ("INTC", EXP, 65), 0.50, 0.55)                               # +67%
    assert "Not filled" in ad.decide(rid, approve=True) and journal.open_trades(MARKET) == []
    assert d.cash == 500


def test_skip_leaves_everything_untouched(journal, cfg, sent):
    idea(journal, "INTC")
    d, ad = make_desk(journal, cfg, FakeChains(55.0).add(65, 0.30, 0.33))
    d.propose_from_today()
    assert ad.decide(ad.pending()[0]["id"], approve=False) == "Skipped INTC."
    assert journal.open_trades(MARKET) == [] and d.cash == 500


# ── exits ────────────────────────────────────────────────────────
def _hold(journal, cfg, ch, day=TODAY):
    idea(journal, "INTC")
    d, ad = make_desk(journal, cfg, ch, day)
    d.propose_from_today()
    ad.decide(ad.pending()[0]["id"], approve=True)
    return d


def test_sells_when_50_becomes_100(journal, cfg, sent):
    ch = FakeChains(55.0).add(65, 0.48, 0.50)
    d = _hold(journal, cfg, ch)
    assert d.cash == pytest.approx(450)
    ch.set(occ("INTC", EXP, 65), 0.95, 1.00)
    assert d.monitor() == []                                                 # bid 0.95 < 1.00 target
    ch.set(occ("INTC", EXP, 65), 1.02, 1.08)
    sold = d.monitor()
    assert sold[0]["reason"] == "take_profit" and sold[0]["pnl"] == pytest.approx(52.0)
    assert d.cash == pytest.approx(552) and journal.open_trades(MARKET) == []
    assert any("SOLD" in m for m in sent)


def test_sells_before_expiry_even_at_a_loss(journal, cfg, sent):
    ch = FakeChains(55.0).add(65, 0.48, 0.50)
    d = _hold(journal, cfg, ch)
    later = OptionsDesk(journal, cfg, chains=ch, now=lambda: datetime(2026, 12, 11, 10, 0, tzinfo=NY))  # 7 days left
    ch.set(occ("INTC", EXP, 65), 0.04, 0.06)
    sold = later.monitor()
    assert sold[0]["reason"] == "expiry_near" and sold[0]["pnl"] == pytest.approx(-46.0)


def test_no_bid_near_expiry_is_worth_nothing(journal, cfg, sent):
    ch = FakeChains(55.0).add(65, 0.48, 0.50)
    d = _hold(journal, cfg, ch)
    later = OptionsDesk(journal, cfg, chains=ch, now=lambda: datetime(2026, 12, 14, 10, 0, tzinfo=NY))
    ch.set(occ("INTC", EXP, 65), 0.0, 0.05)
    assert later.monitor()[0]["pnl"] == pytest.approx(-50.0)                 # most it can lose


def test_limits_on_open_positions(journal, cfg, sent):
    cfg = dataclasses.replace(cfg, opt_max_open=1)
    ch = FakeChains(55.0).add(65, 0.30, 0.33).add(77.5, 0.30, 0.33, root="AMD")
    for s in ("INTC", "AMD"):
        idea(journal, s)
    d, ad = make_desk(journal, cfg, ch)
    assert d.propose_from_today() == 1                                       # room for one


def test_summary_and_parts(journal, cfg, sent):
    d = _hold(journal, cfg, FakeChains(55.0).add(65, 0.30, 0.33))
    s = d.summary()
    assert s["mode"] == "paper" and s["positions"][0]["contracts"] == 1 and s["budget_usd"] == 50
    assert occ_parts("INTC261218C00077500") == ("INTC", date(2026, 12, 18), 77.5)  # parser only


def test_desk_is_off_unless_enabled_and_never_in_broker_mode(journal):
    import main
    base = V2Config()
    assert main.build_options(base, journal, None) is None
    assert main.build_options(dataclasses.replace(base, options_enabled=True, broker_trading=True),
                              journal, None) is None
    assert main.build_options(dataclasses.replace(base, options_enabled=True), journal, None) is not None
