"""Journal backend factory: Supabase if configured, else local SQLite."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("botv2.storage")


def make_journal(db_path: str):
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if url and key:
        from .journal_supabase import SupabaseJournal
        log.info("journal backend: Supabase (%s)", url)
        return SupabaseJournal(url, key)
    from .journal import Journal
    log.info("journal backend: SQLite (%s)", db_path)
    return Journal(db_path)
