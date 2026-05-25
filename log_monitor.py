#!/usr/bin/env python3
"""
log_monitor.py — Real-time trade-bot log monitor with AI self-healing.

Architecture:
  1. Tail journalctl for trade-bot in real-time
  2. Detect known patterns (stall, leak, errors, etc.)
  3. For known fixable issues → auto-restart
  4. For persistent/unknown issues → call Claude API with logs + source code
  5. Claude returns diagnosis + specific file patch
  6. Apply patch, redeploy, verify, Telegram report

Patterns monitored:
  STALL       — no Cycle BEGIN for 15 min       → restart bot
  TSLA_LEAK   — TSLA bar fetch spam             → restart bot
  PF_FLOOD    — get_portfolio_value 5+ fails    → restart bot
  INSUF_BAL   — 40310000 insufficient balance   → AI diagnose + fix
  CYCLE_ERR   — 3+ cycle errors in 10 min       → alert
  CONSEC_SL   — 3 stop-losses in 60 min         → alert
  LOW_BAL     — equity < $400                   → alert
  UNKNOWN_ERR — any ERROR/Exception in logs     → AI diagnose + fix
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import logging
from collections import deque
from datetime import datetime
import select

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SERVICE         = "trade-bot"
BOT_DIR         = os.path.expanduser("~/TRADE_BOT")
ENV_FILE        = os.path.join(BOT_DIR, ".env")
LOG_FILE        = os.path.expanduser("~/trade_bot_monitor.log")
STALL_MINUTES   = 15
COOLDOWN_SECS   = 300    # 5 min between same-type actions
BAL_CHECK_SECS  = 60
LOW_BAL_THRESH  = 400.0

# Source files Claude is allowed to patch
PATCHABLE_FILES = ["execution.py", "strategy.py", "config.py"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MONITOR] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)

# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception:
        pass
    return env

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _tg(token: str, chat_id: str, msg: str) -> None:
    if not token or not chat_id:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg[:4000],
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logging.warning("Telegram send failed: %s", e)

# ---------------------------------------------------------------------------
# RCA report builder
# ---------------------------------------------------------------------------

def _send_rca(
    token: str,
    chat_id: str,
    symptom: str,
    root_cause: str,
    fix: str,
    file_changed: str = "None (restart only)",
    github_sha: str | None = None,
    status: str = "Monitoring...",
    severity: str = "medium",
) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    sev_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(severity, "🟡")
    sha_line = f"🔗 GitHub: <code>{github_sha}</code> ✅" if github_sha else "🔗 GitHub: restart only"
    msg = (
        f"📋 <b>AUTO-FIX RCA REPORT</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕐 Time: {now}\n"
        f"{sev_emoji} Severity: {severity.upper()}\n"
        f"⚠️ Symptom: {symptom}\n"
        f"🔍 Root cause: {root_cause}\n"
        f"🛠 Fix applied: {fix}\n"
        f"📁 File: <code>{file_changed}</code>\n"
        f"{sha_line}\n"
        f"✅ Status: {status}\n"
        f"━━━━━━━━━━━━━━━"
    )
    _tg(token, chat_id, msg)
    logging.info("RCA sent: %s | %s", symptom, fix)


# ---------------------------------------------------------------------------
# Service control
# ---------------------------------------------------------------------------

def _restart_bot(
    token: str,
    chat_id: str,
    reason: str,
    symptom: str | None = None,
    root_cause: str | None = None,
    severity: str = "medium",
) -> bool:
    logging.warning("AUTO-RESTART: %s", reason)
    try:
        subprocess.run(["sudo", "systemctl", "restart", SERVICE], timeout=30)
        time.sleep(12)
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE],
            capture_output=True, text=True,
        )
        ok = result.stdout.strip() == "active"
        status = "Bot restarted — back online ✅" if ok else "🔴 RESTART FAILED — manual action needed!"
        _send_rca(
            token, chat_id,
            symptom=symptom or reason,
            root_cause=root_cause or reason,
            fix=f"Restarted {SERVICE} service",
            file_changed="None (restart only)",
            github_sha=None,
            status=status,
            severity=severity if ok else "critical",
        )
        return ok
    except Exception as e:
        _tg(token, chat_id, f"🔴 Restart exception: <code>{e}</code>")
        return False

# ---------------------------------------------------------------------------
# AI Doctor — Claude reads logs + code, returns a patch
# ---------------------------------------------------------------------------

def _get_recent_logs(lines: int = 80) -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", SERVICE, "--no-pager", "-n", str(lines)],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout
    except Exception:
        return ""


def _read_source(filename: str) -> str:
    path = os.path.join(BOT_DIR, filename)
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def _apply_patch(filename: str, old_str: str, new_str: str) -> bool:
    """Apply old→new string replacement in a source file."""
    path = os.path.join(BOT_DIR, filename)
    try:
        with open(path) as f:
            content = f.read()
        if old_str not in content:
            logging.warning("Patch: old_string not found in %s", filename)
            return False
        new_content = content.replace(old_str, new_str, 1)
        with open(path, "w") as f:
            f.write(new_content)
        logging.info("Patch applied to %s", filename)
        return True
    except Exception as e:
        logging.error("Patch failed for %s: %s", filename, e)
        return False


def _git_commit_and_push(filename: str, commit_msg: str) -> str | None:
    """
    Stage file, commit, push to GitHub.
    Returns commit SHA on success, None on failure.
    Requires GITHUB_TOKEN in .env for HTTPS push.
    """
    env = _load_env()
    gh_token = env.get("GITHUB_TOKEN", "")

    try:
        # Configure remote with token if available
        if gh_token:
            remote_url = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, cwd=BOT_DIR,
            ).stdout.strip()
            # Inject token into URL: https://TOKEN@github.com/...
            if "https://" in remote_url and "@" not in remote_url:
                auth_url = remote_url.replace(
                    "https://", f"https://{gh_token}@"
                )
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=BOT_DIR, capture_output=True,
                )

        # Stage the patched file
        subprocess.run(
            ["git", "add", filename],
            cwd=BOT_DIR, check=True, capture_output=True,
        )

        # Commit
        full_msg = f"{commit_msg}\n\nCo-Authored-By: AI Log Monitor <noreply@anthropic.com>"
        subprocess.run(
            ["git", "commit", "-m", full_msg],
            cwd=BOT_DIR, check=True, capture_output=True,
        )

        # Get commit SHA
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BOT_DIR, capture_output=True, text=True,
        ).stdout.strip()

        # Push
        push_result = subprocess.run(
            ["git", "push", "origin", "master"],
            cwd=BOT_DIR, capture_output=True, text=True,
        )

        if push_result.returncode == 0:
            logging.info("Git push OK — commit %s", sha)
            return sha
        else:
            logging.warning("Git push failed: %s", push_result.stderr[:200])
            return sha  # commit exists locally even if push failed

    except subprocess.CalledProcessError as e:
        logging.error("Git commit/push error: %s", e)
        return None
    except Exception as e:
        logging.error("Git sync error: %s", e)
        return None


def _ai_diagnose_and_fix(
    token: str,
    chat_id: str,
    issue_type: str,
    error_snippet: str,
) -> None:
    """
    Call Claude API with recent logs + source code.
    Claude returns JSON with diagnosis + optional patch.
    Apply patch if safe, restart, report.
    """
    env = _load_env()
    anthropic_key = env.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        _tg(token, chat_id,
            f"⚠️ <b>AI diagnosis skipped</b> — ANTHROPIC_API_KEY not set\n"
            f"Issue: <code>{issue_type}</code>")
        return

    logging.info("AI doctor called for issue: %s", issue_type)
    _tg(token, chat_id,
        f"🤖 <b>AI Doctor activated</b>\n"
        f"Issue: <code>{issue_type}</code>\n"
        f"Analysing logs + source code...")

    # Collect context
    recent_logs = _get_recent_logs(80)
    source_context = ""
    for fname in PATCHABLE_FILES:
        src = _read_source(fname)
        if src:
            source_context += f"\n\n=== {fname} ===\n{src[:4000]}"

    system_prompt = """You are an autonomous trading bot repair agent.
You will receive:
1. Recent bot logs showing an error
2. Source code of the relevant files

Your job:
- Diagnose the root cause precisely
- If fixable via a targeted code patch: return a JSON patch
- If NOT safely fixable (e.g. needs human judgment, money at risk): return alert only

IMPORTANT SAFETY RULES:
- NEVER modify stop_loss_pct or take_profit_pct values
- NEVER modify trade sizing logic to increase position sizes
- ONLY fix clear bugs: wrong variable names, missing conditions, incorrect API calls
- If unsure: return alert_only = true

Return ONLY valid JSON in this exact format:
{
  "diagnosis": "one sentence root cause",
  "severity": "critical|high|medium|low",
  "alert_only": false,
  "patch": {
    "file": "execution.py",
    "old_string": "exact string to replace (must be unique in file)",
    "new_string": "replacement string",
    "explanation": "what this patch fixes"
  }
}

If no code patch needed (alert only), set alert_only=true and omit patch field.
"""

    user_msg = f"""Issue detected: {issue_type}

Error snippet:
{error_snippet}

Recent logs (last 80 lines):
{recent_logs[-3000:]}

Source files:
{source_context}

Diagnose and return fix JSON."""

    try:
        import urllib.request
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_msg}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        raw = data["content"][0]["text"].strip()

        # Parse JSON from Claude response
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        result = json.loads(raw)
        diagnosis  = result.get("diagnosis", "Unknown")
        severity   = result.get("severity", "medium")
        alert_only = result.get("alert_only", True)
        patch      = result.get("patch")

        logging.info("AI diagnosis: %s | severity=%s | alert_only=%s", diagnosis, severity, alert_only)

        if alert_only or not patch:
            _tg(token, chat_id,
                f"🤖 <b>AI Diagnosis</b>\n"
                f"Issue: <code>{issue_type}</code>\n"
                f"Severity: {severity}\n"
                f"Root cause: {diagnosis}\n"
                f"⚠️ Requires manual fix — no safe auto-patch available.")
            return

        # Apply patch
        fname    = patch.get("file", "")
        old_str  = patch.get("old_string", "")
        new_str  = patch.get("new_string", "")
        explain  = patch.get("explanation", "")

        if fname not in PATCHABLE_FILES:
            _tg(token, chat_id,
                f"🤖 AI wants to patch <code>{fname}</code> but it's not in allowed list.\n"
                f"Manual fix needed: {diagnosis}")
            return

        if not old_str or not new_str:
            _tg(token, chat_id,
                f"🤖 AI returned incomplete patch for <code>{fname}</code>.\n"
                f"Diagnosis: {diagnosis}")
            return

        patch_ok = _apply_patch(fname, old_str, new_str)
        if patch_ok:
            # Commit + push to GitHub
            commit_msg = (
                f"AI auto-fix: {explain}\n\n"
                f"Issue: {issue_type}\n"
                f"Diagnosis: {diagnosis}\n"
                f"Severity: {severity}"
            )
            sha = _git_commit_and_push(fname, commit_msg)
            # Restart bot with fresh code
            subprocess.run(["sudo", "systemctl", "restart", SERVICE], timeout=30)
            time.sleep(12)
            svc_ok = subprocess.run(
                ["systemctl", "is-active", SERVICE],
                capture_output=True, text=True,
            ).stdout.strip() == "active"
            # Send full RCA
            _send_rca(token, chat_id,
                symptom=issue_type,
                root_cause=diagnosis,
                fix=f"AI patched code: {explain}",
                file_changed=fname,
                github_sha=sha,
                status="Bot restarted with fix ✅" if svc_ok else "🔴 Restart failed after patch",
                severity=severity)
        else:
            _tg(token, chat_id,
                f"🤖 AI patch FAILED to apply to <code>{fname}</code>\n"
                f"Diagnosis: {diagnosis}\n"
                f"Manual fix needed.")

    except json.JSONDecodeError as e:
        logging.error("AI returned invalid JSON: %s", e)
        _tg(token, chat_id,
            f"🤖 AI diagnosis failed (invalid JSON): <code>{e}</code>\n"
            f"Issue: {issue_type} — manual check needed.")
    except Exception as e:
        logging.error("AI doctor exception: %s", e)
        _tg(token, chat_id,
            f"🤖 AI doctor error: <code>{str(e)[:200]}</code>\n"
            f"Issue: {issue_type}")

# ---------------------------------------------------------------------------
# Balance check
# ---------------------------------------------------------------------------

def _check_balance(token: str, chat_id: str) -> None:
    env = _load_env()
    api_key    = env.get("EXCHANGE_API_KEY", "")
    api_secret = env.get("EXCHANGE_API_SECRET", "")
    if not api_key:
        return
    try:
        if BOT_DIR not in sys.path:
            sys.path.insert(0, BOT_DIR)
        from alpaca.trading.client import TradingClient
        is_paper = env.get("PAPER", "false").lower() in ("1", "true", "yes")
        client = TradingClient(api_key, api_secret, paper=is_paper)
        equity = float(client.get_account().equity)
        logging.info("Balance check: $%.2f", equity)
        if equity < LOW_BAL_THRESH:
            _tg(token, chat_id,
                f"🚨 <b>LOW BALANCE</b>: ${equity:.2f}\n"
                f"Started at $500 — consider pausing bot.")
    except Exception as e:
        logging.warning("Balance check failed: %s", e)

# ---------------------------------------------------------------------------
# Monitor state
# ---------------------------------------------------------------------------

class State:
    def __init__(self) -> None:
        self.last_cycle_begin   = datetime.utcnow()
        self.pf_fails:     deque = deque(maxlen=10)
        self.cycle_errs:   deque = deque(maxlen=10)
        self.sl_times:     deque = deque(maxlen=10)
        self.error_lines:  deque = deque(maxlen=20)
        self.tsla_count         = 0
        self.last_action: dict[str, datetime] = {}
        self.restart_count      = 0
        self.last_restart       = datetime.min

    def can_act(self, key: str, cooldown: int = COOLDOWN_SECS) -> bool:
        last = self.last_action.get(key)
        return last is None or (datetime.utcnow() - last).total_seconds() > cooldown

    def acted(self, key: str) -> None:
        self.last_action[key] = datetime.utcnow()


def _process_line(line: str, state: State, token: str, chat_id: str) -> None:
    now = datetime.utcnow()

    # Track Cycle BEGIN — heartbeat
    if "Cycle BEGIN" in line:
        state.last_cycle_begin = now
        state.tsla_count = 0

    # Track all ERROR/Exception lines for AI context
    if any(kw in line for kw in ("ERROR", "Exception", "Traceback", "raise ")):
        state.error_lines.append(line.strip())

    # TSLA leak
    if "TSLA: bar fetch returned None" in line:
        state.tsla_count += 1
        if state.tsla_count >= 5 and state.can_act("tsla"):
            state.acted("tsla")
            state.tsla_count = 0
            _restart_bot(token, chat_id,
                reason="TSLA spam",
                symptom="TSLA bar fetch spam (5+ occurrences) on crypto VM",
                root_cause="Daybot scheduler started on crypto-only VM — DISABLE_DAY_BOT flag bypassed",
                severity="high")

    # get_portfolio_value flood
    if "get_portfolio_value failed" in line:
        state.pf_fails.append(now)
        recent = [t for t in state.pf_fails if (now - t).total_seconds() < 300]
        if len(recent) >= 5 and state.can_act("pf_flood"):
            state.acted("pf_flood")
            _restart_bot(token, chat_id,
                reason="portfolio value flood",
                symptom="get_portfolio_value failing 5+ times in 5 min",
                root_cause="Daybot running with wrong API keys or paper endpoint mismatch",
                severity="medium")

    # Insufficient balance — AI fix
    if "40310000" in line and state.can_act("insuf_bal", 1800):
        state.acted("insuf_bal")
        _ai_diagnose_and_fix(token, chat_id,
            issue_type="40310000 insufficient balance",
            error_snippet=line.strip())

    # Cycle errors — RCA alert
    if "Cycle error" in line:
        state.cycle_errs.append(now)
        recent = [t for t in state.cycle_errs if (now - t).total_seconds() < 600]
        if len(recent) >= 3 and state.can_act("cycle_err"):
            state.acted("cycle_err")
            _send_rca(token, chat_id,
                symptom="3+ cycle errors in 10 min",
                root_cause="Alpaca API or network connectivity issue",
                fix="Monitoring — bot self-recovers on next cycle",
                file_changed="None",
                status="Watching for recovery...",
                severity="medium")

    # Stop-losses — RCA alert
    if "stop_loss" in line and ("close" in line.lower() or "reason" in line.lower()):
        state.sl_times.append(now)
        recent = [t for t in state.sl_times if (now - t).total_seconds() < 3600]
        if len(recent) >= 3 and state.can_act("consec_sl"):
            state.acted("consec_sl")
            _send_rca(token, chat_id,
                symptom="3 stop-losses triggered in 60 min",
                root_cause="Possible flash crash or sustained downtrend across all symbols",
                fix="No auto-fix — positions protected by SL. Consider /stop if market crashing.",
                file_changed="None",
                status="⚠️ Manual review recommended",
                severity="high")

    # Double thread — RCA restart
    if "start already in progress" in line.lower() or "thread already alive" in line.lower():
        if state.can_act("double_thread"):
            state.acted("double_thread")
            _restart_bot(token, chat_id,
                reason="double-thread",
                symptom="Two bot threads running simultaneously",
                root_cause="Concurrent _start_crypto_bot_internal calls — telegram_bot re-import triggered module-level code twice",
                severity="critical")

    # Unknown persistent errors — AI diagnose
    if "ERROR:root:" in line or "Traceback (most recent call last)" in line:
        if state.can_act("unknown_err", 1800):  # max once per 30 min
            state.acted("unknown_err")
            recent_errors = "\n".join(list(state.error_lines)[-10:])
            _ai_diagnose_and_fix(token, chat_id,
                issue_type="Unexpected ERROR in logs",
                error_snippet=recent_errors)


def _periodic_checks(state: State, token: str, chat_id: str) -> None:
    now = datetime.utcnow()

    # Stall check
    mins_since = (now - state.last_cycle_begin).total_seconds() / 60
    if mins_since >= STALL_MINUTES and state.can_act("stall"):
        state.acted("stall")
        _restart_bot(token, chat_id,
            reason="stall",
            symptom=f"No Cycle BEGIN for {mins_since:.0f} min — bot loop frozen",
            root_cause="Bot thread stalled or crashed silently — loop not advancing",
            severity="critical")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.info("AI Log Monitor starting — service: %s", SERVICE)
    env     = _load_env()
    token   = env.get("TELEGRAM_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")

    _tg(token, chat_id,
        "🟢 <b>AI Log Monitor started</b>\n"
        "Watching trade-bot — will auto-fix issues via Claude API")

    state = State()
    last_bal = datetime.utcnow()

    proc = subprocess.Popen(
        ["journalctl", "-u", SERVICE, "-f", "--no-pager", "-n", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    logging.info("Tailing journalctl for %s...", SERVICE)

    while True:
        ready, _, _ = select.select([proc.stdout], [], [], 5.0)

        if ready:
            line = proc.stdout.readline()
            if line:
                _process_line(line, state, token, chat_id)
        else:
            _periodic_checks(state, token, chat_id)
            if (datetime.utcnow() - last_bal).total_seconds() > BAL_CHECK_SECS:
                last_bal = datetime.utcnow()
                _check_balance(token, chat_id)

        if proc.poll() is not None:
            logging.error("journalctl died — restarting tail")
            time.sleep(5)
            proc = subprocess.Popen(
                ["journalctl", "-u", SERVICE, "-f", "--no-pager", "-n", "0"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )


if __name__ == "__main__":
    main()
