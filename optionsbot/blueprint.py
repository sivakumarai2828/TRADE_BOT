"""Options bot — momentum swing strategy for individual high-beta stocks.

Universe  → NVDA, AMD, TSLA, META, AAPL  (+SPY/QQQ fallback)
Signal    → RSI momentum breakout + volume surge + EMA trend filter
Contract  → slightly OTM (1-3%), 7-14 DTE (2-3 day swing hold)
Confirm   → DeepSeek R1 AI gate (confidence ≥ 0.60)
Entry     → buy call (bullish) or put (bearish) within 25% of balance budget
Exit      → trailing SL (breakeven lock at +50%, +100%, +150%) | TP 200% (3×)
Filters   → SPY trend gate | earnings avoid (3 days) | IV guard (>90% = skip)
Limits    → max 4 contracts open | SHIELD on 2 consecutive losses
Capital   → $500 → 4 × $125 = 100% deployed
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from .state import options_state, OptionsPosition
from .db import save_trade as db_save_trade

_stop_event = threading.Event()
_bot_thread: Optional[threading.Thread] = None

SYMBOLS = ["SPY", "QQQ", "AAPL", "AMD", "MSFT"]  # ETFs first; IWM removed (not optionable in yfinance)
POLL_SECONDS = int(os.getenv("OPTIONS_POLL_SECONDS", "300"))   # 5 min
MAX_POSITIONS = int(os.getenv("OPTIONS_MAX_POSITIONS", "4"))   # 4 × $125 = $500 full capital
_OPTIONS_BUDGET_PCT = float(os.getenv("OPTIONS_BUDGET_PCT", "0.25"))  # 25% per contract
BUDGET = float(os.getenv("OPTIONS_BUDGET", "0"))               # 0 = dynamic
SL_PCT = 0.50          # initial SL: −50% (base; overridden by IV regime + trailing stop)
TP_PCT = 1.50          # take profit: +150% = 2.5× exit (was 200% — too greedy for 0DTE/weekly)

# IV-adaptive SL — fixed 50% fires prematurely in high-IV (IV crush alone can drop premium 50%).
# Widen SL in high-IV environments to give trade room; tighten TP too (IV mean-reverts fast).
_IV_REGIME_SL: dict[str, float] = {
    "low":     0.40,   # cheap premium, tighten SL
    "normal":  0.50,   # standard
    "high":    0.65,   # wider — IV crush risk at open
    "extreme": 0.75,   # widest — premium inflated, needs breathing room
}
_IV_REGIME_TP: dict[str, float] = {
    "low":     1.50,   # normal target
    "normal":  1.50,   # normal target
    "high":    1.25,   # tighten TP — IV crush will erase gains fast
    "extreme": 1.00,   # take money quickly in extreme IV
}
IV_MAX = 0.90          # skip contracts where IV > 90% (too expensive)
EARNINGS_AVOID_DAYS = 3  # skip stock if earnings within N days
DAILY_LOSS_HALT_PCT = 10.0
EXPIRY_GUARD_MIN = 30
MIN_CONFIDENCE = 0.60

# Sector map — avoid same-sector doubles in same direction
_SECTOR = {
    "NVDA": "semis", "AMD": "semis",
    "META": "tech",  "AAPL": "tech",
    "TSLA": "ev",
    "SPY": "index",  "QQQ": "index",
}


# ---------------------------------------------------------------------------
# Filter 1 — SPY market regime
# ---------------------------------------------------------------------------

def _spy_trend() -> str:
    """Check SPY EMA20 on 15-min bars. Returns 'bull' / 'bear' / 'neutral'.

    Gate: only buy calls in bull/neutral. Only buy puts in bear/neutral.
    """
    try:
        import yfinance as yf
        df = yf.download("SPY", period="5d", interval="15m", progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return "neutral"
        if hasattr(df.columns, "levels"):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
        close = df["Close"].squeeze()
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        price = float(close.iloc[-1])
        if price > ema20 * 1.003:
            return "bull"
        if price < ema20 * 0.997:
            return "bear"
        return "neutral"
    except Exception as exc:
        logging.debug("SPY trend check failed: %s", exc)
        return "neutral"


# ---------------------------------------------------------------------------
# Filter 2 — Earnings avoid
# ---------------------------------------------------------------------------

def _has_earnings_soon(symbol: str) -> bool:
    """Return True if earnings within next EARNINGS_AVOID_DAYS days.

    Earnings = IV spike before + IV crush after. Option loses even if stock moves right.
    """
    if symbol in ("SPY", "QQQ"):
        return False  # ETFs have no earnings
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is None:
            return False
        today = datetime.now(timezone.utc).date()
        # yfinance returns DataFrame or dict depending on version
        if hasattr(cal, "columns"):
            dates = cal.columns.tolist()
        elif isinstance(cal, dict):
            dates = list(cal.values())
        else:
            return False
        for d in dates:
            try:
                if hasattr(d, "date"):
                    ed = d.date()
                else:
                    from datetime import date as _date
                    ed = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
                if 0 <= (ed - today).days <= EARNINGS_AVOID_DAYS:
                    logging.info("Earnings avoid [%s]: earnings on %s — skip", symbol, ed)
                    return True
            except Exception:
                continue
    except Exception as exc:
        logging.debug("Earnings check failed %s: %s", symbol, exc)
    return False


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def _signal(symbol: str) -> tuple[str, float, str]:
    """Momentum swing signal for individual high-beta stocks + ETFs.

    15-min bars (less noise than 5-min for 2-3 day swings):
      BUY call  → RSI < 40 rising + price > EMA50 + volume surge
      BUY put   → RSI > 68 rolling over + price < EMA50 + volume surge
    Volume gate: current bar > 1.5× 20-bar avg.
    Returns (action, confidence, reason).
    """
    try:
        import yfinance as yf

        df = yf.download(symbol, period="10d", interval="15m", progress=False, auto_adjust=True)
        if df.empty or len(df) < 55:
            return "HOLD", 0.0, "insufficient data"

        if hasattr(df.columns, "levels"):
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        close  = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi_series = 100 - 100 / (1 + rs)
        rsi      = float(rsi_series.iloc[-1])
        rsi_prev = float(rsi_series.iloc[-3])  # 3 bars ago — detect direction

        # EMA50 trend filter
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        price = float(close.iloc[-1])

        # Volume gate — use iloc[-2] (last *completed* 15-min bar)
        # iloc[-1] is the current forming bar and has 0 volume in yfinance until bar closes
        vol_now   = float(volume.iloc[-2])
        vol_avg   = float(volume.rolling(20).mean().iloc[-2])
        vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0.0
        vol_ok    = vol_ratio >= 1.2  # lowered from 1.5

        # Day change
        day_open   = float(df["Open"].squeeze().resample("1D").first().iloc[-1])
        day_change = (price - day_open) / day_open * 100

        # CALL: RSI oversold recovery — allow price up to 3% below EMA50 (pullback entry)
        # Fix: original required price > EMA50 which is impossible when RSI < 43 (oversold = price down)
        if rsi < 43 and rsi > rsi_prev and price > ema50 * 0.97:
            conf = min(0.55 + (43 - rsi) / 43, 0.88)
            if vol_ok:
                conf   = min(conf + 0.08, 0.92)
                reason = f"RSI {rsi:.0f}↑ oversold recovery, near EMA50, vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            else:
                conf   = max(conf - 0.05, 0.55)
                reason = f"RSI {rsi:.0f}↑ oversold, near EMA50, low vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            return "BUY", round(conf, 2), reason

        # PUT: RSI overbought rollover — allow price up to 5% above EMA50
        # Fix: original required price < EMA50 which is impossible when RSI > 65 (overbought = price up)
        if rsi > 65 and rsi < rsi_prev and price < ema50 * 1.05:
            conf = min(0.55 + (rsi - 65) / 40, 0.88)
            if vol_ok:
                conf   = min(conf + 0.08, 0.92)
                reason = f"RSI {rsi:.0f}↓ overbought rollover, near EMA50, vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            else:
                conf   = max(conf - 0.05, 0.55)
                reason = f"RSI {rsi:.0f}↓ overbought, near EMA50, low vol {vol_ratio:.1f}×, day {day_change:+.1f}%"
            return "SELL", round(conf, 2), reason

        return "HOLD", 0.0, f"RSI {rsi:.0f} no setup (EMA50={ema50:.2f} price={price:.2f} vol={vol_ratio:.1f}×)"
    except Exception as exc:
        logging.warning("Options signal failed %s: %s", symbol, exc)
        return "HOLD", 0.0, "error"


# ---------------------------------------------------------------------------
# LLM confirm
# ---------------------------------------------------------------------------

def _llm_confirm(symbol: str, action: str, reason: str, contract: dict) -> tuple[bool, float]:
    """DeepSeek R1 via NVIDIA NIM — confirms trade setup."""
    import json, re

    prompt = (
        f"Options trade review:\n"
        f"Symbol: {symbol} | Action: BUY {'CALL' if action=='BUY' else 'PUT'}\n"
        f"Strike: ${contract['strike']} | Expiry: {contract['expiry']}\n"
        f"Premium: ${contract['premium']:.2f}/share (cost ${contract['cost']:.0f})\n"
        f"IV: {contract['iv']:.0%} | OTM: {contract.get('otm_pct',0):.1f}% | Signal: {reason}\n"
        f"Exit: trailing SL (breakeven at +50%, lock +50% at +100%) | TP at +200% (3×)\n"
        f"Strategy: 2-3 day momentum swing. Earnings cleared. IV under 90%.\n\n"
        f'Reply JSON only: {{"confirm": true/false, "confidence": 0.0-1.0, "reason": "brief"}}'
    )

    try:
        from daybot.llm_router import deepseek_chat as _ds_chat
        raw = _ds_chat(prompt, max_tokens=100)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        m = re.search(r"\{.*?\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            logging.info("Options AI [%s]: confirm=%s conf=%.2f reason=%s",
                         symbol, data.get("confirm"), data.get("confidence", 0.0),
                         data.get("reason", ""))
            return bool(data.get("confirm", False)), float(data.get("confidence", 0.0))
    except Exception as exc:
        logging.warning("Options LLM confirm failed: %s", exc)
    return False, 0.0


# ---------------------------------------------------------------------------
# Expiry guard
# ---------------------------------------------------------------------------

def _near_expiry(pos: OptionsPosition) -> bool:
    try:
        exp     = datetime.strptime(pos.expiry, "%Y-%m-%d")
        exp_utc = exp.replace(hour=20, minute=0, tzinfo=timezone.utc)
        guard   = exp_utc - timedelta(minutes=EXPIRY_GUARD_MIN)
        return datetime.now(timezone.utc) >= guard
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Trailing stop updater
# ---------------------------------------------------------------------------

def _update_trailing_sl(pos: OptionsPosition, current: float) -> str | None:
    """Upgrade SL as position profits. Returns log message if SL changed, else None.

    Ladder:
      current ≥ entry × 1.4  (+40%)  → SL = entry       (breakeven)
      current ≥ entry × 2.0  (+100%) → SL = entry × 1.5 (+50% locked)
      current ≥ entry × 2.5  (+150%) → SL = entry × 2.0 (+100% locked)

    Breakeven at +40% (was +50%) — options lose theta fast; lock sooner.
    AAPL CALL peaked +47% and reversed to -29% under old +50% threshold.
    """
    entry = pos.entry_premium
    msg   = None

    if current >= entry * 2.5 and pos.sl_price < entry * 2.0:
        pos.sl_price = round(entry * 2.0, 2)
        msg = f"SL → +100% locked ${pos.sl_price:.2f} (option at +150%)"
    elif current >= entry * 2.0 and pos.sl_price < entry * 1.5:
        pos.sl_price = round(entry * 1.5, 2)
        msg = f"SL → +50% locked ${pos.sl_price:.2f} (option at +100%)"
    elif current >= entry * 1.4 and pos.sl_price < entry:
        pos.sl_price = round(entry, 2)
        msg = f"SL → breakeven ${pos.sl_price:.2f} (option at +40%)"

    return msg


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def _run_cycle(api_key: str, secret_key: str, paper: bool) -> None:
    state = options_state

    # ── EXIT CHECK + TRAILING STOP ──────────────────────────────────────────
    with state._lock:
        open_positions = list(state.positions.values())

    for pos in open_positions:
        from .chain import get_current_price
        from .executor import sell_contract

        current = get_current_price(pos.contract_symbol, api_key, secret_key)
        if current is None:
            continue

        pnl     = round((current - pos.entry_premium) * pos.qty * 100, 2)
        pnl_pct = round((current - pos.entry_premium) / pos.entry_premium * 100, 1)

        # Update price + trailing SL inside lock
        with state._lock:
            if pos.contract_symbol not in state.positions:
                continue
            p = state.positions[pos.contract_symbol]
            p.current_premium  = current
            p.pnl              = pnl
            p.pnl_pct          = pnl_pct
            p.highest_premium  = max(p.highest_premium, current)
            trail_msg = _update_trailing_sl(p, current)

        if trail_msg:
            state.add_log("Trailing SL", f"{pos.symbol}: {trail_msg}", "neutral")

        # ── TP1 PARTIAL EXIT: sell half at +100%, let rest ride to 3× ──────
        # Only fires when: qty ≥ 2, gain ≥ +100%, tp1 not yet triggered
        with state._lock:
            if pos.contract_symbol not in state.positions:
                continue
            p = state.positions[pos.contract_symbol]
            tp1_eligible = (
                not p.tp1_hit
                and p.qty >= 2
                and pnl_pct >= 100.0
            )
        if tp1_eligible:
            sell_qty = p.qty // 2
            ok = sell_contract(pos.contract_symbol, sell_qty, api_key, secret_key, paper)
            if ok:
                partial_pnl = round((current - pos.entry_premium) * sell_qty * 100, 2)
                with state._lock:
                    if pos.contract_symbol in state.positions:
                        p2 = state.positions[pos.contract_symbol]
                        p2.qty     -= sell_qty
                        p2.tp1_hit  = True
                        p2.sl_price = p2.entry_premium  # move SL to breakeven
                        state.metrics.balance = round(
                            state.metrics.balance + pos.entry_premium * sell_qty * 100 + partial_pnl, 2
                        )
                        state.metrics.daily_pnl = round(state.metrics.daily_pnl + partial_pnl, 2)
                state.add_log(
                    "TP1 Partial",
                    f"{pos.symbol}: sold {sell_qty}/{pos.qty} at +{pnl_pct:.0f}% → "
                    f"+${partial_pnl:.2f} locked | SL → breakeven ${pos.entry_premium:.2f} | "
                    f"{p.qty - sell_qty} contract riding to 3×",
                    "positive",
                )
                logging.info(
                    "TP1 partial [%s]: sold %d contracts @ $%.2f (+%.0f%%) pnl=$%.2f | "
                    "%d remaining, SL→breakeven",
                    pos.symbol, sell_qty, current, pnl_pct, partial_pnl, p.qty - sell_qty,
                )
                state._save()  # persist updated qty + tp1_hit + sl_price + balance
                db_save_trade(
                    symbol=pos.symbol,
                    contract_symbol=pos.contract_symbol,
                    option_type=pos.option_type,
                    strike=pos.strike,
                    expiry=pos.expiry,
                    qty=sell_qty,
                    entry_premium=pos.entry_premium,
                    exit_premium=current,
                    pnl=partial_pnl,
                    pnl_pct=pnl_pct,
                    exit_reason="tp1_partial",
                    tp1_hit=True,
                )
            continue  # skip full-exit check this cycle

        # Exit decision (using updated sl_price after trailing)
        with state._lock:
            sl_price = state.positions[pos.contract_symbol].sl_price if pos.contract_symbol in state.positions else pos.sl_price

        reason = None
        if current <= sl_price:
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
                    state.metrics.balance   = round(
                        state.metrics.balance + pos.entry_premium * pos.qty * 100 + pnl, 2
                    )
                    if pnl >= 0:
                        state.metrics.wins_today += 1
                    else:
                        state.metrics.losses_today += 1
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
                    "Options closed: %s %s | reason=%s pnl=$%.2f | "
                    "total=%d wins=%d losses=%d wr=%.0f%% balance=$%.2f",
                    pos.symbol, pos.option_type, reason, pnl,
                    state.metrics.total_trades, state.metrics.total_wins,
                    state.metrics.total_losses, state.metrics.win_rate,
                    state.metrics.balance,
                )
                db_save_trade(
                    symbol=pos.symbol,
                    contract_symbol=pos.contract_symbol,
                    option_type=pos.option_type,
                    strike=pos.strike,
                    expiry=pos.expiry,
                    qty=pos.qty,
                    entry_premium=pos.entry_premium,
                    exit_premium=current,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=reason,
                    tp1_hit=pos.tp1_hit,
                )

    # ── ENTRY CHECK ─────────────────────────────────────────────────────────
    with state._lock:
        halted     = state.metrics.daily_loss_halted
        open_count = len(state.positions)
        balance    = state.metrics.balance
        start_bal  = state.metrics.daily_start_balance
        losses     = state.metrics.losses_today
        wins       = state.metrics.wins_today

    if halted:
        return

    # Daily loss halt — use current market VALUE of open positions, not unrealized pnl.
    # Bug: balance already deducted premium cost, so balance + open_pnl (gain/loss only)
    # = $826 + $0 = $826 immediately after buying, showing 17% "loss" even though no loss.
    # Fix: add current_premium × qty × 100 (full market value) instead of just pnl.
    if start_bal > 0:
        open_value = sum(p.current_premium * p.qty * 100 for p in state.positions.values())
        drop = (start_bal - (balance + open_value)) / start_bal * 100
        if drop >= DAILY_LOSS_HALT_PCT:
            with state._lock:
                state.metrics.daily_loss_halted = True
            state.add_log("Daily limit", f"Down {drop:.1f}% — paused until tomorrow", "negative")
            return

    # SHIELD: 2+ losses today → no new entries
    if losses >= 2:
        with state._lock:
            state.metrics.mode = "SHIELD"
        state.add_log("Mode", "SHIELD — 2 losses today, no new entries", "neutral")
        return

    with state._lock:
        state.metrics.mode = "AGGRESSIVE" if wins >= 3 and losses == 0 else "SAFE"

    if open_count >= MAX_POSITIONS:
        return

    # ── FILTER 0a: Day-of-week gate ───────────────────────────────────────────
    # Mon/Wed/Fri only — Tue/Thu consistently underperform (Option Alpha: 230K trades).
    # Tuesday/Thursday are low-conviction drift days with no reliable catalysts.
    _now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    _weekday = _now_et.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    if _weekday in (1, 3):  # Tuesday=1, Thursday=3
        state.add_log("Skipped", f"Day-of-week gate: Tue/Thu — no new entries (low-conviction days)", "neutral")
        return

    # ── FILTER 0b: Entry time gate ────────────────────────────────────────────
    # No new entries before 10:00 AM ET — IV highest, spreads widest at open.
    # Optimal window: 10:00–11:30 AM ET (IV settled, trend direction clear).
    _et_hour = _now_et.hour
    _et_minute = _now_et.minute
    _et_minutes_total = _et_hour * 60 + _et_minute
    if _et_minutes_total < 10 * 60:  # before 10:00 AM ET
        state.add_log("Skipped", f"Entry time gate: {_et_hour:02d}:{_et_minute:02d} ET — no entries before 10:00 AM (high IV/spreads)", "neutral")
        return

    # ── FILTER 0c: VIX gate ───────────────────────────────────────────────────
    # Skip all new entries when VIX > 30 — premiums too expensive, gamma risk too high for $1K.
    try:
        import yfinance as _yf
        _vix_hist = _yf.Ticker("^VIX").fast_info
        _vix_now = float(getattr(_vix_hist, "last_price", 0.0) or 0.0)
        if _vix_now > 30:
            state.add_log("Skipped", f"VIX gate: VIX {_vix_now:.1f} > 30 — premium too expensive, skip all entries", "neutral")
            return
    except Exception:
        pass  # fail open — don't block on VIX fetch error

    # ── FILTER 1: SPY market regime ──────────────────────────────────────────
    spy = _spy_trend()
    state.add_log("Market", f"SPY trend: {spy.upper()}", "neutral")

    # ── EVENING PRE-ANALYSIS GATE ─────────────────────────────────────────────
    # If evening analysis ran last night and targets today, use it as primary filter.
    # Only scan pre-approved symbols + enforce pre-planned direction + use target OTM%.
    from datetime import date as _date
    _today_str = str(_date.today())
    _evening_active = (
        bool(state.evening_approved) and
        state.evening_analysis_date == _today_str
    )
    if _evening_active:
        _scan_symbols = [s for s in state.evening_approved if s in SYMBOLS]
        state.add_log(
            "Evening plan",
            f"Using pre-analysis: {', '.join(_scan_symbols)} | "
            f"IV:{state.evening_iv_regime} market:{state.evening_regime}",
            "neutral",
        )
    else:
        _scan_symbols = SYMBOLS

    # ── SCAN SYMBOLS ─────────────────────────────────────────────────────────
    for symbol in _scan_symbols:
        with state._lock:
            if len(state.positions) >= MAX_POSITIONS:
                break
            already_open = any(p.symbol == symbol for p in state.positions.values())
        if already_open:
            continue

        action, confidence, reason = _signal(symbol)
        state.add_log("Signal", f"{symbol}: {action} conf={confidence:.0%} — {reason}", "neutral")

        if action == "HOLD" or confidence < MIN_CONFIDENCE:
            continue

        # ── Evening direction gate: pre-analysis planned direction must match signal ──
        if _evening_active and symbol in state.evening_direction:
            planned_dir = state.evening_direction[symbol]   # CALL | PUT
            signal_dir = "CALL" if action == "BUY" else "PUT"
            if planned_dir != signal_dir:
                state.add_log(
                    "Skipped",
                    f"{symbol}: signal {signal_dir} ≠ evening plan {planned_dir} — skip",
                    "neutral",
                )
                continue
            # Raise bar for low conviction pre-plans
            _conviction = state.evening_conviction.get(symbol, "medium")
            if _conviction == "low":
                state.add_log("Skipped", f"{symbol}: evening conviction=low — skip", "neutral")
                continue

        # SPY gate: don't buy calls in bear, don't buy puts in bull
        if spy == "bear" and action == "BUY":
            state.add_log("Skipped", f"{symbol}: market bearish — no calls", "neutral")
            continue
        if spy == "bull" and action == "SELL":
            state.add_log("Skipped", f"{symbol}: market bullish — no puts", "neutral")
            continue

        # FILTER 2: Earnings avoid
        if _has_earnings_soon(symbol):
            state.add_log("Skipped", f"{symbol}: earnings within {EARNINGS_AVOID_DAYS}d — IV crush risk", "neutral")
            continue

        # FILTER 3: Sector dedup — don't open same sector + same direction twice
        my_sector = _SECTOR.get(symbol, "other")
        with state._lock:
            sector_conflict = any(
                _SECTOR.get(p.symbol, "x") == my_sector and
                ((p.option_type == "call") == (action == "BUY"))
                for p in state.positions.values()
            )
        if sector_conflict:
            state.add_log("Skipped", f"{symbol}: same sector ({my_sector}) already open in same direction", "neutral")
            continue

        # Pick contract — use evening OTM% target if available
        from .chain import pick_contract
        _budget  = BUDGET if BUDGET > 0 else round(balance * _OPTIONS_BUDGET_PCT, 2)
        _target_otm = state.evening_strike_pct.get(symbol) if _evening_active else None
        contract = pick_contract(symbol, action, _budget, target_otm_pct=_target_otm)
        if not contract:
            state.add_log("Skipped", f"{symbol}: no contract within ${_budget:.0f}", "neutral")
            continue

        # FILTER 4: IV guard — skip if implied volatility too high (expensive premium)
        if contract["iv"] > IV_MAX:
            state.add_log(
                "Skipped",
                f"{symbol}: IV {contract['iv']:.0%} > {IV_MAX:.0%} max — premium too expensive",
                "neutral",
            )
            continue

        # LLM confirm
        confirmed, llm_conf = _llm_confirm(symbol, action, reason, contract)
        final_conf = (confidence + llm_conf) / 2
        state.add_log(
            "AI",
            f"{symbol} {contract['option_type'].upper()} ${contract['strike']} "
            f"exp {contract['expiry']} IV={contract['iv']:.0%} | conf={final_conf:.0%}",
            "neutral",
        )

        if not confirmed or final_conf < MIN_CONFIDENCE:
            state.add_log("Skipped", f"{symbol}: AI rejected (conf={final_conf:.0%})", "neutral")
            continue

        # Balance check — buy 2 contracts if budget allows (enables TP1 partial exit)
        with state._lock:
            balance = state.metrics.balance
        if balance < contract["cost"]:
            state.add_log("Skipped", f"{symbol}: balance ${balance:.0f} < cost ${contract['cost']:.0f}", "neutral")
            continue

        # Buy 2 contracts when balance >= 2× cost AND remaining slots allow it
        # 2 contracts → sell 1 at +100% (lock profit), let 1 ride to 3×
        with state._lock:
            slots_left = MAX_POSITIONS - len(state.positions)
        buy_qty = 2 if (balance >= contract["cost"] * 2 and slots_left >= 2) else 1
        total_cost = contract["cost"] * buy_qty

        # Execute BUY
        from .executor import buy_contract
        ok = buy_contract(contract["contract_symbol"], buy_qty, api_key, secret_key, paper)
        if ok:
            # Use IV-regime-adaptive SL/TP — wider SL in high-IV to survive IV crush at open.
            _iv_reg = state.evening_iv_regime or "normal"
            _sl_pct = _IV_REGIME_SL.get(_iv_reg, SL_PCT)
            _tp_pct = _IV_REGIME_TP.get(_iv_reg, TP_PCT)
            sl = round(contract["premium"] * (1 - _sl_pct), 2)
            tp = round(contract["premium"] * (1 + _tp_pct), 2)
            pos = OptionsPosition(
                symbol          = symbol,
                contract_symbol = contract["contract_symbol"],
                option_type     = contract["option_type"],
                strike          = contract["strike"],
                expiry          = contract["expiry"],
                qty             = buy_qty,
                entry_premium   = contract["premium"],
                current_premium = contract["premium"],
                sl_price        = sl,
                tp_price        = tp,
                entry_time      = datetime.now(timezone.utc).strftime("%H:%M:%S"),
                highest_premium = contract["premium"],
            )
            with state._lock:
                state.positions[contract["contract_symbol"]] = pos
                state.metrics.balance = round(state.metrics.balance - total_cost, 2)
                open_count = len(state.positions)
            state.add_log(
                "Trade BUY",
                f"{symbol} {contract['option_type'].upper()} ${contract['strike']} "
                f"exp {contract['expiry']} IV={contract['iv']:.0%} OTM={contract.get('otm_pct',0):.1f}% "
                f"@ ${contract['premium']:.2f} × {buy_qty} | cost=${total_cost:.0f} "
                f"SL=${sl:.2f}(-{_sl_pct:.0%}) TP=${tp:.2f}(+{_tp_pct:.0%}) iv_regime={_iv_reg} | open={open_count}/{MAX_POSITIONS}",
                "positive",
            )
            state._save()  # persist position so restart doesn't lose it
            logging.info(
                "Options BUY: %s %s $%.0f exp=%s | qty=%d cost=$%.0f SL=$%.2f TP=$%.2f balance=$%.2f",
                symbol, contract["option_type"], contract["strike"], contract["expiry"],
                buy_qty, total_cost, sl, tp, state.metrics.balance,
            )


# ---------------------------------------------------------------------------
# Bot loop
# ---------------------------------------------------------------------------

def _is_market_hours(api_key: str = "", secret_key: str = "", paper: bool = True) -> bool:
    """Check if US market is open. Queries Alpaca clock when creds provided — handles holidays + early closes."""
    now = datetime.now(timezone.utc)
    # Weekend fast-exit — no API call
    if now.weekday() >= 5:
        return False

    # Alpaca clock — authoritative, handles Memorial Day / Christmas / early closes
    if api_key:
        try:
            from alpaca.trading.client import TradingClient
            clock = TradingClient(api_key, secret_key, paper=paper).get_clock()
            return bool(clock.is_open)
        except Exception as exc:
            logging.debug("Alpaca clock check failed (%s) — using time fallback", exc)

    # Time-based fallback (no holiday awareness)
    # 9:35 AM – 3:45 PM ET (EDT = UTC-4)
    et      = now - timedelta(hours=4)
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 35 <= minutes <= 15 * 60 + 45


def _bot_loop(api_key: str, secret_key: str, paper: bool) -> None:
    logging.info("Options bot loop started (paper=%s)", paper)
    options_state.add_log(
        "Bot started",
        f"Options loop — paper={paper} | monitoring since {options_state.metrics.monitor_start_date} | "
        f"max={MAX_POSITIONS} positions | TP=3× trailing SL",
        "neutral",
    )

    _last_day = datetime.now(timezone.utc).date()

    while not _stop_event.is_set():
        # Midnight daily reset
        today = datetime.now(timezone.utc).date()
        if today != _last_day:
            options_state.daily_reset()
            _last_day = today

        if not _is_market_hours(api_key, secret_key, paper):
            _stop_event.wait(60)
            continue

        # ── Friday EOD close — close all positions before weekend ────────────
        # Options hold over weekends bleed theta for 2 days with no chance to react.
        # Close all open positions by 3:30 PM ET on Fridays (before market gets thin).
        _now_et = datetime.now(timezone.utc) - timedelta(hours=4)
        _is_friday = _now_et.weekday() == 4  # Friday=4
        _et_minutes = _now_et.hour * 60 + _now_et.minute
        if _is_friday and _et_minutes >= 15 * 60 + 30 and options_state.positions:
            logging.info("Options Friday EOD: closing all %d positions before weekend", len(options_state.positions))
            options_state.add_log("Friday EOD", "Closing all options before weekend — theta decay risk", "warning")
            for cs in list(options_state.positions.keys()):
                pos = options_state.positions.get(cs)
                if pos:
                    try:
                        from .executor import sell_contract
                        sell_contract(cs, pos.qty, api_key, secret_key, paper)
                        pnl = (pos.current_premium - pos.entry_premium) * pos.qty * 100
                        options_state.record_trade_close(pnl)
                        options_state.add_log("Friday close", f"{pos.symbol} closed before weekend | pnl=${pnl:+.2f}", "neutral")
                        with options_state._lock:
                            options_state.positions.pop(cs, None)
                            options_state.metrics.balance = round(options_state.metrics.balance + pos.current_premium * pos.qty * 100, 2)
                    except Exception as exc:
                        logging.error("Friday EOD close failed [%s]: %s", cs, exc)
            options_state._save()

        try:
            _run_cycle(api_key, secret_key, paper)
        except Exception as exc:
            logging.error("Options bot cycle error: %s", exc)
            options_state.add_log("Error", str(exc)[:120], "negative")
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
