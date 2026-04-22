"""Exchange setup, trade execution, and position monitoring.

Paper trading mode (DRY_RUN=true):
  - Uses real public market data (no API keys required).
  - Simulates orders against a virtual $10,000 USDT balance.
  - Tracks virtual BTC holdings and live PnL on the position.
  - No exchange account or API keys needed to test.

Live mode (DRY_RUN=false):
  - Requires EXCHANGE_API_KEY + EXCHANGE_API_SECRET in .env.
  - Sends real orders to the exchange.
  - Recommended US exchanges: Coinbase Advanced Trade, Kraken.
"""

from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal

import ccxt

from config import BotConfig
from persistence import save_state as _save_state_raw, save_trade


def _save() -> None:
    from dataclasses import asdict
    positions = {s: asdict(p) for s, p in bot_state.positions.items() if p is not None}
    _save_state_raw(bot_state.metrics, bot_state.settings, positions)
from state import PositionData, bot_state
from telegram_notify import notify_buy, notify_sell


# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------

def create_exchange(config: BotConfig):
    """Create a ccxt exchange client.

    Public endpoints (market data) work without API keys, so paper trading
    mode works even with empty key/secret values.
    """
    exchange_class = getattr(ccxt, config.exchange_id, None)
    if exchange_class is None:
        raise ValueError(
            f"Exchange '{config.exchange_id}' is not supported by ccxt. "
            "Check EXCHANGE_ID in your .env file."
        )

    # Alpaca paper trading uses a separate base URL (sandbox mode).
    is_alpaca = config.exchange_id == "alpaca"
    alpaca_opts = {}
    if is_alpaca and config.dry_run:
        alpaca_opts = {"urls": {"api": {"trader": "https://paper-api.alpaca.markets",
                                        "broker": "https://paper-api.alpaca.markets"}}}

    exchange = exchange_class(
        {
            "apiKey": config.api_key or "",
            "secret": config.api_secret or "",
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
            **alpaca_opts,
        }
    )

    # Only activate testnet when keys are present (testnet requires auth).
    if config.testnet and config.api_key and not is_alpaca:
        exchange.set_sandbox_mode(True)
        logging.info("Exchange sandbox/testnet mode enabled")

    exchange.load_markets()

    # Record exchange name and mode in shared state for the UI.
    mode = "paper" if config.dry_run else ("testnet" if config.testnet else "live")
    bot_state.exchange_name = f"{config.exchange_id} ({mode})"
    bot_state.paper_mode = config.dry_run
    bot_state.add_log(
        "Exchange ready",
        f"{config.exchange_id} connected — mode={mode}",
        tone="neutral",
    )

    return exchange


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _d(value) -> Decimal:
    return Decimal(str(value))


def _round_amount(exchange, symbol: str, amount: Decimal) -> Decimal:
    try:
        rounded = exchange.amount_to_precision(symbol, float(amount))
        return Decimal(str(rounded))
    except Exception:
        # Fallback: round to 6 decimal places (sufficient for BTC).
        return amount.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def _validate_trade(config: BotConfig, signal: str, price: Decimal) -> bool:
    if signal not in {"BUY", "SELL"}:
        return False
    if price <= 0:
        logging.error("Refusing trade — price is invalid: %s", price)
        return False
    if bot_state.settings.trade_size_mode == "fixed":
        if bot_state.settings.trade_size_usdt <= 0:
            logging.error("Refusing trade — trade_size_usdt must be positive")
            return False
    if not config.dry_run and (not config.api_key or not config.api_secret):
        logging.error("Refusing live order — EXCHANGE_API_KEY / EXCHANGE_API_SECRET missing")
        return False
    return True


def _close_position(exchange, config: BotConfig, symbol: str, price: Decimal, reason: str) -> None:
    """Sell the open position for symbol and update paper/live state."""
    pos = bot_state.get_position(symbol)
    if pos is None:
        return

    amount = _d(pos.amount)
    base = symbol.split("/")[0]
    entry = _d(pos.entry)

    if config.dry_run:
        proceeds = float(price * amount)
        with bot_state._lock:
            bot_state.metrics.paper_usdt = round(bot_state.metrics.paper_usdt + proceeds, 2)
            held = bot_state.metrics.paper_holdings.get(base, 0.0)
            bot_state.metrics.paper_holdings[base] = max(0.0, round(held - float(amount), 8))
        logging.info("PAPER SELL %s: +$%.2f USDT | paper_usdt=%.2f", symbol, proceeds, bot_state.metrics.paper_usdt)
    else:
        exchange.create_market_sell_order(symbol, float(amount))

    pnl = (price - entry) * amount
    pnl_pct = float((price - entry) / entry * 100)
    is_house_trade = pos.is_house_trade

    logging.info(
        "SELL | reason=%s symbol=%s amount=%s exit=%s entry=%s pnl=%s house=%s",
        reason, symbol, amount, price, entry,
        pnl.quantize(Decimal("0.01"), rounding=ROUND_DOWN), is_house_trade,
    )

    bot_state.set_position(symbol, None)

    tone = "positive" if pnl >= 0 else "negative"
    label = "House trade" if is_house_trade else "Trade executed"
    bot_state.add_log(
        label,
        f"Sold {amount} {symbol} @ ${float(price):,.2f} | "
        f"reason={reason} PnL={float(pnl):+.2f} ({pnl_pct:+.2f}%)"
        + (" [house money]" if is_house_trade else ""),
        tone=tone,
    )

    with bot_state._lock:
        bot_state.metrics.pnl = round(bot_state.metrics.pnl + float(pnl), 2)

        if is_house_trade:
            bot_state.metrics.house_trade_active = False
            pool_change = float(pnl)
            bot_state.metrics.profit_pool = max(0.0, round(bot_state.metrics.profit_pool + pool_change, 2))
            logging.info("House trade closed | pool_change=%+.2f new_pool=%.2f", pool_change, bot_state.metrics.profit_pool)
            bot_state.add_log(
                "Profit pool",
                f"Pool {'grew' if pool_change >= 0 else 'shrank'} by ${pool_change:+.2f} → "
                f"pool=${bot_state.metrics.profit_pool:.2f} | principal=${bot_state.metrics.principal:.2f} safe",
                tone="positive" if pool_change >= 0 else "warning",
            )
        elif float(pnl) > 0 and bot_state.settings.trade_size_mode == "house_money":
            bot_state.metrics.profit_pool = round(bot_state.metrics.profit_pool + float(pnl), 2)
            bot_state.add_log(
                "Profit pool",
                f"+${float(pnl):.2f} added → pool=${bot_state.metrics.profit_pool:.2f} "
                f"(threshold=${bot_state.settings.house_profit_threshold:.2f})",
                tone="positive",
            )

    if config.dry_run:
        bot_state.refresh_paper_balance(symbol, float(price))

    bot_state.record_trade_result(float(pnl))
    bot_state.set_cooldown(symbol, cycles=10)  # wait 10 cycles (~10 min) before re-entering
    bot_state.check_daily_loss_limit()
    notify_sell(symbol, float(price), float(pnl), pnl_pct, reason)

    save_trade(
        symbol=symbol, side="sell", amount=float(amount),
        entry_price=float(entry), exit_price=float(price),
        pnl=float(pnl), pnl_pct=pnl_pct, reason=reason,
        is_house_trade=is_house_trade,
    )
    _save()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_trade(exchange, config: BotConfig, symbol: str, signal: str, price: float,
                  atr: float = 0.0) -> None:
    """Execute BUY/SELL for a specific symbol. Paper mode simulates with virtual USDT.

    When atr > 0 the stop-loss is set dynamically at entry - 2×ATR instead of
    the fixed percentage, giving the trade room proportional to actual volatility.
    """

    current_price = _d(price)
    if not _validate_trade(config, signal, current_price):
        return

    base = symbol.split("/")[0]
    has_position = bot_state.get_position(symbol) is not None

    if signal == "BUY":
        if has_position:
            logging.info("BUY skipped — %s position already open", symbol)
            return

        open_count = sum(1 for p in bot_state.positions.values() if p is not None)
        if open_count >= 2:
            logging.info("BUY skipped — portfolio limit reached (%d/2 open positions)", open_count)
            bot_state.add_log("Trade skipped", f"Portfolio limit: already {open_count} open positions (max 2)", tone="neutral")
            return

        if bot_state.settings.trade_size_mode == "percent":
            available = bot_state.metrics.paper_usdt if config.dry_run else bot_state.metrics.balance
            trade_size = Decimal(str(round(available * bot_state.settings.trade_size_pct / 100, 2)))
            logging.info("Compound mode: %.1f%% of $%.2f = $%.2f", bot_state.settings.trade_size_pct, available, float(trade_size))
        else:
            trade_size = Decimal(str(bot_state.settings.trade_size_usdt))

        if config.dry_run:
            with bot_state._lock:
                available = Decimal(str(bot_state.metrics.paper_usdt))
            if trade_size > available:
                logging.warning("PAPER BUY skipped — insufficient USDT (have %.2f, need %.2f)", float(available), float(trade_size))
                bot_state.add_log("Trade skipped", f"Insufficient paper USDT (${float(available):,.2f} < ${float(trade_size):,.2f})", tone="warning")
                return

        amount = _round_amount(exchange, symbol, trade_size / current_price)
        if amount <= 0:
            logging.error("BUY skipped — calculated amount is zero")
            return

        if config.dry_run:
            with bot_state._lock:
                bot_state.metrics.paper_usdt = round(bot_state.metrics.paper_usdt - float(trade_size), 2)
                held = bot_state.metrics.paper_holdings.get(base, 0.0)
                bot_state.metrics.paper_holdings[base] = round(held + float(amount), 8)
            logging.info("PAPER BUY %s: -$%.2f USDT, +%.6f %s | paper_usdt=%.2f", symbol, float(trade_size), float(amount), base, bot_state.metrics.paper_usdt)
        else:
            exchange.create_market_buy_order(symbol, float(amount))

        # Use adaptive mode SL/TP if mode manager is active, else fall back to settings
        try:
            from api import _crypto_mode_manager
            if _crypto_mode_manager is not None:
                mp = _crypto_mode_manager.params()
                _sl_pct = mp.stop_loss_pct / 100
                _tp_pct = mp.take_profit_pct / 100
                # Also scale trade size by mode multiplier
                trade_size = Decimal(str(round(float(trade_size) * mp.size_multiplier, 2)))
            else:
                raise ImportError
        except (ImportError, Exception):
            _sl_pct = bot_state.settings.stop_loss_pct / 100
            _tp_pct = bot_state.settings.take_profit_pct / 100

        tp_pct = Decimal(str(_tp_pct))
        if atr > 0:
            stop_loss = (current_price - _d(str(round(atr * 2, 8)))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        else:
            stop_loss = (current_price * (1 - Decimal(str(_sl_pct)))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        take_profit = (current_price * (1 + tp_pct)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        pos = PositionData(
            symbol=symbol,
            amount=float(amount),
            entry=float(current_price),
            current=float(current_price),
            pnl=0.0,
            pnl_pct=0.0,
            stop_loss=float(stop_loss),
            take_profit=float(take_profit),
            highest_price=float(current_price),
        )
        bot_state.set_position(symbol, pos)

        logging.info("BUY | symbol=%s amount=%s entry=%s SL=%s TP=%s", symbol, amount, current_price, stop_loss, take_profit)
        bot_state.add_log(
            "Trade executed",
            f"{'Paper ' if config.dry_run else ''}Buy {amount} {symbol} "
            f"@ ${float(current_price):,.2f} | SL=${float(stop_loss):,.2f} TP=${float(take_profit):,.2f}",
            tone="positive",
        )

        notify_buy(symbol, float(amount), float(current_price),
                   float(stop_loss), float(take_profit), float(trade_size))
        _save()

        if config.dry_run:
            bot_state.refresh_paper_balance(symbol, float(current_price))
        return

    if signal == "SELL":
        if not has_position:
            logging.info("SELL skipped — no open %s position", symbol)
            return
        _close_position(exchange, config, symbol, current_price, reason="signal_sell")


def _try_house_money_trade(exchange, config: BotConfig, symbol: str, current_price: float) -> None:
    """Fire an aggressive house money trade on symbol when profit pool hits threshold."""
    s = bot_state.settings
    if s.trade_size_mode != "house_money":
        return

    with bot_state._lock:
        pool = bot_state.metrics.profit_pool
        already_active = bot_state.metrics.house_trade_active
        has_main_position = bot_state.get_position(symbol) is not None

    if already_active or has_main_position:
        return

    if pool < s.house_profit_threshold:
        logging.info("House money: pool $%.2f < threshold $%.2f — waiting", pool, s.house_profit_threshold)
        return

    base = symbol.split("/")[0]
    price = _d(current_price)
    trade_size = _d(str(round(pool, 2)))
    amount = _round_amount(exchange, symbol, trade_size / price)
    if amount <= 0:
        return

    sl_pct = _d(str(s.house_stop_loss_pct / 100))
    tp_pct = _d(str(s.house_take_profit_pct / 100))
    stop_loss = (price * (1 - sl_pct)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    take_profit = (price * (1 + tp_pct)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    if config.dry_run:
        with bot_state._lock:
            bot_state.metrics.paper_usdt = round(bot_state.metrics.paper_usdt - float(trade_size), 2)
            held = bot_state.metrics.paper_holdings.get(base, 0.0)
            bot_state.metrics.paper_holdings[base] = round(held + float(amount), 8)
            bot_state.metrics.profit_pool = 0.0
            bot_state.metrics.house_trade_active = True
    else:
        exchange.create_market_buy_order(symbol, float(amount))
        with bot_state._lock:
            bot_state.metrics.profit_pool = 0.0
            bot_state.metrics.house_trade_active = True

    pos = PositionData(
        symbol=symbol,
        amount=float(amount),
        entry=float(price),
        current=float(price),
        pnl=0.0,
        pnl_pct=0.0,
        stop_loss=float(stop_loss),
        take_profit=float(take_profit),
        highest_price=float(price),
        is_house_trade=True,
    )
    bot_state.set_position(symbol, pos)

    logging.info("HOUSE MONEY TRADE fired | symbol=%s amount=%s @ %s | SL=%s TP=%s", symbol, amount, price, stop_loss, take_profit)
    bot_state.add_log(
        "House money trade",
        f"Aggressive buy {amount} {symbol} @ ${float(price):,.2f} using ${float(trade_size):.2f} profit | "
        f"TP={s.house_take_profit_pct:.0f}% | Principal ${bot_state.metrics.principal:.2f} SAFE",
        tone="positive",
    )


def reconcile_positions(exchange, config: BotConfig) -> None:
    """On startup compare bot_state positions with actual exchange balances.

    Clears ghost positions (bot thinks open, exchange already closed them while
    the server was offline). Skipped in paper mode — virtual state is authoritative.
    """
    if config.dry_run:
        return

    try:
        balance = exchange.fetch_balance()
        open_symbols = [s for s, p in bot_state.positions.items() if p is not None]

        for symbol in open_symbols:
            base = symbol.split("/")[0]
            actual = float(balance.get(base, {}).get("total", 0) or 0)
            pos = bot_state.get_position(symbol)
            if pos is None:
                continue

            if actual < 1e-6:
                logging.warning("Reconcile: ghost position %s cleared (exchange balance=0)", symbol)
                bot_state.set_position(symbol, None)
                bot_state.add_log(
                    "Position reconciled",
                    f"{symbol} ghost position cleared — was closed while bot was offline",
                    tone="warning",
                )
                from telegram_notify import _send
                _send(f"⚠️ <b>Position reconciled</b>\n{symbol} ghost position cleared — closed while bot was offline.")
            elif abs(actual - pos.amount) / max(pos.amount, 1e-9) > 0.05:
                logging.warning("Reconcile: %s amount mismatch bot=%.6f exchange=%.6f — updating", symbol, pos.amount, actual)
                with bot_state._lock:
                    p = bot_state.positions.get(symbol)
                    if p:
                        p.amount = actual
                bot_state.add_log(
                    "Position reconciled",
                    f"{symbol} amount corrected: {pos.amount:.6f} → {actual:.6f}",
                    tone="warning",
                )

        logging.info("Position reconciliation complete — %d symbol(s) checked", len(open_symbols))
    except Exception as exc:
        logging.warning("Position reconciliation failed: %s", exc)


def close_open_position(exchange, config: BotConfig, symbol: str = None) -> bool:
    """Force-close open position(s). If symbol given, close only that one; else close all."""
    symbols_to_close = [symbol] if symbol else [s for s, p in bot_state.positions.items() if p is not None]
    closed_any = False
    for sym in symbols_to_close:
        pos = bot_state.get_position(sym)
        if pos is None:
            continue
        ticker = exchange.fetch_ticker(sym)
        current_price = _d(ticker["last"])
        _close_position(exchange, config, sym, current_price, reason="manual_close")
        closed_any = True
    return closed_any


def monitor_positions(exchange, config: BotConfig) -> None:
    """Check all open positions against SL/TP and update live PnL."""
    open_symbols = [s for s, p in bot_state.positions.items() if p is not None]

    for symbol in open_symbols:
        pos = bot_state.get_position(symbol)
        if pos is None:
            continue

        ticker = exchange.fetch_ticker(symbol)
        current_price = _d(ticker["last"])

        if config.use_trailing_stop:
            highest = max(_d(pos.highest_price), current_price)
            trailing_stop = (highest * (1 - config.trailing_stop_pct)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            with bot_state._lock:
                p = bot_state.positions.get(symbol)
                if p is not None:
                    p.highest_price = float(highest)
                    if float(trailing_stop) > p.stop_loss:
                        p.stop_loss = float(trailing_stop)
                        logging.info("Trailing stop %s → %s", symbol, trailing_stop)

        entry = _d(pos.entry)
        pnl = float((current_price - entry) * _d(pos.amount))
        pnl_pct = float((current_price - entry) / entry * 100)

        with bot_state._lock:
            p = bot_state.positions.get(symbol)
            if p is not None:
                p.current = float(current_price)
                p.pnl = round(pnl, 2)
                p.pnl_pct = round(pnl_pct, 2)

        if config.dry_run:
            bot_state.refresh_paper_balance(symbol, float(current_price))

        logging.info("Monitor %s | price=%s SL=%s TP=%s pnl=%+.2f", symbol, current_price, pos.stop_loss, pos.take_profit, pnl)

        if float(current_price) <= pos.stop_loss:
            _close_position(exchange, config, symbol, current_price, reason="stop_loss")
        elif float(current_price) >= pos.take_profit:
            _close_position(exchange, config, symbol, current_price, reason="take_profit")
