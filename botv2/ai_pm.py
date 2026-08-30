"""AI Portfolio Manager — Claude runs the book.

The model receives everything a human PM would want: market regime, full
candidate data, its portfolio with live P&L, its own trade history and stats,
and its latest self-review memo. It returns a JSON action plan. Code enforces
the hard caps; inside them the AI is free.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("botv2.ai_pm")

SYSTEM_PROMPT = """You are the portfolio manager of a swing-trading book. You have FULL \
discretion within the hard risk caps provided — you pick symbols, entries, stops, targets, \
position intent, exits, and sizing. Your job is to keep the book INVESTED in the best \
opportunities available, not to wait for perfection.

Principles you are evaluated on:
- Quality first, but selectivity is not inaction: pick the highest-expectancy \
candidates available and deploy into them. Chasing extended, broken or overbought \
names remains forbidden - that is what quality means here.
- Asymmetry: only enter when your realistic target is at least 2x the distance to your stop.
- Regime: only a clearly BEARISH benchmark (below a falling 50DMA) justifies sitting \
in cash. A choppy, indecisive or merely non-trending tape does NOT.
- Learn from your own record: your trade history, stats, and your latest self-review memo \
are included. Do not repeat documented mistakes.
- Stops are honored by code automatically. Set them where the thesis is invalidated, not \
at arbitrary percentages.
- You may raise stops on winners (never lower them), take full profits, or cut \
losers early with a reason. Partial exits are not supported — SELL closes the whole position.
- BUY orders without a target above entry, or with reward less than 2x the stop \
distance, are rejected by code. BEFORE submitting any BUY, compute it yourself: \
R = (target - entry) / (entry - stop). If R < 2.0 the order is WASTED - it gets \
rejected and that capital stays idle. When a setup you like gives R < 2.0, either \
place the stop tighter at real structure (e.g. low_20d), or raise the target to the \
next genuine resistance if realistic, or drop the name and pick another. NEVER put a \
target near the entry price. State the R you calculated in each BUY thesis.
- BUY orders within 3 days of a scheduled earnings report are rejected by code. Candidate \
data includes days_to_earnings where known — plan entries around it, and consider earnings \
risk on positions you already hold.
- Candidate data includes stage2_uptrend (Minervini trend-template pass), rs_3m_vs_benchmark \
(3-month relative strength vs the market), and low_20d (recent swing low, a natural stop \
reference). The regime block includes universe breadth. These are context, not rules — but \
your documented losses came from entries that failed exactly these checks.

- CAPITAL DEPLOYMENT IS A REQUIREMENT, not a preference. Your portfolio reports pct_deployed and target_pct_deployed. Unless the benchmark regime is clearly bearish (benchmark below its 50DMA and falling) or a halt is active, you MUST work toward the target every cycle: rank every candidate that satisfies the hard caps and BUY the best available, up to the daily entry limit. Do NOT hold cash because no candidate is 'perfect' - a merely good setup that clears the code caps beats idle cash, and your own screen is stricter than the caps require. Finishing below target is acceptable ONLY if the regime is bearish, a halt is active, or no candidate can satisfy the CODE caps (2R reward/risk, stop distance, no earnings within 3 days); say which one in market_view.

SETUP LABEL (required on BUY and WATCH). Classify every entry as exactly one of:
- BREAKOUT     price is clearing a defined resistance level
- PULLBACK     strong trend has pulled back into support (EMA20 / SMA50 / prior base)
- CONTINUATION already trending, no fresh breakout and no clean pullback
The label is recorded against the trade so performance can be compared by setup
type. Choose the one that genuinely describes the entry; do not default to one.

WATCH - use this when a stock is strong but the CURRENT price is not a good entry.
A great stock is not automatically a great trade. WATCH records an ideal entry;
code arms the trigger, revalidates everything when price arrives, and buys only
if it still passes. A WATCH expires after 10 trading sessions. Requirements:
stop < ideal_entry < target, and at least 2R measured AT THE IDEAL ENTRY.
Prefer WATCH over a marginal BUY at a stretched price.

REPLACE - use when the book is full and a new opportunity is clearly better than
a specific holding. Name both symbols. Code refuses the swap if the old position
already has its stop at or above entry (it cannot lose, so it is not given up),
if it is younger than 5 sessions, or if the new setup does not beat the old
one's planned R by at least 0.5R. Rotate to upgrade the book, never to churn it.

HOLD - an explicit statement that an existing position remains valid. Optional,
but it records your reasoning for later review.

Respond with ONLY a JSON object, no markdown fences, matching:
{
  "market_view": "1-3 sentences on regime and what you're doing about it",
  "actions": [
    {"action": "BUY",  "symbol": "XYZ", "setup": "PULLBACK", "stop": 123.4,
     "target": 150.0, "thesis": "one or two sentences incl. the R you computed",
     "confidence": 0.0-1.0},
    {"action": "WATCH", "symbol": "ABC", "setup": "BREAKOUT", "ideal_entry": 205.0,
     "stop": 194.0, "target": 230.0, "thesis": "why, and what you are waiting for"},
    {"action": "REPLACE", "symbol": "NEW", "replace": "OLD", "setup": "PULLBACK",
     "stop": 90.0, "target": 120.0, "thesis": "why NEW is materially better than OLD"},
    {"action": "HOLD", "symbol": "DEF", "reason": "thesis intact"},
    {"action": "SELL", "symbol": "XYZ", "reason": "why exiting now"},
    {"action": "ADJUST", "symbol": "XYZ", "stop": 130.0, "target": 155.0,
     "reason": "why moving levels"}
  ],
  "watch_next": ["symbols you want extra data on next cycle (max 5)"]
}
An empty actions list is acceptable ONLY when you are at or above target deployment, or the regime is bearish, or a halt is active. If no candidate is worth buying at today's price, WATCH the best of them rather than returning nothing."""


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
    """Talks to Claude directly, or through OpenRouter when a key is provided.

    OpenRouter takes an OpenAI-style chat/completions payload, so the system
    prompt is sent as the first message rather than a separate field.
    """

    OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str, max_tokens: int = 4000,
                 openrouter_key: str = ""):
        self.model = model
        self.max_tokens = max_tokens
        self.openrouter_key = openrouter_key
        self.client = None
        if not openrouter_key:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
        log.info("AI backend: %s | model: %s",
                 "OpenRouter" if openrouter_key else "Anthropic API", model)

    def _call(self, system: str | None, user: str, max_tokens: int) -> str:
        """One completion, returned as plain text. Raises on any API error."""
        if self.openrouter_key:
            import requests
            msgs = ([{"role": "system", "content": system}] if system else [])
            msgs.append({"role": "user", "content": user})
            r = requests.post(
                self.OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self.openrouter_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "messages": msgs,
                      "max_tokens": max_tokens,
                      # Sonnet 5 enables extended reasoning by default. On
                      # 2026-08-26 that consumed the entire completion budget
                      # (4000 reasoning tokens, 0 content) and every cycle
                      # failed with an empty response. This task is structured
                      # extraction, not deep reasoning - turn it off.
                      "reasoning": {"enabled": False}},
                timeout=180,
            )
            r.raise_for_status()
            data = r.json()
            if "choices" not in data:
                raise RuntimeError(f"OpenRouter returned no choices: {str(data)[:300]}")
            choice = data["choices"][0]
            content = choice.get("message", {}).get("content") or ""
            if not content:
                raise RuntimeError(
                    f"OpenRouter returned empty content "
                    f"(finish_reason={choice.get('finish_reason')}, "
                    f"reasoning_tokens="
                    f"{data.get('usage', {}).get('completion_tokens_details', {}).get('reasoning_tokens')})")
            return content
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens,
            **({"system": system} if system else {}),
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    ATTEMPTS = 3  # a transient API error must not silently skip a trading day

    def decide(self, prompt: str) -> dict:
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                text = self._call(SYSTEM_PROMPT, prompt, self.max_tokens)
                decision = _extract_json(text)
                break
            except Exception as exc:
                log.warning("AI decision attempt %d/%d failed: %s", attempt, self.ATTEMPTS, exc)
                if attempt == self.ATTEMPTS:
                    raise
                time.sleep(15 * attempt)
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
        return self._call(None, prompt, 1000).strip()
