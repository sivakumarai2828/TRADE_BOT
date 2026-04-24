"""Claude AI validation for trade signals — returns structured JSON decision."""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from anthropic import Anthropic

_SYSTEM = (
    "You are an intraday stock trading analyst. "
    "You will receive current technical indicators AND a 4-week historical summary. "
    "Use both the recent history and current conditions to assess trade quality. "
    "Respond ONLY with a JSON object — no markdown, no extra text. "
    'Fields: "decision" (BUY, SELL, or HOLD), "confidence" (0.0–1.0), "reason" (one short sentence). '
    'Example: {"decision": "BUY", "confidence": 0.78, "reason": "4-week uptrend intact, RSI pullback near EMA with rising volume"}'
)

CONFIDENCE_THRESHOLD = 0.65


@dataclass
class AIDecision:
    decision: str       # BUY | SELL | HOLD
    confidence: float   # 0.0–1.0
    reason: str


class AIValidator:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        self._client = Anthropic(api_key=api_key, timeout=20.0, max_retries=1)
        self._model = model

    def validate(
        self,
        symbol: str,
        price: float,
        ema: float,
        rsi: float,
        volume: float,
        avg_volume: float,
        trend: str,
        rule_signal: str,
        weekly_context: dict | None = None,
        history_context: dict | None = None,
    ) -> AIDecision:
        if not self._client.api_key:
            return AIDecision("HOLD", 0.0, "No API key")

        # Current conditions
        prompt = (
            f"Stock={symbol}, price=${price:.2f}, EMA(50)=${ema:.2f}, "
            f"RSI={rsi:.1f}, volume={volume:,.0f}, avg_volume={avg_volume:,.0f}, "
            f"trend={trend}, rule_signal={rule_signal}.\n"
        )

        # 4-week price history
        if weekly_context:
            wk = weekly_context
            weekly_str = " | ".join(
                f"W{i+1}: {r:+.1f}%" for i, r in enumerate(wk.get("weekly_returns", []))
            )
            prompt += (
                f"\n4-week price history:"
                f"\n  Weekly returns: {weekly_str}"
                f"\n  4-week total return: {wk.get('four_week_return_pct', 0):+.1f}%"
                f"\n  Range: low=${wk.get('four_week_low', 0):.2f} → high=${wk.get('four_week_high', 0):.2f}"
                f"\n  Current price in 4-week range: {wk.get('position_in_range_pct', 50):.0f}%"
                f" (0%=at low, 100%=at high)"
                f"\n  Support: ${wk.get('support', 0):.2f}  Resistance: ${wk.get('resistance', 0):.2f}"
                f"\n  Volume trend: {wk.get('volume_trend', 'unknown')}\n"
            )

        # Bot trade history from Supabase
        if history_context:
            stats = history_context.get("symbol_stats")
            recent = history_context.get("recent_trades", [])
            sessions = history_context.get("market_sessions", [])

            if stats and stats.get("total_trades", 0) > 0:
                prompt += (
                    f"\nThis bot's track record on {symbol}:"
                    f"\n  {stats['total_trades']} trades — "
                    f"{stats['wins']}W / {stats['losses']}L "
                    f"({stats['win_rate']:.0f}% win rate), "
                    f"avg PnL ${stats['avg_pnl']:+.2f}, "
                    f"total PnL ${stats['total_pnl']:+.2f}\n"
                )
                if recent:
                    outcomes = []
                    for t in recent:
                        outcome = "WIN" if t["pnl"] > 0 else "LOSS"
                        outcomes.append(f"{outcome} ${t['pnl']:+.2f} ({t['exit_reason']})")
                    prompt += f"  Last {len(recent)} trades: {' | '.join(outcomes)}\n"
            else:
                prompt += f"\nNo prior bot trades recorded for {symbol}.\n"

            if sessions:
                prompt += "\nRecent market sessions (last 3 days):\n"
                for s in sessions:
                    prompt += (
                        f"  {s['trade_date']}: SPY {s['spy_return_pct']:+.2f}% "
                        f"({s['market_regime']}) — "
                        f"bot: {s['wins']}W/{s['losses']}L PnL ${s['daily_pnl']:+.2f}\n"
                    )

        prompt += "\nBased on all the above (current technicals, 4-week trend, bot history, market regime) — BUY, SELL, or HOLD?"

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=120,
                temperature=0,
                system=[{
                    "type": "text",
                    "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
            parsed = json.loads(text)
            decision = str(parsed.get("decision", "HOLD")).strip().upper()
            confidence = float(parsed.get("confidence", 0.5))
            reason = str(parsed.get("reason", ""))

            if decision not in {"BUY", "SELL", "HOLD"}:
                decision = "HOLD"

            if confidence < CONFIDENCE_THRESHOLD and decision != "HOLD":
                logging.info("AI %s: %s → HOLD (conf=%.2f < %.2f)",
                             symbol, decision, confidence, CONFIDENCE_THRESHOLD)
                decision = "HOLD"

            logging.info("AI [%s]: %s conf=%.2f — %s", symbol, decision, confidence, reason)
            return AIDecision(decision, confidence, reason)

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logging.warning("AI parse error for %s: %s", symbol, exc)
            return AIDecision("HOLD", 0.0, f"parse error: {exc}")
        except Exception as exc:
            logging.warning("AI call failed for %s: %s", symbol, exc)
            return AIDecision("HOLD", 0.0, f"API error: {exc}")
