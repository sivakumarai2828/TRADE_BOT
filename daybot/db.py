"""Supabase persistence for the day trading bot.

Tables (create once in Supabase SQL editor):

  CREATE TABLE daybot_trades (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT NOT NULL,
    entry_price  FLOAT NOT NULL,
    exit_price   FLOAT NOT NULL,
    qty          INT NOT NULL,
    pnl          FLOAT NOT NULL,
    pnl_pct      FLOAT NOT NULL,
    exit_reason  TEXT,
    ai_confidence FLOAT,
    ai_reason    TEXT,
    trade_date   DATE NOT NULL,
    exit_time    TIMESTAMPTZ DEFAULT now(),
    weekly_context JSONB
  );

  CREATE TABLE daybot_symbol_stats (
    symbol       TEXT PRIMARY KEY,
    total_trades INT DEFAULT 0,
    wins         INT DEFAULT 0,
    losses       INT DEFAULT 0,
    win_rate     FLOAT DEFAULT 0,
    avg_pnl      FLOAT DEFAULT 0,
    total_pnl    FLOAT DEFAULT 0,
    last_trade_date DATE,
    last_updated TIMESTAMPTZ DEFAULT now()
  );

  CREATE TABLE daybot_market_sessions (
    trade_date    DATE PRIMARY KEY,
    spy_return_pct FLOAT,
    market_regime TEXT,
    total_trades  INT DEFAULT 0,
    wins          INT DEFAULT 0,
    losses        INT DEFAULT 0,
    daily_pnl     FLOAT DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT now()
  );
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
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
        logging.warning("Supabase daybot client init failed: %s", exc)
    return _client


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def save_trade(
    symbol: str,
    entry_price: float,
    exit_price: float,
    qty: int,
    pnl: float,
    pnl_pct: float,
    exit_reason: str,
    ai_confidence: float = 0.0,
    ai_reason: str = "",
    weekly_context: dict | None = None,
) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.table("daybot_trades").insert({
            "symbol": symbol,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "qty": qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": exit_reason,
            "ai_confidence": round(ai_confidence, 4),
            "ai_reason": ai_reason[:300] if ai_reason else "",
            "trade_date": date.today().isoformat(),
            "weekly_context": weekly_context or {},
        }).execute()
        _update_symbol_stats(client, symbol, pnl)
        logging.info("Saved daybot trade [%s] pnl=%+.2f", symbol, pnl)
    except Exception as exc:
        logging.warning("Supabase save_trade failed [%s]: %s", symbol, exc)


def _update_symbol_stats(client: Client, symbol: str, pnl: float) -> None:
    try:
        res = client.table("daybot_symbol_stats").select("*").eq("symbol", symbol).execute()
        row = res.data[0] if res.data else None
        if row:
            total = row["total_trades"] + 1
            wins = row["wins"] + (1 if pnl > 0 else 0)
            losses = row["losses"] + (1 if pnl <= 0 else 0)
            total_pnl = round(row["total_pnl"] + pnl, 2)
            client.table("daybot_symbol_stats").update({
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total * 100, 1),
                "avg_pnl": round(total_pnl / total, 2),
                "total_pnl": total_pnl,
                "last_trade_date": date.today().isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }).eq("symbol", symbol).execute()
        else:
            client.table("daybot_symbol_stats").insert({
                "symbol": symbol,
                "total_trades": 1,
                "wins": 1 if pnl > 0 else 0,
                "losses": 1 if pnl <= 0 else 0,
                "win_rate": 100.0 if pnl > 0 else 0.0,
                "avg_pnl": round(pnl, 2),
                "total_pnl": round(pnl, 2),
                "last_trade_date": date.today().isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception as exc:
        logging.warning("Symbol stats update failed [%s]: %s", symbol, exc)


def upsert_market_session(
    spy_return_pct: float,
    market_regime: str,
    total_trades: int,
    wins: int,
    losses: int,
    daily_pnl: float,
) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.table("daybot_market_sessions").upsert({
            "trade_date": date.today().isoformat(),
            "spy_return_pct": round(spy_return_pct, 3),
            "market_regime": market_regime,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "daily_pnl": round(daily_pnl, 2),
        }).execute()
    except Exception as exc:
        logging.warning("Supabase upsert_market_session failed: %s", exc)


# ---------------------------------------------------------------------------
# Read helpers — called before AI validation
# ---------------------------------------------------------------------------

def get_symbol_stats(symbol: str) -> dict | None:
    """Return aggregated win rate / PnL for a symbol, or None."""
    client = _get_client()
    if client is None:
        return None
    try:
        res = client.table("daybot_symbol_stats").select("*").eq("symbol", symbol).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        logging.warning("get_symbol_stats failed [%s]: %s", symbol, exc)
        return None


def get_recent_trades(symbol: str, limit: int = 5) -> list[dict]:
    """Return last N closed trades for a symbol (most recent first)."""
    client = _get_client()
    if client is None:
        return []
    try:
        res = (
            client.table("daybot_trades")
            .select("pnl,pnl_pct,exit_reason,ai_confidence,trade_date")
            .eq("symbol", symbol)
            .order("exit_time", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logging.warning("get_recent_trades failed [%s]: %s", symbol, exc)
        return []


def get_recent_market_sessions(limit: int = 5) -> list[dict]:
    """Return recent market sessions (most recent first)."""
    client = _get_client()
    if client is None:
        return []
    try:
        res = (
            client.table("daybot_market_sessions")
            .select("trade_date,spy_return_pct,market_regime,wins,losses,daily_pnl")
            .order("trade_date", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logging.warning("get_recent_market_sessions failed: %s", exc)
        return []


def build_ai_history_context(symbol: str) -> dict:
    """Build the historical context dict passed to Claude."""
    stats = get_symbol_stats(symbol)
    recent_trades = get_recent_trades(symbol, limit=5)
    market_sessions = get_recent_market_sessions(limit=3)

    return {
        "symbol_stats": stats,
        "recent_trades": recent_trades,
        "market_sessions": market_sessions,
    }
