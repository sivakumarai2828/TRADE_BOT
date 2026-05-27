"""Supabase persistence for the options bot.

Tables (create once in Supabase SQL editor):

  CREATE TABLE optionsbot_trades (
    id               BIGSERIAL PRIMARY KEY,
    symbol           TEXT NOT NULL,
    contract_symbol  TEXT NOT NULL,
    option_type      TEXT NOT NULL,
    strike           FLOAT NOT NULL,
    expiry           DATE NOT NULL,
    qty              INT NOT NULL,
    entry_premium    FLOAT NOT NULL,
    exit_premium     FLOAT NOT NULL,
    pnl              FLOAT NOT NULL,
    pnl_pct          FLOAT NOT NULL,
    exit_reason      TEXT,
    tp1_hit          BOOLEAN DEFAULT FALSE,
    trade_date       DATE NOT NULL,
    exit_time        TIMESTAMPTZ DEFAULT now()
  );

  CREATE TABLE optionsbot_symbol_stats (
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
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

try:
    from supabase import create_client, Client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False

_client: Optional[object] = None


def _get_client():
    global _client
    if not _SUPABASE_AVAILABLE:
        return None
    if _client is not None:
        return _client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        _client = create_client(url, key)
    except Exception as exc:
        logging.warning("Supabase options client init failed: %s", exc)
    return _client


def save_trade(
    symbol: str,
    contract_symbol: str,
    option_type: str,
    strike: float,
    expiry: str,
    qty: int,
    entry_premium: float,
    exit_premium: float,
    pnl: float,
    pnl_pct: float,
    exit_reason: str,
    tp1_hit: bool = False,
) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.table("optionsbot_trades").insert({
            "symbol":          symbol,
            "contract_symbol": contract_symbol,
            "option_type":     option_type,
            "strike":          round(strike, 2),
            "expiry":          expiry,
            "qty":             qty,
            "entry_premium":   round(entry_premium, 4),
            "exit_premium":    round(exit_premium, 4),
            "pnl":             round(pnl, 2),
            "pnl_pct":         round(pnl_pct, 2),
            "exit_reason":     exit_reason,
            "tp1_hit":         tp1_hit,
            "trade_date":      date.today().isoformat(),
        }).execute()
        _update_symbol_stats(client, symbol, pnl)
        logging.info("Saved options trade [%s %s] pnl=%+.2f", symbol, option_type, pnl)
    except Exception as exc:
        logging.warning("Supabase options save_trade failed [%s]: %s", contract_symbol, exc)


def _update_symbol_stats(client, symbol: str, pnl: float) -> None:
    try:
        res = client.table("optionsbot_symbol_stats").select("*").eq("symbol", symbol).execute()
        row = res.data[0] if res.data else None
        if row:
            total = row["total_trades"] + 1
            wins = row["wins"] + (1 if pnl > 0 else 0)
            losses = row["losses"] + (1 if pnl <= 0 else 0)
            total_pnl = round(row["total_pnl"] + pnl, 2)
            client.table("optionsbot_symbol_stats").update({
                "total_trades":     total,
                "wins":             wins,
                "losses":           losses,
                "win_rate":         round(wins / total * 100, 1),
                "avg_pnl":          round(total_pnl / total, 2),
                "total_pnl":        total_pnl,
                "last_trade_date":  date.today().isoformat(),
                "last_updated":     datetime.now(timezone.utc).isoformat(),
            }).eq("symbol", symbol).execute()
        else:
            client.table("optionsbot_symbol_stats").insert({
                "symbol":           symbol,
                "total_trades":     1,
                "wins":             1 if pnl > 0 else 0,
                "losses":           1 if pnl <= 0 else 0,
                "win_rate":         100.0 if pnl > 0 else 0.0,
                "avg_pnl":          round(pnl, 2),
                "total_pnl":        round(pnl, 2),
                "last_trade_date":  date.today().isoformat(),
                "last_updated":     datetime.now(timezone.utc).isoformat(),
            }).execute()
    except Exception as exc:
        logging.warning("Options symbol stats update failed [%s]: %s", symbol, exc)
