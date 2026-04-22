"""Supabase persistence for the 3-bucket profit harvesting system.

Run once in Supabase SQL editor to create the table:

  CREATE TABLE harvest_positions (
    id               BIGSERIAL PRIMARY KEY,
    bot              TEXT NOT NULL,          -- 'day' | 'crypto'
    bucket           TEXT NOT NULL,          -- 'long_term' | 'compound'
    symbol           TEXT NOT NULL,
    amount_invested  FLOAT NOT NULL,
    entry_price      FLOAT NOT NULL,
    quantity         FLOAT NOT NULL,
    current_price    FLOAT,
    target_pct       FLOAT NOT NULL,         -- 30.0 long_term, 15.0 compound
    pnl_pct          FLOAT DEFAULT 0,
    status           TEXT DEFAULT 'open',    -- 'open'|'target_hit'|'closed'|'expired'
    ai_reason        TEXT,
    max_hold_days    INT DEFAULT 90,
    created_at       TIMESTAMPTZ DEFAULT now(),
    closed_at        TIMESTAMPTZ,
    exit_price       FLOAT,
    realized_pnl     FLOAT
  );

  CREATE TABLE harvest_log (
    id               BIGSERIAL PRIMARY KEY,
    bot              TEXT NOT NULL,
    extracted_amount FLOAT NOT NULL,
    trigger_pnl      FLOAT NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now()
  );
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client

_client: Optional[Client] = None


def _get() -> Optional[Client]:
    global _client
    if _client:
        return _client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        _client = create_client(url, key)
    except Exception as exc:
        logging.warning("Harvest Supabase init failed: %s", exc)
    return _client


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_position(
    bot: str,
    bucket: str,
    symbol: str,
    amount_invested: float,
    entry_price: float,
    quantity: float,
    target_pct: float,
    ai_reason: str,
    max_hold_days: int = 90,
) -> Optional[int]:
    """Insert a new harvest position. Returns the new row id or None."""
    client = _get()
    if not client:
        return None
    try:
        res = client.table("harvest_positions").insert({
            "bot": bot,
            "bucket": bucket,
            "symbol": symbol,
            "amount_invested": round(amount_invested, 2),
            "entry_price": round(entry_price, 4),
            "quantity": round(quantity, 6),
            "current_price": round(entry_price, 4),
            "target_pct": target_pct,
            "pnl_pct": 0.0,
            "status": "open",
            "ai_reason": ai_reason[:300],
            "max_hold_days": max_hold_days,
        }).execute()
        row_id = res.data[0]["id"] if res.data else None
        logging.info("Harvest position saved [%s/%s %s] id=%s", bot, bucket, symbol, row_id)
        return row_id
    except Exception as exc:
        logging.warning("harvest save_position failed: %s", exc)
        return None


def update_price(position_id: int, current_price: float, pnl_pct: float) -> None:
    client = _get()
    if not client:
        return
    try:
        client.table("harvest_positions").update({
            "current_price": round(current_price, 4),
            "pnl_pct": round(pnl_pct, 2),
        }).eq("id", position_id).execute()
    except Exception as exc:
        logging.warning("harvest update_price failed: %s", exc)


def close_position(
    position_id: int,
    exit_price: float,
    realized_pnl: float,
    status: str,  # 'target_hit' | 'closed' | 'expired'
) -> None:
    client = _get()
    if not client:
        return
    try:
        client.table("harvest_positions").update({
            "status": status,
            "exit_price": round(exit_price, 4),
            "realized_pnl": round(realized_pnl, 2),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", position_id).execute()
        logging.info("Harvest position %d closed — status=%s pnl=%.2f", position_id, status, realized_pnl)
    except Exception as exc:
        logging.warning("harvest close_position failed: %s", exc)


def log_extraction(bot: str, extracted_amount: float, trigger_pnl: float) -> None:
    client = _get()
    if not client:
        return
    try:
        client.table("harvest_log").insert({
            "bot": bot,
            "extracted_amount": round(extracted_amount, 2),
            "trigger_pnl": round(trigger_pnl, 2),
        }).execute()
    except Exception as exc:
        logging.warning("harvest log_extraction failed: %s", exc)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_open_positions(bot: Optional[str] = None) -> list[dict]:
    """Return all open harvest positions, optionally filtered by bot."""
    client = _get()
    if not client:
        return []
    try:
        q = client.table("harvest_positions").select("*").eq("status", "open")
        if bot:
            q = q.eq("bot", bot)
        res = q.order("created_at", desc=False).execute()
        return res.data or []
    except Exception as exc:
        logging.warning("harvest get_open_positions failed: %s", exc)
        return []
