"""Options desk (paper): a small fixed-dollar call for each US idea you'd buy.

Switched on with OPTIONS_ENABLED=true. PAPER ONLY: fills are virtual, at real
delayed quotes (Yahoo option chains): buys at the ask, sells at the bid. No
broker order is ever placed by this module.

Flow, every US trading day:
1. After the 10:00 ET stock cycle, every US idea that passed the risk rules
   and the Skeptic today gets one option proposal: a call on that stock,
   30-60 days out, the strike nearest the stock price whose whole contract
   fits OPT_BUDGET_USD (default $50) and passes the liquidity filters.
2. The proposal waits for your Approve / Skip (Trade Desk, control page or
   Telegram), like a stock buy. Nothing is bought without it.
3. On Approve the contract is re-quoted; it fills only if the ask still fits
   the budget and has not jumped more than OPT_MAX_DRIFT.
4. Every 20 minutes in the US session each position is marked at the bid.
   It is sold when the bid reaches OPT_TARGET_MULT x the price paid (2.0 =
   "$50 becomes $100"), or OPT_EXIT_DTE days before expiry, whichever comes
   first. There is no stop: the most a position can lose is what was paid.

Positions are journal trades with market "OPT": symbol = contract (OCC),
qty = contracts x 100, entry/exit = premium per share, so the journal's P&L
maths needs no special case.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import notify, suggest

log = logging.getLogger("botv2.options")

MARKET = "OPT"
NY = ZoneInfo("America/New_York")
K_CASH, K_START, K_MARKS = "OPT_cash", "OPT_start_cash", "OPT_marks"


# ── quotes ───────────────────────────────────────────────────────
class YahooChains:
    """Delayed option quotes from Yahoo. Replaceable in tests."""

    def expiries(self, symbol: str) -> list[str]:
        import yfinance as yf
        return list(yf.Ticker(symbol).options)

    def calls(self, symbol: str, expiry: str) -> list[dict]:
        import yfinance as yf
        df = yf.Ticker(symbol).option_chain(expiry).calls
        cols = ["contractSymbol", "strike", "bid", "ask", "lastPrice", "volume", "openInterest"]
        return [{k: (None if v != v else v) for k, v in r.items()}
                for r in df[cols].to_dict("records")]

    def spot(self, symbol: str) -> float | None:
        from . import data
        return data.fetch_price(symbol)


def occ_parts(contract: str) -> tuple[str, date, float]:
    """'INTC261218C00077500' -> ('INTC', 2026-12-18, 77.5)."""
    root = contract[:-15]
    ymd, strike = contract[-15:-9], int(contract[-8:]) / 1000
    return root, date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])), strike


class OptionsDesk:
    def __init__(self, journal, cfg, chains=None, now=None):
        self.journal = journal
        self.cfg = cfg
        self.chains = chains or YahooChains()
        self.now = now or (lambda: datetime.now(NY))
        self.approvals = None                 # ApprovalDesk, set by main
        # True = each proposal waits for Approve; False = the desk buys on its
        # own. main.py wires it to the same approval switch as stock buys.
        self.needs_approval = lambda: self.approvals is not None
        if journal.kv_get(K_CASH) is None:
            journal.kv_set(K_CASH, str(cfg.opt_start_cash))
            journal.kv_set(K_START, str(cfg.opt_start_cash))

    # ── book ─────────────────────────────────────────────────────
    @property
    def cash(self) -> float:
        return float(self.journal.kv_get(K_CASH, "0"))

    def _set_cash(self, v: float) -> None:
        self.journal.kv_set(K_CASH, str(round(v, 2)))

    def marks(self) -> dict:
        try:
            return json.loads(self.journal.kv_get(K_MARKS) or "{}")
        except ValueError:
            return {}

    def positions(self) -> list[dict]:
        marks, out = self.marks(), []
        for t in self.journal.open_trades(MARKET):
            m = marks.get(t["symbol"]) or {}
            bid = m.get("bid")
            px = bid if bid is not None else t["entry"]
            root, exp, strike = occ_parts(t["symbol"])
            out.append({**t, "underlying": root, "expiry": exp.isoformat(), "strike": strike,
                        "contracts": round(t["qty"] / 100), "last_price": px, "mark_ts": m.get("ts"),
                        "market_value": round(px * t["qty"], 2),
                        "unrealized_pnl": round((px - t["entry"]) * t["qty"], 2),
                        "unrealized_pnl_pct": round((px / t["entry"] - 1) * 100, 1) if t["entry"] else 0.0})
        return out

    def equity(self) -> float:
        return round(self.cash + sum(p["market_value"] for p in self.positions()), 2)

    # ── choosing a contract ──────────────────────────────────────
    def pick_contract(self, symbol: str) -> tuple[dict | None, str]:
        """(contract, reason). The strike nearest the stock price, at or above
        it and at most opt_max_otm_pct above it, whose whole contract fits the
        budget and passes the liquidity filters, on the first expiry 30-60
        days out that has one."""
        c = self.cfg
        spot = self.chains.spot(symbol)
        if not spot:
            return None, "no stock price"
        today = self.now().date()
        exps = [e for e in self.chains.expiries(symbol)
                if c.opt_dte_min <= (date.fromisoformat(e) - today).days <= c.opt_dte_max]
        if not exps:
            return None, f"no expiry {c.opt_dte_min}-{c.opt_dte_max} days out"
        why = "no strike fits the budget"
        for exp in exps:
            for q in sorted(self.chains.calls(symbol, exp), key=lambda q: q["strike"]):
                if q["strike"] < spot:
                    continue                                    # only at or above the price
                if q["strike"] > spot * (1 + c.opt_max_otm_pct):
                    why = (f"every affordable strike is more than {c.opt_max_otm_pct:.0%} above "
                           f"the stock price (a contract that far out almost always expires worthless)")
                    break
                bid, ask = q.get("bid") or 0, q.get("ask") or 0
                if ask <= 0 or bid <= 0:
                    why = "no live two-sided quote"
                    continue
                if ask * 100 > c.opt_budget_usd:
                    continue                                    # too expensive; try a higher strike
                if ask < c.opt_min_ask:
                    why = f"only contracts under ${c.opt_min_ask:.2f} fit"
                    continue
                if (q.get("openInterest") or 0) < c.opt_min_open_interest:
                    why = f"too few open contracts at affordable strikes (< {c.opt_min_open_interest})"
                    continue
                if (ask - bid) / ((ask + bid) / 2) > c.opt_max_spread_pct:
                    why = f"bid/ask spread wider than {c.opt_max_spread_pct:.0%}"
                    continue
                return {"contract": q["contractSymbol"], "underlying": symbol, "expiry": exp,
                        "strike": float(q["strike"]), "bid": float(bid), "ask": float(ask),
                        "spot": float(spot), "open_interest": int(q.get("openInterest") or 0),
                        "days": (date.fromisoformat(exp) - today).days,
                        "otm_pct": round(100 * (q["strike"] / spot - 1), 1)}, ""
        return None, why

    # ── daily proposals ──────────────────────────────────────────
    def propose_from_today(self) -> int:
        """One call per US idea that passed the checks today: sent for
        approval when the approval switch is on, bought at once when it is off."""
        ask_first = bool(self.needs_approval()) and self.approvals is not None
        day = suggest.today("US")
        ideas = [s for s in suggest.load(self.journal, "US")
                 if s["day"] == day and s.get("status") in ("approval", "bought", "suggest")]
        held = {occ_parts(t["symbol"])[0] for t in self.journal.open_trades(MARKET)}
        room = self.cfg.opt_max_open - len(held)
        sent = 0
        for s in suggest.ranked(ideas):
            if sent >= room:
                break
            if s["symbol"] in held:
                continue
            try:
                q, why = self.pick_contract(s["symbol"])
            except Exception as exc:
                log.warning("options: chain for %s failed: %s", s["symbol"], type(exc).__name__)
                continue
            if not q:
                log.info("options: no contract for %s — %s", s["symbol"], why)
                self._note_skip(s["symbol"], why)
                continue
            n = int(self.cfg.opt_budget_usd // (q["ask"] * 100))
            req = {"kind": "OPTION", "market": "US", "currency": "USD", "symbol": s["symbol"],
                   "contract": q["contract"], "expiry": q["expiry"], "strike": q["strike"],
                   "days": q["days"], "otm_pct": q["otm_pct"], "spot": q["spot"],
                   "price": q["ask"], "bid": q["bid"], "contracts": n,
                   "cost": round(q["ask"] * 100 * n, 2),
                   "sell_at": round(q["ask"] * self.cfg.opt_target_mult, 2),
                   "stop": None, "target": None, "qty": n,
                   "thesis": f"Call on {s['symbol']}: the stock idea passed the checks today "
                             f"({s.get('priority', '')} priority). {s.get('thesis', '')}"[:300]}
            if ask_first:
                if self.approvals.submit(req):
                    sent += 1
                continue
            ok, note = self.execute_approved(req)            # approval off: same checks, no wait
            log.info("options: auto %s %s — %s", req["contract"], "bought" if ok else "not bought", note)
            if ok:
                sent += 1
                held.add(s["symbol"])
            else:
                self._note_skip(s["symbol"], f"not bought: {note}")
        log.info("options: %d %s from %d idea(s)", sent, "proposal(s) sent" if ask_first else "bought", len(ideas))
        return sent

    def _note_skip(self, symbol: str, why: str) -> None:
        try:
            key = "OPT_skipped"
            items = json.loads(self.journal.kv_get(key) or "[]")
            items.append({"ts": time.time(), "symbol": symbol, "why": why})
            self.journal.kv_set(key, json.dumps(items[-30:]))
        except Exception:
            log.exception("options: could not record skip")

    # ── approval callbacks (ApprovalDesk routes OPTION requests here) ──
    def execute_approved(self, req: dict, max_drift_pct: float = 0.015) -> tuple[bool, str]:
        c = self.cfg
        if any(t["symbol"] == req["contract"] for t in self.journal.open_trades(MARKET)):
            return False, "already held"
        if len(self.journal.open_trades(MARKET)) >= c.opt_max_open:
            return False, f"already {c.opt_max_open} option positions open"
        q = self._quote(req["contract"])
        if not q or not q.get("ask"):
            return False, "no live quote for the contract right now"
        ask = float(q["ask"])
        if ask > float(req["price"]) * (1 + c.opt_max_drift):
            return False, (f"option price ran {100 * (ask / float(req['price']) - 1):.0f}% since the "
                           f"proposal ({req['price']:.2f} -> {ask:.2f})")
        n = int(c.opt_budget_usd // (ask * 100))
        if n < 1:
            return False, f"one contract now costs ${ask * 100:,.0f}, over the ${c.opt_budget_usd:,.0f} budget"
        cost = ask * 100 * n
        if cost > self.cash:
            return False, f"paper options cash ${self.cash:,.2f} is less than ${cost:,.2f}"
        target = round(ask * c.opt_target_mult, 2)
        self.journal.open_trade(MARKET, req["contract"], n * 100, ask, None, target,
                                str(req.get("thesis", ""))[:300], {"setup_type": "OPTION_CALL"})
        self._set_cash(self.cash - cost)
        notify.alert(f"🟢 <b>[OPTIONS · PAPER] BUY {n} × {req['contract']}</b>\n"
                     f"{req['symbol']} {req['strike']:g} call, expires {req['expiry']}\n"
                     f"Paid ${ask:.2f}/share = ${cost:,.2f} · sells at ${target:.2f} "
                     f"(${target * 100 * n:,.0f}) or {c.opt_exit_dte} days before expiry")
        return True, f"{n} contract(s) @ ${ask:.2f} = ${cost:,.2f} (paper)"

    def on_approval_rejected(self, req: dict, why: str) -> None:
        return None

    # ── monitoring (every 20 min in the US session) ─────────────
    def _quote(self, contract: str) -> dict | None:
        root, exp, _ = occ_parts(contract)
        for q in self.chains.calls(root, exp.isoformat()):
            if q["contractSymbol"] == contract:
                return q
        return None

    def monitor(self) -> list[dict]:
        sold, marks, today = [], self.marks(), self.now().date()
        for t in self.journal.open_trades(MARKET):
            try:
                q = self._quote(t["symbol"])
            except Exception as exc:
                log.warning("options: quote %s failed: %s", t["symbol"], type(exc).__name__)
                continue
            bid = float(q["bid"]) if q and q.get("bid") else None
            if bid is not None:
                marks[t["symbol"]] = {"bid": bid, "ask": q.get("ask"), "ts": time.time()}
            _, exp, _ = occ_parts(t["symbol"])
            days_left = (exp - today).days
            reason = None
            if bid is not None and t["target"] and bid >= float(t["target"]):
                reason = "take_profit"
            elif days_left <= self.cfg.opt_exit_dte:
                reason = "expiry_near"
            if not reason:
                continue
            px = bid if bid is not None else 0.0          # no bid near expiry: worth nothing
            if not self.journal.close_trade(t["id"], px, reason):
                continue
            proceeds = px * t["qty"]
            self._set_cash(self.cash + proceeds)
            pnl = proceeds - t["entry"] * t["qty"]
            marks.pop(t["symbol"], None)
            sold.append({"contract": t["symbol"], "fill": px, "pnl": round(pnl, 2), "reason": reason})
            notify.alert(f"{'🟢' if pnl > 0 else '🔴'} <b>[OPTIONS · PAPER] SOLD {t['symbol']}</b> "
                         f"@ ${px:.2f} ({'target reached' if reason == 'take_profit' else f'{days_left} days to expiry'})\n"
                         f"P&L ${pnl:+,.2f}")
        self.journal.kv_set(K_MARKS, json.dumps(marks))
        return sold

    def summary(self) -> dict:
        start = float(self.journal.kv_get(K_START, str(self.cfg.opt_start_cash)))
        pos = self.positions()
        eq = round(self.cash + sum(p["market_value"] for p in pos), 2)
        try:
            skipped = json.loads(self.journal.kv_get("OPT_skipped") or "[]")[-10:]
        except ValueError:
            skipped = []
        return {"currency": "USD", "mode": "paper", "cash": self.cash, "equity": eq,
                "total_return_pct": round((eq / start - 1) * 100, 2) if start else 0.0,
                "positions": pos, "stats": self.journal.stats(MARKET),
                "budget_usd": self.cfg.opt_budget_usd, "target_mult": self.cfg.opt_target_mult,
                "exit_dte": self.cfg.opt_exit_dte, "skipped": list(reversed(skipped))}
