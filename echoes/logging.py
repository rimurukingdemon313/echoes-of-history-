"""Structured logging with secret redaction at the boundary.

Every log line is one JSON object. The redaction pass is not a convenience:
API keys reach this module inside exception messages and provider payloads,
and a log shipped to a hosting dashboard is a place secrets leak from. The
filter runs on the formatted record, so it catches a key that arrived inside
an f-string as well as one passed as a field.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

_SECRET_ENV_NAMES = (
    "GEMINI_API_KEY",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CLIENT_ID",
    "TELEGRAM_BOT_TOKEN",
    "S3_SECRET_KEY",
    "S3_ACCESS_KEY",
    "SEARCH_API_KEY",
    "API_TOKEN",
    "DATABASE_URL",
)

# Shapes that look like credentials even when we never held them in env.
_PATTERNS = (
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),           # Google API keys
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{10,}"),          # Google OAuth tokens
    re.compile(r"\b1//[0-9A-Za-z_\-]{20,}"),             # Google refresh tokens
    re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}"),         # Telegram bot tokens
    re.compile(r"(?i)\bpostgres(?:ql)?://[^\s\"']+"),    # DB URLs with passwords
    re.compile(r"(?i)(authorization|bearer)\s+[A-Za-z0-9._\-]{16,}"),
)

REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Remove anything that looks like a credential from ``text``."""
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class JsonFormatter(logging.Formatter):
    RESERVED = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        try:
            line = json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            line = json.dumps({"ts": payload["ts"], "level": payload["level"],
                               "logger": payload["logger"],
                               "msg": str(payload["msg"])})
        return redact(line)


def configure(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # These two are chatty and say nothing we do not already log ourselves.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Older Piper voices warn once per unmapped phoneme, which on a
    # 90-minute script is thousands of identical lines that say nothing an
    # operator can act on.
    logging.getLogger("piper.phoneme_ids").setLevel(logging.ERROR)


class _ContextAdapter(logging.LoggerAdapter):
    """Carries fixed context without swallowing per-call ``extra``.

    ``logging.LoggerAdapter`` replaces ``extra`` rather than merging it, which
    silently drops the per-call fields that make a line worth reading.
    """

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        merged = dict(self.extra or {})
        merged.update(kwargs.get("extra") or {})
        kwargs["extra"] = merged
        return msg, kwargs

    def bind(self, **context: Any) -> "_ContextAdapter":
        merged = dict(self.extra or {})
        merged.update(context)
        return _ContextAdapter(self.logger, merged)


def get_logger(name: str, **context: Any) -> _ContextAdapter:
    """A logger that carries ``context`` (job id, stage) on every line."""
    return _ContextAdapter(logging.getLogger(name), context)
