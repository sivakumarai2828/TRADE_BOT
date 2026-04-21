"""Flask REST API server for the AI Trading Bot.

Exposes the endpoints the React dashboard consumes and manages the bot's
background execution thread.

Run with:
    python api.py
    # or
    flask --app api run --port 5000

Endpoints
---------
GET  /status     — full bot state (metrics, signal, position, logs, settings)
GET  /signals    — current AI signal only
GET  /positions  — open position only
GET  /logs       — last 30 activity log entries
GET  /candles    — recent OHLCV candles for the chart
POST /start      — start the trading bot loop (optional JSON body with settings)
POST /stop       — stop the trading bot loop
POST /close      — force-close the open position
POST /settings   — update runtime settings without restarting
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from config import load_config
from execution import close_open_position, create_exchange, execute_trade, monitor_positions, reconcile_positions, _try_house_money_trade
from persistence import load_state, save_state as _save_state_raw


def _save() -> None:
    """Save metrics + settings + open positions to Supabase."""
    from dataclasses import asdict
    positions = {s: asdict(p) for s, p in bot_state.positions.items() if p is not None}
    _save_state_raw(bot_state.metrics, bot_state.settings, positions)
from state import bot_state
from strategy import calculate_indicators, generate_signal, get_market_data


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = Flask(__name__)
CORS(app)  # Allow the Vite dev server (port 5173) to call this API

import time as _time_module
_startup_time = _time_module.time()
_last_cycle_time = _time_module.time()  # updated every cycle; watchdog alerts if stale


def _watchdog_loop() -> None:
    """Alert via Telegram if the bot loop hasn't run a cycle in > 10 minutes."""
    while True:
        _time_module.sleep(300)  # check every 5 minutes
        if bot_state.running:
            age = _time_module.time() - _last_cycle_time
            if age > 600:
                from telegram_notify import _send
                _send(
                    f"⚠️ <b>Bot heartbeat missed</b>\n"
                    f"Last cycle was <b>{age / 60:.0f} min</b> ago — bot may be stuck or crashed."
                )
                logging.warning("Watchdog: bot loop stalled for %.0f minutes", age / 60)


threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()

from daybot.blueprint import daybot_bp
from daybot.scheduler import start_scheduler
from telegram_bot import start_telegram_bot
app.register_blueprint(daybot_bp, url_prefix="/daybot")
start_scheduler()
start_telegram_bot()

# Restore persisted state from Supabase on startup.
_saved = load_state()
if _saved:
    m = _saved.get("metrics", {})
    s = _saved.get("settings", {})
    p = _saved.get("positions", {})
    if m:
        bot_state.update_metrics(**{k: v for k, v in m.items() if k != "paper_holdings"})
        bot_state.metrics.paper_usdt = m.get("paper_usdt", bot_state.metrics.paper_usdt)
        bot_state.metrics.paper_holdings = m.get("paper_holdings", {})
    if s:
        bot_state.update_settings(**s)
    if p:
        from state import PositionData
        for sym, pos_dict in p.items():
            try:
                bot_state.set_position(sym, PositionData(**pos_dict))
            except Exception as exc:
                logging.warning("Could not restore position %s: %s", sym, exc)
        logging.info("Restored %d open position(s) from Supabase", len(p))
    logging.info("State restored from Supabase — balance=%.2f", bot_state.metrics.balance)

# ---------------------------------------------------------------------------
# Bot thread management
# ---------------------------------------------------------------------------

_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_exchange = None
_config = None

# Per-symbol error backoff: tracks consecutive failures and cycles to skip
_symbol_errors: dict[str, int] = {}      # symbol → consecutive error count
_symbol_skip_cycles: dict[str, int] = {} # symbol → cycles remaining to skip


def _run_symbol_cycle(symbol: str) -> None:
    """One complete trading cycle for a single symbol."""
    global _exchange, _config

    # Daily loss limit — skip all trading for the day once threshold hit.
    if bot_state.metrics.daily_loss_halted:
        logging.info("Daily loss limit active — skipping %s", symbol)
        return

    # Per-symbol error backoff — skip if this symbol is in cooldown from repeated failures.
    if _symbol_skip_cycles.get(symbol, 0) > 0:
        _symbol_skip_cycles[symbol] -= 1
        logging.info("Backoff [%s] — %d cycle(s) remaining", symbol, _symbol_skip_cycles[symbol])
        return

    # Cooldown — skip signal generation/trading for N cycles after a trade closes.
    if bot_state.is_on_cooldown(symbol):
        remaining = bot_state._cooldowns.get(symbol, 0)
        logging.info("Cooldown [%s] — %d cycle(s) remaining", symbol, remaining)
        bot_state.tick_cooldown(symbol)
        return

    try:
        data = get_market_data(exchange=_exchange, symbol=symbol, timeframe=_config.timeframe, limit=_config.candle_limit)
        data = calculate_indicators(data)
        latest_price = float(data.iloc[-1]["close"])

        result = generate_signal(data, _config, symbol=symbol, exchange=_exchange)

        if bot_state.settings.auto_mode:
            execute_trade(_exchange, _config, symbol, result.action, latest_price, atr=result.atr)

        # House Money fires only on the first/primary symbol to avoid pool fragmentation.
        if symbol == bot_state.settings.active_symbols[0]:
            _try_house_money_trade(_exchange, _config, symbol, latest_price)

        # Success — reset error counter
        _symbol_errors.pop(symbol, None)

    except Exception as exc:
        err_count = _symbol_errors.get(symbol, 0) + 1
        _symbol_errors[symbol] = err_count

        # Exponential backoff: 1st error logs immediately; after 3 consecutive errors
        # skip for 2, 4, 8… up to 16 cycles (~16 min at 60s polling) before retrying.
        if err_count <= 2:
            logging.warning("Cycle error [%s]: %s", symbol, exc)
            bot_state.add_log("Cycle error", f"[{symbol}] {str(exc)[:100]}", tone="negative")
        else:
            skip = min(2 ** (err_count - 2), 16)
            _symbol_skip_cycles[symbol] = skip
            logging.warning("Backoff [%s] after %d errors — pausing %d cycles: %s", symbol, err_count, skip, exc)
            bot_state.add_log(
                "Backoff",
                f"[{symbol}] API errors ({err_count}×) — pausing {skip} cycles",
                tone="warning",
            )


def _run_cycle() -> None:
    """Run a cycle for every active symbol, then update live balance once."""
    global _exchange, _config, _last_cycle_time
    _last_cycle_time = _time_module.time()

    for symbol in list(bot_state.settings.active_symbols):
        _run_symbol_cycle(symbol)

    monitor_positions(_exchange, _config)

    if not _config.dry_run:
        try:
            balance_info = _exchange.fetch_balance()
            usdt_free = float(balance_info.get("USDT", {}).get("free", 0) or 0)
            if usdt_free > 0:
                bot_state.update_metrics(balance=round(usdt_free, 2))
        except Exception:
            pass


def _send_daily_summary() -> None:
    """Send end-of-day summary to Telegram (called on midnight UTC reset)."""
    from dataclasses import asdict
    from telegram_notify import notify_daily_summary
    m = bot_state.metrics
    open_positions = [
        {"symbol": s, "pnl_pct": p.pnl_pct, "stop_loss": p.stop_loss}
        for s, p in bot_state.positions.items() if p is not None
    ]
    daily_pnl = m.balance - m.daily_start_balance
    daily_pnl_pct = (daily_pnl / m.daily_start_balance * 100) if m.daily_start_balance > 0 else 0.0
    from datetime import datetime, timezone
    yesterday = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notify_daily_summary(
        date=yesterday,
        balance=m.balance,
        pnl=m.pnl,
        pnl_pct=m.pnl_pct,
        daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct,
        total_trades=m.total_trades,
        win_count=m.win_count,
        loss_count=m.loss_count,
        win_rate=m.win_rate,
        open_positions=open_positions,
        daily_halted=m.daily_loss_halted,
    )


def _bot_loop() -> None:
    """Runs _run_cycle() on each tick until _stop_event is set."""
    from telegram_notify import notify_bot_started, notify_bot_stopped
    logging.info("Bot loop started")
    bot_state.add_log("Bot started", "Trading loop is running", tone="positive")
    notify_bot_started()

    while not _stop_event.is_set():
        if bot_state.check_daily_reset():
            _send_daily_summary()
        _run_cycle()
        polling = bot_state.settings.polling_seconds
        _stop_event.wait(timeout=polling)

    logging.info("Bot loop stopped")
    bot_state.add_log("Bot stopped", "Trading loop has been stopped", tone="warning")
    notify_bot_stopped()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Lightweight liveness check — returns server uptime and scheduler status."""
    from daybot.scheduler import _scheduler
    import time
    return jsonify({
        "ok": True,
        "server": "flask",
        "uptime_s": round(time.time() - _startup_time, 0),
        "scheduler": _scheduler.running if _scheduler else False,
    })


@app.get("/status")
def status():
    """Return the complete bot state."""
    return jsonify(bot_state.to_dict())


@app.get("/signals")
def signals():
    with bot_state._lock:
        from dataclasses import asdict
        return jsonify(asdict(bot_state.signal))


@app.get("/positions")
def positions():
    with bot_state._lock:
        from dataclasses import asdict
        pos = bot_state.position
        return jsonify(asdict(pos) if pos else None)


@app.get("/logs")
def logs():
    with bot_state._lock:
        from dataclasses import asdict
        return jsonify([asdict(lg) for lg in bot_state.logs[:30]])


@app.get("/candles")
def candles():
    """Return recent OHLCV candles for the chart panel."""
    global _exchange, _config

    if _exchange is None or _config is None:
        return jsonify([])

    symbol = request.args.get("symbol", bot_state.settings.active_symbols[0] if bot_state.settings.active_symbols else "BTC/USDT")

    try:
        raw = _exchange.fetch_ohlcv(symbol, _config.timeframe, limit=60)
        result = [
            {
                "timestamp": int(c[0]),
                "open": c[1],
                "high": c[2],
                "low": c[3],
                "close": c[4],
                "volume": c[5],
            }
            for c in raw
        ]
        return jsonify(result)
    except Exception as exc:
        logging.warning("Failed to fetch candles: %s", exc)
        return jsonify([])


@app.post("/start")
def start():
    """Start the bot loop. Optional JSON body can override runtime settings."""
    global _bot_thread, _stop_event, _exchange, _config

    body = request.get_json(silent=True) or {}

    # Apply any settings sent with the start request.
    if body:
        allowed = {
            "trade_size_usdt", "trade_size_mode", "trade_size_pct",
            "stop_loss_pct", "take_profit_pct", "polling_seconds", "auto_mode",
            "rsi_oversold", "rsi_overbought",
            "house_profit_threshold", "house_take_profit_pct", "house_stop_loss_pct",
            "active_symbols",
        }
        filtered = {k: v for k, v in body.items() if k in allowed}
        if filtered:
            bot_state.update_settings(**filtered)

    with bot_state._lock:
        if bot_state.running:
            return jsonify({"ok": False, "message": "Bot is already running"}), 409

    try:
        _config = load_config()
        _exchange = create_exchange(_config)
        reconcile_positions(_exchange, _config)
    except Exception as exc:
        msg = f"Failed to initialise exchange: {exc}"
        logging.error(msg)
        bot_state.add_log("Start error", msg[:120], tone="negative")
        return jsonify({"ok": False, "message": msg}), 500

    _stop_event = threading.Event()
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
    _bot_thread.start()

    with bot_state._lock:
        bot_state.running = True

    return jsonify({"ok": True, "message": "Bot started"})


@app.post("/stop")
def stop():
    """Stop the bot loop gracefully."""
    global _bot_thread, _stop_event

    with bot_state._lock:
        if not bot_state.running:
            return jsonify({"ok": False, "message": "Bot is not running"}), 409

        bot_state.running = False

    _stop_event.set()
    if _bot_thread is not None:
        _bot_thread.join(timeout=10)

    return jsonify({"ok": True, "message": "Bot stopped"})


@app.post("/close")
def close():
    """Force-close open position(s). Optional JSON body: {"symbol": "ETH/USDT"}."""
    global _exchange, _config

    if _exchange is None or _config is None:
        return jsonify({"ok": False, "message": "Bot has not been started — no exchange connection"}), 400

    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol")  # None → close all open positions

    closed = close_open_position(_exchange, _config, symbol=symbol)
    if closed:
        return jsonify({"ok": True, "message": "Position closed"})
    return jsonify({"ok": False, "message": "No open position to close"}), 404


@app.post("/deposit")
def deposit():
    """Set the paper trading starting balance (simulate a deposit)."""
    body = request.get_json(silent=True) or {}
    amount = body.get("amount")
    if not amount or float(amount) <= 0:
        return jsonify({"ok": False, "message": "Provide a positive 'amount'"}), 400

    with bot_state._lock:
        if any(p is not None for p in bot_state.positions.values()):
            return jsonify({"ok": False, "message": "Cannot change balance while a position is open"}), 409
        bot_state.metrics.paper_usdt = float(amount)
        bot_state.metrics.paper_holdings = {}
        bot_state.metrics.balance = float(amount)
        bot_state.metrics.pnl = 0.0
        bot_state.metrics.pnl_pct = 0.0
        bot_state.metrics.principal = float(amount)
        bot_state.metrics.profit_pool = 0.0
        bot_state.metrics.house_trade_active = False
        bot_state.metrics.balance_detail = f"Paper deposit: ${float(amount):,.2f} USDT"
        bot_state.metrics.peak_balance = float(amount)
        bot_state.metrics.consecutive_losses = 0
        bot_state.metrics.consecutive_wins = 0
        bot_state.metrics.total_trades = 0
        bot_state.metrics.win_count = 0
        bot_state.metrics.loss_count = 0
        bot_state.metrics.win_rate = 0.0
        bot_state.metrics.shield_active = False
        bot_state.metrics.trade_history = []

    bot_state.add_log("Deposit", f"Paper balance set to ${float(amount):,.2f} USDT", tone="positive")
    _save()
    return jsonify({"ok": True, "balance": float(amount)})


@app.post("/settings")
def settings():
    """Update runtime settings without restarting the bot."""
    body = request.get_json(silent=True) or {}
    allowed = {
        "trade_size_usdt", "trade_size_mode", "trade_size_pct",
        "stop_loss_pct", "take_profit_pct",
        "polling_seconds", "auto_mode", "rsi_oversold", "rsi_overbought",
        "house_profit_threshold", "house_take_profit_pct", "house_stop_loss_pct",
        "active_symbols",
        "shield_enabled", "shield_loss_streak", "shield_winrate_min",
        "shield_drawdown_pct", "shield_recovery_winrate",
    }
    filtered = {k: v for k, v in body.items() if k in allowed}

    if not filtered:
        return jsonify({"ok": False, "message": "No valid settings provided"}), 400

    bot_state.update_settings(**filtered)

    # pre_shield_mode lives in Metrics (it's what shield restores on recovery).
    # Allow setting it here so switching to "percent" sticks after shield lifts.
    if "pre_shield_mode" in body and body["pre_shield_mode"] in {"fixed", "percent", "house_money"}:
        with bot_state._lock:
            bot_state.metrics.pre_shield_mode = body["pre_shield_mode"]

    _save()
    with bot_state._lock:
        from dataclasses import asdict
        return jsonify({"ok": True, "settings": asdict(bot_state.settings)})


# ---------------------------------------------------------------------------
# React frontend (production build)
# ---------------------------------------------------------------------------

DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")


@app.get("/", defaults={"path": ""})
@app.get("/<path:path>")
def serve_react(path):
    full = os.path.join(DIST_DIR, path)
    if path and os.path.exists(full):
        return send_from_directory(DIST_DIR, path)
    return send_from_directory(DIST_DIR, "index.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("API_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"}
    app.run(host="0.0.0.0", port=port, debug=debug)
