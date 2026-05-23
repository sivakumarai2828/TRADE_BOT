"""Crypto-specific risk management — extends the base RiskManager for 24/7 markets."""
from __future__ import annotations
import logging
from daybot.risk_manager import RiskManager


class CryptoRiskManager(RiskManager):
    """RiskManager subclass with crypto-specific sizing, volatility gates, and drawdown alerts."""

    def __init__(
        self,
        max_trades_per_day: int = 5,        # crypto is 24/7 — allow more than 3
        max_concurrent: int = 3,
        position_size_pct: float = 0.10,    # 10% per trade — conservative for crypto volatility
        max_daily_loss_pct: float = 0.05,   # 5% daily loss halt
        min_position_usd: float = 20.0,     # minimum $20 notional per trade
        leverage: float = 1.0,              # spot only by default (no margin)
    ) -> None:
        super().__init__(
            max_trades_per_day=max_trades_per_day,
            max_concurrent=max_concurrent,
            position_size_pct=position_size_pct,
            max_daily_loss_pct=max_daily_loss_pct,
        )
        self.min_position_usd = min_position_usd
        self.leverage = leverage

    # ------------------------------------------------------------------
    # ATR-based position sizing
    # ------------------------------------------------------------------

    def calculate_crypto_position(
        self,
        portfolio_usd: float,
        price: float,
        atr: float,
        risk_per_trade_pct: float = 0.01,  # risk 1% of portfolio per trade
    ) -> tuple[float, float]:
        """Size position so 1 ATR stop-loss equals risk_per_trade_pct of portfolio.

        Formula: qty = (portfolio × risk_pct) / atr
        Capped at position_size_pct of portfolio to avoid over-concentration.
        Returns (qty, notional_usd).
        """
        if price <= 0 or atr <= 0:
            logging.warning("calculate_crypto_position: invalid price=%.4f or atr=%.4f", price, atr)
            return 0.0, 0.0

        risk_dollars = portfolio_usd * risk_per_trade_pct       # e.g. $500 × 1% = $5 max risk
        qty_by_risk = risk_dollars / atr                        # qty so 1 ATR = $5 loss

        max_notional = portfolio_usd * self.position_size_pct   # e.g. $500 × 10% = $50 max
        qty_by_cap = max_notional / price

        qty = min(qty_by_risk, qty_by_cap)                      # take the smaller (more conservative)
        notional_usd = round(qty * price, 2)

        if notional_usd < self.min_position_usd:
            logging.info(
                "calculate_crypto_position: notional $%.2f < min $%.2f — skipping",
                notional_usd, self.min_position_usd,
            )
            return 0.0, 0.0

        logging.info(
            "CryptoPosition: qty=%.6f notional=$%.2f (risk_cap=$%.2f atr=%.4f)",
            qty, notional_usd, risk_dollars, atr,
        )
        return round(qty, 6), notional_usd

    # ------------------------------------------------------------------
    # Volatility gate
    # ------------------------------------------------------------------

    def is_too_volatile(self, atr_pct: float) -> bool:
        """Return True if ATR as % of price exceeds 5% — skip hyper-volatile setups."""
        if atr_pct > 5.0:
            logging.warning("Volatility gate: ATR %.1f%% > 5%% — skipping trade", atr_pct)
            return True
        return False

    # ------------------------------------------------------------------
    # Drawdown halt with Telegram alert
    # ------------------------------------------------------------------

    def check_drawdown_and_halt(
        self,
        current_value: float,
        start_value: float,
    ) -> bool:
        """Halt if daily drawdown >= max_daily_loss_pct; send Telegram alert on halt.

        Returns True if trading should be halted, False otherwise.
        Mirrors parent check_daily_loss() but also logs drawdown % and notifies Telegram.
        """
        if start_value <= 0:
            return False

        drawdown_pct = (start_value - current_value) / start_value * 100

        if drawdown_pct >= self.max_daily_loss_pct * 100:
            self._daily_loss_halted = True
            msg = (
                f"🛑 <b>Daily Loss Limit Hit</b>\n"
                f"Drawdown: <b>{drawdown_pct:.1f}%</b> "
                f"(limit {self.max_daily_loss_pct * 100:.0f}%)\n"
                f"Start: ${start_value:,.2f} → Now: ${current_value:,.2f}\n"
                f"Trading halted for today."
            )
            logging.warning("CryptoRiskManager: drawdown %.1f%% — halt triggered", drawdown_pct)
            try:
                from telegram_notify import _send
                _send(msg)
            except Exception as exc:
                logging.warning("Telegram drawdown alert failed: %s", exc)
            return True

        logging.debug(
            "CryptoRiskManager: drawdown %.1f%% — within limit (%.0f%%)",
            drawdown_pct, self.max_daily_loss_pct * 100,
        )
        return False
