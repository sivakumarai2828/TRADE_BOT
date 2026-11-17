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

# The live service sets BOTV2_ENV_FILE=.env.live so it never picks up the
# paper bot's .env (e.g. its Supabase journal) for keys it doesn't set itself.
load_dotenv(os.getenv("BOTV2_ENV_FILE", ".env"))


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
    # "trade" = the bot runs an India paper book; "suggest" = it never opens an
    # India position and publishes a ranked daily list of buy suggestions instead.
    india_mode: str = "trade"
    us_mode: str = "trade"            # same choice for the US (virtual ledger only)

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

    # ── Desk gates (branch v3) ──
    # Skeptic: second model that argues against every new entry. Fail-closed.
    skeptic_enabled: bool = True
    # Empty = same model as the PM. A different model gives a more
    # independent second opinion (e.g. "anthropic/claude-sonnet-5").
    skeptic_model: str = ""
    # "telegram" = every new entry waits for an Approve tap; "off" = fill
    # immediately (V2 behaviour). Exits are never gated.
    approval_mode: str = "telegram"
    approval_timeout_min: float = 390.0   # capped at the market close
    # Cancel an approved entry if price has run this far above the proposal.
    approval_max_drift_pct: float = 0.015

    # ── Broker trading (US via Alpaca). Off = virtual PaperLedger. ──
    # On: orders go to the Alpaca account at alpaca_base_url and the broker
    # is the source of truth. Paper URL first; the live URL also needs
    # live_real_money=True. See check_broker_config().
    broker_trading: bool = False
    live_real_money: bool = False
    max_order_usd: float = 150.0          # hard ceiling on any single buy

    caps: HardCaps = field(default_factory=HardCaps)


def _market_mode(name: str) -> str:
    mode = os.getenv(name, "trade").strip().lower()
    if mode not in ("trade", "suggest"):
        raise SystemExit(f"{name} must be trade or suggest, got {mode!r}")
    return mode


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
        india_mode=_market_mode("INDIA_MODE"),
        us_mode=_market_mode("US_MODE"),
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
        skeptic_enabled=_b("SKEPTIC_ENABLED", "true"),
        skeptic_model=os.getenv("SKEPTIC_MODEL", ""),
        approval_mode=os.getenv("APPROVAL_MODE", "telegram").strip().lower(),
        approval_timeout_min=_f("APPROVAL_TIMEOUT_MIN", "390"),
        approval_max_drift_pct=_f("APPROVAL_MAX_DRIFT_PCT", "0.015"),
        broker_trading=_b("BROKER_TRADING", "false"),
        live_real_money=_b("LIVE_REAL_MONEY", "false"),
        max_order_usd=_f("MAX_ORDER_USD", "150"),
        caps=caps,
    )


def check_broker_config(cfg: V2Config) -> list[str]:
    """Reasons the bot must NOT start in broker mode. Empty list = OK.

    Every rule here guards real money, so a failure stops the process
    rather than falling back to something weaker."""
    if not cfg.broker_trading:
        return []
    from .alpaca_ledger import LIVE_URL, PAPER_URL

    errs = []
    url = cfg.alpaca_base_url.rstrip("/")
    if url not in (PAPER_URL, LIVE_URL):
        errs.append(f"ALPACA_BASE_URL must be {PAPER_URL} or {LIVE_URL}, got {url!r}")
    if url == LIVE_URL and not cfg.live_real_money:
        errs.append("ALPACA_BASE_URL is the LIVE account: also set LIVE_REAL_MONEY=true")
    if cfg.live_real_money and url != LIVE_URL:
        errs.append("LIVE_REAL_MONEY=true but ALPACA_BASE_URL is not the live URL")
    if not (cfg.alpaca_key and cfg.alpaca_secret):
        errs.append("ALPACA_KEY / ALPACA_SECRET missing")
    if cfg.approval_mode != "telegram":
        errs.append("APPROVAL_MODE must be telegram: every new entry needs your tap")
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        errs.append("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing")
    if cfg.trade_india:
        errs.append("TRADE_INDIA must be false (one market per broker process)")
    if not cfg.trade_us:
        errs.append("TRADE_US must be true")
    if cfg.us_mode != "trade":
        errs.append("US_MODE must be trade: a suggestions-only US market places no orders")
    if os.getenv("SUPABASE_URL"):
        errs.append("SUPABASE_URL must be empty: broker mode keeps its own SQLite "
                    "journal so it never mixes with the paper bot's records")
    if cfg.db_path == "botv2.db":
        errs.append("BOTV2_DB must name a separate file (e.g. botv2_live.db)")
    if not 10 <= cfg.max_order_usd <= 5000:
        errs.append("MAX_ORDER_USD must be between 10 and 5000")
    return errs
