"""V2 configuration.

Two kinds of settings:
  1. Tunables  — normal env vars.
  2. HARD CAPS — risk limits enforced in code. The AI portfolio manager
     receives these as constraints and the risk layer clamps/rejects any
     decision that violates them. They cannot be relaxed by the AI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: str) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HardCaps:
    """Non-negotiable risk limits. The AI operates freely INSIDE these."""

    max_position_pct: float = 0.20        # one position <= 20% of equity
    max_risk_per_trade_pct: float = 0.015 # entry->stop loss <= 1.5% of equity
    max_open_positions: int = 6           # per market
    max_new_entries_per_day: int = 2      # per market
    daily_loss_halt_pct: float = 0.03     # day P&L <= -3% -> no new buys today
    max_drawdown_kill_pct: float = 0.10   # equity -10% from peak -> flatten + halt
    min_stop_distance_pct: float = 0.01   # stop must be at least 1% away (no fake stops)
    max_stop_distance_pct: float = 0.15   # and at most 15% away
    long_only: bool = True                # paper phase: no shorts, no leverage


@dataclass(frozen=True)
class V2Config:
    # AI
    anthropic_api_key: str = ""
    # When set, all AI calls go through OpenRouter instead of the Anthropic
    # API. anthropic_model must then be an OpenRouter slug, e.g.
    # "anthropic/claude-sonnet-5".
    openrouter_api_key: str = ""
    anthropic_model: str = "claude-fable-5"

    # Markets
    trade_us: bool = True
    trade_india: bool = True

    # Capital (virtual ledger starting cash per market)
    us_start_cash: float = 3000.0
    india_start_cash: float = 250000.0  # INR

    # Alpaca paper (optional — if absent, US also uses the virtual ledger)
    alpaca_key: str = ""
    alpaca_secret: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # Ops
    db_path: str = "botv2.db"
    log_level: str = "INFO"
    ai_decision_max_tokens: int = 4000
    # 0 = AI decides deployment freely. >0 = aim to be this fraction invested
    # when the regime is constructive. A preference, never a licence to lower
    # entry standards - the hard caps and 2R rule still reject bad trades.
    target_deployment_pct: float = 0.0
    # Held low deliberately: this task is structured extraction, and an
    # unbounded reasoning budget starves the answer (see 2026-08-26).
    ai_reasoning_effort: str = "low"

    caps: HardCaps = field(default_factory=HardCaps)


def load_config() -> V2Config:
    caps = HardCaps(
        max_position_pct=_f("CAP_MAX_POSITION_PCT", "0.20"),
        max_risk_per_trade_pct=_f("CAP_MAX_RISK_PER_TRADE_PCT", "0.015"),
        max_open_positions=_i("CAP_MAX_OPEN_POSITIONS", "6"),
        max_new_entries_per_day=_i("CAP_MAX_NEW_ENTRIES_PER_DAY", "2"),
        daily_loss_halt_pct=_f("CAP_DAILY_LOSS_HALT_PCT", "0.03"),
        max_drawdown_kill_pct=_f("CAP_MAX_DRAWDOWN_KILL_PCT", "0.10"),
    )
    # Sanity-check the caps themselves so a typo can't disable safety.
    assert 0 < caps.max_position_pct <= 0.34, "max_position_pct must be (0, 0.34]"
    assert 0 < caps.max_risk_per_trade_pct <= 0.02, "risk per trade capped at 2%"
    assert 0 < caps.max_drawdown_kill_pct <= 0.15, "kill switch must be <= 15%"
    assert 0 < caps.daily_loss_halt_pct <= 0.05, "daily loss halt must be (0, 5%]"
    assert 1 <= caps.max_open_positions <= 10, "max_open_positions must be 1-10"
    assert 1 <= caps.max_new_entries_per_day <= 5, "max_new_entries_per_day must be 1-5"

    return V2Config(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-fable-5"),
        trade_us=_b("TRADE_US", "true"),
        trade_india=_b("TRADE_INDIA", "true"),
        us_start_cash=_f("US_START_CASH", "3000"),
        india_start_cash=_f("INDIA_START_CASH", "250000"),
        alpaca_key=os.getenv("ALPACA_KEY", os.getenv("APCA_API_KEY_ID", "")),
        alpaca_secret=os.getenv("ALPACA_SECRET", os.getenv("APCA_API_SECRET_KEY", "")),
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        db_path=os.getenv("BOTV2_DB", "botv2.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        ai_decision_max_tokens=_i("AI_DECISION_MAX_TOKENS", "4000"),
        target_deployment_pct=_f("TARGET_DEPLOYMENT_PCT", "0"),
        ai_reasoning_effort=os.getenv("AI_REASONING_EFFORT", "low"),
        caps=caps,
    )
