"""Executes the AI's action plan through the risk layer."""

from __future__ import annotations

import logging

from . import data, notify
from .ai_pm import PortfolioManagerAI, build_prompt
from .journal import Journal
from .ledger import PaperLedger
from .risk import RiskEngine
from .universe import universe_for

log = logging.getLogger("botv2.executor")


class MarketRunner:
    """One decision cycle for one market (US or INDIA)."""

    def __init__(self, market: str, currency: str, cfg, journal: Journal,
                 ai: PortfolioManagerAI, ledger: PaperLedger, alpaca=None):
        self.market = market
        self.currency = currency
        self.cfg = cfg
        self.journal = journal
        self.ai = ai
        self.ledger = ledger
        self.alpaca = alpaca  # AlpacaMirror or None (US only)
        self.risk = RiskEngine(cfg.caps, journal, market)

    # ── stop/target monitor (runs intra-session, no AI call) ────
    def monitor_positions(self) -> None:
        # Kill switch must also work mid-session (review issue #5).
        halt = self.risk.check_halts(self.ledger.equity(), self.ledger.day_pnl())
        if halt and "KILL SWITCH" in halt:
            self._flatten_all(halt)
            return
        for pos in self.ledger.positions():
            if pos.get("price_stale"):
                log.warning("%s %s: price fetch failed — skipping stop/target check",
                            self.market, pos["symbol"])
                continue
            px = pos["last_price"]
            if pos["stop"] and px <= pos["stop"]:
                self._exit(pos, "stop_loss")
            elif pos["target"] and px >= pos["target"]:
                self._exit(pos, "take_profit")

    def _flatten_all(self, halt_msg: str) -> None:
        fills = self.ledger.flatten_all("kill_switch")
        for f in fills:
            if self.alpaca:
                self.alpaca.order(f["symbol"], f["qty"], "sell")
        notify.alert(f"🔴 {self.market} {halt_msg}")

    def _exit(self, pos: dict, reason: str) -> None:
        fill = self.ledger.sell(pos, reason)
        if fill:
            if self.alpaca:
                self.alpaca.order(pos["symbol"], pos["qty"], "sell")
            notify.trade_closed(self.market, fill["symbol"], fill["fill"],
                                fill["pnl"], reason, self.currency)
            log.info("%s exit %s @ %.2f (%s) pnl=%.2f", self.market,
                     pos["symbol"], fill["fill"], reason, fill["pnl"])

    # ── the daily AI decision cycle ──────────────────────────────
    def run_cycle(self) -> None:
        log.info("=== %s decision cycle start ===", self.market)
        equity, day_pnl = self.ledger.equity(), self.ledger.day_pnl()
        halt = self.risk.check_halts(equity, day_pnl)

        if halt and "KILL SWITCH" in halt:
            self._flatten_all(halt)
            return
        if halt:
            notify.alert(f"⚠️ {self.market} {halt}")

        # Gather context
        symbols, benchmarks = universe_for(self.market)
        extra = (self.journal.kv_get(f"{self.market}_watch_next") or "").split(",")
        wanted = list(dict.fromkeys(symbols + [s for s in extra if s]))
        candidates = [s for sym in wanted if (s := data.fetch_snapshot(sym))]
        regime = data.fetch_market_context(benchmarks)

        # Context enrichment (review 2026-07-19): relative strength vs the
        # primary benchmark per candidate, and universe breadth in the regime.
        bench = regime[0] if regime else None
        for c in candidates:
            if bench and c.get("ret_3m_pct") is not None and bench.get("ret_3m_pct") is not None:
                c["rs_3m_vs_benchmark"] = round(c["ret_3m_pct"] - bench["ret_3m_pct"], 2)
        with_200 = [c for c in candidates if c.get("price") and c.get("sma200")]
        if with_200:
            breadth = round(100 * sum(1 for c in with_200 if c["price"] > c["sma200"]) / len(with_200), 1)
            regime = regime + [{"breadth_pct_of_universe_above_200dma": breadth}]
        positions = self.ledger.positions()
        portfolio = {
            "cash": self.ledger.cash,
            "equity": equity,
            "day_realized_pnl": day_pnl,
            "open_positions": [
                {k: p[k] for k in ("symbol", "qty", "entry", "stop", "target",
                                   "last_price", "unrealized_pnl", "unrealized_pnl_pct", "thesis")}
                for p in positions
            ],
        }

        prompt = build_prompt(
            self.market, self.currency, regime, candidates, portfolio,
            self.journal.stats(self.market), self.journal.recent_closed(self.market),
            self.journal.latest_memo(self.market), self.risk.describe(), halt,
        )
        decision = self.ai.decide(prompt)
        # Store the full prompt context with the decision for later analysis.
        self.journal.log_decision(self.market, decision, note=prompt[:50000])
        self.journal.kv_set(f"{self.market}_watch_next",
                            ",".join(decision.get("watch_next", [])[:5]))
        notify.market_view(self.market, decision.get("market_view", ""))

        by_symbol = {p["symbol"]: p for p in positions}
        earnings = {c["symbol"]: c.get("days_to_earnings") for c in candidates}
        for act in decision.get("actions", []):
            try:
                self._apply(act, by_symbol, halt, earnings)
            except Exception as exc:
                log.exception("action failed %s: %s", act, exc)
        log.info("=== %s decision cycle done ===", self.market)

    EARNINGS_BLOCK_DAYS = 3  # no new entries this close to a scheduled report

    def _apply(self, act: dict, by_symbol: dict, halt: str | None,
               earnings: dict | None = None) -> None:
        kind = str(act.get("action", "")).upper()
        sym = str(act.get("symbol", "")).strip()

        if kind == "SELL" and sym in by_symbol:
            self._exit(by_symbol[sym], f"ai_exit: {act.get('reason', '')[:80]}")
            by_symbol.pop(sym, None)  # duplicate-SELL guard (review issue #3)

        elif kind == "ADJUST" and sym in by_symbol:
            pos = by_symbol[sym]
            new_stop, new_target, note = self.risk.validate_adjust(
                sym, pos.get("last_price") or pos["entry"], pos.get("stop"),
                act.get("stop"), act.get("target"),
            )
            if note:
                log.warning("ADJUST %s partially rejected: %s", sym, note)
            if new_stop is not None or new_target is not None:
                self.journal.update_levels(pos["id"], new_stop, new_target)
                log.info("%s adjusted: stop=%s target=%s", sym, new_stop, new_target)

        elif kind == "BUY":
            if halt:
                log.warning("BUY %s rejected — halt active", sym)
                return
            if sym in by_symbol:
                log.info("BUY %s skipped — already held (no pyramiding in v2.0)", sym)
                return
            days = (earnings or {}).get(sym)
            if days is not None and days <= self.EARNINGS_BLOCK_DAYS:
                log.warning("BUY %s rejected — earnings in %d day(s)", sym, days)
                return
            price = data.fetch_price(sym)
            if not price:
                return
            target = float(act.get("target") or 0) or None
            verdict = self.risk.validate_buy(
                sym, price, float(act.get("stop", 0)), target,
                self.ledger.equity(), self.ledger.cash, len(by_symbol),
            )
            if not verdict.ok:
                log.warning("BUY %s rejected by risk: %s", sym, verdict.reason)
                return
            fill = self.ledger.buy(sym, verdict.qty, float(act["stop"]), target,
                                   str(act.get("thesis", ""))[:300])
            if fill:
                by_symbol[sym] = {"symbol": sym}  # count toward max positions
                if self.alpaca:
                    self.alpaca.order(sym, fill["qty"], "buy")
                notify.trade_opened(self.market, sym, fill["qty"], fill["fill"],
                                    float(act["stop"]), act.get("target"),
                                    str(act.get("thesis", "")), self.currency)

    # ── weekly self-review ───────────────────────────────────────
    def weekly_review(self) -> None:
        recent = self.journal.recent_closed(self.market, 25)
        if not recent:
            return
        memo = self.ai.self_review(self.market, self.journal.stats(self.market),
                                   recent, self.journal.latest_memo(self.market))
        self.journal.save_memo(self.market, "self_review", memo)
        notify.alert(f"📝 {self.market} weekly self-review saved:\n{memo[:500]}")
