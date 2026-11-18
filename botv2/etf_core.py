"""ETF core (paper): monthly trend + momentum rotation across 16 ETFs.

Switched on with ETF_CORE_ENABLED=true. PAPER ONLY: virtual fills at Yahoo
prices, no broker orders. The rule is the one tested in the 2026-11-18
backtest (2007-16 and 2017-26):

* pool: SPY QQQ IWM EFA EEM XLK XLF XLV XLE XLI XLY XLP XLU XLB TLT GLD;
  cash = SHY (short-term Treasuries);
* each month, rank the pool by 6-month return; keep the top ETF_TOP_N (5)
  that are above their ~10-month (210-day) average AND beat SHY's 6-month
  return; every empty slot goes to SHY;
* optional ETF_HOLD_SPY_PCT sleeve held in SPY permanently (0.5 = the
  50/50 blend);
* rebalance once a month, on the first US decision cycle of a new month
  (and once right away when the book is new); a holding is only resized
  when it is more than REBAL_BAND of equity away from its target, to keep
  trading small. Fractional shares; cost ETF_COST_PCT per side.

The book lives in the journal kv (ETF_book), separate from the stock and
options books, with a daily equity history for the dashboard.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import notify

log = logging.getLogger("botv2.etf_core")

POOL = ["SPY", "QQQ", "IWM", "EFA", "EEM", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY",
        "XLP", "XLU", "XLB", "TLT", "GLD"]
CASH = "SHY"
NAMES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "US small caps", "EFA": "Developed intl",
         "EEM": "Emerging markets", "XLK": "Tech sector", "XLF": "Financials", "XLV": "Healthcare",
         "XLE": "Energy", "XLI": "Industrials", "XLY": "Consumer discretionary", "XLP": "Consumer staples",
         "XLU": "Utilities", "XLB": "Materials", "TLT": "Long-term Treasuries", "GLD": "Gold",
         "SHY": "Short-term Treasuries (cash)"}
K_BOOK = "ETF_book"
NY = ZoneInfo("America/New_York")
MOM_DAYS, TREND_DAYS = 126, 210        # ~6 months, ~10 months of trading days
REBAL_BAND = 0.02                      # leave a holding alone if within 2% of equity of target


class YahooHistory:
    def closes(self, symbols: list[str]) -> dict[str, list[float]]:
        import yfinance as yf
        df = yf.download(symbols, period="14mo", interval="1d", auto_adjust=True,
                         progress=False, threads=True)["Close"]
        return {s: [float(x) for x in df[s].dropna().tolist()] for s in symbols if s in df.columns}


class ETFCore:
    def __init__(self, journal, cfg, history=None, now=None):
        self.journal = journal
        self.cfg = cfg
        self.history = history or YahooHistory()
        self.now = now or (lambda: datetime.now(NY))

    # ── book ─────────────────────────────────────────────────────
    def book(self) -> dict:
        try:
            b = json.loads(self.journal.kv_get(K_BOOK) or "null")
        except ValueError:
            b = None
        if not b:
            b = {"cash": float(self.cfg.etf_start_cash), "start": float(self.cfg.etf_start_cash),
                 "holdings": {}, "rebalances": [], "history": [], "last_month": None, "marks": {}}
        return b

    def _save(self, b: dict) -> None:
        b["rebalances"] = b["rebalances"][-24:]
        b["history"] = b["history"][-800:]
        self.journal.kv_set(K_BOOK, json.dumps(b))

    @staticmethod
    def equity(b: dict, prices: dict) -> float:
        return b["cash"] + sum(h["shares"] * prices.get(s, b.get("marks", {}).get(s, h["avg"]))
                               for s, h in b["holdings"].items())

    # ── the rule ─────────────────────────────────────────────────
    def targets(self, closes: dict[str, list[float]]) -> tuple[dict[str, float], list[dict]]:
        """(weights, ranking). Weights sum to 1 over POOL + SHY."""
        def mom(s):
            c = closes.get(s) or []
            return c[-1] / c[-1 - MOM_DAYS] - 1 if len(c) > MOM_DAYS else None

        cash_mom = mom(CASH) or 0.0
        rank = []
        for s in POOL:
            c, m = closes.get(s) or [], mom(s)
            if m is None or len(c) < TREND_DAYS:
                continue
            avg = sum(c[-TREND_DAYS:]) / TREND_DAYS
            rank.append({"symbol": s, "name": NAMES[s], "mom_6m": round(m, 4), "price": round(c[-1], 2),
                         "avg_10m": round(avg, 2), "above_trend": c[-1] > avg, "beats_cash": m > cash_mom})
        rank.sort(key=lambda r: -r["mom_6m"])
        n = self.cfg.etf_top_n
        picks = [r["symbol"] for r in rank if r["above_trend"] and r["beats_cash"]][:n]
        for i, r in enumerate(rank, 1):
            r["rank"], r["picked"] = i, r["symbol"] in picks
        sleeve = self.cfg.etf_hold_spy_pct
        w = {s: (1 - sleeve) / n for s in picks}
        if len(picks) < n:
            w[CASH] = w.get(CASH, 0) + (1 - sleeve) * (n - len(picks)) / n
        if sleeve:
            w["SPY"] = w.get("SPY", 0) + sleeve
        return w, rank

    # ── monthly rebalance ────────────────────────────────────────
    def due(self, b: dict | None = None) -> bool:
        b = b or self.book()
        return b.get("last_month") != self.now().strftime("%Y-%m")

    def rebalance(self, force: bool = False) -> dict | None:
        b = self.book()
        if not force and not self.due(b):
            return None
        closes = self.history.closes(POOL + [CASH])
        prices = {s: c[-1] for s, c in closes.items() if c}
        missing = [s for s in POOL + [CASH] if s not in prices]
        if CASH in missing or len(missing) > 4:
            log.warning("etf core: rebalance skipped — no prices for %s", missing)
            return None
        w, rank = self.targets(closes)
        eq = self.equity(b, prices)
        cost = self.cfg.etf_cost_pct
        trades = []
        # sells first (frees cash), then buys
        for s in sorted(set(b["holdings"]) | set(w), key=lambda s: w.get(s, 0)):
            h = b["holdings"].get(s, {"shares": 0.0, "avg": prices.get(s, 0)})
            px = prices.get(s)
            if not px:
                continue
            have, want = h["shares"] * px, w.get(s, 0) * eq
            if abs(want - have) <= REBAL_BAND * eq and not (want == 0 and have > 0):
                continue
            if want < have:                                            # sell down
                sh = h["shares"] if want == 0 else (have - want) / px
                b["cash"] += sh * px * (1 - cost)
                h["shares"] -= sh
                trades.append({"symbol": s, "side": "sell", "shares": round(sh, 6), "price": px,
                               "value": round(sh * px, 2), "pnl": round(sh * (px - h["avg"]), 2)})
                if h["shares"] <= 1e-9:
                    b["holdings"].pop(s, None)
                else:
                    b["holdings"][s] = h
        for s, wt in sorted(w.items(), key=lambda kv: -kv[1]):
            px = prices.get(s)
            h = b["holdings"].get(s, {"shares": 0.0, "avg": px})
            have, want = h["shares"] * px, wt * eq
            if want - have <= REBAL_BAND * eq:
                continue
            spend = min(want - have, b["cash"])
            if spend <= 1:
                continue
            sh = spend * (1 - cost) / px
            h["avg"] = (h["shares"] * h["avg"] + sh * px) / (h["shares"] + sh)
            h["shares"] += sh
            b["cash"] -= spend
            b["holdings"][s] = h
            trades.append({"symbol": s, "side": "buy", "shares": round(sh, 6), "price": px, "value": round(spend, 2)})
        b["marks"] = prices
        b["last_month"] = self.now().strftime("%Y-%m")
        eq_after = self.equity(b, prices)
        reb = {"ts": time.time(), "month": b["last_month"], "equity": round(eq_after, 2),
               "weights": {s: round(v, 4) for s, v in w.items()}, "trades": trades, "ranking": rank}
        b["rebalances"].append(reb)
        b["history"].append({"day": self.now().strftime("%Y-%m-%d"), "equity": round(eq_after, 2)})
        self._save(b)
        self._announce(reb)
        return reb

    def _announce(self, reb: dict) -> None:
        held = ", ".join(f"{s} {v:.0%}" for s, v in sorted(reb["weights"].items(), key=lambda kv: -kv[1]))
        moves = "; ".join(f"{t['side']} {t['symbol']} ${t['value']:,.0f}" for t in reb["trades"]) or "no changes"
        notify.alert(f"📊 <b>[ETF CORE · PAPER] Monthly rebalance {reb['month']}</b>\n"
                     f"Holding: {held}\nTrades: {moves}\nBook ${reb['equity']:,.2f}")

    # ── daily mark (for the equity line) ─────────────────────────
    def mark(self) -> float | None:
        b = self.book()
        if not b["holdings"]:
            return None
        from . import data
        prices = {s: p for s in b["holdings"] if (p := data.fetch_price(s))}
        if not prices:
            return None
        b["marks"] = {**b.get("marks", {}), **prices}
        eq = round(self.equity(b, prices), 2)
        day = self.now().strftime("%Y-%m-%d")
        if b["history"] and b["history"][-1]["day"] == day:
            b["history"][-1]["equity"] = eq
        else:
            b["history"].append({"day": day, "equity": eq})
        self._save(b)
        return eq

    def summary(self) -> dict:
        b = self.book()
        marks = b.get("marks", {})
        eq = self.equity(b, marks)
        holdings = []
        for s, h in sorted(b["holdings"].items(), key=lambda kv: -kv[1]["shares"] * marks.get(kv[0], kv[1]["avg"])):
            px = marks.get(s, h["avg"])
            holdings.append({"symbol": s, "name": NAMES.get(s, s), "shares": round(h["shares"], 4),
                             "avg": round(h["avg"], 2), "price": round(px, 2), "value": round(h["shares"] * px, 2),
                             "weight": round(h["shares"] * px / eq, 4) if eq else 0,
                             "pnl": round(h["shares"] * (px - h["avg"]), 2)})
        last = b["rebalances"][-1] if b["rebalances"] else None
        return {"mode": "paper", "cash": round(b["cash"], 2), "equity": round(eq, 2), "start": b["start"],
                "total_return_pct": round((eq / b["start"] - 1) * 100, 2) if b["start"] else 0.0,
                "holdings": holdings, "history": b["history"][-400:], "last_rebalance": last,
                "rebalances": [{"month": r["month"], "equity": r["equity"], "trades": r["trades"]}
                               for r in reversed(b["rebalances"][-12:])],
                "top_n": self.cfg.etf_top_n, "hold_spy_pct": self.cfg.etf_hold_spy_pct}
