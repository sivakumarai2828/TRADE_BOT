"""India option ideas with ESTIMATED premiums, tracked on paper.

Switched on with INDIA_OPTIONS_ENABLED=true. Nothing here trades: no broker,
no orders. There is no live NSE option-chain feed available to the bot (Yahoo
has none; HDFC's market-data API is "coming soon"), so prices are MODELED:

* for each India stock idea that passed the risk rules and the Skeptic today,
  one call idea on the same stock: the strike nearest the price at or above
  it (strike steps approximated by price band), on the monthly expiry 21-50
  days out (NSE monthly stock options expire on the last Tuesday);
* premium = Black-Scholes from the stock's last 30 days of realized
  volatility x IV_PREMIUM, rate RATE_INR, no dividends;
* lot size from NSE's own fo_mktlots.csv (cached daily). Stocks not in F&O
  are skipped.

The idea is then followed on paper, re-estimated every 20 minutes in the NSE
session from the stock price: "reached target" when the estimate reaches
OPT_TARGET_MULT x the starting estimate, or "closed before expiry"
OPT_EXIT_DTE days before expiry. Every number shown is labelled an estimate;
the real premium can differ a lot, especially for thinly traded strikes.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from . import notify, suggest
from .options_bt import bs_call

log = logging.getLogger("botv2.india_options")

IST = ZoneInfo("Asia/Kolkata")
K_IDEAS, K_LOTS = "INDIA_option_ideas", "INDIA_fo_lots"
LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
RATE_INR = 0.065
IV_PREMIUM = 1.10
VOL_FLOOR = 0.15
KEEP = 200


def strike_step(price: float) -> float:
    """Approximate NSE stock-option strike spacing by price band."""
    for limit, step in ((50, 1), (100, 2.5), (250, 5), (500, 10), (1000, 20),
                        (2500, 50), (5000, 100), (10000, 200), (25000, 500)):
        if price < limit:
            return step
    return 1000


def strike_at_or_above(price: float) -> float:
    step = strike_step(price)
    return math.ceil(price / step - 1e-9) * step


def last_tuesday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 1) % 7)


def pick_expiry(today: date, dte_min: int = 21, dte_max: int = 50) -> date | None:
    y, m = today.year, today.month
    for _ in range(3):
        e = last_tuesday(y, m)
        if dte_min <= (e - today).days <= dte_max:
            return e
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def parse_lots(text: str) -> dict[str, int]:
    """fo_mktlots.csv -> {SYMBOL: lot size for the nearest month}."""
    rows = list(csv.reader(io.StringIO(text)))
    out = {}
    for r in rows[1:]:
        if len(r) > 2 and r[1].strip() and r[2].strip().isdigit():
            out[r[1].strip()] = int(r[2].strip())
    return out


class IndiaOptionIdeas:
    def __init__(self, journal, cfg, now=None, fetch_lots=None, history=None, price=None):
        self.journal = journal
        self.cfg = cfg
        self.now = now or (lambda: datetime.now(IST))
        self._fetch_lots = fetch_lots or self._download_lots
        self._history = history or self._yahoo_closes       # symbol -> list of daily closes
        self._price = price                                  # symbol -> price (defaults to data.fetch_price)

    # ── inputs ───────────────────────────────────────────────────
    @staticmethod
    def _download_lots() -> str:
        r = requests.get(LOTS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        return r.text

    @staticmethod
    def _yahoo_closes(symbol: str) -> list[float]:
        import yfinance as yf
        h = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=True)
        return [float(x) for x in h["Close"].dropna().tolist()]

    def _spot(self, symbol: str) -> float | None:
        if self._price:
            return self._price(symbol)
        from . import data
        return data.fetch_price(symbol)

    def lots(self) -> dict[str, int]:
        today = self.now().date().isoformat()
        try:
            cached = json.loads(self.journal.kv_get(K_LOTS) or "{}")
        except ValueError:
            cached = {}
        if cached.get("day") == today and cached.get("lots"):
            return cached["lots"]
        try:
            lots = parse_lots(self._fetch_lots())
            if lots:
                self.journal.kv_set(K_LOTS, json.dumps({"day": today, "lots": lots}))
                return lots
        except Exception as exc:
            log.warning("india options: lot sizes unavailable (%s) — using cached", type(exc).__name__)
        return cached.get("lots") or {}

    def vol(self, symbol: str) -> float:
        closes = self._history(symbol)[-31:]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 10:
            return 0.35
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        return max(VOL_FLOOR, sd * math.sqrt(252) * IV_PREMIUM)

    # ── storage ──────────────────────────────────────────────────
    def load(self) -> list[dict]:
        try:
            return json.loads(self.journal.kv_get(K_IDEAS) or "[]")
        except ValueError:
            return []

    def _save(self, items: list[dict]) -> None:
        self.journal.kv_set(K_IDEAS, json.dumps(items[-KEEP:]))

    # ── daily ideas ──────────────────────────────────────────────
    def build(self) -> list[dict]:
        today = self.now().date()
        day = suggest.today("INDIA")
        ideas = [s for s in suggest.load(self.journal, "INDIA")
                 if s["day"] == day and s.get("status") == "suggest"]
        items, made = self.load(), []
        have = {(i["day"], i["symbol"]) for i in items}
        lots = self.lots()
        exp = pick_expiry(today)
        for s in suggest.ranked(ideas):
            sym, root = s["symbol"], s["symbol"].replace(".NS", "")
            if (day, sym) in have:
                continue
            lot = lots.get(root)
            if not lot or exp is None:
                log.info("india options: %s skipped — %s", sym, "not in F&O" if not lot else "no expiry")
                continue
            try:
                spot = self._spot(sym) or float(s["price"])
                sigma = self.vol(sym)
            except Exception as exc:
                log.warning("india options: %s data failed: %s", sym, type(exc).__name__)
                continue
            K = strike_at_or_above(spot)
            days = (exp - today).days
            prem = bs_call(spot, K, days / 365.0, RATE_INR, sigma)
            if prem < 0.5:
                continue
            idea = {
                "id": f"{day}-{root}", "day": day, "ts": time.time(), "symbol": sym, "root": root,
                "priority": s.get("priority"), "spot": round(spot, 2), "strike": K,
                "expiry": exp.isoformat(), "days": days, "lot": lot, "vol": round(sigma, 3),
                "exit_by": (exp - timedelta(days=self.cfg.opt_exit_dte)).isoformat(),
                "est_premium": round(prem, 2), "est_cost_lot": round(prem * lot, 0),
                "sell_at": round(prem * self.cfg.opt_target_mult, 2),
                "status": "open", "est_now": round(prem, 2), "marked": time.time(),
                "thesis": str(s.get("thesis", ""))[:300],
            }
            items.append(idea)
            made.append(idea)
        if made:
            self._save(items)
            self._send(made)
        return made

    def _send(self, made: list[dict]) -> None:
        lines = [f"🎯 <b>[INDIA] Option ideas (ESTIMATED premiums, paper only) — {len(made)}</b>"]
        for i in made:
            lines.append(f"• <b>{notify._esc(i['root'])} {i['strike']:g} CE</b> exp {i['expiry']} · "
                         f"est ₹{i['est_premium']:,.2f}/sh × lot {i['lot']} ≈ ₹{i['est_cost_lot']:,.0f} · "
                         f"tracked to ₹{i['sell_at']:,.2f} or until {i['exit_by']}")
        lines.append("Estimates from the stock price, not live quotes: the real premium can differ a lot. "
                     "Not advice; nothing is traded.")
        notify.alert("\n".join(lines))

    # ── paper tracking ───────────────────────────────────────────
    def monitor(self) -> list[dict]:
        today, items, done = self.now().date(), self.load(), []
        for i in items:
            if i.get("status") != "open":
                continue
            exp = date.fromisoformat(i["expiry"])
            days = (exp - today).days
            try:
                spot = self._spot(i["symbol"])
            except Exception:
                spot = None
            if not spot:
                continue
            est = bs_call(spot, i["strike"], max(days, 0) / 365.0, RATE_INR, i["vol"])
            i.update(est_now=round(est, 2), spot_now=round(spot, 2), marked=time.time())
            if est >= i["sell_at"]:
                i.update(status="target", closed=time.time(), est_exit=round(est, 2))
            elif days <= self.cfg.opt_exit_dte:
                i.update(status="expiry_near", closed=time.time(), est_exit=round(est, 2))
            if i["status"] != "open":
                i["est_pnl_pct"] = round(100 * (i["est_exit"] / i["est_premium"] - 1), 1)
                i["est_pnl_lot"] = round((i["est_exit"] - i["est_premium"]) * i["lot"], 0)
                done.append(i)
        self._save(items)
        for i in done:
            notify.alert(f"🎯 [INDIA] Option idea {i['root']} {i['strike']:g} CE "
                         f"{'reached its target' if i['status'] == 'target' else 'closed before expiry'} "
                         f"(estimate {i['est_pnl_pct']:+.0f}%, ≈ ₹{i['est_pnl_lot']:+,.0f} per lot). Paper only.")
        return done

    def recent(self, days: int = 30) -> list[dict]:
        cutoff = time.time() - days * 86400
        return [i for i in reversed(self.load()) if i.get("status") == "open" or i.get("ts", 0) >= cutoff][:40]
