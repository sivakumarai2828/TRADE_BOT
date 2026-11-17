"""Broker-backed ledger for US trading through Alpaca (paper OR live account).

Drop-in replacement for PaperLedger (same cash/equity/positions/day_pnl/buy/
sell/flatten_all interface), but the broker is the source of truth:

* cash / equity / day P&L come from the Alpaca account, never from our maths;
* a trade is written to the journal only after Alpaca reports a fill, at the
  real filled quantity and average price (rejected / unfilled orders leave no
  trace, partial fills are recorded at the partial size);
* every open position gets a GTC stop order that RESTS AT ALPACA, so the stop
  still works if the VM, the bot or the network is down;
* reconcile() (called by the executor every monitor pass) closes trades whose
  broker stop filled, re-places stops that are missing, and alerts on any
  mismatch between the journal and the broker.

Whole shares only: Alpaca accepts GTC stop orders for whole shares, so the
risk engine's integer sizing is kept. For a small account raise
CAP_MAX_POSITION_PCT (max 0.34) and lower CAP_MAX_OPEN_POSITIONS instead.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import data, notify

log = logging.getLogger("botv2.alpaca_ledger")

LIVE_URL = "https://api.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"

OPEN_STATES = {"new", "accepted", "pending_new", "accepted_for_bidding",
               "partially_filled", "pending_replace", "held", "calculated"}
DEAD_STATES = {"canceled", "expired", "rejected", "suspended", "stopped",
               "done_for_day", "replaced"}


class BrokerError(RuntimeError):
    pass


class AlpacaClient:
    """Minimal Alpaca trading REST client. `session` is injectable for tests."""

    def __init__(self, key: str, secret: str, base_url: str, session=None, timeout: float = 10):
        if not (key and secret):
            raise BrokerError("Alpaca key/secret missing")
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    @property
    def is_live(self) -> bool:
        return self.base == LIVE_URL

    def _call(self, method: str, path: str, ok404: bool = False, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def account(self) -> dict:
        return self._call("GET", "/v2/account")

    def clock(self) -> dict:
        return self._call("GET", "/v2/clock")

    def position(self, symbol: str) -> dict | None:
        return self._call("GET", f"/v2/positions/{symbol}", ok404=True)

    def positions(self) -> list[dict]:
        return self._call("GET", "/v2/positions") or []

    def order(self, order_id: str) -> dict | None:
        return self._call("GET", f"/v2/orders/{order_id}", ok404=True)

    def submit(self, **body) -> dict:
        return self._call("POST", "/v2/orders", json=body)

    def replace(self, order_id: str, **body) -> dict:
        return self._call("PATCH", f"/v2/orders/{order_id}", json=body)

    def cancel(self, order_id: str) -> None:
        # 422 = already filled/cancelled; the caller re-reads the order to find out which
        try:
            self._call("DELETE", f"/v2/orders/{order_id}", ok404=True)
        except BrokerError as exc:
            if "422" not in str(exc):
                raise


class AlpacaLedger:
    def __init__(self, journal, market: str, client: AlpacaClient,
                 max_order_usd: float, fill_timeout_s: float = 30, sleep=time.sleep):
        if market != "US":
            raise ValueError("AlpacaLedger trades US stocks only")
        self.journal = journal
        self.market = market
        self.client = client
        self.max_order_usd = float(max_order_usd)
        self.fill_timeout_s = fill_timeout_s
        self._sleep = sleep
        self._alerted: set[str] = set()   # one alert per mismatch, not one per minute

    # ── state (all from the broker) ──────────────────────────────
    @property
    def cash(self) -> float:
        """Spendable cash. buying_power on a cash account excludes unsettled
        sale proceeds, so the lower of the two never spends unsettled money."""
        a = self.client.account()
        return round(min(float(a["cash"]), float(a["buying_power"])), 2)

    def equity(self) -> float:
        return round(float(self.client.account()["equity"]), 2)

    def day_pnl(self) -> float:
        a = self.client.account()
        return round(float(a["equity"]) - float(a["last_equity"]), 2)

    def account_snapshot(self) -> dict:
        """Cash, equity and day P&L from ONE account call (dashboard API)."""
        a = self.client.account()
        return {"cash": round(min(float(a["cash"]), float(a["buying_power"])), 2),
                "equity": round(float(a["equity"]), 2),
                "day_pnl": round(float(a["equity"]) - float(a["last_equity"]), 2)}

    def positions(self) -> list[dict]:
        held = {p["symbol"]: p for p in self.client.positions()}
        out = []
        for t in self.journal.open_trades(self.market):
            bp = held.get(t["symbol"])
            px = float(bp["current_price"]) if bp and bp.get("current_price") else data.fetch_price(t["symbol"])
            stale = px is None
            px = t["entry"] if stale else px
            out.append({
                **t,
                "price_stale": stale,
                "last_price": round(px, 2),
                "market_value": round(px * t["qty"], 2),
                "unrealized_pnl": round((px - t["entry"]) * t["qty"], 2),
                "unrealized_pnl_pct": round((px / t["entry"] - 1) * 100, 2),
            })
        return out

    # ── orders ───────────────────────────────────────────────────
    def _wait(self, order: dict) -> dict:
        """Poll until the order leaves the open states or the timeout passes;
        on timeout cancel it and return its final state."""
        deadline = time.monotonic() + self.fill_timeout_s
        while order.get("status") in OPEN_STATES and time.monotonic() < deadline:
            self._sleep(1)
            order = self.client.order(order["id"]) or order
        if order.get("status") in OPEN_STATES:
            self.client.cancel(order["id"])
            for _ in range(10):
                order = self.client.order(order["id"]) or order
                if order.get("status") not in OPEN_STATES | {"pending_cancel"}:
                    break
                self._sleep(1)
        return order

    def _market(self, symbol: str, qty: int, side: str) -> dict:
        o = self.client.submit(symbol=symbol, qty=str(qty), side=side, type="market",
                               time_in_force="day",
                               client_order_id=f"botv2-{side}-{symbol}-{uuid.uuid4().hex[:10]}")
        return self._wait(o)

    def _stop_key(self, trade_id) -> str:
        return f"{self.market}_stoporder_{trade_id}"

    def _place_stop(self, trade_id, symbol: str, qty: float, stop: float) -> str | None:
        try:
            o = self.client.submit(symbol=symbol, qty=str(int(qty)), side="sell", type="stop",
                                   stop_price=str(round(stop, 2)), time_in_force="gtc",
                                   client_order_id=f"botv2-stop-{trade_id}-{uuid.uuid4().hex[:8]}")
        except BrokerError as exc:
            log.error("stop order for %s failed: %s", symbol, exc)
            notify.alert(f"🚨 [{self.market}] Could not place the broker stop for {symbol} "
                         f"({exc}). The bot keeps checking the stop itself and will retry.")
            return None
        self.journal.kv_set(self._stop_key(trade_id), o["id"])
        return o["id"]

    def buy(self, symbol: str, qty: float, stop: float, target: float,
            thesis: str, meta: dict | None = None) -> dict | None:
        if any(t["symbol"] == symbol for t in self.journal.open_trades(self.market)):
            log.warning("buy %s refused — position already open (duplicate guard)", symbol)
            return None
        if not self.client.clock().get("is_open"):
            log.warning("buy %s refused — market closed (no queued orders)", symbol)
            return None
        px = data.fetch_price(symbol)
        if not px:
            log.warning("no price for %s, buy skipped", symbol)
            return None
        qty = int(min(qty, self.max_order_usd / px, self.cash / px))
        if qty < 1:
            log.warning("buy %s refused — under 1 share within MAX_ORDER_USD/cash", symbol)
            return None

        o = self._market(symbol, qty, "buy")
        filled = int(float(o.get("filled_qty") or 0))
        if filled < 1:
            log.warning("buy %s not filled (status %s)", symbol, o.get("status"))
            return None
        fill_px = float(o["filled_avg_price"])
        trade_id = self.journal.open_trade(self.market, symbol, filled, fill_px,
                                           stop, target, thesis, meta)
        self._place_stop(trade_id, symbol, filled, stop)
        return {"trade_id": trade_id, "symbol": symbol, "qty": filled, "fill": fill_px}

    def _stop_fill(self, trade: dict) -> dict | None:
        """The broker stop order if it has filled, else None."""
        sid = self.journal.kv_get(self._stop_key(trade["id"]))
        o = self.client.order(sid) if sid else None
        return o if o and o.get("status") == "filled" else None

    def _close(self, trade: dict, px: float, reason: str) -> dict | None:
        if not self.journal.close_trade(trade["id"], px, reason):
            return None
        self.journal.kv_set(self._stop_key(trade["id"]), "")
        return {"symbol": trade["symbol"], "qty": trade["qty"], "fill": px,
                "pnl": round((px - trade["entry"]) * trade["qty"], 2)}

    def sell(self, trade: dict, reason: str) -> dict | None:
        # 1. take the resting stop down first so the shares can't be sold twice
        sid = self.journal.kv_get(self._stop_key(trade["id"]))
        if sid:
            self.client.cancel(sid)
            st = self._wait(self.client.order(sid) or {"id": sid, "status": "canceled"})
            if st.get("status") == "filled":     # the stop beat us to it
                return self._close(trade, float(st["filled_avg_price"]), "stop_loss")
        # 2. never sell more than the broker holds (long-only: no accidental short)
        bp = self.client.position(trade["symbol"])
        held = int(float(bp["qty"])) if bp else 0
        qty = min(int(trade["qty"]), held)
        if qty < 1:
            notify.alert(f"🚨 [{self.market}] {trade['symbol']}: journal shows an open position "
                         f"but Alpaca holds none. Check the account; not selling.")
            return None
        o = self._market(trade["symbol"], qty, "sell")
        if int(float(o.get("filled_qty") or 0)) < qty:
            notify.alert(f"🚨 [{self.market}] SELL {trade['symbol']} did not fully fill "
                         f"(status {o.get('status')}). Re-placing the stop; check the account.")
            self._place_stop(trade["id"], trade["symbol"], held - int(float(o.get("filled_qty") or 0)),
                             float(trade["stop"] or 0) or float(trade["entry"]) * 0.95)
            return None
        return self._close(trade, float(o["filled_avg_price"]), reason)

    def flatten_all(self, reason: str) -> list[dict]:
        return [r for t in self.journal.open_trades(self.market)
                if (r := self.sell(t, reason))]

    def move_stop(self, trade: dict, new_stop: float) -> None:
        sid = self.journal.kv_get(self._stop_key(trade["id"]))
        if sid:
            try:
                o = self.client.replace(sid, stop_price=str(round(new_stop, 2)))
                self.journal.kv_set(self._stop_key(trade["id"]), o["id"])  # replace -> new id
                return
            except BrokerError as exc:
                log.warning("stop replace %s failed (%s) — cancel and re-place", trade["symbol"], exc)
                self.client.cancel(sid)
                if self._stop_fill(trade):
                    return                      # it filled meanwhile; reconcile() records it
        self._place_stop(trade["id"], trade["symbol"], trade["qty"], new_stop)

    def reconcile(self) -> list[dict]:
        """Sync the journal with the broker. Returns trades closed by broker stops."""
        closed = []
        trades = self.journal.open_trades(self.market)
        for t in trades:
            sid = self.journal.kv_get(self._stop_key(t["id"]))
            o = self.client.order(sid) if sid else None
            status = (o or {}).get("status")
            if status == "filled":
                r = self._close(t, float(o["filled_avg_price"]), "stop_loss")
                if r:
                    closed.append(r)
            elif status in OPEN_STATES or status == "pending_cancel":
                continue
            elif self.client.position(t["symbol"]):
                log.warning("%s has no live stop (status %s) — re-placing", t["symbol"], status)
                self._place_stop(t["id"], t["symbol"], t["qty"], float(t["stop"] or 0)
                                 or float(t["entry"]) * 0.95)
            else:
                self._alert_once(f"missing-{t['id']}",
                                 f"🚨 [{self.market}] {t['symbol']}: open in the journal, but Alpaca "
                                 f"holds no shares and no stop filled. Check the account.")
        mine = {t["symbol"] for t in trades}
        for p in self.client.positions():
            if p.get("asset_class", "us_equity") == "us_equity" and p["symbol"] not in mine:
                self._alert_once(f"extra-{p['symbol']}",
                                 f"ℹ️ [{self.market}] Alpaca holds {p['qty']} {p['symbol']} that the "
                                 f"bot did not open. It will not be managed by the bot.")
        return closed

    def _alert_once(self, key: str, msg: str) -> None:
        day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if f"{day}:{key}" not in self._alerted:
            self._alerted.add(f"{day}:{key}")
            notify.alert(msg)

    def preflight(self) -> str:
        """Fail fast at startup: blocked account or bad keys stop the bot."""
        a = self.client.account()
        if a.get("trading_blocked") or a.get("account_blocked") or a.get("status") != "ACTIVE":
            raise BrokerError(f"Alpaca account not tradable (status {a.get('status')}, "
                              f"trading_blocked {a.get('trading_blocked')})")
        if self.journal.kv_get(f"{self.market}_start_cash") is None:   # "since start" baseline
            self.journal.kv_set(f"{self.market}_start_cash", str(round(float(a["equity"]), 2)))
        return (f"{'LIVE' if self.client.is_live else 'PAPER'} Alpaca account ACTIVE, "
                f"equity ${float(a['equity']):,.2f}, cash ${float(a['cash']):,.2f}")
