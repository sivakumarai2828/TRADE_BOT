"""Flask blueprint — all /daybot/* endpoints and the intraday trading loop."""
from __future__ import annotations

import functools
import logging
import threading
from datetime import datetime, timezone, time as dtime

from flask import Blueprint, jsonify

from .ai_validator import AIValidator
from .config import load_config
from .db import build_ai_history_context, save_trade as db_save_trade, upsert_market_session
from .executor import TradeExecutor
from .filters import StockFilter, has_earnings_soon
from .indicators import add_indicators
from .logger import TradeLogger
from .position_monitor import PositionMonitor
from .risk_manager import RiskManager
from .scanner import MarketScanner
from .state import DayPosition, DaySignal, day_state
from .strategy import generate_signal

daybot_bp = Blueprint("daybot", __name__)

# ---------------------------------------------------------------------------
# Module singletons (created on first /daybot/start)
# ---------------------------------------------------------------------------
_config = None
_scanner: MarketScanner | None = None
_filter = StockFilter()
_ai: AIValidator | None = None
_executor: TradeExecutor | None = None
_monitor: PositionMonitor | None = None
_risk: RiskManager | None = None
_logger: TradeLogger | None = None

_bot_thread: threading.Thread | None = None
_stop_event = threading.Event()
_mode_manager = None   # DayModeManager — created on /start
_harvester = None      # ProfitHarvester — created on /start

# Trading windows (ET): active 9:50–11:30, 14:00–15:30; close-only 15:30–15:50
# 9:35–9:50 is the opening range — most chaotic, institutions are still positioning.
# No new entries until 9:50 when price action stabilises.
_WINDOWS = [
    (dtime(9, 50), dtime(11, 30)),
    (dtime(14, 0), dtime(15, 30)),
]
_CLOSE_ONLY_START = dtime(15, 30)
_CLOSE_ONLY_END = dtime(15, 50)


def _et_now() -> dtime:
    """Current time in US/Eastern with proper DST handling."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).time()


def _in_trading_window() -> bool:
    now = _et_now()
    return any(start <= now <= end for start, end in _WINDOWS)


def _in_close_only_window() -> bool:
    now = _et_now()
    return _CLOSE_ONLY_START <= now <= _CLOSE_ONLY_END


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

_API_TIMEOUT = 12  # seconds — Alpaca SDK has no built-in timeout; wrap to prevent loop stall


def _run_with_timeout(fn, *args, timeout=_API_TIMEOUT, **kwargs):
    """Run fn in a daemon thread. Returns (result, timed_out).
    ThreadPoolExecutor.shutdown(wait=True) blocks on hung threads — daemon threads don't.
    """
    result_box = [None]
    exc_box = [None]

    def _target():
        try:
            result_box[0] = fn(*args, **kwargs)
        except Exception as e:
            exc_box[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None, True  # timed out — thread still running but won't block loop
    if exc_box[0]:
        raise exc_box[0]
    return result_box[0], False

# Singleton data client — reuse one TCP+TLS connection instead of handshaking per call
_data_client = None

# Circuit breaker — pause trading if Alpaca is repeatedly unreachable
_alpaca_fail_count = 0
_alpaca_pause_until = 0.0
_ALPACA_FAIL_THRESHOLD = 3   # consecutive timeouts before pause
_ALPACA_PAUSE_SECONDS = 300  # 5 min cooldown


def _alpaca_ok() -> bool:
    """Return False during circuit-breaker pause."""
    import time as _t
    global _alpaca_fail_count, _alpaca_pause_until
    if _alpaca_pause_until and _t.time() < _alpaca_pause_until:
        return False
    if _alpaca_pause_until and _t.time() >= _alpaca_pause_until:
        _alpaca_pause_until = 0.0
        _alpaca_fail_count = 0
        logging.info("Alpaca circuit breaker reset — resuming")
    return True


def _alpaca_record_failure() -> None:
    import time as _t
    global _alpaca_fail_count, _alpaca_pause_until
    _alpaca_fail_count += 1
    if _alpaca_fail_count >= _ALPACA_FAIL_THRESHOLD:
        _alpaca_pause_until = _t.time() + _ALPACA_PAUSE_SECONDS
        logging.warning("Alpaca circuit breaker OPEN — pausing %ds after %d failures",
                        _ALPACA_PAUSE_SECONDS, _alpaca_fail_count)
        try:
            from telegram_notify import notify_api_timeout
            notify_api_timeout("DayBot", "data.alpaca.markets", _alpaca_fail_count)
        except Exception:
            pass


def _alpaca_record_success() -> None:
    global _alpaca_fail_count
    _alpaca_fail_count = 0


def _patch_alpaca_timeout(client, seconds: int = 10):
    """Inject a default timeout into an Alpaca SDK client's requests.Session."""
    orig = client._session.request

    @functools.wraps(orig)
    def _request(method, url, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return orig(method, url, **kwargs)

    client._session.request = _request
    return client


def _get_data_client():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        _data_client = StockHistoricalDataClient(_config.alpaca_api_key, _config.alpaca_secret_key)
        _patch_alpaca_timeout(_data_client, 10)
    return _data_client


def _fetch_bars(symbol: str, limit: int = 100) -> dict | None:
    """Fetch intraday bars and compute indicators. Returns dict or None on error."""
    if not _alpaca_ok():
        return None
    try:
        result, timed_out = _run_with_timeout(_fetch_bars_impl, symbol, limit)
        if timed_out:
            logging.warning("Bar fetch timed out [%s]", symbol)
            _alpaca_record_failure()
            return None
        _alpaca_record_success()
        return result
    except Exception as exc:
        logging.warning("Bar fetch failed [%s]: %s", symbol, exc)
        return None


def _fetch_bars_impl(symbol: str, limit: int = 100) -> dict | None:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from datetime import timedelta
    import pandas as pd

    from alpaca.data.enums import DataFeed
    client = _get_data_client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)  # fetch last 5 days to get enough bars
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start, end=end, limit=limit,
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if df is None or df.empty:
        return None

    # Flatten multi-index if present
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol") if symbol in df.index.get_level_values("symbol") else df

    df = df.rename(columns={"open": "open", "high": "high", "low": "low",
                             "close": "close", "volume": "volume"})
    if len(df) < 20:
        return None

    df = add_indicators(df)
    latest = df.dropna(subset=["ema_50", "rsi"]).iloc[-1]
    prev_close = float(df["close"].iloc[-2]) if len(df) >= 2 else float(latest["close"])

    vwap = float(latest["vwap"]) if "vwap" in latest and not pd.isna(latest["vwap"]) else 0.0
    return {
        "symbol": symbol,
        "price": float(latest["close"]),
        "ema": float(latest["ema_50"]),
        "rsi": float(latest["rsi"]),
        "volume": float(latest["volume"]),
        "avg_volume": float(latest["vol_avg"]) if latest["vol_avg"] > 0 else 1,
        "day_change_pct": (float(latest["close"]) - prev_close) / prev_close * 100,
        "vwap": vwap,
    }


# Cache weekly context per symbol to avoid fetching every cycle (refresh every 6 hours)
_weekly_cache: dict[str, tuple[float, dict]] = {}  # symbol -> (timestamp, context)
_WEEKLY_TTL = 6 * 3600  # 6 hours


def _fetch_weekly_context(symbol: str) -> dict | None:
    """Fetch 4 weeks of daily bars and return a compact summary for Claude."""
    import time
    now_ts = time.time()
    cached = _weekly_cache.get(symbol)
    if cached and (now_ts - cached[0]) < _WEEKLY_TTL:
        return cached[1]

    try:
        result, timed_out = _run_with_timeout(_fetch_weekly_context_impl, symbol, now_ts)
        if timed_out:
            logging.warning("Weekly context fetch timed out [%s]", symbol)
            return None
        return result
    except Exception as exc:
        logging.warning("Weekly context fetch failed [%s]: %s", symbol, exc)
        return None


def _fetch_weekly_context_impl(symbol: str, now_ts: float) -> dict | None:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from datetime import timedelta
    import pandas as pd

    from alpaca.data.enums import DataFeed
    client = _get_data_client()
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=5)  # 5 weeks to guarantee 4 full weeks of trading days

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start, end=end,
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if df is None or df.empty:
        return None

    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol") if symbol in df.index.get_level_values("symbol") else df

    df = df.rename(columns={"open": "open", "high": "high", "low": "low",
                             "close": "close", "volume": "volume"})
    df = df.sort_index()

    if len(df) < 10:
        return None

    closes = df["close"].values

    # Split into 4 weekly buckets (approx 5 trading days each)
    days = min(len(df), 20)
    df_recent = df.iloc[-days:]
    closes_r = df_recent["close"].values
    vols_r = df_recent["volume"].values

    week_chunks = [closes_r[i:i+5] for i in range(0, min(20, len(closes_r)), 5)]
    weekly_returns = []
    for chunk in week_chunks[-4:]:
        if len(chunk) >= 2:
            ret = (chunk[-1] - chunk[0]) / chunk[0] * 100
            weekly_returns.append(round(ret, 2))

    four_week_high = float(df_recent["high"].max())
    four_week_low = float(df_recent["low"].min())
    current_price = float(closes[-1])
    price_range = four_week_high - four_week_low
    position_in_range = round((current_price - four_week_low) / price_range * 100, 1) if price_range > 0 else 50.0

    four_week_return = round((closes_r[-1] - closes_r[0]) / closes_r[0] * 100, 2) if len(closes_r) >= 2 else 0.0

    # Simple support/resistance: recent swing low/high
    support = round(float(df_recent["low"].iloc[-10:].min()), 2)
    resistance = round(float(df_recent["high"].iloc[-10:].max()), 2)

    # Volume trend: compare last week avg vs 3-week avg
    last_week_vol = float(vols_r[-5:].mean()) if len(vols_r) >= 5 else 0
    prior_vol = float(vols_r[:-5].mean()) if len(vols_r) > 5 else last_week_vol
    vol_trend = "rising" if last_week_vol > prior_vol * 1.05 else "falling" if last_week_vol < prior_vol * 0.95 else "stable"

    context = {
        "weekly_returns": weekly_returns,
        "four_week_return_pct": four_week_return,
        "four_week_high": round(four_week_high, 2),
        "four_week_low": round(four_week_low, 2),
        "position_in_range_pct": position_in_range,
        "support": support,
        "resistance": resistance,
        "volume_trend": vol_trend,
    }
    _weekly_cache[symbol] = (now_ts, context)
    logging.info("Weekly context [%s]: 4wk_return=%.1f%% pos_in_range=%.0f%%", symbol, four_week_return, position_in_range)
    return context


def _get_spy_return() -> float:
    """Return today's SPY % change (used for market regime tagging)."""
    try:
        snapshot = _fetch_weekly_context("SPY")
        if snapshot and snapshot.get("weekly_returns"):
            return snapshot["weekly_returns"][-1]
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Main trading loop
# ---------------------------------------------------------------------------

def _handle_mode_switch(new_mode: str, old_mode: str) -> None:
    """Apply new mode params to state and send Telegram alert."""
    from telegram_notify import notify_mode_change
    params = _mode_manager.params()
    with day_state._lock:
        day_state.metrics.current_mode = new_mode
        day_state.metrics.position_size_pct = params.position_size_pct
        day_state.normal_size_pct = params.position_size_pct
    day_state.add_log(
        "Mode", f"{old_mode} → {new_mode} | size={params.position_size_pct*100:.0f}% "
        f"SL={params.stop_loss_pct*100:.1f}% TP={params.take_profit_pct*100:.1f}%",
        "positive" if new_mode == "AGGRESSIVE" else "warning" if new_mode == "SHIELD" else "neutral",
    )
    notify_mode_change("DayBot", old_mode, new_mode, params)


def _run_cycle() -> None:
    global _scanner, _risk, _monitor, _ai, _logger

    try:
        pv, timed_out = _run_with_timeout(_executor.get_portfolio_value)
        portfolio_value = pv if not timed_out and pv is not None else (day_state.metrics.portfolio_value or 0.0)
        if timed_out:
            logging.warning("get_portfolio_value timed out — using cached value")
    except Exception as exc:
        logging.warning("get_portfolio_value failed: %s", exc)
        portfolio_value = day_state.metrics.portfolio_value or 0.0

    try:
        mkt_open, timed_out = _run_with_timeout(_executor.is_market_open)
        if timed_out:
            mkt_open = day_state.metrics.market_open
    except Exception:
        mkt_open = day_state.metrics.market_open

    with day_state._lock:
        day_state.metrics.portfolio_value = portfolio_value
        day_state.metrics.market_open = mkt_open

    # --- Close-only window: close all, persist session, stop bot (runs once) ---
    if _in_close_only_window():
        if not getattr(_run_cycle, "_eod_done", False):
            _run_cycle._eod_done = True
            if day_state.positions:
                day_state.add_log("EOD", "Close-only window — closing all positions", "warning")
                _executor.close_all_positions()
                with day_state._lock:
                    day_state.positions.clear()
            _logger.generate_eod_report()
            m = day_state.metrics
            spy_ret = _get_spy_return()
            regime = "trending_up" if spy_ret > 0.3 else "trending_down" if spy_ret < -0.3 else "choppy"
            upsert_market_session(
                spy_return_pct=spy_ret, market_regime=regime,
                total_trades=m.trades_today, wins=m.wins_today,
                losses=m.losses_today, daily_pnl=m.daily_pnl,
            )
            _stop_bot_internal()
        return

    # --- Monitor existing positions every cycle ---
    try:
        _, timed_out = _run_with_timeout(_monitor.monitor_positions)
        if timed_out:
            logging.warning("Position monitor timed out — skipping")
    except Exception as exc:
        logging.warning("Position monitor error: %s", exc)

    # --- Check daily loss limit ---
    if _risk.check_daily_loss(portfolio_value):
        with day_state._lock:
            already_halted = day_state.metrics.daily_loss_halted
            day_state.metrics.daily_loss_halted = True
        if not already_halted:
            from telegram_notify import notify_daily_loss_halted
            m = day_state.metrics
            start = m.daily_start_value or portfolio_value
            loss_pct = (start - portfolio_value) / start * 100 if start > 0 else 0
            notify_daily_loss_halted("DayBot", loss_pct)
        day_state.add_log("Risk", "Daily loss limit hit — no new trades today", "negative")
        return

    # --- Refresh watchlist every N minutes (runs regardless of trading window) ---
    scan_interval = _config.scan_interval_minutes * 60
    now_ts = datetime.now(timezone.utc).timestamp()
    if not hasattr(_run_cycle, "_last_scan") or (now_ts - _run_cycle._last_scan) >= scan_interval:
        try:
            symbols, timed_out = _run_with_timeout(_scanner.run_scan)
            if timed_out:
                logging.warning("Scanner timed out — keeping previous watchlist")
                symbols = list(day_state.watchlist) or []
        except Exception as exc:
            logging.warning("Scanner error: %s", exc)
            symbols = list(day_state.watchlist) or []
        _logger.log_scan(symbols)
        with day_state._lock:
            day_state.watchlist = symbols
        _run_cycle._last_scan = now_ts

    # --- Skip new entries outside trading windows ---
    if not _in_trading_window():
        return

    # --- No-trade alert after 90 min in window with 0 trades ---
    if not getattr(_run_cycle, "_no_trade_alerted", False):
        if not hasattr(_run_cycle, "_window_entry_time"):
            _run_cycle._window_entry_time = datetime.now(timezone.utc).timestamp()
        elapsed = (datetime.now(timezone.utc).timestamp() - _run_cycle._window_entry_time) / 60
        if elapsed >= 90 and day_state.metrics.trades_today == 0:
            _run_cycle._no_trade_alerted = True
            from telegram_notify import notify_no_trades_alert
            notify_no_trades_alert("DayBot", int(elapsed))

    # --- Adaptive mode evaluation ---
    spy_ret = _get_spy_return()
    new_mode, old_mode = _mode_manager.evaluate(day_state.metrics, spy_return=spy_ret)
    if old_mode is not None:
        _handle_mode_switch(new_mode, old_mode)
    mode_params = _mode_manager.params()

    # --- Per-symbol cycle (only pre-market approved stocks if list is available) ---
    approved = day_state.premarket_approved
    if approved:
        filtered = [s for s in day_state.watchlist if s in approved]
        universe = filtered if filtered else list(day_state.watchlist)
    else:
        universe = list(day_state.watchlist)
    for i, symbol in enumerate(universe):
        if i > 0:
            import time as _time; _time.sleep(2)  # stagger fetches to reduce I/O on e2-micro
        has_pos = symbol in day_state.positions
        data = _fetch_bars(symbol)
        if data is None:
            continue

        # Rule-based signal
        sig = generate_signal(
            symbol=symbol,
            price=data["price"], ema=data["ema"], rsi=data["rsi"],
            volume=data["volume"], avg_volume=data["avg_volume"],
            has_position=has_pos,
        )
        day_state.set_signal(DaySignal(
            symbol=symbol, action=sig.action,
            rsi=sig.rsi, price=sig.price, ema=sig.ema,
        ))

        if sig.action == "HOLD":
            continue

        # Fetch 4-week historical context for Claude
        weekly_ctx = _fetch_weekly_context(symbol)

        # Fetch bot trade history + market sessions from Supabase for Claude
        history_ctx = build_ai_history_context(symbol)

        # AI validation with historical context
        ai_dec = _ai.validate(
            symbol=symbol, price=sig.price, ema=sig.ema,
            rsi=sig.rsi, volume=sig.volume, avg_volume=sig.avg_volume,
            trend=sig.trend, rule_signal=sig.action,
            weekly_context=weekly_ctx,
            history_context=history_ctx,
        )
        _logger.log_ai_validation(symbol, ai_dec.decision, ai_dec.confidence, ai_dec.reason)

        # Update signal with AI data
        with day_state._lock:
            if symbol in day_state.signals:
                day_state.signals[symbol].ai_confidence = ai_dec.confidence
                day_state.signals[symbol].ai_reason = ai_dec.reason

        # AI veto: only block when AI gives the OPPOSITE signal with >70% confidence.
        # AI returning HOLD (uncertain) does NOT block the rule signal.
        if (
            ai_dec.decision != sig.action
            and ai_dec.decision != "HOLD"
            and ai_dec.confidence > 0.70
        ):
            logging.info(
                "%s: rule=%s AI=%s conf=%.2f — strong AI veto, skipping",
                symbol, sig.action, ai_dec.decision, ai_dec.confidence,
            )
            continue
        if ai_dec.decision != sig.action:
            logging.info(
                "%s: rule=%s AI=%s conf=%.2f — AI uncertain, proceeding with rule",
                symbol, sig.action, ai_dec.decision, ai_dec.confidence,
            )

        # --- BUY ---
        if sig.action == "BUY":
            # SHIELD / SAFE block momentum breakouts — only take pullback setups
            if not mode_params.allow_breakout and "breakout" in sig.reason.lower():
                day_state.add_log(
                    "Skipped", f"{symbol}: breakout filtered in {_mode_manager.mode} mode", "neutral"
                )
                continue

            if has_earnings_soon(symbol, days_ahead=2):
                day_state.add_log("Skipped", f"{symbol}: earnings within 2 days — too risky", "warning")
                continue

            vwap = data.get("vwap", 0.0)
            if vwap > 0:
                vwap_pct = (sig.price - vwap) / vwap * 100
                if vwap_pct < -1.5:
                    day_state.add_log("Skipped", f"{symbol}: price {vwap_pct:.1f}% below VWAP ${vwap:.2f} — too bearish", "neutral")
                    continue

            ok, reason = _risk.can_trade(symbol, portfolio_value)
            if not ok:
                day_state.add_log("Skipped", f"{symbol}: {reason}", "neutral")
                continue

            qty = _risk.calculate_position_size(portfolio_value, sig.price, state=day_state)
            try:
                _executor.place_buy_order(symbol, qty)
                # Use mode-specific SL/TP instead of fixed config values
                sl = round(sig.price * (1 - mode_params.stop_loss_pct), 2)
                tp = round(sig.price * (1 + mode_params.take_profit_pct), 2)
                pos = DayPosition(
                    symbol=symbol, qty=qty, entry_price=sig.price,
                    current_price=sig.price, stop_loss=sl, take_profit=tp,
                )
                pos._ai_confidence = ai_dec.confidence
                pos._ai_reason = ai_dec.reason
                pos._weekly_context = weekly_ctx
                with day_state._lock:
                    day_state.positions[symbol] = pos
                _risk.register_trade(symbol)
                _logger.log_trade(symbol, "BUY", sig.price, qty, sig.reason)
            except Exception as exc:
                day_state.add_log("Error", f"BUY {symbol} failed: {exc}", "negative")

        # --- SELL (signal exit, not SL/TP) ---
        elif sig.action == "SELL" and has_pos:
            pos = day_state.positions.get(symbol)
            if pos:
                try:
                    _executor.place_sell_order(symbol, pos.qty)
                    pnl = (sig.price - pos.entry_price) * pos.qty
                    pnl_pct = (sig.price - pos.entry_price) / pos.entry_price * 100
                    with day_state._lock:
                        day_state.positions.pop(symbol, None)
                        day_state.metrics.daily_pnl += round(pnl, 2)
                    _risk.deregister_trade(symbol)
                    day_state.record_trade_result(pnl)
                    _logger.log_trade(symbol, "SELL", sig.price, pos.qty, sig.reason)
                    db_save_trade(
                        symbol=symbol, entry_price=pos.entry_price,
                        exit_price=sig.price, qty=pos.qty,
                        pnl=pnl, pnl_pct=pnl_pct, exit_reason="signal_sell",
                        ai_confidence=getattr(pos, "_ai_confidence", 0.0),
                        ai_reason=getattr(pos, "_ai_reason", ""),
                        weekly_context=getattr(pos, "_weekly_context", None),
                    )
                except Exception as exc:
                    day_state.add_log("Error", f"SELL {symbol} failed: {exc}", "negative")


def _bot_loop() -> None:
    global _risk
    _run_cycle._eod_done = False          # reset EOD guard for new trading day
    _run_cycle._no_trade_alerted = False  # reset no-trade alert for new day
    if hasattr(_run_cycle, "_window_entry_time"):
        del _run_cycle._window_entry_time
    logging.info("Day bot loop started")
    day_state.add_log("Day Bot", "Trading loop started", "positive")

    try:
        pv, _ = _run_with_timeout(_executor.get_portfolio_value)
        portfolio_value = pv if pv is not None else 0.0
    except Exception:
        portfolio_value = 0.0
    _risk.reset_daily(portfolio_value)
    with day_state._lock:
        day_state.metrics.daily_start_value = portfolio_value
        # Sync position size from config into state (so dashboard shows correct %)
        day_state.metrics.position_size_pct = _config.position_size_pct
        day_state.normal_size_pct = _config.position_size_pct

    while not _stop_event.is_set():
        try:
            _run_cycle()
        except Exception as exc:
            logging.exception("Day bot cycle error: %s", exc)
            day_state.add_log("Error", str(exc)[:120], "negative")
        _stop_event.wait(timeout=_config.loop_interval_seconds)

    # Send EOD summary to Telegram whenever the bot stops
    try:
        from telegram_notify import notify_daybot_summary
        from datetime import datetime, timezone
        m = day_state.metrics
        start = m.daily_start_value or m.portfolio_value
        daily_pnl = m.portfolio_value - start
        daily_pnl_pct = (daily_pnl / start * 100) if start > 0 else 0.0
        notify_daybot_summary(
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            portfolio_value=m.portfolio_value,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            trades=m.trades_today,
            wins=m.wins_today,
            losses=m.losses_today,
            halted=m.daily_loss_halted,
        )
    except Exception:
        pass

    # Profit extraction: if daily profit ≥ threshold, open a long-term harvest position
    try:
        if _harvester and daily_pnl > 0:
            from telegram_notify import notify_harvest_extraction
            spy_ret = _get_spy_return()
            regime = "trending_up" if spy_ret > 0.3 else "trending_down" if spy_ret < -0.3 else "choppy"
            extracted = _harvester.check_and_extract(
                daily_pnl=daily_pnl,
                bot_type="day",
                watchlist=list(day_state.watchlist),
                market_regime=regime,
            )
            if extracted:
                notify_harvest_extraction("DayBot", extracted, regime)
    except Exception as exc:
        logging.warning("Harvest extraction failed: %s", exc)

    logging.info("Day bot loop stopped")
    day_state.add_log("Day Bot", "Trading loop stopped", "warning")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@daybot_bp.post("/start")
def start():
    global _config, _scanner, _ai, _executor, _monitor, _risk, _logger
    global _bot_thread, _mode_manager, _harvester

    if day_state.running:
        return jsonify({"ok": False, "message": "Already running"})

    _config = load_config()
    if not _config.alpaca_api_key:
        return jsonify({"ok": False, "message": "EXCHANGE_API_KEY not set"}), 400

    _scanner = MarketScanner(_config.alpaca_api_key, _config.alpaca_secret_key)
    _ai = AIValidator(_config.anthropic_api_key, _config.claude_model)
    _executor = TradeExecutor(_config.alpaca_api_key, _config.alpaca_secret_key, paper=True, budget=_config.paper_budget)
    _risk = RiskManager(
        max_trades_per_day=_config.max_trades_per_day,
        max_concurrent=_config.max_concurrent_trades,
        position_size_pct=_config.position_size_pct,
        max_daily_loss_pct=_config.max_daily_loss_pct,
    )
    _logger = TradeLogger(day_state)

    from alpaca.data.historical import StockHistoricalDataClient
    data_client = StockHistoricalDataClient(_config.alpaca_api_key, _config.alpaca_secret_key)
    _monitor = PositionMonitor(
        data_client, _executor, _risk, day_state,
        stop_loss_pct=_config.stop_loss_pct,
        take_profit_pct=_config.take_profit_pct,
    )

    from daybot.mode_manager import DayModeManager
    from harvest.manager import ProfitHarvester
    _mode_manager = DayModeManager()
    _harvester = ProfitHarvester(
        anthropic_api_key=_config.anthropic_api_key,
        alpaca_api_key=_config.alpaca_api_key,
        alpaca_secret_key=_config.alpaca_secret_key,
        claude_model=_config.claude_model,
    )

    _stop_event.clear()
    day_state.running = True
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
    _bot_thread.start()

    return jsonify({"ok": True, "message": "Day bot started"})


def _start_bot_internal() -> None:
    """Called by the scheduler — mirrors /start without HTTP context."""
    global _config, _scanner, _ai, _executor, _monitor, _risk, _logger, _bot_thread
    global _mode_manager, _harvester

    if day_state.running:
        return

    _config = load_config()
    if not _config.alpaca_api_key:
        logging.warning("Scheduler auto-start: EXCHANGE_API_KEY not set")
        return

    _scanner = MarketScanner(_config.alpaca_api_key, _config.alpaca_secret_key)
    _ai = AIValidator(_config.anthropic_api_key, _config.claude_model)
    _executor = TradeExecutor(_config.alpaca_api_key, _config.alpaca_secret_key, paper=True, budget=_config.paper_budget)
    _risk = RiskManager(
        max_trades_per_day=_config.max_trades_per_day,
        max_concurrent=_config.max_concurrent_trades,
        position_size_pct=_config.position_size_pct,
        max_daily_loss_pct=_config.max_daily_loss_pct,
    )
    _logger = TradeLogger(day_state)

    from alpaca.data.historical import StockHistoricalDataClient
    data_client = StockHistoricalDataClient(_config.alpaca_api_key, _config.alpaca_secret_key)
    _monitor = PositionMonitor(
        data_client, _executor, _risk, day_state,
        stop_loss_pct=_config.stop_loss_pct,
        take_profit_pct=_config.take_profit_pct,
    )

    from daybot.mode_manager import DayModeManager
    from harvest.manager import ProfitHarvester
    _mode_manager = DayModeManager()
    _harvester = ProfitHarvester(
        anthropic_api_key=_config.anthropic_api_key,
        alpaca_api_key=_config.alpaca_api_key,
        alpaca_secret_key=_config.alpaca_secret_key,
        claude_model=_config.claude_model,
    )

    _stop_event.clear()
    day_state.running = True
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
    _bot_thread.start()
    logging.info("Scheduler: day bot started internally")


def _stop_bot_internal() -> None:
    """Called by the scheduler — mirrors /stop without HTTP context."""
    global _data_client
    _stop_event.set()
    day_state.running = False
    _data_client = None  # force fresh client+connection on next start


@daybot_bp.post("/stop")
def stop():
    _stop_bot_internal()
    return jsonify({"ok": True, "message": "Day bot stopped"})


@daybot_bp.get("/status")
def status():
    return jsonify(day_state.to_dict())


@daybot_bp.get("/positions")
def positions():
    from dataclasses import asdict
    with day_state._lock:
        return jsonify({s: asdict(p) for s, p in day_state.positions.items()})


@daybot_bp.get("/signals")
def signals():
    from dataclasses import asdict
    with day_state._lock:
        return jsonify({s: asdict(sig) for s, sig in day_state.signals.items()})


@daybot_bp.get("/watchlist")
def watchlist():
    with day_state._lock:
        return jsonify({"watchlist": day_state.watchlist})


@daybot_bp.get("/logs")
def logs():
    from dataclasses import asdict
    with day_state._lock:
        return jsonify([asdict(lg) for lg in day_state.logs[:30]])


@daybot_bp.post("/settings")
def settings():
    """Update trade mode, position size, and shield thresholds at runtime."""
    from flask import request
    body = request.get_json(silent=True) or {}
    allowed_metric = {"trade_mode", "position_size_pct", "profit_pool"}
    allowed_state = {"shield_loss_streak", "shield_recovery_wins", "shield_size_pct", "normal_size_pct"}

    with day_state._lock:
        for k, v in body.items():
            if k in allowed_metric:
                if k == "trade_mode" and v not in {"fixed", "compound", "house_money"}:
                    return jsonify({"ok": False, "message": f"Invalid trade_mode: {v}"}), 400
                setattr(day_state.metrics, k, v)
                if k == "trade_mode":
                    day_state.metrics.pre_shield_mode = v
            elif k in allowed_state:
                setattr(day_state, k, v)

        from dataclasses import asdict
        return jsonify({
            "ok": True,
            "trade_mode": day_state.metrics.trade_mode,
            "position_size_pct": day_state.metrics.position_size_pct,
            "shield_active": day_state.metrics.shield_active,
            "profit_pool": day_state.metrics.profit_pool,
        })


@daybot_bp.get("/suggestions")
def suggestions():
    """AI-generated stock suggestions from evening analysis (entry zone, SL, target per symbol)."""
    with day_state._lock:
        approved = day_state.premarket_approved or day_state.evening_approved or []
        data = []
        for sym in approved:
            entry_zone = day_state.evening_entry_zones.get(sym, [])
            data.append({
                "symbol": sym,
                "direction": day_state.evening_direction.get(sym, "BUY"),
                "entry_low": entry_zone[0] if len(entry_zone) == 2 else None,
                "entry_high": entry_zone[1] if len(entry_zone) == 2 else None,
                "stop": day_state.evening_stop_levels.get(sym),
                "target": day_state.evening_targets.get(sym),
                "note": day_state.evening_notes.get(sym, ""),
                "regime": day_state.evening_regime,
            })
    return jsonify({"suggestions": data, "regime": day_state.evening_regime})


@daybot_bp.get("/india-suggestions")
def india_suggestions():
    """Indian NSE stock suggestions from evening analysis."""
    with day_state._lock:
        data = []
        for sym in day_state.india_approved:
            entry_zone = day_state.india_entry_zones.get(sym, [])
            data.append({
                "symbol": sym,
                "display": sym.replace(".NS", ""),
                "direction": day_state.india_direction.get(sym, "BUY"),
                "entry_low": entry_zone[0] if len(entry_zone) == 2 else None,
                "entry_high": entry_zone[1] if len(entry_zone) == 2 else None,
                "stop": day_state.india_stop_levels.get(sym),
                "target": day_state.india_targets.get(sym),
                "note": day_state.india_notes.get(sym, ""),
                "regime": day_state.india_regime,
                "analysis_date": day_state.india_analysis_date,
                "rank": day_state.india_rank.get(sym, 99),
                "conviction": day_state.india_conviction.get(sym, ""),
            })
    return jsonify({
        "suggestions": data,
        "regime": day_state.india_regime,
        "analysis_date": day_state.india_analysis_date,
    })


@daybot_bp.post("/debug/seed-watchlist")
def debug_seed_watchlist():
    """DEV ONLY — seed state with test watchlist so options picker can run."""
    symbols = ["AAPL", "NVDA", "SPY", "MSFT", "AMD"]
    with day_state._lock:
        day_state.evening_approved = symbols
        day_state.premarket_approved = symbols
        day_state.evening_regime = "trending_up"
        day_state.evening_direction = {s: "BUY" for s in symbols}
        day_state.evening_entry_zones = {
            "AAPL": [210, 215], "NVDA": [130, 135], "SPY": [550, 555],
            "MSFT": [420, 425], "AMD": [160, 165],
        }
        day_state.evening_notes = {
            "AAPL": "momentum setup", "NVDA": "AI tailwind",
            "SPY": "market uptrend", "MSFT": "cloud growth", "AMD": "data center demand",
        }
    return jsonify({"ok": True, "seeded": symbols})


@daybot_bp.post("/run-options-picker")
def run_options_picker_adhoc():
    """Trigger options picker on demand (runs in background thread)."""
    import os, threading
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"ok": False, "message": "ANTHROPIC_API_KEY not set"}), 400

    def _run():
        from .options_picker import run_options_analysis
        run_options_analysis(anthropic_api_key=anthropic_key)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Options analysis started — results ready in ~30 seconds"})


@daybot_bp.post("/run-india-analysis")
def run_india_analysis_adhoc():
    """Trigger India NSE evening analysis on demand (runs in background thread)."""
    import os, threading
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"ok": False, "message": "ANTHROPIC_API_KEY not set"}), 400

    def _run():
        from .india_agent import run_india_analysis
        run_india_analysis(anthropic_api_key=anthropic_key)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "India analysis started — results ready in ~30 seconds"})


@daybot_bp.post("/run-evening-analysis")
def run_evening_analysis_adhoc():
    """Trigger US evening analysis on demand (runs in background thread)."""
    import os, threading
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    alpaca_key = os.getenv("EXCHANGE_API_KEY", "")
    alpaca_secret = os.getenv("EXCHANGE_API_SECRET", "")
    if not anthropic_key or not alpaca_key:
        return jsonify({"ok": False, "message": "API keys not set"}), 400

    def _run():
        from .evening_agent import run_evening_analysis
        run_evening_analysis(
            anthropic_api_key=anthropic_key,
            alpaca_api_key=alpaca_key,
            alpaca_secret_key=alpaca_secret,
        )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "US evening analysis started — results ready in ~60 seconds"})


@daybot_bp.get("/options-suggestions")
def options_suggestions():
    """Latest options picks from the 9:15 AM AI options picker."""
    with day_state._lock:
        return jsonify({
            "picks": day_state.options_picks,
            "date": day_state.options_picks_date,
        })


@daybot_bp.get("/user-positions")
def user_positions_get():
    """All open user-logged manual positions (Robinhood stocks + options)."""
    from user_positions import get_open_positions
    positions = get_open_positions()
    return jsonify({"positions": positions})


@daybot_bp.post("/user-positions")
def user_positions_post():
    """Log a new user manual position."""
    from flask import request
    from user_positions import save_user_position
    body = request.get_json(silent=True) or {}
    required = ["symbol", "side", "asset_type", "qty", "entry_price"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify({"ok": False, "message": f"Missing fields: {missing}"}), 400
    row = save_user_position(
        symbol=body["symbol"],
        side=body["side"],
        asset_type=body["asset_type"],
        qty=float(body["qty"]),
        entry_price=float(body["entry_price"]),
        stop_price=body.get("stop_price"),
        target_price=body.get("target_price"),
        notes=body.get("notes", ""),
        option_type=body.get("option_type"),
        strike=body.get("strike"),
        expiry=body.get("expiry"),
        underlying_stop=body.get("underlying_stop"),
    )
    return jsonify({"ok": True, "position": row})


@daybot_bp.post("/user-positions/<int:position_id>/close")
def user_positions_close(position_id: int):
    """Close a user position by ID."""
    from flask import request
    from user_positions import close_user_position
    body = request.get_json(silent=True) or {}
    exit_price = body.get("exit_price")
    if not exit_price:
        return jsonify({"ok": False, "message": "exit_price required"}), 400
    ok = close_user_position(position_id, float(exit_price), reason=body.get("reason", "manual"))
    return jsonify({"ok": ok})
