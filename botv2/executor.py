"""Executes the AI's action plan through the risk layer.

V3 adds three capabilities on top of the V2 buy/sell/adjust loop:

  ATTRIBUTION  every position records the setup type, regime, sector and
               indicator readings at entry, so closed trades can be grouped by
               setup and the question "which entry method creates the edge"
               becomes answerable.
  WATCH        the model can flag a stock it wants at a *different* price. The
               20-minute monitor arms the trigger; nothing is bought without a
               full revalidation at trigger time.
  REPLACE      when the book is full, a materially better opportunity can take
               the place of the weakest holding instead of being skipped.
"""

from __future__ import annotations

import logging
import time

from . import data, notify
from .ai_pm import PortfolioManagerAI, build_prompt
from .journal import Journal
from .ledger import PaperLedger
from .risk import RiskEngine
from .universe import sector_for, universe_for

log = logging.getLogger("botv2.executor")

SETUPS = ("BREAKOUT", "PULLBACK", "CONTINUATION")


def classify_regime(regime: list[dict]) -> str:
    """BULL / NEUTRAL / CHOPPY / BEAR from the primary benchmark.

    Deterministic and code-owned: the model reports a regime in its prose, but
    the label stored against a trade must not be the model's own opinion.
    """
    bench = next((r for r in regime if r.get("symbol")), None)
    if not bench:
        return "UNKNOWN"
    px, sma50, r3 = bench.get("price"), bench.get("sma50"), bench.get("ret_3m_pct")
    if px is None or sma50 is None:
        return "UNKNOWN"
    above = px > sma50
    if r3 is None:
        return "BULL" if above else "BEAR"
    if above and r3 > 5:
        return "BULL"
    if not above and r3 < -5:
        return "BEAR"
    return "NEUTRAL" if above else "CHOPPY"


class MarketRunner:
    """One decision cycle for one market (US or INDIA)."""

    EARNINGS_BLOCK_DAYS = 3      # no new entries this close to a scheduled report
    MIN_HOLD_SESSIONS = 5        # a position is immune from REPLACE until this old
    REPLACE_MIN_EDGE = 0.5       # new R must beat the old position's R by this much

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
        self._snapshots: dict[str, dict] = {}   # symbol -> latest snapshot
        self._regime_label = "UNKNOWN"

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
        self.check_watch_triggers(halt)

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

    # ── WATCH triggers (V3) ──────────────────────────────────────
    def check_watch_triggers(self, halt: str | None = None) -> None:
        """Arm pending watches. Price reaching the level is necessary but never
        sufficient — every trigger is revalidated before it becomes an order."""
        try:
            self.journal.expire_watches(self.market)
            watches = self.journal.active_watches(self.market)
        except Exception:
            log.exception("watch lookup failed")
            return
        if not watches:
            return
        held = {p["symbol"] for p in self.ledger.positions()}
        for w in watches:
            sym = w["symbol"]
            if sym in held:
                self.journal.resolve_watch(w["id"], "cancelled", "already held")
                continue
            px = data.fetch_price(sym)
            if px is None:
                continue
            if px > (w["ideal_entry"] or 0) * 1.005:      # not at the level yet
                continue
            if halt:
                log.info("watch %s at level but halt active", sym)
                continue
            self._trigger_watch(w, px)

    def _trigger_watch(self, w: dict, price: float) -> None:
        """Revalidate a watch from scratch, then buy or cancel."""
        sym = w["symbol"]
        snap = data.fetch_snapshot(sym)
        if not snap:
            self.journal.resolve_watch(w["id"], "cancelled", "no data at trigger")
            return
        days = snap.get("days_to_earnings")
        if days is not None and days <= self.EARNINGS_BLOCK_DAYS:
            self.journal.resolve_watch(w["id"], "cancelled", f"earnings in {days}d")
            return
        m200 = snap.get("sma200")
        if m200 and snap.get("price") and snap["price"] < m200:
            self.journal.resolve_watch(w["id"], "cancelled", "lost 200DMA")
            return
        verdict = self.risk.validate_buy(
            sym, price, float(w["stop"] or 0), float(w["target"] or 0) or None,
            self.ledger.equity(), self.ledger.cash,
            len(self.ledger.positions()),
        )
        if not verdict.ok:
            log.warning("watch %s trigger rejected: %s", sym, verdict.reason)
            self.journal.resolve_watch(w["id"], "cancelled", verdict.reason[:120])
            return
        meta = self._meta_for(sym, snap, w.get("setup"), price, w["stop"], w["target"])
        fill = self.ledger.buy(sym, verdict.qty, float(w["stop"]),
                               float(w["target"]), f"[watch] {w.get('thesis', '')}"[:300],
                               meta=meta)
        if fill:
            self.journal.resolve_watch(w["id"], "triggered", f"filled @ {fill['fill']}")
            if self.alpaca:
                self.alpaca.order(sym, fill["qty"], "buy")
            notify.trade_opened(self.market, sym, fill["qty"], fill["fill"],
                                w["stop"], w["target"],
                                f"WATCH triggered: {w.get('thesis', '')}", self.currency)
            log.info("%s WATCH triggered %s @ %.2f", self.market, sym, fill["fill"])

    # ── attribution (V3) ─────────────────────────────────────────
    def _meta_for(self, sym: str, snap: dict | None, setup: str | None,
                  entry: float, stop: float | None, target: float | None) -> dict:
        """Indicator readings and context frozen at entry, for later analysis."""
        snap = snap or self._snapshots.get(sym) or {}
        atr, px = snap.get("atr14"), snap.get("price")
        planned_r = None
        try:
            if stop and target and entry > float(stop):
                planned_r = round((float(target) - entry) / (entry - float(stop)), 3)
        except (TypeError, ValueError):
            pass
        setup = setup if setup in SETUPS else None
        return {
            "setup_type": setup,
            "market_regime": self._regime_label,
            "sector": sector_for(sym),
            "rsi_entry": snap.get("rsi14"),
            "atr_pct_entry": round(100 * atr / px, 3) if atr and px else None,
            "vol_ratio_entry": snap.get("vol20_vs_vol50"),
            "rs_entry": snap.get("rs_3m_vs_benchmark"),
            "stage2_entry": snap.get("stage2_uptrend"),
            "planned_r": planned_r,
        }

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
        self._regime_label = classify_regime(regime)

        # Context enrichment (review 2026-07-19): relative strength vs the
        # primary benchmark per candidate, and universe breadth in the regime.
        bench = regime[0] if regime else None
        for c in candidates:
            if bench and c.get("ret_3m_pct") is not None and bench.get("ret_3m_pct") is not None:
                c["rs_3m_vs_benchmark"] = round(c["ret_3m_pct"] - bench["ret_3m_pct"], 2)
        with_200 = [c for c in candidates if c.get("price") and c.get("sma200")]
        if with_200:
            breadth = round(100 * sum(1 for c in with_200 if c["price"] > c["sma200"]) / len(with_200), 1)
            regime = regime + [{"breadth_pct_of_universe_above_200dma": breadth,
                                "regime_label": self._regime_label}]
        self._snapshots = {c["symbol"]: c for c in candidates}

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

        portfolio["pct_deployed"] = round(100 * (equity - self.ledger.cash) / equity, 1) if equity else 0.0
        _tgt = getattr(self.cfg, "target_deployment_pct", 0.0)
        if _tgt:
            portfolio["target_pct_deployed"] = round(_tgt * 100, 1)
        try:
            self.journal.expire_watches(self.market)
            portfolio["active_watches"] = [
                {k: w.get(k) for k in ("symbol", "setup", "ideal_entry", "stop", "target")}
                for w in self.journal.active_watches(self.market)
            ]
        except Exception:
            log.exception("watch state unavailable for prompt")

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

    def _apply(self, act: dict, by_symbol: dict, halt: str | None,
               earnings: dict | None = None) -> None:
        kind = str(act.get("action", "")).upper()
        sym = str(act.get("symbol", "")).strip()

        if kind == "SELL" and sym in by_symbol:
            self._exit(by_symbol[sym], f"ai_exit: {act.get('reason', '')[:80]}")
            by_symbol.pop(sym, None)  # duplicate-SELL guard (review issue #3)

        elif kind == "HOLD":
            return                     # explicit no-op, recorded in the decision

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

        elif kind == "WATCH":
            self._add_watch(act, by_symbol, earnings)

        elif kind == "REPLACE":
            self._replace(act, by_symbol, halt, earnings)

        elif kind == "BUY":
            self._buy(act, by_symbol, halt, earnings)

    # ── WATCH creation ───────────────────────────────────────────
    def _add_watch(self, act: dict, by_symbol: dict, earnings: dict | None) -> None:
        sym = str(act.get("symbol", "")).strip()
        entry = act.get("ideal_entry") or act.get("entry")
        stop, target = act.get("stop"), act.get("target")
        if not sym or entry is None or stop is None or target is None:
            log.warning("WATCH %s ignored — needs ideal_entry, stop and target", sym)
            return
        if sym in by_symbol:
            log.info("WATCH %s ignored — already held", sym)
            return
        try:
            entry, stop, target = float(entry), float(stop), float(target)
        except (TypeError, ValueError):
            log.warning("WATCH %s ignored — non-numeric levels", sym)
            return
        if not (stop < entry < target):
            log.warning("WATCH %s ignored — needs stop < entry < target", sym)
            return
        rr = (target - entry) / (entry - stop)
        if rr < self.risk.MIN_REWARD_RISK:
            log.warning("WATCH %s ignored — %.2fR at ideal entry < %.1fR",
                        sym, rr, self.risk.MIN_REWARD_RISK)
            return
        setup = str(act.get("setup", "")).upper()
        self.journal.add_watch(self.market, sym, setup if setup in SETUPS else None,
                               entry, stop, target, str(act.get("thesis", ""))[:300])
        log.info("%s WATCH armed %s @ %.2f (%.2fR)", self.market, sym, entry, rr)

    # ── capital rotation ─────────────────────────────────────────
    def _replace(self, act: dict, by_symbol: dict, halt: str | None,
                 earnings: dict | None) -> None:
        """Swap the named holding for a better opportunity.

        Three protections, in order of importance:
          1. a position whose stop already sits at or above entry cannot lose,
             so it is never given up for an unproven one;
          2. positions younger than MIN_HOLD_SESSIONS are immune, which stops
             the book churning on daily noise;
          3. the new setup must beat the old position's planned R by a margin,
             not merely tie it.
        """
        sym = str(act.get("symbol", "")).strip()
        old_sym = str(act.get("replace") or act.get("replaces") or "").strip()
        if halt:
            log.warning("REPLACE %s rejected — halt active", sym)
            return
        if old_sym not in by_symbol:
            log.warning("REPLACE rejected — %r is not an open position", old_sym)
            return
        old = by_symbol[old_sym]

        if old.get("stop") and old.get("entry") and float(old["stop"]) >= float(old["entry"]):
            log.warning("REPLACE %s rejected — stop is at/above entry (risk-free)", old_sym)
            return
        age_days = (time.time() - (old.get("ts_open") or 0)) / 86400
        if age_days < self.MIN_HOLD_SESSIONS:
            log.warning("REPLACE %s rejected — held %.1f days < %d minimum",
                        old_sym, age_days, self.MIN_HOLD_SESSIONS)
            return

        price = data.fetch_price(sym)
        if not price:
            return
        try:
            new_stop = float(act.get("stop", 0))
            new_target = float(act.get("target") or 0) or None
        except (TypeError, ValueError):
            return
        if not new_target or new_stop <= 0 or price <= new_stop:
            log.warning("REPLACE %s rejected — invalid levels", sym)
            return
        new_r = (new_target - price) / (price - new_stop)
        old_r = old.get("planned_r")
        if old_r is None and old.get("target") and old.get("entry") and old.get("initial_stop"):
            try:
                old_r = ((float(old["target"]) - float(old["entry"]))
                         / (float(old["entry"]) - float(old["initial_stop"])))
            except (TypeError, ZeroDivisionError, ValueError):
                old_r = None
        if old_r is not None and new_r < old_r + self.REPLACE_MIN_EDGE:
            log.warning("REPLACE %s rejected — %.2fR does not beat %s (%.2fR) by %.1f",
                        sym, new_r, old_sym, old_r, self.REPLACE_MIN_EDGE)
            return

        log.info("%s REPLACE %s -> %s (%.2fR)", self.market, old_sym, sym, new_r)
        self._exit(old, f"replaced_by_{sym}")
        by_symbol.pop(old_sym, None)
        self._buy(act, by_symbol, halt, earnings)

    # ── BUY ──────────────────────────────────────────────────────
    def _buy(self, act: dict, by_symbol: dict, halt: str | None,
             earnings: dict | None) -> None:
        sym = str(act.get("symbol", "")).strip()
        if halt:
            log.warning("BUY %s rejected — halt active", sym)
            return
        if sym in by_symbol:
            log.info("BUY %s skipped — already held (no pyramiding)", sym)
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
        setup = str(act.get("setup", "")).upper()
        meta = self._meta_for(sym, self._snapshots.get(sym), setup,
                              price, act.get("stop"), target)
        fill = self.ledger.buy(sym, verdict.qty, float(act["stop"]), target,
                               str(act.get("thesis", ""))[:300], meta=meta)
        if fill:
            by_symbol[sym] = {"symbol": sym}  # count toward max positions
            self.journal.cancel_watch_symbol(self.market, sym, "bought directly")
            if self.alpaca:
                self.alpaca.order(sym, fill["qty"], "buy")
            notify.trade_opened(self.market, sym, fill["qty"], fill["fill"],
                                float(act["stop"]), act.get("target"),
                                str(act.get("thesis", "")), self.currency)
            log.info("%s BUY %s %.0f @ %.2f (%s)", self.market, sym, fill["qty"],
                     fill["fill"], meta.get("setup_type") or "unlabelled")

    # ── weekly self-review ───────────────────────────────────────
    def weekly_review(self) -> None:
        recent = self.journal.recent_closed(self.market, 25)
        if not recent:
            return
        memo = self.ai.self_review(self.market, self.journal.stats(self.market),
                                   recent, self.journal.latest_memo(self.market))
        self.journal.save_memo(self.market, "self_review", memo)
        notify.alert(f"📝 {self.market} weekly self-review saved:\n{memo[:500]}")
