"""Trade logger and EOD report generator."""
from __future__ import annotations
import logging
import os
import requests
from datetime import datetime, timezone
from .state import DayBotState


class TradeLogger:
    def __init__(self, state: DayBotState) -> None:
        self._state = state

    def log_scan(self, symbols: list[str]) -> None:
        self._state.add_log("Scan", f"{len(symbols)} candidates: {', '.join(symbols[:8])}")
        logging.info("Scan result: %s", symbols)

    def log_shortlist(self, symbols: list[str]) -> None:
        msg = ", ".join(symbols) if symbols else "none passed filters"
        self._state.add_log("Shortlist", f"{len(symbols)} passed: {msg}")
        logging.info("Shortlist: %s", symbols)

    def log_signal(self, symbol: str, action: str, reason: str) -> None:
        tone = "positive" if action == "BUY" else "negative" if action == "SELL" else "neutral"
        self._state.add_log("Signal", f"{symbol}: {action} — {reason}", tone)

    def log_ai_validation(self, symbol: str, decision: str, confidence: float, reason: str) -> None:
        self._state.add_log(
            "AI",
            f"{symbol}: {decision} (conf={confidence:.2f}) — {reason}",
        )

    def log_trade(self, symbol: str, action: str, price: float, qty: int, reason: str) -> None:
        tone = "positive" if action == "BUY" else "negative"
        self._state.add_log(
            f"Trade {action}",
            f"{symbol} @ ${price:.2f} × {qty} | {reason}",
            tone,
        )

    def generate_eod_report(self) -> str:
        m = self._state.metrics
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report = (
            f"📋 <b>Day Bot EOD — {date_str}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Portfolio: <b>${m.portfolio_value:,.2f}</b>\n"
            f"📅 Daily P&L: <b>${m.daily_pnl:+.2f} ({m.daily_pnl_pct:+.2f}%)</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 Trades: {m.trades_today} | ✅ {m.wins_today}W / ❌ {m.losses_today}L\n"
            f"🤖 Status: {'🔴 Halted' if m.daily_loss_halted else '🟢 Active'}"
        )
        self._send_telegram(report)
        logging.info("EOD report sent")
        return report

    def _send_telegram(self, message: str) -> None:
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
            logging.warning("Telegram send failed: %s", exc)
