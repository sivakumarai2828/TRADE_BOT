"""Supabase (Postgres) journal backend — same interface as journal.Journal.

Selected automatically when SUPABASE_URL + SUPABASE_KEY (secret key) are set.
Tables are prefixed v2_ to avoid any clash with old V1 tables.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("botv2.journal_supabase")

SCHEMA_SQL = """
create table if not exists v2_trades (
    id bigint generated always as identity primary key,
    ts_open double precision, ts_close double precision,
    market text, symbol text, side text,
    qty double precision, entry double precision, exit_price double precision,
    stop double precision, target double precision,
    pnl double precision, pnl_pct double precision,
    thesis text, exit_reason text,
    status text default 'open'
);
create table if not exists v2_decisions (
    id bigint generated always as identity primary key,
    ts double precision, market text, raw_json text, note text
);
create table if not exists v2_memos (
    id bigint generated always as identity primary key,
    ts double precision, market text, kind text, body text
);
create table if not exists v2_kv (k text primary key, v text);
alter table v2_trades enable row level security;
alter table v2_decisions enable row level security;
alter table v2_memos enable row level security;
alter table v2_kv enable row level security;
"""


class SupabaseJournal:
    def __init__(self, url: str, key: str):
        from supabase import create_client
        self.c = create_client(url, key)

    # ── decisions ───────────────────────────────────────────────
    def log_decision(self, market: str, decision: dict, note: str = "") -> None:
        self.c.table("v2_decisions").insert({
            "ts": time.time(), "market": market,
            "raw_json": json.dumps(decision), "note": note,
        }).execute()

    def recent_decisions(self, market: str, limit: int = 10) -> list[dict]:
        r = self.c.table("v2_decisions").select("ts, raw_json").eq("market", market) \
            .order("ts", desc=True).limit(limit).execute()
        return r.data or []

    # ── trades ──────────────────────────────────────────────────
    def open_trade(self, market, symbol, qty, entry, stop, target, thesis) -> int:
        r = self.c.table("v2_trades").insert({
            "ts_open": time.time(), "market": market, "symbol": symbol,
            "side": "long", "qty": qty, "entry": entry,
            "stop": stop, "target": target, "thesis": thesis,
        }).execute()
        return r.data[0]["id"]

    def close_trade(self, trade_id: int, exit_price: float, reason: str) -> bool:
        row = self.c.table("v2_trades").select("qty, entry, status") \
            .eq("id", trade_id).execute()
        if not row.data or row.data[0]["status"] != "open":
            return False
        qty, entry = row.data[0]["qty"], row.data[0]["entry"]
        pnl = (exit_price - entry) * qty
        pnl_pct = (exit_price / entry - 1) * 100 if entry else 0
        upd = self.c.table("v2_trades").update({
            "ts_close": time.time(), "exit_price": exit_price,
            "pnl": pnl, "pnl_pct": pnl_pct,
            "exit_reason": reason, "status": "closed",
        }).eq("id", trade_id).eq("status", "open").execute()
        return bool(upd.data)

    def update_levels(self, trade_id: int, stop, target) -> None:
        patch = {}
        if stop is not None:
            patch["stop"] = stop
        if target is not None:
            patch["target"] = target
        if patch:
            self.c.table("v2_trades").update(patch).eq("id", trade_id).execute()

    def open_trades(self, market: str) -> list[dict]:
        r = self.c.table("v2_trades").select("*").eq("market", market) \
            .eq("status", "open").execute()
        return r.data or []

    def recent_closed(self, market: str, limit: int = 15) -> list[dict]:
        r = self.c.table("v2_trades").select("*").eq("market", market) \
            .eq("status", "closed").order("ts_close", desc=True).limit(limit).execute()
        return r.data or []

    def all_trades(self, market: str, limit: int = 50) -> list[dict]:
        r = self.c.table("v2_trades").select(
            "ts_open, ts_close, symbol, qty, entry, exit_price, stop, target,"
            " pnl, pnl_pct, thesis, exit_reason, status"
        ).eq("market", market).order("ts_open", desc=True).limit(limit).execute()
        return r.data or []

    def stats(self, market: str) -> dict:
        rows = self.recent_closed(market, 1000)
        n = len(rows)
        if not n:
            return {"closed_trades": 0, "total_pnl": 0,
                    "win_rate_pct": None, "avg_win": None, "avg_loss": None}
        pnls = [t["pnl"] or 0 for t in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "closed_trades": n,
            "total_pnl": round(sum(pnls), 2),
            "win_rate_pct": round(100 * len(wins) / n, 1),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        }

    def entries_today(self, market: str) -> int:
        midnight = time.time() - (time.time() % 86400)
        r = self.c.table("v2_trades").select("id", count="exact") \
            .eq("market", market).gte("ts_open", midnight).execute()
        return r.count or 0

    # ── memos ───────────────────────────────────────────────────
    def save_memo(self, market: str, kind: str, body: str) -> None:
        self.c.table("v2_memos").insert({
            "ts": time.time(), "market": market, "kind": kind, "body": body,
        }).execute()

    def latest_memo(self, market: str, kind: str = "self_review") -> str | None:
        r = self.c.table("v2_memos").select("body").eq("market", market) \
            .eq("kind", kind).order("ts", desc=True).limit(1).execute()
        return r.data[0]["body"] if r.data else None

    # ── kv ──────────────────────────────────────────────────────
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.c.table("v2_kv").select("v").eq("k", key).execute()
        return r.data[0]["v"] if r.data else default

    def kv_set(self, key: str, value: str) -> None:
        self.c.table("v2_kv").upsert({"k": key, "v": value}).execute()
