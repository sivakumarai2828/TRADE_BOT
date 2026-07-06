"""SQLite journal — the bot's memory.

Stores every AI decision, every executed trade with its thesis, and the AI's
own periodic self-review memos. All of it is fed back into future prompts so
the portfolio manager learns from its own record.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts REAL, market TEXT, raw_json TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    ts_open REAL, ts_close REAL,
    market TEXT, symbol TEXT, side TEXT,
    qty REAL, entry REAL, exit_price REAL,
    stop REAL, target REAL,
    pnl REAL, pnl_pct REAL,
    thesis TEXT, exit_reason TEXT,
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY,
    ts REAL, market TEXT, kind TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY, v TEXT
);
"""


class Journal:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── decisions ───────────────────────────────────────────────
    def log_decision(self, market: str, decision: dict, note: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO decisions (ts, market, raw_json, note) VALUES (?,?,?,?)",
                (time.time(), market, json.dumps(decision), note),
            )

    def recent_decisions(self, market: str, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, raw_json FROM decisions WHERE market=? ORDER BY ts DESC LIMIT ?",
                (market, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def all_trades(self, market: str, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts_open, ts_close, symbol, qty, entry, exit_price, stop, target,"
                " pnl, pnl_pct, thesis, exit_reason, status"
                " FROM trades WHERE market=? ORDER BY ts_open DESC LIMIT ?",
                (market, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── trades ──────────────────────────────────────────────────
    def open_trade(self, market: str, symbol: str, qty: float, entry: float,
                   stop: float, target: float, thesis: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO trades (ts_open, market, symbol, side, qty, entry, stop, target, thesis)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), market, symbol, "long", qty, entry, stop, target, thesis),
            )
            return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, reason: str) -> bool:
        """Close a trade. Returns False if already closed (double-close guard)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT qty, entry, status FROM trades WHERE id=?", (trade_id,)
            ).fetchone()
            if not row or row["status"] != "open":
                return False
            pnl = (exit_price - row["entry"]) * row["qty"]
            pnl_pct = (exit_price / row["entry"] - 1) * 100 if row["entry"] else 0
            c.execute(
                "UPDATE trades SET ts_close=?, exit_price=?, pnl=?, pnl_pct=?,"
                " exit_reason=?, status='closed' WHERE id=? AND status='open'",
                (time.time(), exit_price, pnl, pnl_pct, reason, trade_id),
            )
            return True

    def update_levels(self, trade_id: int, stop: float | None, target: float | None) -> None:
        with self._conn() as c:
            if stop is not None:
                c.execute("UPDATE trades SET stop=? WHERE id=?", (stop, trade_id))
            if target is not None:
                c.execute("UPDATE trades SET target=? WHERE id=?", (target, trade_id))

    def open_trades(self, market: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE market=? AND status='open'", (market,)
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_closed(self, market: str, limit: int = 15) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE market=? AND status='closed'"
                " ORDER BY ts_close DESC LIMIT ?", (market, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self, market: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) n, SUM(pnl) total,"
                " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins,"
                " AVG(CASE WHEN pnl > 0 THEN pnl END) avg_win,"
                " AVG(CASE WHEN pnl <= 0 THEN pnl END) avg_loss"
                " FROM trades WHERE market=? AND status='closed'", (market,)
            ).fetchone()
            n = row["n"] or 0
            return {
                "closed_trades": n,
                "total_pnl": round(row["total"] or 0, 2),
                "win_rate_pct": round(100 * (row["wins"] or 0) / n, 1) if n else None,
                "avg_win": round(row["avg_win"], 2) if row["avg_win"] else None,
                "avg_loss": round(row["avg_loss"], 2) if row["avg_loss"] else None,
            }

    def entries_today(self, market: str) -> int:
        midnight = time.time() - (time.time() % 86400)
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) n FROM trades WHERE market=? AND ts_open>=?",
                (market, midnight),
            ).fetchone()
            return row["n"] or 0

    # ── memos (AI self-review) ──────────────────────────────────
    def save_memo(self, market: str, kind: str, body: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO memos (ts, market, kind, body) VALUES (?,?,?,?)",
                (time.time(), market, kind, body),
            )

    def latest_memo(self, market: str, kind: str = "self_review") -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT body FROM memos WHERE market=? AND kind=? ORDER BY ts DESC LIMIT 1",
                (market, kind),
            ).fetchone()
            return row["body"] if row else None

    # ── key/value (equity peaks, halts) ─────────────────────────
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kv (k, v) VALUES (?,?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value)
            )
