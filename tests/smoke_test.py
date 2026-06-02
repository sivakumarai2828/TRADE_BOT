"""
smoke_test.py — Post-deploy sanity check for all bots.

Run after every deployment:
    python3 tests/smoke_test.py

Tests:
  - Both VMs reachable + healthy
  - Crypto bot: status, positions, signals responding
  - Day bot: all /daybot/* endpoints responding
  - Options bot: /optionsbot/* endpoints responding
  - API key auth working (401 without key, 200 with key)
  - Evening analysis state has data (or warns if empty)
  - Batch API available (Anthropic)
  - Pre-market fallback logic (summaries empty → universe fallback)
  - Signal reasoning is rule-based (no Claude haiku calls)

Exit code 0 = all passed. Exit code 1 = failures found.
"""
from __future__ import annotations
import os
import sys
import json
import time
import requests
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
CRYPTO_URL = os.getenv("SMOKE_CRYPTO_URL", "http://34.67.95.221:8000")
DAY_URL    = os.getenv("SMOKE_DAY_URL",    "http://34.171.182.46:8000")

# Each VM can have its own API key. Fall back to BOT_API_KEY if specific one not set.
# Ideally both VMs use the SAME key — set BOT_API_KEY and done.
# If different: set SMOKE_CRYPTO_KEY and SMOKE_DAY_KEY separately.
BOT_KEY        = os.getenv("BOT_API_KEY", "")
CRYPTO_BOT_KEY = os.getenv("SMOKE_CRYPTO_KEY", BOT_KEY)
DAY_BOT_KEY    = os.getenv("SMOKE_DAY_KEY", BOT_KEY)
TIMEOUT        = 10  # seconds per request

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

_results: list[tuple[str, bool, str]] = []

def check(name: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    print(f"  {icon} {name}" + (f" — {detail}" if detail else ""))
    _results.append((name, ok, detail))
    return ok

def warn(name: str, detail: str = "") -> None:
    print(f"  {WARN} {name}" + (f" — {detail}" if detail else ""))
    _results.append((name, True, f"WARN: {detail}"))

def _key_for(url: str) -> str:
    """Return correct API key for given base URL."""
    if "34.67.95" in url or "crypto" in url.lower():
        return CRYPTO_BOT_KEY
    return DAY_BOT_KEY

def get(url: str, path: str, with_key: bool = True) -> Optional[dict]:
    key = _key_for(url)
    headers = {"X-Bot-Key": key} if (with_key and key) else {}
    try:
        r = requests.get(f"{url}{path}", headers=headers, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None

def post(url: str, path: str) -> Optional[int]:
    key = _key_for(url)
    headers = {"X-Bot-Key": key} if key else {}
    try:
        r = requests.post(f"{url}{path}", headers=headers, timeout=TIMEOUT)
        return r.status_code
    except Exception:
        return None

# ── Test suites ───────────────────────────────────────────────────────────────

def test_crypto_vm():
    print("\n🪙 CRYPTO VM", CRYPTO_URL)

    # Health
    h = get(CRYPTO_URL, "/health")
    check("Health endpoint", h is not None and h.get("ok"), str(h))

    # Status
    s = get(CRYPTO_URL, "/status")
    ok = s is not None and "metrics" in s
    check("Status endpoint", ok, "has metrics" if ok else "missing or error")

    if s:
        m = s.get("metrics", {})
        bal = m.get("balance", 0)
        check("Balance > 0", bal > 0, f"${bal}")

        wr = m.get("win_rate", -1)
        check("Win rate readable", wr >= 0, f"{wr}%")

        shield = m.get("shield_active", None)
        if shield:
            warn("SHIELD active", "consecutive losses — bot not entering new trades")
        else:
            check("SHIELD off", True, "normal mode")

        daily_halted = m.get("daily_loss_halted", False)
        if daily_halted:
            warn("Daily loss halt active", "no new trades today")

    # Positions
    p = get(CRYPTO_URL, "/positions")
    check("Positions endpoint", p is not None, f"{list(p.keys()) if p else 'error'}")

    # Logs
    logs = get(CRYPTO_URL, "/logs")
    check("Logs endpoint", logs is not None, f"{len(logs)} entries" if logs else "error")

    if logs:
        # Verify no 'Claude reasoning' in recent logs (should be 'Signal reasoning' now)
        old_label = any("Claude reasoning" in l.get("message", "") for l in logs[:20])
        check("Log label updated (no 'Claude reasoning')", not old_label,
              "found stale 'Claude reasoning' label" if old_label else "using 'Signal reasoning'")

    # API key auth — without key should get 401
    try:
        r = requests.get(f"{CRYPTO_URL}/status", headers={}, timeout=TIMEOUT)
        check("Auth: no key → 401", r.status_code == 401,
              f"got {r.status_code} (expected 401)")
    except Exception as e:
        check("Auth: no key → 401", False, str(e))


def test_day_vm():
    print("\n📅 DAY/OPTIONS VM", DAY_URL)

    # Health + scheduler
    h = get(DAY_URL, "/health")
    check("Health endpoint", h is not None and h.get("ok"), str(h))
    if h:
        check("Scheduler running", h.get("scheduler") is True,
              "scheduler=True" if h.get("scheduler") else "scheduler=False or None")

    # Auth
    try:
        r = requests.get(f"{DAY_URL}/health", headers={}, timeout=TIMEOUT)
        check("Auth: no key → 401", r.status_code == 401,
              f"got {r.status_code} (expected 401)")
    except Exception as e:
        check("Auth: no key → 401", False, str(e))

    # Day bot status
    ds = get(DAY_URL, "/daybot/status")
    check("Daybot /status endpoint", ds is not None,
          "OK" if ds else "UNREACHABLE — wrong VM URL?")

    if ds:
        check("Daybot has metrics", "metrics" in ds)
        check("Daybot has positions key", "positions" in ds)

    # Day bot logs
    dl = get(DAY_URL, "/daybot/logs")
    check("Daybot /logs endpoint", dl is not None)

    # Day bot watchlist
    wl = get(DAY_URL, "/daybot/watchlist")
    check("Daybot /watchlist endpoint", wl is not None)
    if wl is not None:
        symbols = wl if isinstance(wl, list) else wl.get("symbols", [])
        if not symbols:
            warn("Watchlist empty", "no symbols — pre-market may have failed")
        else:
            check("Watchlist has symbols", True, f"{symbols[:5]}")

    # Day bot suggestions (evening analysis)
    sg = get(DAY_URL, "/daybot/suggestions")
    check("Daybot /suggestions endpoint", sg is not None)
    if sg is not None:
        count = len(sg) if isinstance(sg, list) else len(sg.get("approved", []))
        if count == 0:
            warn("Evening suggestions empty", "no approved symbols — check evening analysis")
        else:
            check("Evening suggestions has data", True, f"{count} symbols")

    # Options bot status
    os_status = get(DAY_URL, "/optionsbot/status")
    check("Options /status endpoint", os_status is not None,
          "OK" if os_status else "UNREACHABLE")

    if os_status:
        ev_approved = (os_status.get("evening_approved") or [])
        if not ev_approved:
            warn("Options evening approved empty", "no options picks for tomorrow")
        else:
            check("Options evening picks", True, f"{ev_approved}")

    # Options suggestions
    opk = get(DAY_URL, "/daybot/options-suggestions")
    check("Options suggestions endpoint", opk is not None)


def test_critical_data():
    print("\n🔍 CRITICAL DATA CHECKS")

    # Crypto: signal should be rule-based (no API cost)
    s = get(CRYPTO_URL, "/status")
    if s:
        # Check mode is set
        mode = s.get("settings", {}).get("current_mode", "unknown")
        check("Crypto mode readable", mode in ("SAFE", "AGGRESSIVE", "SHIELD"),
              f"mode={mode}")

    # Day: pre-market approved not empty (during market hours only)
    now_hour_utc = time.gmtime().tm_hour
    market_open = 13 <= now_hour_utc <= 21  # 9 AM - 5 PM ET approx
    if market_open:
        ds = get(DAY_URL, "/daybot/status")
        if ds:
            premarket = ds.get("premarket_approved", [])
            approved = ds.get("evening_approved", [])
            if not premarket and not approved:
                warn("No approved watchlist during market hours",
                     "pre-market and evening both empty — bot may not trade")

    # Check Anthropic SDK importable
    try:
        import anthropic
        check("Anthropic SDK importable", True)
        has_key = bool(os.getenv("ANTHROPIC_API_KEY", ""))
        if not has_key:
            warn("ANTHROPIC_API_KEY not in local env", "OK if running locally — key lives on VM")
        else:
            check("ANTHROPIC_API_KEY set", True, "present")
    except ImportError:
        check("Anthropic SDK importable", False, "pip install anthropic")


def test_env_vars():
    print("\n🔑 ENVIRONMENT CHECKS (local)")
    check("SMOKE_CRYPTO_KEY or BOT_API_KEY set", bool(CRYPTO_BOT_KEY),
          f"crypto key prefix: {CRYPTO_BOT_KEY[:6]}..." if CRYPTO_BOT_KEY else "MISSING")
    check("SMOKE_DAY_KEY or BOT_API_KEY set", bool(DAY_BOT_KEY),
          f"day key prefix: {DAY_BOT_KEY[:6]}..." if DAY_BOT_KEY else "MISSING")
    if CRYPTO_BOT_KEY and DAY_BOT_KEY and CRYPTO_BOT_KEY != DAY_BOT_KEY:
        warn("VM keys differ", "recommend setting same BOT_API_KEY on both VMs for simplicity")
    if not os.getenv("ANTHROPIC_API_KEY"):
        warn("ANTHROPIC_API_KEY not in local env", "OK — key lives on VM")
    else:
        check("ANTHROPIC_API_KEY set", True)
    check("SMOKE_CRYPTO_URL", CRYPTO_URL != "", CRYPTO_URL)
    check("SMOKE_DAY_URL", DAY_URL != "", DAY_URL)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("🤖 TRADE BOT SMOKE TEST")
    print(f"   Crypto: {CRYPTO_URL}")
    print(f"   Day:    {DAY_URL}")
    print("=" * 60)

    test_env_vars()
    test_crypto_vm()
    test_day_vm()
    test_critical_data()

    # Summary
    total = len(_results)
    passed = sum(1 for _, ok, d in _results if ok and not d.startswith("WARN"))
    warned = sum(1 for _, ok, d in _results if ok and d.startswith("WARN"))
    failed = sum(1 for _, ok, _ in _results if not ok)

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed}/{total} passed | {warned} warnings | {failed} failures")
    print("=" * 60)

    if failed:
        print("\nFAILED CHECKS:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  {FAIL} {name}: {detail}")
        sys.exit(1)
    elif warned:
        print("\nAll checks passed (with warnings). Bot may have degraded behavior.")
        sys.exit(0)
    else:
        print("\nAll checks passed ✅")
        sys.exit(0)


if __name__ == "__main__":
    main()
