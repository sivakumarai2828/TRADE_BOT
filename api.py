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

# ---------------------------------------------------------------------------
# Route auth — protect all mutation routes with X-Bot-Key header.
# Set BOT_API_KEY in .env. Requests from localhost are always allowed.
# GET/OPTIONS requests are always public (dashboard reads, health checks).
# ---------------------------------------------------------------------------
_BOT_API_KEY = os.getenv("BOT_API_KEY", "")

@app.before_request
def _require_api_key():
    if request.method in ("GET", "OPTIONS", "HEAD"):
        return  # read-only routes are public
    remote = request.remote_addr or ""
    is_localhost = remote in ("127.0.0.1", "::1", "localhost")
    if is_localhost:
        return  # internal calls (Telegram bot on same VM) always allowed
    if not _BOT_API_KEY:
        return  # key not configured → open (backwards compat for local dev)
    provided = request.headers.get("X-Bot-Key", "")
    if provided != _BOT_API_KEY:
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

import time as _time_module
_startup_time = _time_module.time()
_last_cycle_time = _time_module.time()  # updated every cycle; watchdog alerts if stale


def _watchdog_auto_recover() -> None:
    """Restart the bot loop thread when it's confirmed stuck beyond recovery."""
    global _bot_thread, _stop_event, _last_cycle_time
    logging.warning("Watchdog: triggering auto-recovery — restarting bot loop")
    from telegram_notify import _send
    _send("🔄 <b>Watchdog auto-recovery</b>\nBot loop was stuck 20+ min — force-restarting loop. Positions preserved.")

    _stop_event.set()
    if _bot_thread and _bot_thread.is_alive():
        _bot_thread.join(timeout=15)

    # Reset running so _start_crypto_bot_internal creates a fresh exchange + loop.
    with bot_state._lock:
        bot_state.running = False
    _last_cycle_time = _time_module.time()
    err = _start_crypto_bot_internal()
    if err:
        logging.error("Watchdog: recovery failed — %s", err)
    else:
        logging.info("Watchdog: bot loop restarted successfully")


def _watchdog_loop() -> None:
    """Auto-recover bot loop if stuck > 20 minutes. No alert for short API blips."""
    while True:
        _time_module.sleep(300)  # check every 5 minutes
        if bot_state.running:
            age = _time_module.time() - _last_cycle_time
            logging.debug("Watchdog: last cycle %.0f s ago", age)
            if age > 1200:  # 20 minutes — attempt auto-recovery + alert
                logging.warning("Watchdog: bot loop stalled for %.0f minutes", age / 60)
                try:
                    _watchdog_auto_recover()
                except Exception as exc:
                    logging.error("Watchdog auto-recovery failed: %s", exc)


# Flask debug reloader spawns a parent + child process — both would import this module
# and start a watchdog. Only start in the child (WERKZEUG_RUN_MAIN=true) or in
# non-debug runs where the env var is absent.
if os.environ.get("WERKZEUG_RUN_MAIN", "true") == "true":
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()

from daybot.blueprint import daybot_bp
from daybot.scheduler import start_scheduler
from telegram_bot import start_telegram_bot
app.register_blueprint(daybot_bp, url_prefix="/daybot")
start_scheduler()
start_telegram_bot()

# Options bot — paper trading on same VM as day bot (not on crypto-bot-vm)
if os.getenv("DISABLE_OPTIONS_BOT", "").lower() not in ("1", "true", "yes"):
    try:
        from optionsbot.blueprint import start_options_bot as _start_opts
        _opts_api_key = os.getenv("ALPACA_API_KEY") or os.getenv("EXCHANGE_API_KEY", "")
        _opts_secret  = os.getenv("ALPACA_SECRET_KEY") or os.getenv("EXCHANGE_API_SECRET", "")
        if _opts_api_key:
            _start_opts(_opts_api_key, _opts_secret, paper=True)
    except ModuleNotFoundError:
        pass  # optionsbot not deployed on this VM

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
    # Sanity check: if restored balance equals peak (Supabase snapshotted at historical peak)
    # and daily_start_balance is lower, the balance is stale — reset to daily_start.
    _ds = bot_state.metrics.daily_start_balance
    _bal = bot_state.metrics.balance
    _peak = bot_state.metrics.peak_balance
    if _ds > 0 and _bal > 0 and _bal == _peak and _bal > _ds * 1.5:
        bot_state.metrics.balance = _ds
        logging.warning("Supabase balance=%.2f equals peak (stale) — reset to daily_start=%.2f", _bal, _ds)
    # Immediately sync real Alpaca balance on startup (don't wait for first 5-min cycle)
    try:
        import os as _os
        _key = _os.getenv("EXCHANGE_API_KEY"); _secret = _os.getenv("EXCHANGE_API_SECRET")
        _real_bal = 0.0
        if _key and _secret:
            # Try Alpaca SDK first
            try:
                from alpaca.trading.client import TradingClient as _TC_startup
                _tc_s = _TC_startup(_key, _secret, paper=True)
                _acct_s = _tc_s.get_account()
                _real_bal = round(float(_acct_s.equity), 2)
            except Exception:
                # Fallback: CCXT fetch_balance — use TOTAL to include open crypto positions
                try:
                    import ccxt as _ccxt
                    _ex = _ccxt.alpaca({"apiKey": _key, "secret": _secret, "options": {"defaultType": "crypto"}})
                    _bal_info = _ex.fetch_balance()
                    _usdt = float(_bal_info.get("USDT", {}).get("total", 0) or 0)
                    _usd  = float(_bal_info.get("USD",  {}).get("total", 0) or 0)
                    _real_bal = round(_usdt or _usd, 2)
                except Exception as _ccxt_e:
                    logging.warning("Startup CCXT balance fallback failed: %s", _ccxt_e)
            if _real_bal > 0:
                bot_state.metrics.balance = _real_bal
                bot_state.metrics.balance_detail = f"Live (Alpaca): USD {_real_bal:,.2f}"
                if bot_state.metrics.peak_balance >= 9_000:
                    bot_state.metrics.peak_balance = _real_bal
                logging.info("Startup: real Alpaca equity synced — $%.2f", _real_bal)
    except Exception as _e:
        logging.warning("Startup balance sync failed: %s", _e)

# Restore daybot evening analysis + India analysis from DB on startup.
try:
    from daybot.evening_db import load_evening_analysis
    from daybot.state import day_state as _day_state
    from datetime import date as _date
    _today = _date.today().isoformat()
    from datetime import timedelta as _td; _tomorrow = (_date.today() + _td(days=1)).isoformat()
    for _td in filter(None, [_tomorrow, _today]):
        _row = load_evening_analysis(_td)
        if _row:
            import json as _json

            def _load(val, default):
                if isinstance(val, (list, dict)):
                    return val
                try:
                    return _json.loads(val) if val else default
                except Exception:
                    return default

            with _day_state._lock:
                _day_state.evening_approved = _load(_row.get("approved"), [])
                _day_state.evening_entry_zones = _load(_row.get("entry_zones"), {})
                _day_state.evening_stop_levels = _load(_row.get("stop_levels"), {})
                _day_state.evening_targets = _load(_row.get("targets"), {})
                _day_state.evening_direction = _load(_row.get("direction"), {})
                _day_state.evening_notes = _load(_row.get("notes"), {})
                _day_state.evening_risk_flags = _load(_row.get("risk_flags"), {})
                _day_state.evening_regime = _row.get("regime", "")
                _day_state.evening_analysis_date = _td
            logging.info("Evening analysis restored from DB for %s (%d stocks)", _td, len(_day_state.evening_approved))
            break
except Exception as _exc:
    logging.warning("Evening analysis DB restore failed: %s", _exc)

try:
    from daybot.india_agent import load_india_results_from_db
    load_india_results_from_db()
except Exception as _exc:
    logging.warning("India analysis DB restore failed: %s", _exc)

# Restore today's day bot session metrics (wins, losses, pnl, portfolio) from DB.
try:
    from daybot.state import day_state as _day_state
    from persistence import _get_client as _pgc
    import datetime as _dt
    _today_str = _dt.date.today().isoformat()
    _pc = _pgc()
    if _pc:
        _sr = _pc.table("daybot_market_sessions").select("*").eq("trade_date", _today_str).limit(1).execute()
        if _sr.data:
            _row = _sr.data[0]
            _daily_pnl = float(_row.get("daily_pnl", 0.0) or 0.0)
            # Compute cumulative portfolio value: $1000 starting + sum of all sessions
            _all = _pc.table("daybot_market_sessions").select("daily_pnl").execute()
            _total_pnl = sum(float(r.get("daily_pnl") or 0) for r in _all.data)
            _portfolio_value = 1000.0 + _total_pnl
            with _day_state._lock:
                _day_state.metrics.wins_today = _row.get("wins", 0) or 0
                _day_state.metrics.losses_today = _row.get("losses", 0) or 0
                _day_state.metrics.daily_pnl = _daily_pnl
                _day_state.metrics.portfolio_value = _portfolio_value
                _day_state.metrics.daily_start_value = _portfolio_value - _daily_pnl
            logging.info("Day bot session restored: %s wins=%d losses=%d pnl=%.2f portfolio=%.2f",
                         _today_str, _day_state.metrics.wins_today, _day_state.metrics.losses_today,
                         _daily_pnl, _portfolio_value)
except Exception as _exc:
    logging.warning("Day bot session restore failed: %s", _exc)

# --- Startup Telegram alert ---
def _send_startup_alert() -> None:
    import os, requests as _req, platform
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import psutil
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        mem_line = f"💾 RAM: {mem.used // 1024 // 1024}MB / {mem.total // 1024 // 1024}MB"
        swap_line = f"🔄 Swap: {swap.used // 1024 // 1024}MB / {swap.total // 1024 // 1024}MB"
    except Exception:
        mem_line = ""
        swap_line = ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "🚀 <b>Trade Bot Service Started</b>",
        f"🕐 {now}",
        "━━━━━━━━━━━━━━━",
        "✅ Crypto Bot: auto-starting…",
        "✅ Day Bot: auto-starts at 9:35 AM ET",
        "✅ Scheduler: running",
    ]
    if mem_line:
        lines += ["━━━━━━━━━━━━━━━", mem_line, swap_line]
    try:
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass

threading.Thread(target=_send_startup_alert, daemon=True, name="startup-alert").start()


def _auto_start_bots() -> None:
    """Auto-start crypto bot and day bot (if market hours) on service startup."""
    import time as _t
    _t.sleep(10)  # let Flask + scheduler fully initialise first

    # --- Crypto bot: always start ---
    # On fresh service start no thread can be running — reset flag so start proceeds.
    with bot_state._lock:
        bot_state.running = False
    try:
        err = _start_crypto_bot_internal()
        if err:
            logging.warning("Crypto bot auto-start failed: %s", err)
        else:
            logging.info("Crypto bot auto-started on service startup")
    except Exception as exc:
        logging.warning("Crypto bot auto-start exception: %s", exc)

    # --- Day bot: start only if mid-session restart (9:35–15:55 ET, Mon–Fri) ---
    if os.getenv("DISABLE_DAY_BOT", "").lower() not in ("1", "true", "yes"):
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            is_weekday = now_et.weekday() < 5
            et_minutes = now_et.hour * 60 + now_et.minute
            in_trading_hours = (9 * 60 + 35) <= et_minutes <= (15 * 60 + 55)
            if is_weekday and in_trading_hours:
                from daybot.scheduler import job_autostart
                job_autostart()
                logging.info("Day bot auto-started on mid-session service restart (%02d:%02d ET)",
                             now_et.hour, now_et.minute)
        except Exception as exc:
            logging.warning("Day bot auto-start exception: %s", exc)


threading.Thread(target=_auto_start_bots, daemon=True, name="bot-autostart").start()

# ---------------------------------------------------------------------------
# Bot thread management
# ---------------------------------------------------------------------------

_bot_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_exchange = None
_config = None
_crypto_mode_manager = None   # CryptoModeManager — created on /start
_harvester = None             # ProfitHarvester — created on /start

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

        # API timeout alerts suppressed — backoff handles retries, watchdog alerts if truly stuck


def _handle_crypto_mode_switch(new_mode: str, old_mode: str) -> None:
    """Apply new mode params to state and send Telegram alert."""
    from telegram_notify import notify_mode_change
    params = _crypto_mode_manager.params()
    bot_state.update_settings(current_mode=new_mode)
    bot_state.add_log(
        "Mode",
        f"{old_mode} → {new_mode} | size ×{params.size_multiplier} "
        f"SL={params.stop_loss_pct:.1f}% TP={params.take_profit_pct:.1f}%",
        "positive" if new_mode == "AGGRESSIVE" else "warning" if new_mode == "SHIELD" else "neutral",
    )
    notify_mode_change("CryptoBot", old_mode, new_mode, params)


def _run_cycle() -> None:
    """Run a cycle for every active symbol, then update live balance once."""
    global _exchange, _config, _last_cycle_time
    _last_cycle_time = _time_module.time()

    # Adaptive mode evaluation — runs once per cycle before any symbol
    if _crypto_mode_manager is not None:
        from strategy import _get_htf_trend
        btc_trend = _get_htf_trend(_exchange, "BTC/USD")
        new_mode, old_mode = _crypto_mode_manager.evaluate(bot_state.metrics, btc_trend=btc_trend)
        if old_mode is not None:
            _handle_crypto_mode_switch(new_mode, old_mode)

    for symbol in list(bot_state.settings.active_symbols):
        _run_symbol_cycle(symbol)
        _time_module.sleep(2)  # stagger fetches to reduce concurrent I/O on e2-micro

    try:
        monitor_positions(_exchange, _config)
    except Exception as exc:
        logging.warning("monitor_positions error (skipped): %s", exc)

    if _config is not None and not _config.dry_run:
        try:
            # Alpaca uses USD equity — use TradingClient for accurate balance
            try:
                from alpaca.trading.client import TradingClient as _TC
                _tc = _TC(_config.api_key, _config.api_secret, paper=True)
                _acct = _tc.get_account()
                new_bal = round(float(_acct.equity), 2)
            except Exception:
                # Fallback: ccxt fetch_balance — use TOTAL (free+used) to include
                # open crypto position value, not just free cash (which omits holdings).
                balance_info = _exchange.fetch_balance()
                usdt_total = float(balance_info.get("USDT", {}).get("total", 0) or 0)
                usd_total  = float(balance_info.get("USD",  {}).get("total", 0) or 0)
                new_bal = round(usdt_total or usd_total, 2)
            if new_bal > 0:
                bot_state.update_metrics(balance=new_bal, balance_detail=f"Live (Alpaca): USD {new_bal:,.2f}")
                # Fix peak_balance - defaults to 0k paper value; sync to real balance on first live fetch
                if bot_state.metrics.peak_balance >= 9_000:
                    bot_state.metrics.peak_balance = new_bal
        except Exception:
            pass


def _send_daily_summary() -> None:
    """Send end-of-day summary to Telegram and trigger harvest extraction."""
    from telegram_notify import notify_daily_summary, notify_harvest_extraction
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

    # Harvest extraction at daily reset
    try:
        if _harvester and daily_pnl > 0:
            from strategy import _get_htf_trend
            btc_trend = _get_htf_trend(_exchange, "BTC/USD") if _exchange else "neutral"
            regime = "trending_up" if btc_trend == "up" else "trending_down" if btc_trend == "down" else "choppy"
            extracted = _harvester.check_and_extract(
                daily_pnl=daily_pnl,
                bot_type="crypto",
                watchlist=[],
                market_regime=regime,
            )
            if extracted:
                notify_harvest_extraction("CryptoBot", extracted, regime)
            # Also monitor existing harvest positions
            _harvester.monitor(
                bot_type="crypto",
                watchlist=[],
                market_regime=regime,
                on_base_increase=lambda bonus: bot_state.update_metrics(
                    paper_usdt=round(bot_state.metrics.paper_usdt + bonus, 2)
                ),
            )
    except Exception as exc:
        logging.warning("Harvest daily check failed: %s", exc)


def _bot_loop() -> None:
    """Runs _run_cycle() on each tick until _stop_event is set."""
    from telegram_notify import notify_bot_started, notify_bot_stopped
    logging.info("Bot loop started")
    bot_state.add_log("Bot started", "Trading loop is running", tone="positive")
    notify_bot_started()

    while not _stop_event.is_set():
        if bot_state.check_daily_reset():
            _send_daily_summary()
        try:
            _run_cycle()
        except Exception as exc:
            logging.exception("Unhandled cycle error — loop continues: %s", exc)
            bot_state.add_log("Cycle crash", str(exc)[:120], tone="negative")
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


def _start_crypto_bot_internal() -> str | None:
    """Start the crypto bot loop. Returns error message string or None on success."""
    global _bot_thread, _stop_event, _exchange, _config, _crypto_mode_manager, _harvester

    with bot_state._lock:
        if bot_state.running:
            return "Bot is already running"

    try:
        _config = load_config()
        _exchange = create_exchange(_config)
        reconcile_positions(_exchange, _config)
        # Fix peak_balance — default is $10k paper value; reset to real balance on live startup
        if not _config.dry_run and bot_state.metrics.peak_balance >= 9_000:
            bot_state.metrics.peak_balance = bot_state.metrics.balance
            logging.info("peak_balance reset to real balance: $%.2f", bot_state.metrics.balance)
    except Exception as exc:
        msg = f"Failed to initialise exchange: {exc}"
        logging.error(msg)
        bot_state.add_log("Start error", msg[:120], tone="negative")
        return msg

    from crypto_mode_manager import CryptoModeManager
    from harvest.manager import ProfitHarvester
    _crypto_mode_manager = CryptoModeManager()
    # Restore persisted mode on restart — don't reset to SAFE every restart
    _persisted_mode = bot_state.settings.current_mode
    if _persisted_mode in ("SAFE", "AGGRESSIVE", "SHIELD"):
        _crypto_mode_manager._mode = _persisted_mode
        # Restore _trades_at_switch so anti-flip guard requires NEW trades before re-evaluating
        # (without this, mode re-evaluates immediately and drops AGGRESSIVE with 0 streak)
        _crypto_mode_manager._trades_at_switch = bot_state.metrics.total_trades
        logging.info("Mode manager restored: %s | trades_at_switch=%d",
                     _persisted_mode, bot_state.metrics.total_trades)
    _harvester = ProfitHarvester(
        anthropic_api_key=_config.anthropic_api_key,
        alpaca_api_key=_config.api_key,
        alpaca_secret_key=_config.api_secret,
        claude_model=_config.anthropic_model,
    )

    _stop_event = threading.Event()
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
    _bot_thread.start()

    with bot_state._lock:
        bot_state.running = True

    return None


@app.post("/start")
def start():
    """Start the bot loop. Optional JSON body can override runtime settings."""
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

    err = _start_crypto_bot_internal()
    if err:
        return jsonify({"ok": False, "message": err}), 409 if "already running" in err else 500
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


# ---------------------------------------------------------------------------
# Options Bot endpoints
# ---------------------------------------------------------------------------

@app.get("/optionsbot/status")
def optionsbot_status():
    try:
        from optionsbot.state import options_state
    except ModuleNotFoundError:
        return jsonify({"running": False, "positions": {}, "metrics": {}, "logs": []})
    with options_state._lock:
        positions = {
            cs: {
                "symbol": p.symbol,
                "contract_symbol": p.contract_symbol,
                "option_type": p.option_type,
                "strike": p.strike,
                "expiry": p.expiry,
                "qty": p.qty,
                "entry_premium": p.entry_premium,
                "current_premium": p.current_premium,
                "sl_price": p.sl_price,
                "tp_price": p.tp_price,
                "entry_time": p.entry_time,
                "pnl": p.pnl,
                "pnl_pct": p.pnl_pct,
            }
            for cs, p in options_state.positions.items()
        }
        m = options_state.metrics
        metrics = {
            "wins_today": m.wins_today,
            "losses_today": m.losses_today,
            "daily_pnl": m.daily_pnl,
            "total_trades": m.total_trades,
            "win_rate": m.win_rate,
            "mode": m.mode,
            "balance": m.balance,
            "daily_loss_halted": m.daily_loss_halted,
        }
    return jsonify({
        "running": options_state.running,
        "positions": positions,
        "metrics": metrics,
        "logs": options_state.logs[:30],
    })


@app.post("/optionsbot/start")
def optionsbot_start():
    try:
        from optionsbot.state import options_state
        from optionsbot.blueprint import start_options_bot
    except ModuleNotFoundError:
        return jsonify({"ok": False, "message": "optionsbot not available on this VM"}), 404
    if options_state.running:
        return jsonify({"ok": False, "message": "Already running"})
    api_key = os.getenv("ALPACA_API_KEY") or os.getenv("EXCHANGE_API_KEY", "")
    secret  = os.getenv("ALPACA_SECRET_KEY") or os.getenv("EXCHANGE_API_SECRET", "")
    start_options_bot(api_key, secret, paper=True)
    return jsonify({"ok": True, "message": "Options bot started (paper)"})


@app.post("/optionsbot/stop")
def optionsbot_stop():
    try:
        from optionsbot.blueprint import stop_options_bot
    except ModuleNotFoundError:
        return jsonify({"ok": False, "message": "optionsbot not available on this VM"}), 404
    stop_options_bot()
    return jsonify({"ok": True, "message": "Options bot stopped"})


@app.post("/reset-halt")
def reset_halt():
    """Clear daily loss halt so trading resumes immediately."""
    with bot_state._lock:
        bot_state.metrics.daily_loss_halted = False
        bot_state.metrics.daily_start_balance = bot_state.metrics.balance
    bot_state.add_log("Reset", "Daily loss halt cleared — trading resumed", tone="neutral")
    return jsonify({"ok": True})


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

    if not filtered and "daily_trades_limit" not in body and "pre_shield_mode" not in body:
        return jsonify({"ok": False, "message": "No valid settings provided"}), 400

    bot_state.update_settings(**filtered)

    # daily_trades_limit lives in Metrics — update live without restart
    if "daily_trades_limit" in body:
        try:
            with bot_state._lock:
                bot_state.metrics.daily_trades_limit = int(body["daily_trades_limit"])
        except (ValueError, TypeError):
            pass

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


@app.route("/profit-report", methods=["GET", "POST"])
def profit_report():
    import requests as _req
    from telegram_notify import notify_profit_status
    from daybot.state import day_state
    dm = day_state.metrics

    # Fetch real crypto data from crypto-bot-vm
    crypto_url = os.getenv("CRYPTO_BOT_URL", "")
    cm_balance, cm_wr, cm_trades, cm_running = 500.0, 0.0, 0, False
    if crypto_url:
        try:
            cr = _req.get(f"{crypto_url}/status", timeout=5)
            if cr.ok:
                cd = cr.json()
                cmet = cd.get("metrics", {})
                cm_balance = cmet.get("balance", 500.0)
                cm_wr = cmet.get("win_rate", 0.0)
                cm_trades = cmet.get("daily_trades_count", 0)
                cm_running = cd.get("running", False)
        except Exception:
            pass

    notify_profit_status(
        crypto_balance=cm_balance,
        crypto_principal=500.0,
        crypto_wr=cm_wr,
        crypto_trades_today=cm_trades,
        crypto_running=cm_running,
        day_portfolio=dm.portfolio_value,
        day_daily_pnl=dm.daily_pnl or 0.0,
        day_wins=dm.wins_today,
        day_losses=dm.losses_today,
        day_running=day_state.running,
    )
    return jsonify({"ok": True, "message": "Profit status sent to Telegram"})


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
