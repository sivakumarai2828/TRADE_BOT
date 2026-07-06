"""AI Portfolio Manager — Claude runs the book.

The model receives everything a human PM would want: market regime, full
candidate data, its portfolio with live P&L, its own trade history and stats,
and its latest self-review memo. It returns a JSON action plan. Code enforces
the hard caps; inside them the AI is free.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("botv2.ai_pm")

SYSTEM_PROMPT = """You are the portfolio manager of a swing-trading book. You have FULL \
discretion within the hard risk caps provided — you pick symbols, entries, stops, targets, \
position intent, exits, and when to do nothing. Doing nothing is often the best trade.

Principles you are evaluated on:
- Expectancy over activity. You are NOT required to trade. Forced trades are the #1 \
failure mode of the previous system.
- Asymmetry: only enter when your realistic target is at least 2x the distance to your stop.
- Regime first: in a weak or choppy market benchmark, hold cash without apology.
- Learn from your own record: your trade history, stats, and your latest self-review memo \
are included. Do not repeat documented mistakes.
- Stops are honored by code automatically. Set them where the thesis is invalidated, not \
at arbitrary percentages.
- You may raise stops on winners (never lower them), take full profits, or cut \
losers early with a reason. Partial exits are not supported — SELL closes the whole position.
- BUY orders without a target above entry, or with reward less than 2x the stop distance, \
are rejected by code. Always include a realistic stop AND target.

Respond with ONLY a JSON object, no markdown fences, matching:
{
  "market_view": "1-3 sentences on regime and what you're doing about it",
  "actions": [
    {"action": "BUY",  "symbol": "XYZ", "stop": 123.4, "target": 150.0,
     "thesis": "one or two sentences", "confidence": 0.0-1.0},
    {"action": "SELL", "symbol": "XYZ", "reason": "why exiting now"},
    {"action": "ADJUST", "symbol": "XYZ", "stop": 130.0, "target": 155.0,
     "reason": "why moving levels"}
  ],
  "watch_next": ["symbols you want extra data on next cycle (max 5)"]
}
An empty actions list is a valid, respectable answer."""


def build_prompt(market: str, currency: str, regime: list[dict], candidates: list[dict],
                 portfolio: dict, stats: dict, recent_trades: list[dict],
                 memo: str | None, caps: dict, halt_reason: str | None) -> str:
    recent = [
        {"symbol": t["symbol"], "pnl": t["pnl"], "pnl_pct": t["pnl_pct"],
         "exit_reason": t["exit_reason"], "thesis": t["thesis"]}
        for t in recent_trades
    ]
    parts = [
        f"MARKET: {market} (all prices in {currency}). Today's decision cycle.",
        f"\nHARD RISK CAPS (enforced by code):\n{json.dumps(caps, indent=1)}",
        f"\nMARKET REGIME (benchmarks):\n{json.dumps(regime, indent=1)}",
        f"\nYOUR PORTFOLIO:\n{json.dumps(portfolio, indent=1)}",
        f"\nYOUR TRACK RECORD:\n{json.dumps(stats, indent=1)}",
        f"\nYOUR LAST {len(recent)} CLOSED TRADES:\n{json.dumps(recent, indent=1)}",
    ]
    if memo:
        parts.append(f"\nYOUR LATEST SELF-REVIEW MEMO (written by you, follow it):\n{memo}")
    if halt_reason:
        parts.append(f"\nACTIVE RESTRICTION: {halt_reason}\n"
                     "You may only SELL or ADJUST this cycle. No BUY actions.")
    parts.append(f"\nCANDIDATE DATA ({len(candidates)} symbols):\n{json.dumps(candidates, indent=1)}")
    parts.append("\nProduce your action plan JSON now.")
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in AI response: {text[:200]}")
    return json.loads(text[start:end + 1])


class PortfolioManagerAI:
    def __init__(self, api_key: str, model: str, max_tokens: int = 4000):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens

    def decide(self, prompt: str) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        decision = _extract_json(text)
        decision.setdefault("actions", [])
        decision.setdefault("market_view", "")
        decision.setdefault("watch_next", [])
        log.info("AI view: %s | %d actions", decision["market_view"], len(decision["actions"]))
        return decision

    def self_review(self, market: str, stats: dict, recent_trades: list[dict],
                    prev_memo: str | None) -> str:
        """Weekly reflection: what worked, what didn't, rules for next week."""
        prompt = (
            f"You are reviewing your own {market} swing-trading performance.\n"
            f"STATS: {json.dumps(stats)}\n"
            f"RECENT CLOSED TRADES (thesis vs outcome): "
            f"{json.dumps([{k: t[k] for k in ('symbol','pnl','pnl_pct','exit_reason','thesis')} for t in recent_trades])}\n"
            + (f"YOUR PREVIOUS MEMO: {prev_memo}\n" if prev_memo else "")
            + "\nWrite a candid self-review memo (max 250 words): patterns in wins/losses, "
              "mistakes to stop repeating, adjustments for next week. This memo will be "
              "injected into your future decision prompts, so make every line actionable. "
              "Plain text only."
        )
        resp = self.client.messages.create(
            model=self.model, max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
