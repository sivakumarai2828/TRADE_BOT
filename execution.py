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
import os
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
            "timeout": 30000,  # 30s — prevents indefinite hang on slow/unresponsive API
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
    if not config.dry_run:
        bot_state.metrics.balance_detail = "Live trading (Alpaca)"
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


def _dynamic_trade_pct(balance: float, principal: float) -> float:
    """Return trade size % — Upgraded Option-B: 50% base for 2-position 100% utilization.

    Max 2 open positions × 50% each = 100% capital deployed.
    Scales slightly on strong gains, compresses on drawdown.
    """
    if principal <= 0:
        return 50.0
    ratio = balance / principal  # 1.0 = breakeven, 2.0 = 100% gain
    if ratio >= 2.0:   return 55.0  # 100%+ gain — scale slightly
    if ratio >= 1.5:   return 52.0  # 50–100% gain
    if ratio >= 1.25:  return 50.0  # 25–50% gain
    if ratio >= 0.95:  return 50.0  # base (at or near starting capital)
    return 45.0                     # drawdown — compress slightly


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

    sell_value = float(price * amount)
    sell_fee = sell_value * 0.0015
    if config.dry_run:
        proceeds = sell_value
        with bot_state._lock:
            bot_state.metrics.paper_usdt = round(bot_state.metrics.paper_usdt + proceeds, 2)
            held = bot_state.metrics.paper_holdings.get(base, 0.0)
            bot_state.metrics.paper_holdings[base] = max(0.0, round(held - float(amount), 8))
            bot_state.metrics.total_fees_paid = round(bot_state.metrics.total_fees_paid + sell_fee, 4)
            bot_state.metrics.daily_fees_paid = round(bot_state.metrics.daily_fees_paid + sell_fee, 4)
        logging.info("PAPER SELL %s: +$%.2f USDT | paper_usdt=%.2f | fee~$%.4f", symbol, proceeds, bot_state.metrics.paper_usdt, sell_fee)
    else:
        # Use TradingClient.close_position for Alpaca — avoids qty rounding mismatches
        try:
            from alpaca.trading.client import TradingClient
            tc = TradingClient(config.api_key, config.api_secret, paper=False)
            alpaca_sym = symbol.replace("/", "")  # "ETH/USD" → "ETHUSD"
            tc.close_position(alpaca_sym)
        except Exception:
            exchange.create_market_sell_order(symbol, float(amount))
        with bot_state._lock:
            bot_state.metrics.total_fees_paid = round(bot_state.metrics.total_fees_paid + sell_fee, 4)
            bot_state.metrics.daily_fees_paid = round(bot_state.metrics.daily_fees_paid + sell_fee, 4)

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
    # Variable cooldown by exit reason — shorter after wins, longer after losses.
    _cooldown_map = {
        "stop_loss":    6,  # 30 min — market went wrong, give it time
        "time_exit":    4,  # 20 min — neutral exit, medium wait
        "take_profit":  2,  # 10 min — market was strong, re-enter sooner
        "manual_close": 3,  # 15 min — manual override
    }
    _cooldown_cycles = _cooldown_map.get(reason, 4)
    bot_state.set_cooldown(symbol, cycles=_cooldown_cycles)
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

        max_positions = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
        open_count = sum(1 for p in bot_state.positions.values() if p is not None)
        if open_count >= max_positions:
            logging.info("BUY skipped — portfolio limit reached (%d/%d open positions)", open_count, max_positions)
            bot_state.add_log("Trade skipped", f"Portfolio limit: already {open_count} open positions (max {max_positions})", tone="neutral")
            return

        with bot_state._lock:
            daily_count = bot_state.metrics.daily_trades_count
            daily_limit = bot_state.metrics.daily_trades_limit
            already_notified = bot_state.metrics.daily_limit_notified
        if daily_count >= daily_limit:
            logging.info("BUY skipped — daily trade limit reached (%d/%d)", daily_count, daily_limit)
            bot_state.add_log("Trade skipped", f"Daily limit: {daily_count}/{daily_limit} trades today", tone="neutral")
            if not already_notified:
                with bot_state._lock:
                    bot_state.metrics.daily_limit_notified = True
                from telegram_notify import notify_daily_trade_limit
                notify_daily_trade_limit("Crypto Bot", daily_count, daily_limit, symbol)
            return

        if bot_state.settings.trade_size_mode == "percent":
            if config.dry_run:
                available = bot_state.metrics.paper_usdt
            else:
                # Live: use actual cash from Alpaca (not equity — equity includes locked position value).
                # equity = cash + open positions; only cash can be spent on new BUYs.
                try:
                    from alpaca.trading.client import TradingClient as _TC2
                    _tc2 = _TC2(config.api_key, config.api_secret, paper=False)
                    _acct = _tc2.get_account()
                    available = float(_acct.cash)
                    logging.info("Live cash from Alpaca: $%.2f (equity $%.2f)", available, bot_state.metrics.balance)
                except Exception as _e:
                    # Fallback to equity with 10% buffer to avoid over-spending
                    available = bot_state.metrics.balance * 0.90
                    logging.warning("Cash fetch failed (%s) — using 90%% of equity $%.2f", _e, available)
            dynamic_pct = _dynamic_trade_pct(bot_state.metrics.balance, bot_state.metrics.principal)
            trade_size = Decimal(str(round(available * dynamic_pct / 100, 2)))
            logging.info("Base sizing: %.1f%% of cash $%.2f = $%.2f", dynamic_pct, available, float(trade_size))
        else:
            trade_size = Decimal(str(bot_state.settings.trade_size_usdt))

        # Apply mode multiplier + resolve SL/TP BEFORE calculating order amount
        try:
            from api import _crypto_mode_manager
            if _crypto_mode_manager is not None:
                mp = _crypto_mode_manager.params()
                trade_size = Decimal(str(round(float(trade_size) * mp.size_multiplier, 2)))
                _sl_pct = mp.stop_loss_pct / 100
                _tp_pct = mp.take_profit_pct / 100
                logging.info("Mode [%s]: size×%.1f → $%.2f | SL=%.1f%% TP=%.1f%%",
                             _crypto_mode_manager.mode, mp.size_multiplier, float(trade_size),
                             mp.stop_loss_pct, mp.take_profit_pct)
            else:
                raise ImportError
        except (ImportError, Exception):
            _sl_pct = bot_state.settings.stop_loss_pct / 100
            _tp_pct = bot_state.settings.take_profit_pct / 100

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

        estimated_fee = float(trade_size) * 0.0015
        if config.dry_run:
            with bot_state._lock:
                bot_state.metrics.paper_usdt = round(bot_state.metrics.paper_usdt - float(trade_size), 2)
                held = bot_state.metrics.paper_holdings.get(base, 0.0)
                bot_state.metrics.paper_holdings[base] = round(held + float(amount), 8)
                bot_state.metrics.daily_trades_count += 1
                bot_state.metrics.total_fees_paid = round(bot_state.metrics.total_fees_paid + estimated_fee, 4)
                bot_state.metrics.daily_fees_paid = round(bot_state.metrics.daily_fees_paid + estimated_fee, 4)
            logging.info("PAPER BUY %s: -$%.2f USDT, +%.6f %s | paper_usdt=%.2f | daily=%d/%d | fee~$%.4f",
                         symbol, float(trade_size), float(amount), base, bot_state.metrics.paper_usdt,
                         bot_state.metrics.daily_trades_count, bot_state.metrics.daily_trades_limit, estimated_fee)
        else:
            _order = exchange.create_market_buy_order(symbol, float(amount))
            # Verify order was accepted — rejected/cancelled orders must not create a PositionData
            _order_status = (_order or {}).get("status", "")
            _filled_qty = float((_order or {}).get("filled", amount))  # fallback to amount if not reported
            if _order_status in ("canceled", "cancelled", "expired", "rejected"):
                logging.error("BUY order REJECTED by exchange [%s] — status=%s. No position created.", symbol, _order_status)
                bot_state.add_log("Trade rejected", f"{symbol} BUY rejected by Alpaca (status={_order_status})", tone="negative")
                return
            # Use actual filled amount if available (handles partial fills)
            if _filled_qty > 0:
                amount = Decimal(str(_filled_qty))
            with bot_state._lock:
                bot_state.metrics.daily_trades_count += 1
                bot_state.metrics.total_fees_paid = round(bot_state.metrics.total_fees_paid + estimated_fee, 4)
                bot_state.metrics.daily_fees_paid = round(bot_state.metrics.daily_fees_paid + estimated_fee, 4)

        tp_pct = Decimal(str(_tp_pct))
        pct_stop = (current_price * (1 - Decimal(str(_sl_pct)))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if atr > 0:
            atr_stop = (current_price - _d(str(round(atr * 2, 8)))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            stop_loss = min(atr_stop, pct_stop)  # wider stop wins (lower price = more breathing room)
        else:
            stop_loss = pct_stop
        take_profit = (current_price * (1 + tp_pct)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        from datetime import datetime, timezone as _tz
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
            entry_time=datetime.now(_tz.utc).isoformat(),
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
    """On startup compare bot_state positions with actual Alpaca positions.

    Uses TradingClient (not ccxt fetch_balance) — ccxt Alpaca fetch_balance can
    silently return 0 on auth errors, causing real positions to be wrongly cleared.
    Also imports positions Alpaca has that bot_state doesn't know about.
    Skipped in paper mode — virtual state is authoritative.
    """
    if config.dry_run:
        return

    try:
        from alpaca.trading.client import TradingClient
        from state import PositionData
        tc = TradingClient(config.api_key, config.api_secret, paper=False)
        alpaca_positions = tc.get_all_positions()

        # Build lookup: ccxt symbol (e.g. "ETH/USD") → Alpaca position
        alpaca_map = {}
        for ap in alpaca_positions:
            # Alpaca symbol "ETHUSD" → ccxt "ETH/USD"
            raw = ap.symbol  # e.g. "ETHUSD"
            ccxt_sym = raw[:-3] + "/USD" if raw.endswith("USD") else raw
            alpaca_map[ccxt_sym] = ap

        open_symbols = [s for s, p in bot_state.positions.items() if p is not None]

        # Check existing bot positions against Alpaca
        for symbol in open_symbols:
            pos = bot_state.get_position(symbol)
            if pos is None:
                continue
            ap = alpaca_map.get(symbol)
            if ap is None:
                # Alpaca confirmed closed — real ghost
                logging.warning("Reconcile: ghost position %s cleared (not on Alpaca)", symbol)
                bot_state.set_position(symbol, None)
                bot_state.add_log(
                    "Position reconciled",
                    f"{symbol} ghost position cleared — was closed while bot was offline",
                    tone="warning",
                )
                from telegram_notify import _send
                _send(f"⚠️ <b>Position reconciled</b>\n{symbol} ghost position cleared — closed while bot was offline.")
            else:
                actual = float(ap.qty)
                if abs(actual - pos.amount) > 1e-8:
                    logging.warning("Reconcile: %s amount synced bot=%.8f → alpaca=%.8f", symbol, pos.amount, actual)
                    with bot_state._lock:
                        p = bot_state.positions.get(symbol)
                        if p:
                            p.amount = actual

        # Import positions Alpaca has that bot_state doesn't know about
        for ccxt_sym, ap in alpaca_map.items():
            if bot_state.get_position(ccxt_sym) is not None:
                continue
            entry = float(ap.avg_entry_price)
            current = float(ap.current_price or entry)
            pnl = float(ap.unrealized_pl or 0)
            pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0.0
            sl = round(entry * (1 - float(config.stop_loss_pct) / 100), 2)
            tp = round(entry * (1 + float(config.take_profit_pct) / 100), 2)
            imported = PositionData(
                symbol=ccxt_sym,
                amount=float(ap.qty),
                entry=entry,
                current=current,
                pnl=pnl,
                pnl_pct=pnl_pct,
                stop_loss=sl,
                take_profit=tp,
                highest_price=max(entry, current),
                is_house_trade=False,
            )
            bot_state.set_position(ccxt_sym, imported)
            logging.info("Reconcile: imported %s from Alpaca (entry=%.2f sl=%.2f tp=%.2f)", ccxt_sym, entry, sl, tp)
            bot_state.add_log(
                "Position reconciled",
                f"{ccxt_sym} imported from Alpaca — entry=${entry:.2f} SL=${sl:.2f} TP=${tp:.2f}",
                tone="positive",
            )

        logging.info("Position reconciliation complete — %d bot / %d alpaca position(s)", len(open_symbols), len(alpaca_positions))
    except Exception as exc:
        logging.warning("Position reconciliation failed (skipping — bot state preserved): %s", exc)


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


def _partial_close(exchange, config: BotConfig, symbol: str, price: Decimal, fraction: float = 0.5) -> None:
    """Sell a fraction of an open position (TP1 partial take-profit).

    Sells `fraction` of current amount, moves SL to breakeven, marks tp1_hit=True.
    Remaining position continues running toward TP2 (full TP%).
    """
    pos = bot_state.get_position(symbol)
    if pos is None or pos.tp1_hit:
        return

    entry = _d(pos.entry)
    sell_amount = _round_amount(exchange, symbol, _d(str(round(pos.amount * fraction, 8))))
    if sell_amount <= 0:
        logging.warning("TP1 partial sell skipped — rounded amount is zero for %s", symbol)
        return

    base = symbol.split("/")[0]
    sell_value = float(price * sell_amount)
    sell_fee = sell_value * 0.0015
    partial_pnl = float((price - entry) * sell_amount)
    partial_pnl_pct = float((price - entry) / entry * 100)

    if config.dry_run:
        proceeds = sell_value
        with bot_state._lock:
            bot_state.metrics.paper_usdt = round(bot_state.metrics.paper_usdt + proceeds, 2)
            held = bot_state.metrics.paper_holdings.get(base, 0.0)
            bot_state.metrics.paper_holdings[base] = max(0.0, round(held - float(sell_amount), 8))
            bot_state.metrics.total_fees_paid = round(bot_state.metrics.total_fees_paid + sell_fee, 4)
            bot_state.metrics.daily_fees_paid = round(bot_state.metrics.daily_fees_paid + sell_fee, 4)
        logging.info("PAPER TP1 SELL %s: %.6f units @ $%.2f | pnl=+$%.2f | fee~$%.4f",
                     symbol, float(sell_amount), float(price), partial_pnl, sell_fee)
    else:
        try:
            exchange.create_market_sell_order(symbol, float(sell_amount))
        except Exception as exc:
            logging.error("TP1 partial sell FAILED for %s: %s — position unchanged", symbol, exc)
            return
        with bot_state._lock:
            bot_state.metrics.total_fees_paid = round(bot_state.metrics.total_fees_paid + sell_fee, 4)
            bot_state.metrics.daily_fees_paid = round(bot_state.metrics.daily_fees_paid + sell_fee, 4)

    # Update position: reduce amount, lock SL at breakeven, flag tp1_hit
    with bot_state._lock:
        p = bot_state.positions.get(symbol)
        if p is not None:
            p.amount = max(0.0, round(p.amount - float(sell_amount), 8))
            p.tp1_hit = True
            p.stop_loss = float(entry)  # SL → breakeven, can't lose on remaining

    bot_state.add_log(
        "TP1 hit 🎯",
        f"{symbol} +{partial_pnl_pct:.1f}% — sold {fraction*100:.0f}% @ ${float(price):,.2f} "
        f"| locked +${partial_pnl:.2f} | SL → breakeven | 50% running",
        tone="positive",
    )

    from telegram_notify import _send
    _send(
        f"🎯 <b>TP1 Hit — Partial Exit</b>\n"
        f"{symbol} +{partial_pnl_pct:.1f}%\n"
        f"Sold 50% @ ${float(price):,.2f} | +${partial_pnl:.2f} locked\n"
        f"SL moved to breakeven. Remaining 50% running to TP2."
    )

    save_trade(
        symbol=symbol, side="sell_partial", amount=float(sell_amount),
        entry_price=float(entry), exit_price=float(price),
        pnl=partial_pnl, pnl_pct=partial_pnl_pct, reason="tp1_partial",
        is_house_trade=pos.is_house_trade,
    )

    if config.dry_run:
        bot_state.refresh_paper_balance(symbol, float(price))
    _save()


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

        # Breakeven SL — when position up ≥1%, raise SL to entry price.
        # Winning trade can never become a loser.
        if pnl_pct >= 1.0 and float(entry) > pos.stop_loss:
            with bot_state._lock:
                p = bot_state.positions.get(symbol)
                if p is not None and float(entry) > p.stop_loss:
                    logging.info("Breakeven SL %s | pnl=+%.1f%% → SL raised from %.4f to entry %.4f",
                                 symbol, pnl_pct, p.stop_loss, float(entry))
                    bot_state.add_log(
                        "Breakeven SL",
                        f"{symbol} up {pnl_pct:.1f}% — SL moved to entry ${float(entry):,.4f}",
                        tone="positive",
                    )
                    p.stop_loss = float(entry)

        # Upgraded Option-B: NO partial TP1 exit.
        # Hold full position → single clean exit at TP (6.5%) or SL (3%).
        # Breakeven SL already fired at +1% above — position is risk-free from there.

        logging.info("Monitor %s | price=%s SL=%s TP=%s pnl=%+.2f", symbol, current_price, pos.stop_loss, pos.take_profit, pnl)

        if float(current_price) <= pos.stop_loss:
            _close_position(exchange, config, symbol, current_price, reason="stop_loss")
        elif float(current_price) >= pos.take_profit:
            _close_position(exchange, config, symbol, current_price, reason="take_profit")
        elif pos.entry_time and pnl < 0:
            try:
                from datetime import datetime, timezone as _tz
                entry_dt = datetime.fromisoformat(pos.entry_time)
                hours_open = (datetime.now(_tz.utc) - entry_dt).total_seconds() / 3600
                if hours_open >= 6:
                    logging.info("Time-based exit %s — %.1fh open, pnl=%.2f", symbol, hours_open, pnl)
                    _close_position(exchange, config, symbol, current_price, reason="time_exit")
            except Exception:
                pass
