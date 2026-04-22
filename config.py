"""Configuration loading for the crypto trading bot.

All secrets and tunable risk settings are read from environment variables.
Create a .env file from .env.example before running the bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv


load_dotenv()


def _get_decimal(name: str, default: str) -> Decimal:
    value = os.getenv(name, default)
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a valid number, got {value!r}") from exc

    if parsed <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return parsed


def _get_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class BotConfig:
    exchange_id: str
    symbol: str
    timeframe: str
    candle_limit: int
    trade_size_usdt: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    trailing_stop_pct: Decimal
    use_trailing_stop: bool
    testnet: bool
    dry_run: bool
    polling_seconds: int
    api_key: str
    api_secret: str
    anthropic_api_key: str
    anthropic_model: str


def load_config() -> BotConfig:
    """Load and validate bot configuration from environment variables."""

    candle_limit = int(os.getenv("CANDLE_LIMIT", "100"))
    polling_seconds = int(os.getenv("POLLING_SECONDS", "60"))

    if candle_limit < 50:
        raise ValueError("CANDLE_LIMIT must be at least 50 so the 50 SMA can be calculated")
    if polling_seconds < 10:
        raise ValueError("POLLING_SECONDS should be at least 10 to avoid excessive API calls")

    trade_size = _get_decimal("TRADE_SIZE_USDT", "100")

    # Support both the new generic key names and the old BINANCE_* names so
    # existing .env files keep working without changes.
    api_key = (
        os.getenv("EXCHANGE_API_KEY")
        or os.getenv("BINANCE_API_KEY", "")
    )
    api_secret = (
        os.getenv("EXCHANGE_API_SECRET")
        or os.getenv("BINANCE_API_SECRET", "")
    )

    exchange_id = os.getenv("EXCHANGE_ID", "coinbase")
    _SUPPORTED = {"bybit", "coinbase", "kraken", "binanceus", "binance", "alpaca"}
    if exchange_id not in _SUPPORTED:
        raise ValueError(
            f"EXCHANGE_ID={exchange_id!r} is not in the supported list: {sorted(_SUPPORTED)}"
        )

    return BotConfig(
        exchange_id=exchange_id,
        symbol=os.getenv("SYMBOL", "BTC/USDT"),
        timeframe=os.getenv("TIMEFRAME", "1m"),
        candle_limit=candle_limit,
        trade_size_usdt=trade_size,
        stop_loss_pct=_get_decimal("STOP_LOSS_PCT", "0.02"),
        take_profit_pct=_get_decimal("TAKE_PROFIT_PCT", "0.05"),
        trailing_stop_pct=_get_decimal("TRAILING_STOP_PCT", "0.015"),
        use_trailing_stop=_get_bool("USE_TRAILING_STOP", "true"),
        testnet=_get_bool("USE_TESTNET", "true"),
        dry_run=_get_bool("DRY_RUN", "true"),
        polling_seconds=polling_seconds,
        api_key=api_key,
        api_secret=api_secret,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    )
