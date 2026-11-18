"""Suggest-only mode: the bot recommends buys for a market but never trades it.

Used for INDIA (INDIA_MODE=suggest) and, if wanted, the US (US_MODE=suggest).
A traded market records the same ranked list for the dashboard (see
MarketRunner._gate_entry) without changing how it trades. The daily AI cycle, the hard risk rules
and the Skeptic all run exactly as for a traded market; the only difference
is the last step. Instead of an approval request or a fill, every entry that
survives is stored as a suggestion, ranked by priority, sent to Telegram as
one daily list and shown on the dashboard. No ledger, no order, no position.

Suggestions live in the journal kv (works on SQLite and Supabase alike).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import notify

KEEP = 120                       # stored suggestions (roughly a month)
TZ = {"INDIA": "Asia/Kolkata", "US": "America/New_York"}


def _key(market: str) -> str:
    return f"{market}_suggestions"


def today(market: str) -> str:
    return datetime.now(ZoneInfo(TZ.get(market, "UTC"))).strftime("%Y-%m-%d")


def day_of(ts: float, market: str) -> str:
    """The market-local date of a timestamp."""
    return datetime.fromtimestamp(float(ts), ZoneInfo(TZ.get(market, "UTC"))).strftime("%Y-%m-%d")


def load(journal, market: str) -> list[dict]:
    try:
        return json.loads(journal.kv_get(_key(market)) or "[]")
    except (TypeError, ValueError):
        return []


def priority(confidence, r: float | None) -> tuple[str, float]:
    """(label, score). The AI's own confidence (0-1) ranks the list; when it
    gave none, the reward/risk multiple stands in. Higher score = buy first."""
    try:
        conf = float(confidence)
        conf = conf if 0 <= conf <= 1 else None
    except (TypeError, ValueError):
        conf = None
    score = conf if conf is not None else min(0.9, 0.2 * (r or 0))
    return ("HIGH" if score >= 0.7 else "MEDIUM" if score >= 0.5 else "LOW"), round(score, 3)


def record(journal, market: str, currency: str, proposal: dict, caps,
           status: str = "suggest", **extra) -> dict | None:
    """Store one suggestion. Returns it, or None if this symbol was already
    suggested today (a re-run cycle or a watch trigger must not repeat it).

    status says what the bot does with it: "suggest" (nothing: suggest-only
    market), "approval" (sent to Telegram, bought only if approved),
    "bought" (approvals are off, the bot filled it) or "blocked" (the AI
    proposed it but a check stopped it; see record_blocked). A real
    suggestion replaces a blocked one for the same symbol and day; a blocked
    one never replaces anything."""
    items, day, sym = load(journal, market), today(market), proposal["symbol"]
    same = [s for s in items if s["day"] == day and s["symbol"] == sym]
    if same and (status == "blocked" or any(s.get("status") != "blocked" for s in same)):
        return None
    items = [s for s in items if not (s["day"] == day and s["symbol"] == sym)]
    price, stop = float(proposal["price"]), float(proposal["stop"])
    target = float(proposal["target"]) if proposal.get("target") else None
    r = round((target - price) / (price - stop), 2) if target and price > stop else None
    label, score = priority(proposal.get("confidence"), r)
    stop_dist = (price - stop) / price if price else 0
    # Share of the user's own capital that keeps the loss at the stop inside
    # the per-trade risk cap, never above the single-position cap.
    size_pct = min(caps.max_position_pct, caps.max_risk_per_trade_pct / stop_dist) if stop_dist > 0 else 0
    s = {
        "id": f"{day}-{sym}", "ts": time.time(), "day": day, "market": market,
        "currency": currency, "symbol": sym,
        "kind": proposal.get("kind", "BUY"), "setup": proposal.get("setup") or "",
        "price": round(price, 2), "stop": round(stop, 2),
        "target": round(target, 2) if target else None, "r": r,
        "priority": label, "score": score, "status": status,
        "size_pct": round(100 * size_pct, 1),
        "risk_pct": round(100 * size_pct * stop_dist, 2),
        "thesis": str(proposal.get("thesis", ""))[:400],
        "skeptic": str(proposal.get("skeptic", ""))[:300],
        **extra,
    }
    items.append(s)
    journal.kv_set(_key(market), json.dumps(items[-KEEP_TRACKED:]))
    return s


def record_blocked(journal, market: str, currency: str, proposal: dict, caps,
                   by: str, reason: str, ruling: str = "") -> dict | None:
    """Keep an idea the AI proposed but a check stopped, with the reason, so
    the pages can show what was considered. by = "skeptic" (the second AI
    check) or "risk" (the code's risk rules). Never sent as a suggestion."""
    return record(journal, market, currency, proposal, caps, status="blocked",
                  blocked_by=by, reason=str(reason)[:400], ruling=ruling)


def ranked(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda s: (-s.get("score", 0), -(s.get("r") or 0)))


def recent(journal, market: str, days: int = 7) -> list[dict]:
    """Suggestions from the last `days` suggestion-days, newest day first,
    best first inside each day, each with its rank for that day. Ideas a
    check stopped follow their day's suggestions, unranked (rank None)."""
    items = load(journal, market)
    keep = sorted({s["day"] for s in items}, reverse=True)[:days]
    out = []
    for day in keep:
        rows = [x for x in items if x["day"] == day]
        for n, s in enumerate(ranked([x for x in rows if x.get("status") != "blocked"]), 1):
            out.append(dict(s, rank=n))
        out += [dict(s, rank=None) for s in ranked([x for x in rows if x.get("status") == "blocked"])]
    return out


# ── what-if tracking: how every idea would have done ─────────────
# Each idea (passed or stopped) is followed on paper from its scan price as
# if it had been bought: it "hits target" or "hits stop" the first time a
# monitor pass sees the price at or beyond either level, or is closed at the
# last price after TRACK_DAYS calendar days. That puts ideas the checks
# passed and ideas they stopped on the same footing, so their results can be
# compared. Prices are checked every 20 minutes, not tick by tick.
TRACK_DAYS = 30
KEEP_TRACKED = 400


def track_outcomes(journal, market: str, price_fn, now_ts: float | None = None) -> list[dict]:
    """Update the what-if outcome of each open idea. Returns the ones closed now."""
    items = load(journal, market)
    now_ts = now_ts or time.time()
    closed, changed = [], False
    prices: dict = {}
    for s in items:
        o = s.get("outcome") or {"status": "open"}
        if o.get("status") != "open" or not s.get("stop") or not s.get("target") or not s.get("price"):
            continue
        sym = s["symbol"]
        if sym not in prices:
            try:
                prices[sym] = price_fn(sym)
            except Exception:
                prices[sym] = None
        px = prices[sym]
        if not px:
            continue
        o.update(last=round(px, 2), marked=now_ts)
        entry = float(s["price"])
        if px >= float(s["target"]):
            o.update(status="target", exit=float(s["target"]))
        elif px <= float(s["stop"]):
            o.update(status="stop", exit=float(s["stop"]))
        elif now_ts - float(s.get("ts") or now_ts) > TRACK_DAYS * 86400:
            o.update(status="expired", exit=round(px, 2))
        if o["status"] != "open":
            o.update(ret=round(o["exit"] / entry - 1, 4), closed=now_ts)
            closed.append(s)
        else:
            o["ret_now"] = round(px / entry - 1, 4)
        s["outcome"] = o
        changed = True
    if changed:
        journal.kv_set(_key(market), json.dumps(items[-KEEP_TRACKED:]))
    return closed


def outcome_stats(items: list[dict]) -> list[dict]:
    """Results by group: passed the checks, stopped by the second AI check
    (by ruling), stopped by the risk rules."""
    def group(s):
        if s.get("status") != "blocked":
            return "Passed the checks"
        if s.get("blocked_by") == "skeptic":
            return f"Stopped by the second AI check ({s.get('ruling') or 'WAIT'})"
        return "Stopped by the risk rules"
    out = {}
    for s in items:
        o = s.get("outcome") or {}
        if not s.get("stop") or not s.get("target"):
            continue
        g = out.setdefault(group(s), {"group": group(s), "ideas": 0, "closed": 0, "target": 0, "stop": 0,
                                      "expired": 0, "rets": [], "open_rets": []})
        g["ideas"] += 1
        if o.get("status") in ("target", "stop", "expired"):
            g["closed"] += 1
            g[o["status"]] += 1
            g["rets"].append(o["ret"])
        elif o.get("ret_now") is not None:
            g["open_rets"].append(o["ret_now"])
    rows = []
    for g in out.values():
        r, op = g.pop("rets"), g.pop("open_rets")
        g["avg_closed_ret"] = round(sum(r) / len(r), 4) if r else None
        g["win_rate"] = round(g["target"] / g["closed"], 3) if g["closed"] else None
        g["avg_open_ret"] = round(sum(op) / len(op), 4) if op else None
        rows.append(g)
    order = {"Passed the checks": 0}
    return sorted(rows, key=lambda g: (order.get(g["group"], 1), g["group"]))


def blocked_today(journal, market: str) -> int:
    d = today(market)
    return sum(1 for s in load(journal, market) if s["day"] == d and s.get("status") == "blocked")


def attach_approvals(items: list[dict], requests: list[dict], market: str) -> list[dict]:
    """Add what happened to each suggestion that was sent for approval:
    {"id", "status", "note", "created", "resolved"}; status is pending, filled,
    rejected (skipped), expired, cancelled or failed. Matched on day + symbol."""
    tz = ZoneInfo(TZ.get(market, "UTC"))
    by = {}
    for r in requests:
        if r.get("market") != market or not r.get("created"):
            continue
        day = datetime.fromtimestamp(float(r["created"]), tz).strftime("%Y-%m-%d")
        by[(day, r.get("symbol"))] = r
    for s in items:
        r = by.get((s["day"], s["symbol"]))
        if r:
            s["approval"] = {"id": r.get("id"), "status": r.get("status"),
                             "note": r.get("note", ""), "created": r.get("created"),
                             "resolved": r.get("resolved")}
    return items


ICON = {"HIGH": "🔥", "MEDIUM": "🟡", "LOW": "⚪"}


def _line(n: int | None, s: dict) -> str:
    c = "₹" if s.get("currency") == "INR" else "$"
    head = f"{n}. " if n else ""
    tgt = f" · target {c}{s['target']:,.2f} ({s['r']}R)" if s.get("target") else ""
    return (f"{head}{ICON.get(s['priority'], '')} <b>{s['priority']} · {notify._esc(s['symbol'])}</b>\n"
            f"Buy near {c}{s['price']:,.2f} · stop {c}{s['stop']:,.2f}{tgt}\n"
            f"Size: up to {s['size_pct']:g}% of your capital (risks {s['risk_pct']:g}% if stopped)\n"
            f"💡 {notify._esc(s['thesis'][:220])}")


def send_digest(market: str, items: list[dict], blocked: int = 0) -> None:
    """One message per daily cycle, best first."""
    day = datetime.now(ZoneInfo(TZ.get(market, "UTC"))).strftime("%-d %b")
    stopped = (f"\n{blocked} idea{'s' if blocked != 1 else ''} the AI proposed today "
               f"{'were' if blocked != 1 else 'was'} stopped by the checks; the dashboard's "
               "Suggestions tab shows each one with the reason.") if blocked else ""
    if not items:
        notify.alert(f"📋 <b>[{market}] No buy suggestions for {day}.</b>\n"
                     f"Nothing passed the risk rules and the Skeptic today.{stopped}")
        return
    body = "\n\n".join(_line(n, s) for n, s in enumerate(ranked(items), 1))
    notify.alert(f"📋 <b>[{market}] Buy suggestions for {day} ({len(items)})</b>\n\n{body}\n\n"
                 "Suggestions only: the bot places no orders in this market. "
                 f"Prices are from the time of the scan; check the live price before buying.{stopped}")


def send_one(market: str, s: dict) -> None:
    """A watch level was reached during the session."""
    notify.alert(f"📋 <b>[{market}] New buy suggestion (watch level reached)</b>\n\n{_line(None, s)}\n\n"
                 "Suggestion only: the bot places no orders in this market.")
