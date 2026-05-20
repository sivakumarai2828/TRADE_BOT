"""Options bot main loop — crypto bot logic applied to SPY/QQQ options.

Signal  → RSI + EMA on underlying (same as crypto/day bot)
Confirm → OpenRouter Llama (free, same as day bot BUY gate)
Entry   → buy call (bullish) or put (bearish) within $150 budget
Exit    → SL 50% of premium | TP 100% gain | expiry guard 30min before close
Limits  → max 2 contracts open | SHIELD on 2 consecutive losses
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

SYMBOLS = ["SPY", "QQQ"]
POLL_SECONDS = int(os.getenv("OPTIONS_POLL_SECONDS", "300"))   # 5 min
MAX_POSITIONS = int(os.getenv("OPTIONS_MAX_POSITIONS", "2"))
BUDGET = float(os.getenv("OPTIONS_BUDGET", "150"))             # $ per contract
SL_PCT = 0.50     # stop loss: lose 50% of premium
TP_PCT = 1.00     # take profit: gain 100% of premium
DAILY_LOSS_HALT_PCT = 10.0
EXPIRY_GUARD_MIN = 30
MIN_CONFIDENCE = 0.55


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def _signal(symbol: str) -> tuple[str, float, str]:
    """RSI(14) + EMA50 on 5-min bars. Returns (action, confidence, reason)."""
    try:
        import yfinance as yf

        df = yf.download(symbol, period="5d", interval="5m", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return "HOLD", 0.0, "insufficient data"

        # Flatten MultiIndex columns (yfinance ≥0.2.38 returns (field, ticker) columns)
        if hasattr(df.columns, "levels"):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        close = df["Close"].squeeze()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])

        ema50 = float(close.ewm(span=50).mean().iloc[-1])
        price = float(close.iloc[-1])
        day_open = float(df["Open"].squeeze().iloc[0])
        day_change = (price - day_open) / day_open * 100

        if rsi < 35 and price > ema50 and day_change > -0.5:
            conf = min(0.50 + (35 - rsi) / 50, 0.85)
            return "BUY", conf, f"RSI {rsi:.0f} oversold, above EMA50, day {day_change:+.1f}%"
        if rsi > 65 and price < ema50 and day_change < 0.5:
            conf = min(0.50 + (rsi - 65) / 50, 0.85)
            return "SELL", conf, f"RSI {rsi:.0f} overbought, below EMA50, day {day_change:+.1f}%"

        return "HOLD", 0.0, f"RSI {rsi:.0f} neutral"
    except Exception as exc:
        logging.warning("Options signal failed %s: %s", symbol, exc)
        return "HOLD", 0.0, "error"


# ---------------------------------------------------------------------------
# LLM confirm (OpenRouter Llama — free)
# ---------------------------------------------------------------------------

def _llm_confirm(symbol: str, action: str, reason: str, contract: dict) -> tuple[bool, float]:
    try:
        import json, re, requests

        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            return True, 0.6

        prompt = (
            f"Options trade review:\n"
            f"Symbol: {symbol} | Action: BUY {'CALL' if action=='BUY' else 'PUT'}\n"
            f"Strike: ${contract['strike']} | Expiry: {contract['expiry']}\n"
            f"Premium: ${contract['premium']:.2f}/share (cost ${contract['cost']:.0f})\n"
            f"IV: {contract['iv']:.0%} | Signal: {reason}\n"
            f"SL at ${contract['premium']*0.5:.2f} (−50%) | TP at ${contract['premium']*2:.2f} (+100%)\n\n"
            f'Reply JSON only: {{"confirm": true/false, "confidence": 0.0-1.0, "reason": "brief"}}'
        )

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "meta-llama/llama-3.3-70b-instruct:free",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 80,
            },
            timeout=15,
        )
        text = resp.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group())
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
                    state.metrics.total_trades += 1
                    if pnl >= 0:
                        state.metrics.wins_today += 1
                    else:
                        state.metrics.losses_today += 1
                    total = state.metrics.wins_today + state.metrics.losses_today
                    state.metrics.win_rate = round(
                        state.metrics.wins_today / total * 100, 1
                    ) if total > 0 else 0.0
                tone = "positive" if pnl >= 0 else "negative"
                state.add_log(
                    "Closed",
                    f"{pos.symbol} {pos.option_type.upper()} ${pos.strike} | "
                    f"{reason} | PnL ${pnl:+.2f} ({pnl_pct:+.1f}%)",
                    tone,
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

        # Pick contract
        from .chain import pick_contract
        contract = pick_contract(symbol, action, BUDGET)
        if not contract:
            state.add_log("Skipped", f"{symbol}: no contract within ${BUDGET:.0f} budget", "neutral")
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
    options_state.add_log("Bot started", f"Options loop running — paper={paper}", "neutral")

    while not _stop_event.is_set():
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
