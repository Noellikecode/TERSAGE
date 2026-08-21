"""Redaction applied at every log and audit sink.

Two layers, because either one alone fails:

* **Key-name redaction** catches structured fields whose name says they are
  sensitive -- ``password``, ``document_text``, ``prompt``, ``ssn``.
* **Value-pattern redaction** catches sensitive values that arrived under an
  innocent key -- an email address inside a free-text note, a bucket URI in an
  error message, an API key pasted into a config dump.

Street addresses are deliberately *not* redacted: the address is the subject of
the system, and redacting it would make every log useless.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

REDACTED: Final[str] = "[REDACTED]"
MAX_LOGGED_STRING: Final[int] = 2000

#: Field names whose contents never belong in a log or an audit record.
_SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|authorization|auth|cookie|"
    r"session|private[_-]?key|prompt|completion|model[_-]?output|document[_-]?text|"
    r"raw[_-]?text|narrative[_-]?draft|"
    r"patient|medical|diagnosis|ssn|social[_-]?security|dob|date[_-]?of[_-]?birth|"
    r"phone|email|occupant[_-]?name|resident[_-]?name)",
    re.IGNORECASE,
)

_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # email
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # US phone
    re.compile(r"\b(?:\+1[ .\-]?)?\(?\d{3}\)?[ .\-]?\d{3}[ .\-]?\d{4}\b"),
    # SSN
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Google API key
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    # Bearer / JWT
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    # Cloud Storage / internal bucket URIs -- source internals never surface
    re.compile(r"\bgs://[A-Za-z0-9._\-/]+"),
)


def redact_text(value: str) -> str:
    """Redact sensitive substrings and bound the length."""
    redacted = value
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    if len(redacted) > MAX_LOGGED_STRING:
        redacted = redacted[:MAX_LOGGED_STRING] + "...[truncated]"
    return redacted


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact_value(key: str, value: Any, *, depth: int = 0) -> Any:
    """Redact one key/value pair, recursing into containers."""
    if is_sensitive_key(key):
        return REDACTED
    if depth > 6:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(str(k), v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact_value(key, item, depth=depth + 1) for item in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return redact_text(str(value))


def redact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a whole structured payload."""
    return {str(k): redact_value(str(k), v) for k, v in payload.items()}


def redact_str_mapping(payload: Mapping[str, str]) -> dict[str, str]:
    """Redact a string-only mapping, preserving the type."""
    return {
        str(k): (REDACTED if is_sensitive_key(str(k)) else redact_text(str(v)))
        for k, v in payload.items()
    }
