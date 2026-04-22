"""3-Bucket Profit Harvesting Manager.

Bucket 1 — Active Trading  (existing bot, always $1k day / $500 crypto)
Bucket 2 — Long-Term       (60-90 day hold, target +30%, funded by daily profits)
Bucket 3 — Compound        (2-4 week hold, target +15%, funded by long-term profits only)

Flow:
  EOD: if daily_pnl >= EXTRACT_THRESHOLD
       → extract profit → Claude picks a long-term stock/crypto
       → open long_term position in Supabase

  Daily monitor: fetch current price for every open position
       → if pnl_pct >= target: CLOSE and split profits
           capital (original invested) → open next long-term position
           profit (gain only):
               50% → add to active trading base
               50% → open compound position (Claude picks candidate)
       → if hold_days > max_hold_days: force close (expired)

  Compound monitor: same as above at target 15%
       → on hit: full proceeds (capital + profit) → split
           50% → active trading base
           50% → back to long-term bucket

All positions tracked in Supabase harvest_positions table.
Paper-mode: prices fetched via Alpaca data API (no real orders placed).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from harvest.db import (
    close_position,
    get_open_positions,
    log_extraction,
    save_position,
    update_price,
)
from harvest.picker import pick_compound, pick_long_term


class ProfitHarvester:
    def __init__(
        self,
        anthropic_api_key: str,
        alpaca_api_key: str,
        alpaca_secret_key: str,
        claude_model: str = "claude-sonnet-4-6",
    ) -> None:
        self._ai_key = anthropic_api_key
        self._alp_key = alpaca_api_key
        self._alp_secret = alpaca_secret_key
        self._model = claude_model

        self._extract_threshold = float(os.getenv("HARVEST_EXTRACT_THRESHOLD", "50"))
        self._long_term_target = float(os.getenv("HARVEST_LONG_TERM_TARGET_PCT", "30"))
        self._compound_target = float(os.getenv("HARVEST_COMPOUND_TARGET_PCT", "15"))
        self._base_trading_bonus: float = 0.0  # accumulated bonus for active trading base

    # ------------------------------------------------------------------
    # EOD extraction — called when day bot stops
    # ------------------------------------------------------------------

    def check_and_extract(
        self,
        daily_pnl: float,
        bot_type: str,           # 'day' | 'crypto'
        watchlist: list[str],
        market_regime: str = "neutral",
    ) -> Optional[float]:
        """If daily_pnl >= threshold, extract profit and open a long-term position.

        Returns the extracted amount or None if threshold not met.
        """
        if daily_pnl < self._extract_threshold:
            logging.info(
                "Harvest: daily_pnl $%.2f < threshold $%.2f — no extraction",
                daily_pnl, self._extract_threshold,
            )
            return None

        candidates = _get_candidates(bot_type, watchlist)
        pick = pick_long_term(
            api_key=self._ai_key,
            candidates=candidates,
            market_regime=market_regime,
            bot_type=bot_type,
            amount=daily_pnl,
            model=self._model,
        )
        if pick is None:
            logging.info("Harvest: no long-term candidate found — profit kept in base")
            return None

        entry_price = self._get_price(pick.symbol)
        if entry_price <= 0:
            logging.warning("Harvest: could not fetch price for %s", pick.symbol)
            return None

        qty = round(daily_pnl / entry_price, 6)
        pos_id = save_position(
            bot=bot_type,
            bucket="long_term",
            symbol=pick.symbol,
            amount_invested=daily_pnl,
            entry_price=entry_price,
            quantity=qty,
            target_pct=pick.target_pct,
            ai_reason=pick.reason,
            max_hold_days=pick.max_hold_days,
        )
        if pos_id:
            log_extraction(bot_type, daily_pnl, daily_pnl)
            logging.info(
                "Harvest: extracted $%.2f → %s @ $%.2f (id=%d)",
                daily_pnl, pick.symbol, entry_price, pos_id,
            )

        return daily_pnl

    # ------------------------------------------------------------------
    # Daily monitor — call once per day (or on bot start)
    # ------------------------------------------------------------------

    def monitor(
        self,
        bot_type: str,
        watchlist: list[str],
        market_regime: str = "neutral",
        on_base_increase: Optional[callable] = None,
    ) -> None:
        """Check all open harvest positions, close targets, reinvest profits."""
        positions = get_open_positions(bot=bot_type)
        if not positions:
            return

        now = datetime.now(timezone.utc)

        for pos in positions:
            symbol = pos["symbol"]
            current_price = self._get_price(symbol)
            if current_price <= 0:
                continue

            entry = pos["entry_price"]
            qty = pos["quantity"]
            invested = pos["amount_invested"]
            pnl_pct = (current_price - entry) / entry * 100
            target = pos["target_pct"]
            bucket = pos["bucket"]

            # Update current price in DB
            update_price(pos["id"], current_price, pnl_pct)

            # Check expiry
            created = datetime.fromisoformat(pos["created_at"].replace("Z", "+00:00"))
            hold_days = (now - created).days
            max_days = pos.get("max_hold_days", 90)

            if hold_days >= max_days:
                current_value = current_price * qty
                pnl = current_value - invested
                close_position(pos["id"], current_price, pnl, "expired")
                self._handle_close(
                    bucket=bucket, invested=invested, pnl=pnl,
                    bot_type=bot_type, watchlist=watchlist,
                    market_regime=market_regime,
                    on_base_increase=on_base_increase,
                    reason="expired",
                )
                continue

            # Check target hit
            if pnl_pct >= target:
                current_value = current_price * qty
                pnl = current_value - invested
                close_position(pos["id"], current_price, pnl, "target_hit")
                logging.info(
                    "Harvest target hit! [%s/%s %s] pnl=+%.1f%% ($%.2f)",
                    bot_type, bucket, symbol, pnl_pct, pnl,
                )
                self._handle_close(
                    bucket=bucket, invested=invested, pnl=pnl,
                    bot_type=bot_type, watchlist=watchlist,
                    market_regime=market_regime,
                    on_base_increase=on_base_increase,
                    reason="target_hit",
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_close(
        self,
        bucket: str,
        invested: float,
        pnl: float,
        bot_type: str,
        watchlist: list[str],
        market_regime: str,
        on_base_increase: Optional[callable],
        reason: str,
    ) -> None:
        """Split proceeds according to the 3-bucket rules."""
        if pnl <= 0:
            # Loss or breakeven — recycle capital into next long-term pick
            self._open_next("long_term", invested, bot_type, watchlist, market_regime)
            return

        if bucket == "long_term":
            # Capital → next long-term position
            self._open_next("long_term", invested, bot_type, watchlist, market_regime)
            # Profit: 50% → active trading base, 50% → compound bucket
            base_bonus = round(pnl * 0.5, 2)
            compound_amount = round(pnl * 0.5, 2)
            self._base_trading_bonus += base_bonus
            if on_base_increase:
                on_base_increase(base_bonus)
            logging.info(
                "Harvest split: +$%.2f to base, $%.2f to compound", base_bonus, compound_amount
            )
            if compound_amount >= 10:
                self._open_next("compound", compound_amount, bot_type, watchlist, market_regime)

        elif bucket == "compound":
            # Full proceeds: 50% → active trading base, 50% → long-term
            total = invested + pnl
            base_bonus = round(total * 0.5, 2)
            lt_amount = round(total * 0.5, 2)
            self._base_trading_bonus += base_bonus
            if on_base_increase:
                on_base_increase(base_bonus)
            logging.info(
                "Harvest compound closed: +$%.2f to base, $%.2f to long-term", base_bonus, lt_amount
            )
            if lt_amount >= 10:
                self._open_next("long_term", lt_amount, bot_type, watchlist, market_regime)

    def _open_next(
        self,
        bucket: str,
        amount: float,
        bot_type: str,
        watchlist: list[str],
        market_regime: str,
    ) -> None:
        candidates = _get_candidates(bot_type, watchlist)
        if bucket == "long_term":
            pick = pick_long_term(self._ai_key, candidates, market_regime, bot_type, amount, self._model)
            target_pct = self._long_term_target
            max_days = 90
        else:
            pick = pick_compound(self._ai_key, candidates, market_regime, bot_type, amount, self._model)
            target_pct = self._compound_target
            max_days = 30

        if pick is None:
            logging.info("Harvest: no candidate for %s — $%.2f held in base", bucket, amount)
            self._base_trading_bonus += amount
            return

        entry_price = self._get_price(pick.symbol)
        if entry_price <= 0:
            return

        qty = round(amount / entry_price, 6)
        save_position(
            bot=bot_type, bucket=bucket, symbol=pick.symbol,
            amount_invested=amount, entry_price=entry_price, quantity=qty,
            target_pct=target_pct, ai_reason=pick.reason, max_hold_days=max_days,
        )

    def _get_price(self, symbol: str) -> float:
        """Fetch latest price via Alpaca data API."""
        try:
            import requests
            # Alpaca supports both stock and crypto symbols
            is_crypto = "/" in symbol
            if is_crypto:
                url = "https://data.alpaca.markets/v1beta3/crypto/us/latest/bars"
                params = {"symbols": symbol}
                headers = {
                    "APCA-API-KEY-ID": self._alp_key,
                    "APCA-API-SECRET-KEY": self._alp_secret,
                }
                resp = requests.get(url, params=params, headers=headers, timeout=8)
                resp.raise_for_status()
                bars = resp.json().get("bars", {})
                bar = bars.get(symbol) or bars.get(symbol.replace("/", ""), {})
                return float(bar.get("c", 0))
            else:
                url = "https://data.alpaca.markets/v2/stocks/bars/latest"
                params = {"symbols": symbol, "feed": "iex"}
                headers = {
                    "APCA-API-KEY-ID": self._alp_key,
                    "APCA-API-SECRET-KEY": self._alp_secret,
                }
                resp = requests.get(url, params=params, headers=headers, timeout=8)
                resp.raise_for_status()
                bars = resp.json().get("bars", {})
                bar = bars.get(symbol, {})
                return float(bar.get("c", 0))
        except Exception as exc:
            logging.warning("Harvest price fetch failed [%s]: %s", symbol, exc)
            return 0.0

    @property
    def pending_base_bonus(self) -> float:
        """Accumulated base trading bonus not yet applied."""
        return self._base_trading_bonus

    def clear_base_bonus(self) -> float:
        """Return and clear accumulated base bonus."""
        bonus = self._base_trading_bonus
        self._base_trading_bonus = 0.0
        return bonus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_candidates(bot_type: str, watchlist: list[str]) -> list[str]:
    """Return candidate symbols for the picker."""
    if bot_type == "crypto":
        return ["BTC/USD", "ETH/USD"]
    # For day bot: use current watchlist, fallback to well-known large caps
    if watchlist:
        return watchlist[:10]
    return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD"]
