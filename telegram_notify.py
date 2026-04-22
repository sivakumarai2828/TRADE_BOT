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
