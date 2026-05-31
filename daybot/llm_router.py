"""LLM Router — centralised model selection.

Routing strategy (from memory/llm_routing_strategy.md):
  claude-sonnet-4-6        → critical decisions (evening analysis only)
  deepseek-v4-flash/NVIDIA → reasoning-heavy tasks (India, options)
  llama-3.3-70b/OpenRouter → simple tasks (premarket, signals)
  claude-haiku-4-5         → fallback when any free LLM fails
  pure Python              → exits, risk checks, rule engine
"""
from __future__ import annotations
import json
import logging
import os
import re


def _claude_haiku_chat(prompt: str, system: str = "", max_tokens: int = 1500) -> str:
    """Claude Haiku fallback — used when free LLMs fail.
    System prompt is cached (5-min ephemeral TTL) — 90% cost reduction on input tokens
    when the same system prompt is reused across rapid consecutive calls.
    """
    from anthropic import Anthropic
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""), timeout=30.0, max_retries=1)
    messages_arg = [{"role": "user", "content": prompt}]
    kwargs = dict(model="claude-haiku-4-5", max_tokens=max_tokens, temperature=0, messages=messages_arg)
    if system:
        # Cache system prompt — 5-min TTL matches typical polling intervals (5-min cycles).
        # Cache write: 1.25× base cost once. Cache read: 0.10× base — 90% saving on repeated calls.
        kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    resp = client.messages.create(**kwargs)
    return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()


# ---------------------------------------------------------------------------
# DeepSeek via NVIDIA NIM (reasoning-heavy) → Haiku fallback
# Model: deepseek-v4-flash (replaces retired deepseek-r1-distill-llama-70b)
# ---------------------------------------------------------------------------

_NVIDIA_MODEL = "deepseek-ai/deepseek-v4-flash"


def deepseek_chat(prompt: str, system: str = "", max_tokens: int = 1500) -> str:
    """Call DeepSeek via NVIDIA NIM. Falls back to Claude Haiku on failure."""
    api_key = os.getenv("NVIDIA_API_KEY", "")
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=api_key)
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            response = client.chat.completions.create(
                model=_NVIDIA_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            logging.warning("DeepSeek failed (%s) — falling back to Claude Haiku", exc)
    else:
        logging.warning("NVIDIA_API_KEY not set — using Claude Haiku")

    return _claude_haiku_chat(prompt, system=system, max_tokens=max_tokens)


def deepseek_json(prompt: str, system: str = "", max_tokens: int = 1500) -> dict | list | None:
    """Call DeepSeek, parse JSON. Falls back to Claude Haiku on failure."""
    try:
        text = deepseek_chat(prompt, system=system, max_tokens=max_tokens)
        # Strip <think>...</think> blocks (DeepSeek R1 chain-of-thought)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
            text = m.group(1).strip() if m else re.sub(r"```[a-z]*", "", text).strip()
        start = text.find("{") if "{" in text else text.find("[")
        if start == -1:
            return None
        end = text.rfind("}") + 1 if "{" in text else text.rfind("]") + 1
        return json.loads(text[start:end])
    except Exception as exc:
        logging.warning("deepseek_json parse failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# OpenRouter Llama 3.3 70B (simple tasks, free) → Haiku fallback
# ---------------------------------------------------------------------------

def llama_chat(prompt: str, system: str = "", max_tokens: int = 400) -> str:
    """Call Llama via OpenRouter. Falls back to Claude Haiku on 429 or failure."""
    import requests, time
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if api_key:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(3):
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "meta-llama/llama-3.3-70b-instruct:free", "messages": messages,
                      "max_tokens": max_tokens, "temperature": 0},
                timeout=30,
            )
            if resp.status_code == 429:
                wait = 10 * (2 ** attempt)
                logging.warning("OpenRouter 429 — waiting %ds (attempt %d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            try:
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                logging.warning("OpenRouter error (%s) — falling back to Claude Haiku", exc)
                break
        else:
            logging.warning("OpenRouter rate limited after 3 retries — falling back to Claude Haiku")
    else:
        logging.warning("OPENROUTER_API_KEY not set — using Claude Haiku")

    return _claude_haiku_chat(prompt, system=system, max_tokens=max_tokens)


def llama_json(prompt: str, system: str = "", max_tokens: int = 400) -> dict | list | None:
    """Call Llama, parse JSON. Falls back to Claude Haiku on failure."""
    try:
        text = llama_chat(prompt, system=system, max_tokens=max_tokens)
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
            text = m.group(1).strip() if m else re.sub(r"```[a-z]*", "", text).strip()
        start = text.find("{") if "{" in text else text.find("[")
        if start == -1:
            return None
        end = text.rfind("}") + 1 if "{" in text else text.rfind("]") + 1
        return json.loads(text[start:end])
    except Exception as exc:
        logging.warning("llama_json parse failed: %s", exc)
        return None
