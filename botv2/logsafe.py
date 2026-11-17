"""Keep secrets out of the logs.

A failed request's error message includes its URL, and Telegram puts the
bot token in the URL (https://api.telegram.org/bot<TOKEN>/getUpdates), so a
plain `log.warning("...: %s", exc)` wrote the token into journald. This
filter rewrites every record, including tracebacks, before any handler
writes it.
"""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),       # Telegram bot token
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "<anthropic-key>"),
    (re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"), "<openrouter-key>"),
    (re.compile(r"sb_secret_[A-Za-z0-9_-]{10,}"), "<supabase-key>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), "<jwt>"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        except Exception:          # never lose a log line over redaction
            pass
        return True


def install() -> None:
    """Attach the filter to every handler on the root logger."""
    f = RedactSecrets()
    for h in logging.getLogger().handlers:
        if not any(isinstance(x, RedactSecrets) for x in h.filters):
            h.addFilter(f)
