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
from execution import close_open_position, create_exchange, execute_trade, monitor_positions, _try_house_money_trade
from persistence import load_state, save_state
from state import bot_state
from strategy import calculate_indicators, generate_signal, get_market_data


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)  # Allow the Vite dev server (port 5173) to call this API

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Restore persisted state from Supabase on startup.
_saved = load_state()
if _saved:
    m = _saved.get("metrics", {})
    s = _saved.get("settings", {})
    if m:
        bot_state.update_metrics(**{k: v for k, v in m.items() if k != "paper_holdings"})
        bot_state.metrics.paper_usdt = m.get("paper_usdt", bot_state.metrics.paper_usdt)
        bot_state.metrics.paper_holdings = m.get("paper_holdings", {})
    if s:
        bot_state.update_settings(**s)
    logging.info("State restored from Supabase — balance=%.2f", bot_state.metrics.balance)

# ---------------------------------------------------------------------------
# Bot thread management
# ---------------------------------------------------------------------------

_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_exchange = None
_config = None


def _run_symbol_cycle(symbol: str) -> None:
    """One complete trading cycle for a single symbol."""
    global _exchange, _config

    try:
        data = get_market_data(exchange=_exchange, symbol=symbol, timeframe=_config.timeframe, limit=_config.candle_limit)
        data = calculate_indicators(data)
        latest_price = float(data.iloc[-1]["close"])

        result = generate_signal(data, _config, symbol=symbol)

        if bot_state.settings.auto_mode:
            execute_trade(_exchange, _config, symbol, result.action, latest_price)

        # House Money fires only on the first/primary symbol to avoid pool fragmentation.
        if symbol == bot_state.settings.active_symbols[0]:
            _try_house_money_trade(_exchange, _config, symbol, latest_price)

    except Exception as exc:
        logging.exception("Cycle error [%s]: %s", symbol, exc)
        bot_state.add_log("Cycle error", f"[{symbol}] {str(exc)[:100]}", tone="negative")


def _run_cycle() -> None:
    """Run a cycle for every active symbol, then update live balance once."""
    global _exchange, _config

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


def _bot_loop() -> None:
    """Runs _run_cycle() on each tick until _stop_event is set."""
    from telegram_notify import notify_bot_started, notify_bot_stopped
    logging.info("Bot loop started")
    bot_state.add_log("Bot started", "Trading loop is running", tone="positive")
    notify_bot_started()

    while not _stop_event.is_set():
        _run_cycle()
        polling = bot_state.settings.polling_seconds
        _stop_event.wait(timeout=polling)

    logging.info("Bot loop stopped")
    bot_state.add_log("Bot stopped", "Trading loop has been stopped", tone="warning")
    notify_bot_stopped()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

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
        # Let runtime settings override .env values.
        _config = _config  # BotConfig is frozen; settings in bot_state take precedence at use time.
        _exchange = create_exchange(_config)
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
    save_state(bot_state.metrics, bot_state.settings)
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
