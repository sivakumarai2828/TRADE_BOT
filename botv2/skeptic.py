"""Skeptic — a second opinion whose only job is to argue against new entries.

The portfolio manager is told to stay invested, so it has every reason to talk
itself into a trade. The Skeptic sees one proposed entry at a time, must name
the three strongest reasons it fails, and rules BUY, WAIT or SKIP. Only BUY
lets the entry go ahead.

It runs AFTER the code risk checks (so it never wastes a call on an order the
caps would reject anyway) and BEFORE any capital moves.

Fail-closed: if the call errors or the reply can't be parsed, the ruling is
SKIP. An outage costs a missed trade, never an unchecked one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .ai_pm import _extract_json

log = logging.getLogger("botv2.skeptic")

RULINGS = ("BUY", "WAIT", "SKIP")

SKEPTIC_SYSTEM = """You are the SKEPTIC on a swing-trading desk. Another model \
(the portfolio manager) has proposed ONE long entry. It is paid to stay invested; \
you are paid for the losing trades you correctly refuse. A day where you approve \
everything is a failed day.

For the proposed entry:
1. Name the THREE strongest, specific reasons this trade will fail, using the data \
given (extension from moving averages, RSI, relative strength, stage-2 status, \
volume, earnings proximity, regime, stop placement vs real structure, whether the \
target is a realistic level). Generic risk statements do not count.
2. Then rule:
   BUY  - the objections are weak and the setup survives them.
   WAIT - the stock is fine but the entry price or timing is not (e.g. extended, \
needs a pullback, event pending).
   SKIP - the setup itself is flawed.
If a key fact is missing from the data, treat it as a reason against, not for.

Respond with ONLY a JSON object, no markdown:
{"reasons": ["...", "...", "..."], "ruling": "BUY|WAIT|SKIP", "summary": "one sentence"}"""


@dataclass
class SkepticVerdict:
    ruling: str                          # BUY | WAIT | SKIP
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""

    @property
    def approved(self) -> bool:
        return self.ruling == "BUY"


def build_skeptic_prompt(market: str, currency: str, proposal: dict,
                         snapshot: dict | None, regime_label: str,
                         portfolio: dict | None = None) -> str:
    price, stop, target = proposal["price"], proposal["stop"], proposal.get("target")
    rr = None
    try:
        if target and price > stop:
            rr = round((float(target) - price) / (price - float(stop)), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        rr = None
    body = {
        "market": market,
        "currency": currency,
        "proposal": {
            "kind": proposal.get("kind", "BUY"),
            "symbol": proposal["symbol"],
            "setup": proposal.get("setup"),
            "entry_price_now": price,
            "stop": stop,
            "target": target,
            "reward_risk_R": rr,
            "stop_distance_pct": round(100 * (price - float(stop)) / price, 2) if price else None,
            "pm_thesis": proposal.get("thesis", ""),
        },
        "market_regime": regime_label,
        "candidate_data": snapshot or "NOT AVAILABLE",
    }
    if portfolio:
        body["portfolio"] = portfolio
    return ("Proposed entry for your review:\n"
            + json.dumps(body, default=str, indent=1)
            + "\n\nArgue against it, then rule BUY, WAIT or SKIP.")


class Skeptic:
    """Wraps any object exposing `_call(system, user, max_tokens) -> str`
    (PortfolioManagerAI does). Pass a separate instance to use a different
    model from the PM, which gives a genuinely independent second opinion."""

    MAX_TOKENS = 2500

    def __init__(self, backend):
        self.backend = backend

    def review(self, market: str, currency: str, proposal: dict,
               snapshot: dict | None, regime_label: str,
               portfolio: dict | None = None) -> SkepticVerdict:
        prompt = build_skeptic_prompt(market, currency, proposal, snapshot,
                                      regime_label, portfolio)
        try:
            raw = self.backend._call(SKEPTIC_SYSTEM, prompt, self.MAX_TOKENS)
            data = _extract_json(raw)
        except Exception as exc:
            log.warning("skeptic failed for %s — fail-closed SKIP: %s",
                        proposal.get("symbol"), exc)
            return SkepticVerdict("SKIP", summary="skeptic unavailable (fail-closed)",
                                  error=str(exc)[:200])
        return parse_verdict(data)


def parse_verdict(data) -> SkepticVerdict:
    if not isinstance(data, dict):
        return SkepticVerdict("SKIP", summary="unparseable skeptic reply",
                              error="not an object")
    ruling = str(data.get("ruling", "")).strip().upper()
    if ruling not in RULINGS:
        return SkepticVerdict("SKIP", summary=f"invalid ruling {ruling!r} (fail-closed)",
                              error="bad ruling")
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    reasons = [str(r)[:240] for r in reasons][:3]
    return SkepticVerdict(ruling, reasons, str(data.get("summary", ""))[:300])
