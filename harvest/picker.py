"""Claude-powered stock/crypto picker for the harvest portfolios.

pick_long_term()  — selects best candidate for 60-90 day hold targeting +30%
pick_compound()   — selects best candidate for 2-4 week hold targeting +15%

Both return a PickResult(symbol, reason) or None if no good candidate found.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic


@dataclass
class PickResult:
    symbol: str
    reason: str
    target_pct: float
    max_hold_days: int


def pick_long_term(
    api_key: str,
    candidates: list[str],
    market_regime: str,
    bot_type: str,           # 'day' | 'crypto'
    amount: float,
    model: str = "claude-sonnet-4-6",
) -> Optional[PickResult]:
    """Ask Claude to pick the best long-term candidate from the list.

    Returns None if no strong candidate found (Claude says SKIP).
    """
    if not candidates or not api_key:
        return None

    client = Anthropic(api_key=api_key)
    asset_type = "crypto asset (BTC/USD or ETH/USD only)" if bot_type == "crypto" else "US stock"

    prompt = (
        f"I have ${amount:.2f} in day-trading profits to invest long-term.\n"
        f"Bot type: {bot_type} | Market regime: {market_regime}\n"
        f"Candidates: {', '.join(candidates)}\n\n"
        f"Select the single best {asset_type} from the candidates for a 60-90 day hold "
        f"targeting at least 30% gain. Consider: trend strength, recent momentum, "
        f"volume trends, support levels, and market conditions.\n"
        f"If market is bearish or no strong candidate exists, return SKIP.\n"
        "Respond with JSON only:\n"
        '{"symbol": "AAPL", "reason": "one sentence", "confidence": 0.82}\n'
        'or {"symbol": "SKIP", "reason": "why skipping", "confidence": 0.0}'
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=150,
            temperature=0,
            system=[{
                "type": "text",
                "text": (
                    "You are a portfolio analyst selecting long-term investment candidates. "
                    "Respond ONLY with valid JSON — no markdown, no extra text."
                ),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        parsed = json.loads(text)
        symbol = str(parsed.get("symbol", "SKIP")).upper()
        reason = str(parsed.get("reason", ""))
        confidence = float(parsed.get("confidence", 0.0))

        if symbol == "SKIP" or confidence < 0.6:
            logging.info("Harvest long-term picker: SKIP (%s)", reason)
            return None

        logging.info("Harvest long-term pick: %s (conf=%.2f) — %s", symbol, confidence, reason)
        return PickResult(symbol=symbol, reason=reason, target_pct=30.0, max_hold_days=90)

    except Exception as exc:
        logging.warning("Harvest long-term picker failed: %s", exc)
        return None


def pick_compound(
    api_key: str,
    candidates: list[str],
    market_regime: str,
    bot_type: str,
    amount: float,
    model: str = "claude-sonnet-4-6",
) -> Optional[PickResult]:
    """Ask Claude to pick the best short-term (2-4 week) compound candidate."""
    if not candidates or not api_key:
        return None

    client = Anthropic(api_key=api_key)
    asset_type = "crypto asset" if bot_type == "crypto" else "US stock"

    prompt = (
        f"I have ${amount:.2f} in harvest profits to compound short-term.\n"
        f"Bot type: {bot_type} | Market regime: {market_regime}\n"
        f"Candidates: {', '.join(candidates)}\n\n"
        f"Select the best {asset_type} for a 2-4 week hold targeting 15% gain. "
        f"Prioritize strong near-term catalysts, breakout setups, or oversold recovery plays.\n"
        f"If no strong setup exists, return SKIP.\n"
        "Respond with JSON only:\n"
        '{"symbol": "TSLA", "reason": "one sentence", "confidence": 0.75}\n'
        'or {"symbol": "SKIP", "reason": "why skipping", "confidence": 0.0}'
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=150,
            temperature=0,
            system=[{
                "type": "text",
                "text": (
                    "You are a short-term trading analyst. "
                    "Respond ONLY with valid JSON — no markdown, no extra text."
                ),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        parsed = json.loads(text)
        symbol = str(parsed.get("symbol", "SKIP")).upper()
        reason = str(parsed.get("reason", ""))
        confidence = float(parsed.get("confidence", 0.0))

        if symbol == "SKIP" or confidence < 0.6:
            logging.info("Harvest compound picker: SKIP (%s)", reason)
            return None

        logging.info("Harvest compound pick: %s (conf=%.2f) — %s", symbol, confidence, reason)
        return PickResult(symbol=symbol, reason=reason, target_pct=15.0, max_hold_days=30)

    except Exception as exc:
        logging.warning("Harvest compound picker failed: %s", exc)
        return None
