"""Telegram notifications for trade events."""

from __future__ import annotations

import logging
import os
import requests


def _send(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as exc:
        logging.warning("Telegram notify failed: %s", exc)


def notify_buy(symbol: str, amount: float, price: float, stop_loss: float, take_profit: float, trade_size: float) -> None:
    _send(
        f"🟢 <b>BUY {symbol}</b>\n"
        f"Price: ${price:,.2f}\n"
        f"Amount: {amount:.6f} (${trade_size:.2f})\n"
        f"Stop Loss: ${stop_loss:,.2f}\n"
        f"Take Profit: ${take_profit:,.2f}"
    )


def notify_sell(symbol: str, price: float, pnl: float, pnl_pct: float, reason: str) -> None:
    icon = "🟢" if pnl >= 0 else "🔴"
    reason_label = {"stop_loss": "Stop Loss hit", "take_profit": "Take Profit hit",
                    "signal_sell": "Signal SELL", "manual_close": "Manual close"}.get(reason, reason)
    _send(
        f"{icon} <b>SELL {symbol}</b>\n"
        f"Price: ${price:,.2f}\n"
        f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
        f"Reason: {reason_label}"
    )


def notify_shield_on(reason: str) -> None:
    _send(f"🛡 <b>Auto-Shield ACTIVATED</b>\n{reason}\nSwitched to House Money mode — principal protected.")


def notify_shield_off(mode: str) -> None:
    _send(f"✅ <b>Auto-Shield OFF</b>\nMarket recovered — back to {mode} mode.")


def notify_daily_summary(
    date: str,
    balance: float,
    pnl: float,
    pnl_pct: float,
    daily_pnl: float,
    daily_pnl_pct: float,
    total_trades: int,
    win_count: int,
    loss_count: int,
    win_rate: float,
    open_positions: list,
    daily_halted: bool,
) -> None:
    status = "🔴 Daily limit hit" if daily_halted else "🟢 Active"
    pos_lines = ""
    for p in open_positions:
        icon = "📈" if p["pnl_pct"] >= 0 else "📉"
        pos_lines += f"\n  {icon} {p['symbol']}: {p['pnl_pct']:+.2f}% (SL ${p['stop_loss']:,.2f})"

    _send(
        f"📊 <b>Daily Summary — {date}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Balance: <b>${balance:,.2f}</b> (started $500)\n"
        f"📅 Today's PnL: <b>${daily_pnl:+.2f} ({daily_pnl_pct:+.2f}%)</b>\n"
        f"📈 Total PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 Trades today: {total_trades} | ✅ {win_count}W / ❌ {loss_count}L\n"
        f"📊 Win Rate: {win_rate:.1f}%\n"
        f"🤖 Bot Status: {status}"
        + (f"\n━━━━━━━━━━━━━━━\n🔓 Open Positions:{pos_lines}" if pos_lines else "")
    )


def notify_mode_change(bot: str, old_mode: str, new_mode: str, params) -> None:
    icons = {"SAFE": "🟡", "AGGRESSIVE": "🚀", "SHIELD": "🛡"}
    icon = icons.get(new_mode, "🔄")
    if hasattr(params, "position_size_pct"):
        size_line = f"Position size: {params.position_size_pct*100:.0f}%"
        sl_line = f"SL {params.stop_loss_pct*100:.1f}% / TP {params.take_profit_pct*100:.1f}%"
    else:
        size_line = f"Size multiplier: ×{params.size_multiplier}"
        sl_line = f"SL {params.stop_loss_pct:.1f}% / TP {params.take_profit_pct:.1f}%"
    _send(
        f"{icon} <b>{bot} Mode: {old_mode} → {new_mode}</b>\n"
        f"{size_line} | {sl_line}"
    )


def notify_harvest_extraction(bot: str, amount: float, regime: str) -> None:
    _send(
        f"🌱 <b>{bot} Harvest</b>\n"
        f"Extracted <b>${amount:.2f}</b> profit → long-term position opened\n"
        f"Market regime: {regime}"
    )


def notify_harvest_target(bot: str, bucket: str, symbol: str, pnl_pct: float, pnl: float) -> None:
    _send(
        f"🎯 <b>{bot} Harvest Target Hit!</b>\n"
        f"[{bucket}] {symbol}: <b>+{pnl_pct:.1f}%</b> (${pnl:+.2f})\n"
        f"Profits reinvested per 3-bucket split."
    )


def notify_daybot_summary(
    date: str,
    portfolio_value: float,
    daily_pnl: float,
    daily_pnl_pct: float,
    trades: int,
    wins: int,
    losses: int,
    halted: bool,
) -> None:
    status = "🔴 Daily limit hit" if halted else "✅ Market closed"
    icon = "📈" if daily_pnl >= 0 else "📉"
    _send(
        f"📊 <b>Day Bot Summary — {date}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Portfolio: <b>${portfolio_value:,.2f}</b>\n"
        f"{icon} Today's PnL: <b>${daily_pnl:+.2f} ({daily_pnl_pct:+.2f}%)</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 Trades: {trades} | ✅ {wins}W / ❌ {losses}L\n"
        f"🤖 Status: {status}"
    )


def notify_bot_started() -> None:
    _send("🚀 <b>AI Trade Bot started</b>\nMonitoring BTC/USD, ETH/USD, SOL/USD")


def notify_bot_stopped() -> None:
    _send("⛔ <b>AI Trade Bot stopped</b>")


def notify_api_timeout(bot: str, symbol: str, consecutive: int) -> None:
    _send(
        f"⚠️ <b>{bot} API Timeout</b>\n"
        f"{symbol} — {consecutive} consecutive failures\n"
        f"Alpaca data feed unreachable. Bot is retrying automatically."
    )


def notify_no_trades_alert(bot: str, minutes_in_window: int) -> None:
    _send(
        f"🔕 <b>{bot} — No Trades Yet</b>\n"
        f"Been in trading window for <b>{minutes_in_window} min</b> with 0 trades.\n"
        f"Check bot logs — signals may be too tight or data feed issues."
    )


def notify_health_check(
    crypto_running: bool,
    crypto_balance: float,
    crypto_trades: int,
    crypto_errors: int,
    day_running: bool,
    day_trades: int,
    day_pnl: float,
    alpaca_ok: bool,
) -> None:
    crypto_icon = "✅" if crypto_running else "🔴"
    day_icon = "✅" if day_running else "⏸"
    alpaca_icon = "✅" if alpaca_ok else "❌"
    _send(
        f"🩺 <b>11 AM Health Check</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{crypto_icon} Crypto Bot: {'running' if crypto_running else 'stopped'} | "
        f"${crypto_balance:.2f} | {crypto_trades} trades | {crypto_errors} errors\n"
        f"{day_icon} Day Bot: {'running' if day_running else 'stopped'} | "
        f"{day_trades} trades | ${day_pnl:+.2f} PnL\n"
        f"{alpaca_icon} Alpaca API: {'reachable' if alpaca_ok else 'UNREACHABLE'}\n"
        f"━━━━━━━━━━━━━━━"
    )


def notify_daily_loss_halted(bot: str, loss_pct: float) -> None:
    _send(
        f"🛑 <b>{bot} — Daily Loss Limit Hit</b>\n"
        f"Loss: <b>{loss_pct:.1f}%</b> — no new trades today.\n"
        f"Bot will resume tomorrow after midnight reset."
    )


def notify_options_suggestions(picks: list, vix: float, regime: str) -> None:
    if not picks:
        return
    regime_icon = {"trending_up": "📈", "trending_down": "📉", "sideways": "↔️"}.get(regime, "❓")
    vix_note = " ⚠️ High VIX — size small" if vix > 25 else ""
    lines = [
        f"🎯 <b>Options Suggestions — {len(picks)} setup(s)</b>",
        f"{regime_icon} Regime: {regime.replace('_',' ').title()} | VIX: {vix:.1f}{vix_note}",
        f"━━━━━━━━━━━━━━━",
        f"<i>Suggestions only — trade manually on Robinhood</i>",
        f"━━━━━━━━━━━━━━━",
    ]
    for p in picks:
        opt_icon = "📞" if p.get("option_type") == "call" else "📉"
        lines += [
            f"{opt_icon} <b>{p['symbol']} ${p.get('strike')} {p.get('option_type','').upper()}"
            f" exp {p.get('expiry')}</b>",
            f"  Entry: ~${p.get('entry_price',0):.2f} | Target: ~${p.get('target_price',0):.2f}"
            f" ({round((p.get('target_price',0)/max(p.get('entry_price',0.01),0.01)-1)*100):.0f}% gain)",
            f"  Underlying stop: ${p.get('underlying_stop',0):.2f}",
            f"  OI: {p.get('open_interest',0):,} | IV: {p.get('iv',0)*100:.0f}%",
            f"  ↳ {p.get('reason','')}",
            f"━━━━━━━━━━━━━━━",
        ]
    _send("\n".join(lines))


def notify_user_stop_loss(
    symbol: str,
    asset_type: str,
    current_price: float,
    stop_price: float,
    entry_price: float,
    option_detail: str = "",
) -> None:
    pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
    if asset_type == "option":
        _send(
            f"🚨 <b>OPTIONS STOP ALERT — {symbol}</b>\n"
            f"Underlying at <b>${current_price:.2f}</b> — below your stop ${stop_price:.2f}\n"
            f"Position: {option_detail}\n"
            f"⚠️ Consider exiting on Robinhood now."
        )
    else:
        icon = "📉" if pnl_pct < 0 else "📈"
        _send(
            f"🚨 <b>STOP LOSS ALERT — {symbol}</b>\n"
            f"{icon} Price: <b>${current_price:.2f}</b> | Stop: ${stop_price:.2f}\n"
            f"Entry: ${entry_price:.2f} | P&L: {pnl_pct:+.1f}%\n"
            f"⚠️ Consider exiting on Robinhood now."
        )


def notify_market_close_reminder(open_positions: list) -> None:
    if not open_positions:
        return
    lines = [
        "⏰ <b>Market closes in 20 min!</b>",
        f"You have <b>{len(open_positions)}</b> open position(s) — review on Robinhood:",
    ]
    for p in open_positions[:5]:
        sym = p.get("symbol", "")
        atype = p.get("asset_type", "stock")
        entry = p.get("entry_price", 0)
        stop = p.get("stop_price") or p.get("underlying_stop") or 0
        detail = f" {p.get('option_type','').upper()} ${p.get('strike')} {p.get('expiry')}" if atype == "option" else ""
        lines.append(f"  • {sym}{detail} | Entry ${entry:.2f} | Stop ${stop:.2f}")
    _send("\n".join(lines))


def notify_morning_briefing(approved: list, entry_zones: dict, stop_levels: dict,
                             targets: dict, notes: dict, regime: str) -> None:
    if not approved:
        return
    regime_icon = {"trending_up": "📈", "trending_down": "📉", "sideways": "↔️"}.get(regime, "❓")
    lines = [
        f"☀️ <b>Morning Briefing — Today's Stock Picks</b>",
        f"{regime_icon} Regime: {regime.replace('_',' ').title()}",
        f"━━━━━━━━━━━━━━━",
        f"<i>Suggestions only — monitor and trade manually</i>",
        f"━━━━━━━━━━━━━━━",
    ]
    for sym in approved[:8]:
        ez = entry_zones.get(sym, [])
        sl = stop_levels.get(sym)
        tp = targets.get(sym)
        note = notes.get(sym, "")
        entry_str = f"${ez[0]:.2f}–${ez[1]:.2f}" if len(ez) == 2 else "—"
        sl_str = f"${sl:.2f}" if sl else "—"
        tp_str = f"${tp:.2f}" if tp else "—"
        lines.append(f"<b>{sym}</b> | Entry: {entry_str} | SL: {sl_str} | Target: {tp_str}")
        if note:
            lines.append(f"  ↳ {note}")
    _send("\n".join(lines))
