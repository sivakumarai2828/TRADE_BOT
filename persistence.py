"""Supabase persistence layer for bot state, trades, and logs."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import Optional

from supabase import create_client, Client

_client: Optional[Client] = None


def _get_client() -> Optional[Client]:
    global _client
    if _client is not None:
        return _client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        _client = create_client(url, key)
    except Exception as exc:
        logging.warning("Supabase client init failed: %s", exc)
    return _client


def save_state(metrics, settings) -> None:
    """Upsert full bot state snapshot to Supabase."""
    client = _get_client()
    if client is None:
        return
    try:
        data = {"metrics": asdict(metrics), "settings": asdict(settings)}
        client.table("bot_state").upsert({"key": "main", "data": data}).execute()
    except Exception as exc:
        logging.warning("Supabase save_state failed: %s", exc)


def load_state() -> Optional[dict]:
    """Load the last saved bot state from Supabase. Returns dict or None."""
    client = _get_client()
    if client is None:
        return None
    try:
        result = client.table("bot_state").select("data").eq("key", "main").execute()
        if result.data:
            return result.data[0]["data"]
    except Exception as exc:
        logging.warning("Supabase load_state failed: %s", exc)
    return None


def save_trade(symbol: str, side: str, amount: float, entry_price: float,
               exit_price: float, pnl: float, pnl_pct: float,
               reason: str, is_house_trade: bool = False) -> None:
    """Insert a completed trade record."""
    client = _get_client()
    if client is None:
        return
    try:
        client.table("trade_history").insert({
            "symbol": symbol, "side": side, "amount": amount,
            "entry_price": entry_price, "exit_price": exit_price,
            "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
            "is_house_trade": is_house_trade,
        }).execute()
    except Exception as exc:
        logging.warning("Supabase save_trade failed: %s", exc)


def save_log(time: str, log_type: str, message: str, tone: str) -> None:
    """Insert a log entry."""
    client = _get_client()
    if client is None:
        return
    try:
        client.table("bot_logs").insert({
            "time": time, "type": log_type, "message": message, "tone": tone,
        }).execute()
    except Exception as exc:
        logging.warning("Supabase save_log failed: %s", exc)
