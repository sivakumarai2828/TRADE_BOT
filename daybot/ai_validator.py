"""AI validation for trade signals — OpenRouter (free) primary, Claude fallback."""
from __future__ import annotations
import json
import logging
import os
import urllib.request
import urllib.parse
from dataclasses import dataclass
from anthropic import Anthropic, BadRequestError as _AnthropicBadRequest

# Mutable container — avoids `global` keyword inside methods
_state = {"credits_exhausted": False}

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

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


import time as _time
_ai_cache: dict = {}   # symbol → (AIDecision, timestamp) — 10 min TTL vs OpenRouter rate limit
_AI_CACHE_TTL = 600


def _send_credit_alert() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        msg = (
            "🚨 Anthropic API credits exhausted!\n"
            "Day bot switched to OpenRouter free LLM (Llama 3.3 70B).\n"
            "Top up: console.anthropic.com/settings/billing"
        )
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10
        )
    except Exception as exc:
        logging.warning("Credit alert Telegram send failed: %s", exc)


def _call_openrouter(prompt: str, symbol: str) -> AIDecision:
    """Call OpenRouter free model (Llama 3.3 70B). Retries on 429. Raises on failure."""
    import time as _time
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set")

    payload = json.dumps({
        "model": _OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 150,
        "temperature": 0,
    }).encode()

    for attempt in range(3):
        req = urllib.request.Request(
            _OPENROUTER_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://trade-bot",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (2 ** attempt)
                logging.warning("OpenRouter 429 [%s] — waiting %ds (attempt %d/3)", symbol, wait, attempt + 1)
                _time.sleep(wait)
                continue
            raise

    text = body["choices"][0]["message"]["content"].strip()
    # Strip markdown fences if model wraps JSON
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    parsed = json.loads(text)
    decision = str(parsed.get("decision", "HOLD")).strip().upper()
    if decision not in {"BUY", "SELL", "HOLD"}:
        decision = "HOLD"
    confidence = float(parsed.get("confidence", 0.5))
    reason = str(parsed.get("reason", ""))

    if confidence < CONFIDENCE_THRESHOLD and decision != "HOLD":
        decision = "HOLD"

    logging.info("OpenRouter [%s]: %s conf=%.2f — %s", symbol, decision, confidence, reason)
    return AIDecision(decision, confidence, reason)


class AIValidator:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5") -> None:
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
        prompt = self._build_prompt(
            symbol, price, ema, rsi, volume, avg_volume, trend, rule_signal,
            weekly_context, history_context,
        )

        # 0. Cache hit — reuse decision for 10 min to avoid OpenRouter rate limit
        cached = _ai_cache.get(symbol)
        if cached:
            decision, ts = cached
            if _time.time() - ts < _AI_CACHE_TTL:
                return decision

        # 1. Try OpenRouter free model first
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        if openrouter_key:
            try:
                result = _call_openrouter(prompt, symbol)
                _ai_cache[symbol] = (result, _time.time())
                return result
            except Exception as exc:
                logging.warning("OpenRouter failed for %s: %s — falling back to Claude", symbol, exc)

        # 2. Claude fallback (Haiku — cheapest)
        if not self._client.api_key or _state["credits_exhausted"]:
            return AIDecision("HOLD", 0.0, "no AI available — rule engine only")

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=120,
                temperature=0,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
            parsed = json.loads(text)
            decision = str(parsed.get("decision", "HOLD")).strip().upper()
            if decision not in {"BUY", "SELL", "HOLD"}:
                decision = "HOLD"
            confidence = float(parsed.get("confidence", 0.5))
            reason = str(parsed.get("reason", ""))
            if confidence < CONFIDENCE_THRESHOLD and decision != "HOLD":
                decision = "HOLD"
            logging.info("Claude-fallback [%s]: %s conf=%.2f — %s", symbol, decision, confidence, reason)
            return AIDecision(decision, confidence, reason)

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logging.warning("AI parse error for %s: %s", symbol, exc)
            return AIDecision("HOLD", 0.0, f"parse error: {exc}")
        except _AnthropicBadRequest as exc:
            if "credit balance" in str(exc).lower() and not _state["credits_exhausted"]:
                _state["credits_exhausted"] = True
                logging.error("Anthropic credits exhausted — rule engine only")
                _send_credit_alert()
            return AIDecision("HOLD", 0.0, "credits exhausted — rule engine only")
        except Exception as exc:
            logging.warning("Claude call failed for %s: %s", symbol, exc)
            return AIDecision("HOLD", 0.0, f"API error: {exc}")

    def _build_prompt(
        self, symbol, price, ema, rsi, volume, avg_volume, trend, rule_signal,
        weekly_context, history_context,
    ) -> str:
        prompt = (
            f"Stock={symbol}, price=${price:.2f}, EMA(50)=${ema:.2f}, "
            f"RSI={rsi:.1f}, volume={volume:,.0f}, avg_volume={avg_volume:,.0f}, "
            f"trend={trend}, rule_signal={rule_signal}.\n"
        )
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
                    outcomes = [
                        f"{'WIN' if t['pnl'] > 0 else 'LOSS'} ${t['pnl']:+.2f} ({t['exit_reason']})"
                        for t in recent
                    ]
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
        prompt += "\nBased on all the above — BUY, SELL, or HOLD?"
        return prompt
