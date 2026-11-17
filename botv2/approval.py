"""Per-trade human approval over Telegram.

When enabled, no new position is opened until Siva taps Approve. Exits are
never gated — stops, targets, kill switch and AI SELLs still run on their own,
because a delayed exit is a risk and a delayed entry is not.

Flow
  1. An entry passes the risk caps and the Skeptic.
  2. MarketRunner calls ApprovalDesk.submit(); the request is stored in the
     journal kv store and sent to Telegram with Approve / Skip buttons.
  3. The scheduler loop calls ApprovalDesk.poll() every ~20 s. It reads button
     taps (getUpdates), expires stale requests, and on Approve hands the
     request back to MarketRunner.execute_approved(), which revalidates
     everything at the CURRENT price before filling.

Only updates from the configured TELEGRAM_CHAT_ID are honoured.
Text commands also work: /pending, /approve <id>, /reject <id>.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time

import requests

log = logging.getLogger("botv2.approval")

KV_REQUESTS = "approval_requests"
KV_OFFSET = "telegram_update_offset"
KEEP_RESOLVED = 50          # resolved requests kept for the audit trail
# A request never outlives the session it was made in: approving after the
# close would fill at a stale last price. (tz, close hour, close minute)
SESSION_CLOSE = {"US": ("America/New_York", 16, 0), "INDIA": ("Asia/Kolkata", 15, 30)}


def deadline(market: str, created: float, timeout_s: float) -> float:
    """When a request expires: timeout_s after it was made, but no later than
    that day's market close. A request made after the close keeps the plain
    timeout (the price re-check at approval still guards it)."""
    end = created + timeout_s
    tz_close = SESSION_CLOSE.get(market)
    if tz_close:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz, h, m = tz_close
        close = datetime.fromtimestamp(created, ZoneInfo(tz)).replace(
            hour=h, minute=m, second=0, microsecond=0).timestamp()
        if created < close:
            end = min(end, close)
    return end


class ApprovalDesk:
    def __init__(self, journal, token: str, chat_id: str,
                 timeout_min: float = 90, max_drift_pct: float = 0.015,
                 http=requests):
        self.journal = journal
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout_s = timeout_min * 60
        self.max_drift_pct = max_drift_pct
        self.http = http
        self.runners: dict = {}            # set by main after runners exist

    # ── storage ──────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        raw = self.journal.kv_get(KV_REQUESTS) or "[]"
        try:
            reqs = json.loads(raw)
            return reqs if isinstance(reqs, list) else []
        except ValueError:
            log.error("approval store corrupt — starting empty")
            return []

    def _save(self, reqs: list[dict]) -> None:
        pending = [r for r in reqs if r["status"] == "pending"]
        done = [r for r in reqs if r["status"] != "pending"][-KEEP_RESOLVED:]
        self.journal.kv_set(KV_REQUESTS, json.dumps(pending + done))

    def _expires(self, req: dict) -> float:
        return float(req.get("expires") or req["created"] + self.timeout_s)

    def pending(self, market: str | None = None) -> list[dict]:
        return [r for r in self._load() if r["status"] == "pending"
                and (market is None or r["market"] == market)]

    # ── submit ───────────────────────────────────────────────────
    def submit(self, req: dict) -> str | None:
        """Store and announce a request. Returns its id, or None when a
        request for the same symbol is already waiting (no duplicate pings)."""
        reqs = self._load()
        if any(r["status"] == "pending" and r["market"] == req["market"]
               and r["symbol"] == req["symbol"] for r in reqs):
            log.info("approval for %s %s already pending", req["market"], req["symbol"])
            return None
        req = dict(req)
        req.update(id=secrets.token_hex(3), status="pending",
                   created=time.time(), message_id=None)
        req["expires"] = deadline(req["market"], req["created"], self.timeout_s)
        msg_id = self._send(self._request_text(req), buttons=req["id"])
        req["message_id"] = msg_id
        reqs.append(req)
        self._save(reqs)
        log.info("approval %s requested: %s %s %s", req["id"], req["market"],
                 req["kind"], req["symbol"])
        return req["id"]

    # ── decisions ────────────────────────────────────────────────
    def decide(self, req_id: str, approve: bool, source: str = "telegram") -> str:
        reqs = self._load()
        req = next((r for r in reqs if r["id"] == req_id), None)
        if req is None:
            return f"No request {req_id}."
        if req["status"] != "pending":
            return f"{req['symbol']} already {req['status']}."
        if time.time() > self._expires(req):
            self._resolve(req, "expired", "approval window passed")
            self._save(reqs)
            return f"{req['symbol']} expired before approval."

        runner = self.runners.get(req["market"])
        if not approve:
            self._resolve(req, "rejected", f"skipped via {source}")
            if runner:
                runner.on_approval_rejected(req, "rejected by Siva")
            self._save(reqs)
            return f"Skipped {req['symbol']}."

        if runner is None:
            self._resolve(req, "failed", "market runner not active")
            self._save(reqs)
            return f"{req['market']} is not running here."
        try:
            ok, note = runner.execute_approved(req, self.max_drift_pct)
        except Exception as exc:
            log.exception("approved %s failed", req_id)
            ok, note = False, f"error: {exc}"
        self._resolve(req, "filled" if ok else "cancelled", note)
        if not ok:
            runner.on_approval_rejected(req, f"cancelled at fill: {note}")
        self._save(reqs)
        return f"{'Filled' if ok else 'Not filled'} {req['symbol']}: {note}"

    def _resolve(self, req: dict, status: str, note: str) -> None:
        req["status"], req["note"], req["resolved"] = status, note[:200], time.time()
        icon = {"filled": "✅", "rejected": "⏭", "expired": "⌛"}.get(status, "⚠️")
        if req.get("message_id"):
            self._edit(req["message_id"],
                       self._request_text(req) + f"\n\n{icon} <b>{status.upper()}</b> — "
                       + html.escape(note))

    def expire_stale(self) -> int:
        reqs = self._load()
        n = 0
        for r in reqs:
            if r["status"] == "pending" and time.time() > self._expires(r):
                self._resolve(r, "expired", "no answer in time")
                runner = self.runners.get(r["market"])
                if runner:
                    try:
                        runner.on_approval_rejected(r, "approval expired")
                    except Exception:
                        log.exception("expiry hook failed for %s", r["id"])
                n += 1
        if n:
            self._save(reqs)
        return n

    # ── Telegram polling ─────────────────────────────────────────
    def poll(self) -> None:
        self.expire_stale()
        offset = int(self.journal.kv_get(KV_OFFSET) or 0)
        try:
            r = self.http.get(self._url("getUpdates"),
                              params={"offset": offset, "timeout": 0,
                                      "allowed_updates": json.dumps(["callback_query", "message"])},
                              timeout=10)
            updates = r.json().get("result", [])
        except Exception as exc:
            log.warning("telegram getUpdates failed: %s", type(exc).__name__)
            return
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            try:
                self._handle(u)
            except Exception:
                log.exception("telegram update %s failed", u.get("update_id"))
        if updates:
            self.journal.kv_set(KV_OFFSET, str(offset))

    def _handle(self, u: dict) -> None:
        cq = u.get("callback_query")
        if cq:
            chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            if chat != self.chat_id:
                log.warning("ignored callback from unknown chat %s", chat)
                return
            action, _, req_id = str(cq.get("data", "")).partition(":")
            if action in ("ap", "rj"):
                result = self.decide(req_id, approve=(action == "ap"))
                self._answer(cq["id"], result)
            return
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
            return
        parts = str(msg.get("text", "")).strip().split()
        if not parts:
            return
        cmd = parts[0].lower().split("@")[0]
        if cmd == "/pending":
            p = self.pending()
            self._send("No pending approvals." if not p else "\n".join(
                f"<code>{r['id']}</code> {r['market']} {r['kind']} {r['symbol']} @ {r['price']}"
                for r in p))
        elif cmd in ("/approve", "/reject") and len(parts) > 1:
            self._send(html.escape(self.decide(parts[1], approve=(cmd == "/approve"),
                                               source="command")))

    # ── message formatting / Telegram API ────────────────────────
    def _request_text(self, r: dict) -> str:
        c = "₹" if r.get("currency") == "INR" else "$"
        rr = ""
        try:
            rr = f" ({(r['target'] - r['price']) / (r['price'] - r['stop']):.1f}R)"
        except (TypeError, ZeroDivisionError, KeyError):
            pass
        lines = [
            f"🟡 <b>APPROVAL NEEDED</b> <code>{r['id']}</code>",
            f"[{r['market']}] <b>{r['kind']} {r['symbol']}</b>"
            + (f" (replaces {html.escape(str(r['replace']))})" if r.get("replace") else ""),
            (f"~{float(r['qty']):.0f} sh" if r.get("qty") else "size set at fill")
            + f" @ {c}{r['price']:,.2f} | stop {c}{r['stop']:,.2f}"
            + (f" | target {c}{r['target']:,.2f}{rr}" if r.get("target") else ""),
            f"💡 {html.escape(str(r.get('thesis', ''))[:200])}",
        ]
        if r.get("skeptic"):
            lines.append(f"🧐 Skeptic BUY: {html.escape(str(r['skeptic'])[:200])}")
        left = max(0, int((self._expires(r) - time.time()) // 60)) if r.get("created") else int(self.timeout_s // 60)
        lines.append(f"⏱ Expires in {left // 60}h {left % 60}m (market close at the latest). "
                     f"Re-checked at current price on approval.")
        return "\n".join(lines)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _send(self, text: str, buttons: str | None = None) -> int | None:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"ap:{buttons}"},
                {"text": "⏭ Skip", "callback_data": f"rj:{buttons}"},
            ]]}
        try:
            r = self.http.post(self._url("sendMessage"), json=payload, timeout=8)
            return r.json().get("result", {}).get("message_id")
        except Exception as exc:
            log.warning("telegram send failed: %s", type(exc).__name__)
            return None

    def _edit(self, message_id: int, text: str) -> None:
        try:
            self.http.post(self._url("editMessageText"),
                           json={"chat_id": self.chat_id, "message_id": message_id,
                                 "text": text, "parse_mode": "HTML"}, timeout=8)
        except Exception as exc:
            log.warning("telegram edit failed: %s", type(exc).__name__)

    def _answer(self, callback_id: str, text: str) -> None:
        try:
            self.http.post(self._url("answerCallbackQuery"),
                           json={"callback_query_id": callback_id, "text": text[:190]},
                           timeout=8)
        except Exception as exc:
            log.warning("telegram answer failed: %s", type(exc).__name__)
