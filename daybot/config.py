"""Day-trading bot config — reads from the shared .env, no new keys needed."""
from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DayBotConfig:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    anthropic_api_key: str
    max_trades_per_day: int
    max_concurrent_trades: int
    position_size_pct: float      # fraction of portfolio per trade
    max_daily_loss_pct: float     # halt if daily loss exceeds this
    scan_interval_minutes: int
    loop_interval_seconds: int
    stop_loss_pct: float
    take_profit_pct: float
    claude_model: str
    paper_budget: float            # cap portfolio to this amount (0 = no cap)


def load_config() -> DayBotConfig:
    return DayBotConfig(
        alpaca_api_key=os.getenv("EXCHANGE_API_KEY", ""),
        alpaca_secret_key=os.getenv("EXCHANGE_API_SECRET", ""),
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        max_trades_per_day=int(os.getenv("DAY_MAX_TRADES", "3")),
        max_concurrent_trades=int(os.getenv("DAY_MAX_CONCURRENT", "2")),
        position_size_pct=float(os.getenv("DAY_POSITION_SIZE_PCT", "0.05")),
        max_daily_loss_pct=float(os.getenv("DAY_MAX_DAILY_LOSS_PCT", "0.03")),
        scan_interval_minutes=int(os.getenv("DAY_SCAN_INTERVAL_MINUTES", "15")),
        loop_interval_seconds=int(os.getenv("DAY_LOOP_INTERVAL_SECONDS", "60")),
        stop_loss_pct=float(os.getenv("DAY_STOP_LOSS_PCT", "0.01")),
        take_profit_pct=float(os.getenv("DAY_TAKE_PROFIT_PCT", "0.025")),
        claude_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        paper_budget=float(os.getenv("DAY_PAPER_BUDGET", "0")),
    )
