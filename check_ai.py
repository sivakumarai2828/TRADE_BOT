"""Quick check that the configured AI backend is reachable.

Run from the TRADE_BOT_V2 directory with the .env loaded:
    set -a && . ./.env && set +a && /home/konda/venv311/bin/python3 check_ai.py

Prints the active backend and model, makes one tiny API call, and reports
WORKING or FAILED. Costs a fraction of a cent. Use after rotating an API key
or changing ANTHROPIC_MODEL.
"""
from botv2.ai_pm import PortfolioManagerAI
from botv2.config import load_config

cfg = load_config()
print(f"backend : {'OpenRouter' if cfg.openrouter_api_key else 'Anthropic API'}")
print(f"model   : {cfg.anthropic_model}")
try:
    ai = PortfolioManagerAI(cfg.anthropic_api_key, cfg.anthropic_model, 200,
                            cfg.openrouter_api_key)
    reply = ai._call(None, "Reply with exactly: OK", 50).strip()
    print(f"response: {reply[:80]!r}")
    print("RESULT  : WORKING")
except Exception as exc:
    print(f"RESULT  : FAILED -> {type(exc).__name__}: {str(exc)[:300]}")
    raise SystemExit(1)
