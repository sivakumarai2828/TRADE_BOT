"""Control plane between the dashboard and the trading bot.

The dashboard API and the bot are separate processes (a crash in one must
never take down the other), so they never call each other. They share the
journal's kv store instead:

  FLAGS     ctl_<MARKET>_paused, ctl_skeptic, ctl_approval
            Written by the API, read by the bot on every loop pass.
  QUEUE     ctl_queue — commands the API enqueues and the bot executes
            (run a cycle, close a position, flatten, approve/reject).
            Results land in ctl_results so the dashboard can show them.
  STATUS    bot_status — what the bot process is doing, written each loop.

Pause means "no NEW entries": the AI decision cycle and WATCH triggers stop,
but stops, targets and the kill switch keep running. An approval you tap
yourself still goes through while paused.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("botv2.control")

K_QUEUE, K_RESULTS, K_STATUS = "ctl_queue", "ctl_results", "bot_status"
COMMANDS = {"run_cycle", "close", "flatten", "approve", "reject", "monitor", "reset_kill"}
MARKET_CMDS = {"run_cycle", "close", "flatten", "monitor", "reset_kill"}


def _load(journal, key: str, default):
    try:
        v = json.loads(journal.kv_get(key) or "null")
        return default if v is None else v
    except ValueError:
        return default


# ── flags ────────────────────────────────────────────────────────
def is_paused(journal, market: str) -> bool:
    return journal.kv_get(f"ctl_{market}_paused") == "1"


def set_paused(journal, market: str, paused: bool) -> None:
    journal.kv_set(f"ctl_{market}_paused", "1" if paused else "0")


def gate_on(journal, gate: str, default: bool) -> bool:
    """gate is 'skeptic' or 'approval'. Unset = the .env default."""
    v = journal.kv_get(f"ctl_{gate}")
    return default if v not in ("on", "off") else v == "on"


def set_gate(journal, gate: str, on: bool) -> None:
    journal.kv_set(f"ctl_{gate}", "on" if on else "off")


# ── queue ────────────────────────────────────────────────────────
def enqueue(journal, cmd: str, market: str | None = None, arg: str | None = None,
            source: str = "dashboard") -> dict:
    if cmd not in COMMANDS:
        raise ValueError(f"unknown command {cmd!r}")
    if cmd in MARKET_CMDS and market not in ("US", "INDIA"):
        raise ValueError(f"{cmd} needs market US or INDIA")
    if cmd in ("close", "approve", "reject") and not arg:
        raise ValueError(f"{cmd} needs an argument")
    item = {"id": secrets.token_hex(3), "ts": time.time(), "cmd": cmd,
            "market": market, "arg": arg, "source": source}
    q = _load(journal, K_QUEUE, [])
    q.append(item)
    journal.kv_set(K_QUEUE, json.dumps(q[-50:]))
    return item


def results(journal) -> list[dict]:
    return _load(journal, K_RESULTS, [])


def _record(journal, item: dict, ok: bool, note: str) -> None:
    res = results(journal)
    res.append({**item, "done": time.time(), "ok": ok, "note": str(note)[:240]})
    journal.kv_set(K_RESULTS, json.dumps(res[-30:]))


def drain(journal, runners: dict, desk=None) -> int:
    """Run queued commands inside the bot process. Called every loop pass."""
    q = _load(journal, K_QUEUE, [])
    if not q:
        return 0
    journal.kv_set(K_QUEUE, "[]")       # take ownership before running anything
    for item in q:
        try:
            ok, note = _run(item, runners, desk)
        except Exception as exc:
            log.exception("control command %s failed", item)
            ok, note = False, f"{type(exc).__name__}: {exc}"
        _record(journal, item, ok, note)
        log.info("control %s %s %s -> %s %s", item["cmd"], item.get("market"),
                 item.get("arg"), "ok" if ok else "failed", note)
    return len(q)


def _run(item: dict, runners: dict, desk) -> tuple[bool, str]:
    cmd, market, arg = item["cmd"], item.get("market"), item.get("arg")
    if cmd in ("approve", "reject"):
        if desk is None:
            return False, "approval desk is not enabled on the bot"
        msg = desk.decide(arg, approve=(cmd == "approve"), source="dashboard")
        return not msg.startswith(("No request", "Not filled")), msg
    r = runners.get(market)
    if r is None:
        return False, f"{market} bot is not running"
    if cmd == "run_cycle":
        if is_paused(r.journal, market):
            return False, f"{market} is paused — resume it first"
        r.run_cycle()
        mark(r.journal, market, "cycle")
        return True, "decision cycle completed"
    if cmd == "reset_kill":
        return reset_kill_switch(r)
    if cmd == "monitor":
        r.monitor_positions()
        return True, "stops/targets checked"
    if cmd == "close":
        pos = next((p for p in r.ledger.positions() if p["symbol"] == arg), None)
        if pos is None:
            return False, f"{arg} is not an open {market} position"
        r._exit(pos, "manual_close_dashboard")
        return True, f"closed {arg}"
    if cmd == "flatten":
        n = len(r.ledger.positions())
        for pos in r.ledger.positions():
            r._exit(pos, "manual_flatten_dashboard")
        return True, f"closed {n} position(s)"
    return False, "unhandled"


def reset_kill_switch(r) -> tuple[bool, str]:
    """Manual reset after a kill switch. The equity peak is reset to today's
    equity too: otherwise a book still ~10% under its old peak would trip
    again on the very next check. The 10% limit is measured from here."""
    m = r.market
    was = r.journal.kv_get(f"{m}_killed") == "1"
    equity = r.ledger.equity()
    old_peak = r.journal.kv_get(f"{m}_equity_peak")
    r.journal.kv_set(f"{m}_killed", "0")
    r.journal.kv_set(f"{m}_equity_peak", str(equity))
    from . import notify
    notify.alert(f"♻️ [{m}] Kill switch reset from dashboard. Equity {equity:,.2f} "
                 f"(old peak {old_peak}) is the new peak; next halt at "
                 f"{equity * (1 - r.risk.caps.max_drawdown_kill_pct):,.2f}.")
    return True, (f"{'reset' if was else 'was not tripped; peak reset'} — new peak "
                  f"{equity:,.2f}, halts again below "
                  f"{equity * (1 - r.risk.caps.max_drawdown_kill_pct):,.2f}")


# ── applying flags to live runners ───────────────────────────────
def apply_flags(journal, runners: dict, skeptic, desk, cfg) -> None:
    sk_on = skeptic is not None and gate_on(journal, "skeptic", cfg.skeptic_enabled)
    ap_on = desk is not None and gate_on(journal, "approval", cfg.approval_mode == "telegram")
    for m, r in runners.items():
        r.skeptic = skeptic if sk_on else None
        r.approvals = desk if ap_on else None
        r.entries_paused = is_paused(journal, m)


# ── status the dashboard reads ───────────────────────────────────
def mark(journal, market: str, what: str) -> None:
    """Record when a cycle or monitor last ran for a market."""
    st = _load(journal, f"{market}_runs", {})
    st[what] = time.time()
    journal.kv_set(f"{market}_runs", json.dumps(st))


def write_status(journal, runners: dict, started: float, schedule: dict) -> None:
    now = datetime.now(timezone.utc)
    markets = {}
    for m, (tz, decide_at, open_at, close_at) in schedule.items():
        if m not in runners:
            continue
        local = now.astimezone(tz)
        nxt = local.replace(hour=decide_at[0], minute=decide_at[1], second=0, microsecond=0)
        while nxt <= local or nxt.weekday() >= 5:
            nxt = (nxt + timedelta(days=1)).replace(hour=decide_at[0], minute=decide_at[1])
        hm = (local.hour, local.minute)
        markets[m] = {
            "session_open": local.weekday() < 5 and open_at <= hm <= close_at,
            "next_cycle": nxt.timestamp(),
            "tz": str(tz),
        }
    journal.kv_set(K_STATUS, json.dumps({
        "version": "v3", "pid": os.getpid(), "started": started,
        "updated": time.time(), "markets": markets,
    }))


def snapshot(journal, cfg_defaults: dict) -> dict:
    """Everything the control page needs that /api/summary doesn't have."""
    status = _load(journal, K_STATUS, {})
    hb = float(journal.kv_get("heartbeat", "0") or 0)
    out = {
        "bot_alive": (time.time() - hb) < 120,
        "heartbeat_age_s": round(time.time() - hb) if hb else None,
        "status": status,
        "gates": {g: gate_on(journal, g, cfg_defaults[g]) for g in ("skeptic", "approval")},
        "queue": _load(journal, K_QUEUE, []),
        "results": list(reversed(results(journal)))[:15],
        "approvals": [r for r in _load(journal, "approval_requests", [])
                      if r.get("status") == "pending"],
        "approvals_recent": [r for r in reversed(_load(journal, "approval_requests", []))
                             if r.get("status") != "pending"][:10],
        "markets": {},
    }
    for m in ("US", "INDIA"):
        out["markets"][m] = {
            "paused": is_paused(journal, m),
            "killed": journal.kv_get(f"{m}_killed") == "1",
            "runs": _load(journal, f"{m}_runs", {}),
            "skeptic_log": list(reversed(_load(journal, f"{m}_skeptic_log", [])))[:12],
        }
        try:
            journal.expire_watches(m)
            out["markets"][m]["watches"] = journal.active_watches(m)
        except Exception:
            out["markets"][m]["watches"] = []
    return out
