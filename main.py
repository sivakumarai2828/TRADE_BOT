"""Standalone CLI entry point for the trading bot.

Run this directly (without the Flask API server) for headless operation:

    python main.py

For the full dashboard experience use api.py instead, which exposes REST
endpoints the React frontend consumes.
"""

from __future__ import annotations

import logging
import time

import schedule

from config import load_config
from execution import create_exchange, execute_trade, monitor_positions
from state import bot_state
from strategy import calculate_indicators, generate_signal, get_market_data


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_cycle(exchange, config) -> None:
    """Run one complete trading cycle."""
    try:
        data = get_market_data(
            exchange=exchange,
            symbol=config.symbol,
            timeframe=config.timeframe,
            limit=config.candle_limit,
        )
        data = calculate_indicators(data)
        latest_price = float(data.iloc[-1]["close"])

        result = generate_signal(data, config)
        execute_trade(exchange, config, result.action, latest_price)
        monitor_positions(exchange, config)

    except Exception as exc:
        logging.exception("Trading cycle failed: %s", exc)
        bot_state.add_log("Cycle error", str(exc)[:120], tone="negative")


def main() -> None:
    setup_logging()
    config = load_config()

    logging.info(
        "Starting bot | exchange=%s symbol=%s timeframe=%s trade_size=%s testnet=%s dry_run=%s",
        config.exchange_id,
        config.symbol,
        config.timeframe,
        config.trade_size_usdt,
        config.testnet,
        config.dry_run,
    )

    exchange = create_exchange(config)

    with bot_state._lock:
        bot_state.running = True

    # Run once immediately, then on the configured schedule.
    run_cycle(exchange, config)
    schedule.every(config.polling_seconds).seconds.do(run_cycle, exchange=exchange, config=config)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Bot stopped by user (Ctrl-C)")
        with bot_state._lock:
            bot_state.running = False


if __name__ == "__main__":
    main()
