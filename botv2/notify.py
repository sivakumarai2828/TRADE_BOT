"""Telegram notifications (same env vars as V1: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)."""

from __future__ import annotations

import html
import logging
import os

import requests

log = logging.getLogger("botv2.notify")


def _esc(text) -> str:
    """Escape AI-generated text so it can't break Telegram HTML mode."""
    return html.escape(str(text))


def _send(msg: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as exc:
        log.warning("telegram failed: %s", exc)


def _cur(currency: str) -> str:
    return "₹" if currency == "INR" else "$"


def trade_opened(market, symbol, qty, price, stop, target, thesis, currency) -> None:
    c = _cur(currency)
    _send(
        f"🟢 <b>[{market}] BUY {symbol}</b>\n"
        f"{qty:.0f} sh @ {c}{price:,.2f}\n"
        f"Stop: {c}{stop:,.2f}" + (f" | Target: {c}{float(target):,.2f}" if target else "") +
        f"\n💡 {_esc(thesis[:200])}"
    )


def trade_closed(market, symbol, price, pnl, reason, currency) -> None:
    c = _cur(currency)
    icon = "🟢" if pnl >= 0 else "🔴"
    _send(f"{icon} <b>[{market}] SELL {symbol}</b> @ {c}{price:,.2f}\n"
          f"P&L: {c}{pnl:+,.2f} ({_esc(reason)})")


def market_view(market: str, view: str) -> None:
    if view:
        _send(f"🧠 <b>[{market}] PM view:</b> {_esc(view[:400])}")


def alert(msg: str) -> None:
    _send(msg)
