"""Options bot — momentum swing strategy for individual high-beta stocks.

Universe  → NVDA, AMD, TSLA, META, AAPL  (+SPY/QQQ fallback)
Signal    → RSI momentum breakout + volume surge + EMA trend filter
Contract  → slightly OTM (1-3%), 7-14 DTE (2-3 day swing hold)
Confirm   → DeepSeek R1 AI gate (confidence ≥ 0.60)
Entry     → buy call (bullish) or put (bearish) within 25% of balance budget
Exit      → SL 50% of premium | TP 200% gain (3x) | expiry guard 30min before close
Limits    → max 2 contracts open | SHIELD on 2 consecutive losses
Capital   → $500 → 2 × $125 contracts deployed, $250 reserve
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from .state import options_state, OptionsPosition

_stop_event = threading.Event()
_bot_thread: Optional[threading.Thread] = None

SYMBOLS = ["NVDA", "AMD", "TSLA", "META", "AAPL", "SPY", "QQQ"]
POLL_SECONDS = int(os.getenv("OPTIONS_POLL_SECONDS", "300"))   # 5 min
MAX_POSITIONS = int(os.getenv("OPTIONS_MAX_POSITIONS", "2"))
_OPTIONS_BUDGET_PCT = float(os.getenv("OPTIONS_BUDGET_PCT", "0.25"))  # 25% of balance per contract (~$125 on $500)
BUDGET = float(os.getenv("OPTIONS_BUDGET", "0"))               # 0 = use dynamic pct of balance
SL_PCT = 0.50     # stop loss: lose 50% of premium ($62 on $125 contract)
TP_PCT = 2.00     # take profit: 200% gain = 3x (your target, e.g. $125 → $375)
DAILY_LOSS_HALT_PCT = 10.0
EXPIRY_GUARD_MIN = 30
MIN_CONFIDENCE = 0.60  # raised from 0.55 — individual stocks need stronger signal


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def _signal(symbol: str) -> tuple[str, float, str]:
    """Momentum swing signal for individual high-beta stocks + ETFs.

    Uses 15-min bars (better for 2-3 day swings than 5-min noise):
      BUY  call  → RSI < 40 recovering + price > EMA50 + volume surge
      BUY  put   → RSI > 68 rolling over + price < EMA50 + volume surge
    Volume gate: current bar volume must exceed 1.5× 20-bar avg.
    Returns (action, confidence, reason).
    """
    try:
        import yfinance as yf

        # 15-min bars: 10d gives ~260 bars — enough for EMA50 + volume avg
        df = yf.download(symbol, period="10d", interval="15m", progress=False, auto_adjust=True)
        if df.empty or len(df) < 55:
            return "HOLD", 0.0, "insufficient data"

        # Flatten MultiIndex columns (yfinance ≥0.2.38)
        if hasattr(df.columns, "levels"):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        # RSI(14)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])
        rsi_prev = float((100 - 100 / (1 + rs)).iloc[-3])  # 3 bars ago — detect direction

        # EMA50 trend filter
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        price = float(close.iloc[-1])

        # Volume gate: current bar vs 20-bar avg (momentum confirmation)
        vol_now = float(volume.iloc[-1])
        vol_avg = float(volume.rolling(20).mean().iloc[-1])
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0.0
        vol_ok = vol_ratio >= 1.5  # 1.5× avg = confirmed momentum

        # Day change for context
        day_open = float(df["Open"].squeeze().resample("1D").first().iloc[-1])
        day_change = (price - day_open) / day_open * 100

        # ── CALL signal: RSI recovering from oversold, trending up, volume confirms ──
        # rsi_prev > rsi ensures RSI is RISING (not still falling)
        if rsi < 40 and rsi > rsi_prev and price > ema50:
            conf = min(0.55 + (40 - rsi) / 40, 0.88)
            if vol_ok:
                conf = min(conf + 0.08, 0.92)  # volume boost
                reason = f"RSI {rsi:.0f}↑ oversold recovery, above EMA50, vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            else:
                conf = max(conf - 0.05, 0.55)
                reason = f"RSI {rsi:.0f}↑ oversold, above EMA50, low vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            return "BUY", round(conf, 2), reason

        # ── PUT signal: RSI rolling over from overbought, trending down, volume confirms ──
        if rsi > 68 and rsi < rsi_prev and price < ema50:
            conf = min(0.55 + (rsi - 68) / 40, 0.88)
            if vol_ok:
                conf = min(conf + 0.08, 0.92)
                reason = f"RSI {rsi:.0f}↓ overbought rollover, below EMA50, vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            else:
                conf = max(conf - 0.05, 0.55)
                reason = f"RSI {rsi:.0f}↓ overbought, below EMA50, low vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            return "SELL", round(conf, 2), reason

        return "HOLD", 0.0, f"RSI {rsi:.0f} neutral (EMA50={ema50:.2f} price={price:.2f})"
    except Exception as exc:
        logging.warning("Options signal failed %s: %s", symbol, exc)
        return "HOLD", 0.0, "error"


# ---------------------------------------------------------------------------
# LLM confirm (OpenRouter Llama — free)
# ---------------------------------------------------------------------------

def _llm_confirm(symbol: str, action: str, reason: str, contract: dict) -> tuple[bool, float]:
    """DeepSeek R1 via NVIDIA NIM — Haiku fallback. Replaces free Llama."""
    import json, re

    prompt = (
        f"Options trade review:\n"
        f"Symbol: {symbol} | Action: BUY {'CALL' if action=='BUY' else 'PUT'}\n"
        f"Strike: ${contract['strike']} | Expiry: {contract['expiry']}\n"
        f"Premium: ${contract['premium']:.2f}/share (cost ${contract['cost']:.0f})\n"
        f"IV: {contract['iv']:.0%} | Signal: {reason}\n"
        f"SL at ${contract['premium']*0.5:.2f} (−50%) | TP at ${contract['premium']*3:.2f} (+200%, 3×)\n"
        f"OTM: {contract.get('otm_pct',0):.1f}% | Strategy: 2-3 day momentum swing\n\n"
        f'Reply JSON only: {{"confirm": true/false, "confidence": 0.0-1.0, "reason": "brief"}}'
    )

    try:
        from daybot.llm_router import deepseek_chat as _ds_chat
        raw = _ds_chat(prompt, max_tokens=100)
        # Strip DeepSeek <think> blocks
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            logging.info("Options DeepSeek-R1 [%s]: confirm=%s conf=%.2f", symbol,
                         data.get("confirm"), data.get("confidence", 0.0))
            return bool(data.get("confirm", False)), float(data.get("confidence", 0.0))
    except Exception as exc:
        logging.warning("Options LLM confirm failed: %s", exc)
    return False, 0.0


# ---------------------------------------------------------------------------
# Expiry guard
# ---------------------------------------------------------------------------

def _near_expiry(pos: OptionsPosition) -> bool:
    try:
        # Options expire at market close (4 PM ET = 20:00 UTC in summer, 21:00 in winter)
        exp = datetime.strptime(pos.expiry, "%Y-%m-%d")
        # Use 20:00 UTC as conservative close (EDT)
        exp_utc = exp.replace(hour=20, minute=0, tzinfo=timezone.utc)
        guard = exp_utc - timedelta(minutes=EXPIRY_GUARD_MIN)
        return datetime.now(timezone.utc) >= guard
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def _run_cycle(api_key: str, secret_key: str, paper: bool) -> None:
    state = options_state

    # --- EXIT CHECK ---
    with state._lock:
        open_positions = list(state.positions.values())

    for pos in open_positions:
        from .chain import get_current_price
        from .executor import sell_contract

        current = get_current_price(pos.contract_symbol, api_key, secret_key)
        if current is None:
            continue

        pnl = round((current - pos.entry_premium) * pos.qty * 100, 2)
        pnl_pct = round((current - pos.entry_premium) / pos.entry_premium * 100, 1)

        with state._lock:
            if pos.contract_symbol in state.positions:
                p = state.positions[pos.contract_symbol]
                p.current_premium = current
                p.pnl = pnl
                p.pnl_pct = pnl_pct
                p.highest_premium = max(p.highest_premium, current)

        reason = None
        if current <= pos.sl_price:
            reason = "stop_loss"
        elif current >= pos.tp_price:
            reason = "take_profit"
        elif _near_expiry(pos):
            reason = "expiry_guard"

        if reason:
            ok = sell_contract(pos.contract_symbol, pos.qty, api_key, secret_key, paper)
            if ok:
                with state._lock:
                    state.positions.pop(pos.contract_symbol, None)
                    state.metrics.daily_pnl = round(state.metrics.daily_pnl + pnl, 2)
                    state.metrics.balance = round(
                        state.metrics.balance + pos.entry_premium * pos.qty * 100 + pnl, 2
                    )
                    if pnl >= 0:
                        state.metrics.wins_today += 1
                    else:
                        state.metrics.losses_today += 1
                # Persist cumulative stats (survives restart)
                state.record_trade_close(pnl)
                tone = "positive" if pnl >= 0 else "negative"
                state.add_log(
                    "Closed",
                    f"{pos.symbol} {pos.option_type.upper()} ${pos.strike} | "
                    f"{reason} | PnL ${pnl:+.2f} ({pnl_pct:+.1f}%) | "
                    f"Balance ${state.metrics.balance:.2f}",
                    tone,
                )
                logging.info(
                    "Options trade closed: %s %s | reason=%s pnl=$%.2f | "
                    "total=%d wins=%d losses=%d wr=%.0f%% balance=$%.2f",
                    pos.symbol, pos.option_type, reason, pnl,
                    state.metrics.total_trades, state.metrics.total_wins,
                    state.metrics.total_losses, state.metrics.win_rate,
                    state.metrics.balance,
                )

    # --- ENTRY CHECK ---
    with state._lock:
        halted = state.metrics.daily_loss_halted
        open_count = len(state.positions)
        balance = state.metrics.balance
        start_bal = state.metrics.daily_start_balance
        losses = state.metrics.losses_today
        consecutive_wins = state.metrics.wins_today

    if halted:
        return

    # Daily loss halt (same fix as crypto: include open PnL)
    if start_bal > 0:
        open_pnl = sum(p.pnl for p in state.positions.values())
        drop = (start_bal - (balance + open_pnl)) / start_bal * 100
        if drop >= DAILY_LOSS_HALT_PCT:
            with state._lock:
                state.metrics.daily_loss_halted = True
            state.add_log(
                "Daily limit hit",
                f"Balance dropped {drop:.1f}% — paused until tomorrow",
                "negative",
            )
            return

    # SHIELD mode: 2+ consecutive losses → no new trades
    if losses >= 2:
        with state._lock:
            state.metrics.mode = "SHIELD"
        state.add_log("Mode", "SHIELD — 2 losses today, no new trades", "neutral")
        return

    # AGGRESSIVE mode: 3+ wins, 0 losses
    if consecutive_wins >= 3 and losses == 0:
        with state._lock:
            state.metrics.mode = "AGGRESSIVE"
    else:
        with state._lock:
            state.metrics.mode = "SAFE"

    if open_count >= MAX_POSITIONS:
        return

    # --- SCAN SYMBOLS ---
    for symbol in SYMBOLS:
        with state._lock:
            already_open = any(p.symbol == symbol for p in state.positions.values())
        if already_open:
            continue

        action, confidence, reason = _signal(symbol)
        state.add_log(
            "Signal",
            f"{symbol}: {action} conf={confidence:.0%} — {reason}",
            "neutral",
        )

        if action == "HOLD" or confidence < MIN_CONFIDENCE:
            continue

        # Pick contract — dynamic 25% of balance, or fixed BUDGET if set
        from .chain import pick_contract
        _budget = BUDGET if BUDGET > 0 else round(state.metrics.balance * _OPTIONS_BUDGET_PCT, 2)
        contract = pick_contract(symbol, action, _budget)
        if not contract:
            state.add_log("Skipped", f"{symbol}: no contract within ${_budget:.0f} budget", "neutral")
            continue

        # LLM confirm
        confirmed, llm_conf = _llm_confirm(symbol, action, reason, contract)
        final_conf = (confidence + llm_conf) / 2
        state.add_log(
            "AI",
            f"{symbol} {contract['option_type'].upper()} ${contract['strike']} "
            f"exp {contract['expiry']} | conf={final_conf:.0%}",
            "neutral",
        )

        if not confirmed or final_conf < MIN_CONFIDENCE:
            state.add_log("Skipped", f"{symbol}: LLM rejected (conf={final_conf:.0%})", "neutral")
            continue

        # Balance check
        with state._lock:
            balance = state.metrics.balance
        if balance < contract["cost"]:
            state.add_log(
                "Skipped",
                f"{symbol}: insufficient balance ${balance:.0f} < ${contract['cost']:.0f}",
                "neutral",
            )
            continue

        # Execute BUY
        from .executor import buy_contract
        ok = buy_contract(contract["contract_symbol"], 1, api_key, secret_key, paper)
        if ok:
            sl = round(contract["premium"] * (1 - SL_PCT), 2)
            tp = round(contract["premium"] * (1 + TP_PCT), 2)
            pos = OptionsPosition(
                symbol=symbol,
                contract_symbol=contract["contract_symbol"],
                option_type=contract["option_type"],
                strike=contract["strike"],
                expiry=contract["expiry"],
                qty=1,
                entry_premium=contract["premium"],
                current_premium=contract["premium"],
                sl_price=sl,
                tp_price=tp,
                entry_time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                highest_premium=contract["premium"],
            )
            with state._lock:
                state.positions[contract["contract_symbol"]] = pos
                state.metrics.balance = round(state.metrics.balance - contract["cost"], 2)
            state.add_log(
                "Trade BUY",
                f"{symbol} {contract['option_type'].upper()} ${contract['strike']} "
                f"exp {contract['expiry']} @ ${contract['premium']:.2f} | "
                f"cost=${contract['cost']:.0f} | SL=${sl:.2f} TP=${tp:.2f}",
                "positive",
            )


# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    # 9:35 AM – 3:45 PM ET (ET = UTC-4 in summer)
    et = now - timedelta(hours=4)
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 35 <= minutes <= 15 * 60 + 45


def _bot_loop(api_key: str, secret_key: str, paper: bool) -> None:
    logging.info("Options bot loop started (paper=%s)", paper)
    options_state.add_log(
        "Bot started",
        f"Options loop running — paper={paper} | monitoring since {options_state.metrics.monitor_start_date}",
        "neutral",
    )

    _last_day = datetime.now(timezone.utc).date()

    while not _stop_event.is_set():
        # Midnight daily reset
        today = datetime.now(timezone.utc).date()
        if today != _last_day:
            options_state.daily_reset()
            _last_day = today

        if not _is_market_hours():
            _stop_event.wait(60)
            continue
        try:
            _run_cycle(api_key, secret_key, paper)
        except Exception as exc:
            logging.error("Options bot cycle error: %s", exc)
            options_state.add_log("Error", str(exc)[:100], "negative")
        _stop_event.wait(POLL_SECONDS)

    logging.info("Options bot loop stopped")


def start_options_bot(api_key: str, secret_key: str, paper: bool = True) -> None:
    global _bot_thread
    if options_state.running:
        return
    _stop_event.clear()
    options_state.running = True
    with options_state._lock:
        options_state.metrics.daily_start_balance = options_state.metrics.balance
    _bot_thread = threading.Thread(
        target=_bot_loop, args=(api_key, secret_key, paper), daemon=True, name="options-bot"
    )
    _bot_thread.start()


def stop_options_bot() -> None:
    _stop_event.set()
    options_state.running = False
    options_state.add_log("Bot stopped", "Options trading loop stopped", "neutral")
