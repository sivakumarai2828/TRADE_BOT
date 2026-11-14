"""AlpacaLedger — broker-backed ledger (paper or live Alpaca account).

Every test runs against FakeAlpaca, an in-memory stand-in for the Alpaca
REST API: no network, no keys, no money. Each rule pinned here protects
real money once LIVE_REAL_MONEY is on.
"""
from __future__ import annotations

import dataclasses
import itertools
import types

import pytest

from botv2 import data, notify
from botv2.alpaca_ledger import LIVE_URL, PAPER_URL, AlpacaLedger, BrokerError
from botv2.config import V2Config, check_broker_config


class FakeAlpaca:
    is_live = False

    def __init__(self, cash=450.0, price=50.0):
        self.cash_ = cash
        self.bp = cash
        self.price = price
        self.is_open = True
        self.orders: dict[str, dict] = {}
        self.held: dict[str, float] = {}
        self.ids = (f"o{i}" for i in itertools.count(1))
        self.next_fill = "filled"      # filled | rejected | partial
        self.fail_stop = False

    # account
    def account(self):
        eq = self.cash_ + sum(q * self.price for q in self.held.values())
        return {"cash": str(self.cash_), "buying_power": str(self.bp), "equity": str(eq),
                "last_equity": str(eq), "status": "ACTIVE", "trading_blocked": False}

    def clock(self):
        return {"is_open": self.is_open}

    def position(self, s):
        q = self.held.get(s, 0)
        return {"symbol": s, "qty": str(q), "current_price": str(self.price)} if q else None

    def positions(self):
        return [self.position(s) | {"asset_class": "us_equity"} for s, q in self.held.items() if q]

    def order(self, oid):
        return dict(self.orders[oid]) if oid in self.orders else None

    def submit(self, **b):
        oid = next(self.ids)
        qty = float(b["qty"])
        o = {"id": oid, **b, "filled_qty": "0", "filled_avg_price": None}
        if b["type"] == "stop":
            if self.fail_stop:
                raise BrokerError("POST /v2/orders -> 403: stop refused")
            o["status"] = "new"
        elif self.next_fill == "rejected":
            o["status"] = "rejected"
        else:
            filled = qty if self.next_fill == "filled" else max(1.0, qty // 2)
            o.update(status="filled" if filled == qty else "canceled",
                     filled_qty=str(filled), filled_avg_price=str(self.price))
            sign = 1 if b["side"] == "buy" else -1
            self.held[b["symbol"]] = self.held.get(b["symbol"], 0) + sign * filled
            self.cash_ -= sign * filled * self.price
        self.orders[oid] = o
        return dict(o)

    def replace(self, oid, **b):
        self.orders[oid]["status"] = "replaced"
        new = dict(self.orders[oid], **b, id=next(self.ids), status="new")
        self.orders[new["id"]] = new
        return dict(new)

    def cancel(self, oid):
        if self.orders[oid]["status"] in ("new", "accepted"):
            self.orders[oid]["status"] = "canceled"

    # test helper: the resting stop triggers at the broker
    def trigger_stop(self, oid, px):
        o = self.orders[oid]
        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(px))
        self.held[o["symbol"]] -= float(o["qty"])
        self.cash_ += float(o["qty"]) * px


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda m: sent.append(m))
    return sent


@pytest.fixture
def broker(monkeypatch, alerts):
    fake = FakeAlpaca()
    monkeypatch.setattr(data, "fetch_price", lambda s: fake.price)
    return fake


@pytest.fixture
def led(journal, broker):
    return AlpacaLedger(journal, "US", broker, max_order_usd=150, sleep=lambda s: None)


def _stop_id(led, trade_id):
    return led.journal.kv_get(f"US_stoporder_{trade_id}")


# ── buys ─────────────────────────────────────────────────────────
def test_buy_records_real_fill_and_places_resting_stop(led, broker):
    broker.price = 49.37
    f = led.buy("AMD", 2, stop=46.0, target=56.0, thesis="t")
    assert f["qty"] == 2 and f["fill"] == 49.37
    t = led.journal.open_trades("US")[0]
    assert t["entry"] == 49.37 and t["qty"] == 2
    stop = broker.order(_stop_id(led, t["id"]))
    assert stop["type"] == "stop" and stop["time_in_force"] == "gtc"
    assert stop["stop_price"] == "46.0" and stop["qty"] == "2" and stop["side"] == "sell"


def test_rejected_order_records_nothing(led, broker):
    broker.next_fill = "rejected"
    assert led.buy("AMD", 2, 46, 56, "t") is None
    assert led.journal.open_trades("US") == []


def test_partial_fill_recorded_at_filled_size(led, broker):
    broker.next_fill = "partial"
    f = led.buy("AMD", 2, 46, 56, "t")
    assert f["qty"] == 1
    t = led.journal.open_trades("US")[0]
    assert t["qty"] == 1 and broker.order(_stop_id(led, t["id"]))["qty"] == "1"


def test_market_closed_places_no_order(led, broker):
    broker.is_open = False
    assert led.buy("AMD", 2, 46, 56, "t") is None
    assert broker.orders == {}


def test_max_order_usd_caps_size(led, broker):
    broker.price = 40.0
    f = led.buy("AMD", 10, 38, 46, "t")      # 10 x $40 = $400 > $150 cap
    assert f["qty"] == 3


def test_spends_only_settled_cash(led, broker):
    broker.bp = 60.0                          # rest of the cash is unsettled
    assert led.cash == 60.0
    assert led.buy("AMD", 2, 46, 56, "t")["qty"] == 1


def test_duplicate_symbol_refused(led, broker):
    led.buy("AMD", 1, 46, 56, "t")
    assert led.buy("AMD", 1, 46, 56, "t") is None


def test_stop_failure_alerts_and_reconcile_replaces_it(led, broker, alerts):
    broker.fail_stop = True
    led.buy("AMD", 2, 46, 56, "t")
    assert any("Could not place the broker stop" in a for a in alerts)
    broker.fail_stop = False
    led.reconcile()
    t = led.journal.open_trades("US")[0]
    assert broker.order(_stop_id(led, t["id"]))["status"] == "new"


# ── exits ────────────────────────────────────────────────────────
def test_sell_cancels_stop_then_sells(led, broker):
    led.buy("AMD", 2, 46, 56, "t")
    t = led.journal.open_trades("US")[0]
    sid = _stop_id(led, t["id"])
    broker.price = 55.0
    f = led.sell(t, "take_profit")
    assert f["fill"] == 55.0 and f["pnl"] == pytest.approx(10.0)
    assert broker.order(sid)["status"] == "canceled"
    assert broker.held["AMD"] == 0 and led.journal.open_trades("US") == []


def test_sell_after_broker_stop_filled_does_not_sell_twice(led, broker):
    led.buy("AMD", 2, 46, 56, "t")
    t = led.journal.open_trades("US")[0]
    broker.trigger_stop(_stop_id(led, t["id"]), 45.9)
    n_orders = len(broker.orders)
    f = led.sell(t, "ai_exit")
    assert f["fill"] == 45.9 and len(broker.orders) == n_orders   # no new sell order
    assert broker.held["AMD"] == 0                                # never went short


def test_sell_never_exceeds_broker_holding(led, broker, alerts):
    led.buy("AMD", 2, 46, 56, "t")
    t = led.journal.open_trades("US")[0]
    broker.orders[_stop_id(led, t["id"])]["status"] = "canceled"
    broker.held["AMD"] = 0                    # sold by hand in the Alpaca app
    assert led.sell(t, "ai_exit") is None
    assert any("Alpaca holds none" in a for a in alerts)


def test_reconcile_closes_trade_when_broker_stop_fills(led, broker):
    led.buy("AMD", 2, 46, 56, "t")
    t = led.journal.open_trades("US")[0]
    broker.trigger_stop(_stop_id(led, t["id"]), 45.8)
    closed = led.reconcile()
    assert closed[0]["fill"] == 45.8 and led.journal.open_trades("US") == []


def test_reconcile_alerts_once_on_unknown_broker_position(led, broker, alerts):
    broker.held["TSLA"] = 1
    led.reconcile()
    led.reconcile()
    assert sum("did not open" in a for a in alerts) == 1


def test_move_stop_replaces_broker_order(led, broker):
    led.buy("AMD", 2, 46, 56, "t")
    t = led.journal.open_trades("US")[0]
    old = _stop_id(led, t["id"])
    led.move_stop(t, 48.0)
    new = _stop_id(led, t["id"])
    assert new != old and broker.order(new)["stop_price"] == "48.0"
    assert broker.order(old)["status"] == "replaced"


# ── executor wiring ─────────────────────────────────────────────
def _runner(caps, journal, led):
    from botv2.executor import MarketRunner
    cfg = types.SimpleNamespace(caps=caps, target_deployment_pct=0.0)
    ai = types.SimpleNamespace(decide=lambda p: {"actions": []})
    return MarketRunner("US", "USD", cfg, journal, ai, led)


def test_monitor_records_broker_stop_fill(caps, journal, led, broker, alerts):
    r = _runner(caps, journal, led)
    led.buy("AMD", 2, 46, 56, "t")
    t = journal.open_trades("US")[0]
    broker.trigger_stop(_stop_id(led, t["id"]), 45.9)
    broker.price = 45.9
    r.monitor_positions()
    assert journal.open_trades("US") == []
    assert any("stop_loss (broker)" in a for a in alerts)


def test_adjust_moves_broker_stop(caps, journal, led, broker):
    r = _runner(caps, journal, led)
    led.buy("AMD", 2, 46, 56, "t")
    broker.price = 54.0
    pos = led.positions()[0]
    r._apply({"action": "ADJUST", "symbol": "AMD", "stop": 50.0}, {"AMD": pos}, None)
    t = journal.open_trades("US")[0]
    assert t["stop"] == 50.0
    assert broker.order(_stop_id(led, t["id"]))["stop_price"] == "50.0"


# ── startup guards ──────────────────────────────────────────────
def _cfg(**kw):
    base = V2Config(broker_trading=True, alpaca_key="k", alpaca_secret="s",
                    alpaca_base_url=PAPER_URL, approval_mode="telegram",
                    telegram_token="t", telegram_chat_id="c", trade_india=False,
                    db_path="botv2_live.db")
    return dataclasses.replace(base, **kw)


def test_valid_paper_broker_config_passes(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert check_broker_config(_cfg()) == []


def test_paper_mode_unaffected():
    assert check_broker_config(V2Config()) == []


@pytest.mark.parametrize("kw, needle", [
    ({"alpaca_base_url": LIVE_URL}, "LIVE_REAL_MONEY"),
    ({"live_real_money": True}, "not the live URL"),
    ({"approval_mode": "off"}, "APPROVAL_MODE"),
    ({"trade_india": True}, "TRADE_INDIA"),
    ({"db_path": "botv2.db"}, "BOTV2_DB"),
    ({"alpaca_key": ""}, "ALPACA_KEY"),
    ({"max_order_usd": 100000}, "MAX_ORDER_USD"),
    ({"alpaca_base_url": "https://evil.example"}, "ALPACA_BASE_URL"),
])
def test_unsafe_broker_config_refused(monkeypatch, kw, needle):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    errs = check_broker_config(_cfg(**kw))
    assert any(needle in e for e in errs), errs


def test_shared_supabase_refused(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    assert any("SUPABASE_URL" in e for e in check_broker_config(_cfg()))


def test_live_config_with_confirmation_passes(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert check_broker_config(_cfg(alpaca_base_url=LIVE_URL, live_real_money=True)) == []
