"""Supabase persistence for evening sub-agent analysis results.

Table (create once in Supabase SQL editor):

  CREATE TABLE IF NOT EXISTS evening_watchlist (
      id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
      trade_date    DATE NOT NULL UNIQUE,
      regime        TEXT,
      approved      TEXT[] NOT NULL DEFAULT '{}',
      skip          TEXT[] DEFAULT '{}',
      entry_zones   JSONB DEFAULT '{}',
      risk_flags    JSONB DEFAULT '{}',
      notes         JSONB DEFAULT '{}',
      raw_result    JSONB,
      created_at    TIMESTAMPTZ DEFAULT NOW()
  );
"""
from __future__ import annotations

import logging
import os
from typing import Optional

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception as exc:
        logging.warning("Evening DB Supabase init failed: %s", exc)
        return None


def save_evening_analysis(trade_date: str, result: dict) -> None:
    """Upsert evening analysis result for a given trade date."""
    client = _get_client()
    if client is None:
        return
    try:
        import json
        client.table("evening_watchlist").upsert({
            "trade_date": trade_date,
            "regime": result.get("regime", "unknown"),
            "approved": result.get("approved", []),
            "skip": result.get("skip", []),
            "entry_zones": result.get("entry_zones", {}),
            "risk_flags": result.get("risk_flags", {}),
            "notes": result.get("notes", {}),
            "raw_result": result,
        }).execute()
        logging.info("Evening analysis saved to Supabase for %s", trade_date)
    except Exception as exc:
        logging.warning("Evening DB save failed: %s", exc)


def load_evening_analysis(trade_date: str) -> Optional[dict]:
    """Load evening analysis for a given trade date. Returns None if not found."""
    client = _get_client()
    if client is None:
        return None
    try:
        res = client.table("evening_watchlist").select("*").eq("trade_date", trade_date).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as exc:
        logging.warning("Evening DB load failed: %s", exc)
        return None
