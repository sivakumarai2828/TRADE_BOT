"""India option ideas: estimated premiums, NSE calendar and lots, paper tracking. No network."""
from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from botv2 import notify, suggest
from botv2.config import V2Config
from botv2.india_options import (IndiaOptionIdeas, last_tuesday, parse_lots, pick_expiry,
                                 strike_at_or_above, strike_step)
from botv2.options_bt import bs_call

IST = ZoneInfo("Asia/Kolkata")
LOTS_CSV = ("UNDERLYING ,SYMBOL ,NOV-26 ,DEC-26 \n"
            "NIFTY 50 ,NIFTY ,65 ,65 \n"
            "TITAN COMPANY LIMITED ,TITAN ,175 ,175 \n"
            "RELIANCE INDUSTRIES ,RELIANCE ,500 ,500 \n")


def test_nse_monthly_expiry_is_the_last_tuesday():
    assert last_tuesday(2026, 11) == date(2026, 11, 24)
    assert last_tuesday(2026, 12) == date(2026, 12, 29)
    assert last_tuesday(2027, 3) == date(2027, 3, 30)
    assert pick_expiry(date(2026, 11, 18)) == date(2026, 12, 29)      # Nov 24 is too close (6 days)


def test_strikes_and_lots():
    assert strike_step(3452) == 100 and strike_at_or_above(3452) == 3500
    assert strike_step(820) == 20 and strike_at_or_above(820) == 820
    assert strike_at_or_above(1201.2) == 1250
    assert parse_lots(LOTS_CSV) == {"NIFTY": 65, "TITAN": 175, "RELIANCE": 500}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "_send", lambda m: out.append(m))
    return out


def make(journal, day=datetime(2026, 11, 18, 10, 2, tzinfo=IST), px=3452.0):
    cfg = dataclasses.replace(V2Config(), india_options_enabled=True)
    closes = [3000 * math.exp(0.01 * math.sin(k)) for k in range(40)]      # ~16% annual vol
    state = {"px": px}
    ideas = IndiaOptionIdeas(journal, cfg, now=lambda: day, fetch_lots=lambda: LOTS_CSV,
                             history=lambda s: closes, price=lambda s: state["px"])
    return ideas, state


def passed(journal, symbol="TITAN.NS", status="suggest"):
    return suggest.record(journal, "INDIA", "INR", {"kind": "BUY", "symbol": symbol, "price": 3452,
                                                    "stop": 3310, "target": 3790, "confidence": 0.8,
                                                    "thesis": "breakout"}, V2Config().caps, status=status)


def test_each_passed_india_idea_gets_one_estimated_call(journal, sent, monkeypatch):
    monkeypatch.setattr(suggest, "today", lambda m: "2026-11-18")
    passed(journal, "TITAN.NS")
    passed(journal, "ICICIPRULI.NS")                                   # not in F&O: skipped
    suggest.record_blocked(journal, "INDIA", "INR", {"symbol": "RELIANCE.NS", "price": 1400, "stop": 1350,
                                                     "target": 1550}, V2Config().caps, "skeptic", "x")
    ideas, _ = make(journal)
    made = ideas.build()
    assert [i["root"] for i in made] == ["TITAN"]
    i = made[0]
    assert i["strike"] == 3500 and i["expiry"] == "2026-12-29" and i["lot"] == 175 and i["days"] == 41
    expect = bs_call(3452, 3500, 41 / 365, 0.065, i["vol"])
    assert i["est_premium"] == pytest.approx(round(expect, 2)) and i["est_cost_lot"] == round(expect * 175)
    assert i["sell_at"] == pytest.approx(round(expect * 2, 2))
    assert any("ESTIMATED" in m and "TITAN 3500 CE" in m for m in sent)
    assert ideas.build() == []                                          # once per day


def test_tracking_reaches_target_on_a_rally(journal, sent, monkeypatch):
    monkeypatch.setattr(suggest, "today", lambda m: "2026-11-18")
    passed(journal)
    ideas, state = make(journal)
    i = ideas.build()[0]
    state["px"] = 3460
    assert ideas.monitor() == [] and ideas.load()[0]["status"] == "open"
    state["px"] = 3700
    done = ideas.monitor()
    assert done[0]["status"] == "target" and done[0]["est_pnl_pct"] >= 100
    assert done[0]["est_pnl_lot"] == pytest.approx((done[0]["est_exit"] - i["est_premium"]) * 175, abs=1)


def test_tracking_closes_before_expiry(journal, sent, monkeypatch):
    monkeypatch.setattr(suggest, "today", lambda m: "2026-11-18")
    passed(journal)
    ideas, state = make(journal)
    ideas.build()
    late, _ = make(journal, day=datetime(2026, 12, 22, 11, 0, tzinfo=IST))
    late._price = lambda s: 3400.0
    done = late.monitor()
    assert done[0]["status"] == "expiry_near" and done[0]["est_pnl_pct"] < 0


def test_lot_sizes_fall_back_to_the_cached_copy(journal, sent):
    ideas, _ = make(journal)
    assert ideas.lots()["TITAN"] == 175
    broken = IndiaOptionIdeas(journal, ideas.cfg, now=lambda: datetime(2026, 11, 19, 10, 0, tzinfo=IST),
                              fetch_lots=lambda: 1 / 0)
    assert broken.lots()["TITAN"] == 175


def test_wired_only_for_a_suggest_only_india(journal):
    import main
    assert V2Config().india_options_enabled is False
