"""Secret redaction for logs, error messages and reports."""

from __future__ import annotations

import logging
import re
import threading

_lock = threading.Lock()
_known_secrets: set[str] = set()

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|authorization|token|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"),
    re.compile(r"gsk_[A-Za-z0-9]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
]

MASK = "***REDACTED***"


def register_secret(value: str | None) -> None:
    """Remember a secret value so that it is always masked in logs/messages."""
    if value and len(value) >= 4:
        with _lock:
            _known_secrets.add(value)


def unregister_secret(value: str | None) -> None:
    if value:
        with _lock:
            _known_secrets.discard(value)


def redact(text: object) -> str:
    """Return ``text`` as a string with known secrets and key-like tokens masked."""
    s = str(text)
    with _lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in s:
            s = s.replace(secret, MASK)
    for pat in _PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: m.group(1) + MASK, s)
        else:
            s = pat.sub(MASK, s)
    return s


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from every record before output."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args - keep raw message
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
