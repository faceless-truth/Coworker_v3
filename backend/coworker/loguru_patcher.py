"""Loguru patcher that redacts secret-bearing fields from log record extras.

Wired in coworker.logging.setup_logging() via logger.configure(patcher=...)
so every log emission passes through this function before reaching any
sink. The patcher mutates record["extra"] in place — Loguru's contract
for patcher callables.

Match strategy
--------------
Case-insensitive *substring* match against a fixed list of patterns.
A key is redacted if any pattern in _SENSITIVE_PATTERNS appears
anywhere in key.lower(). The substring approach is deliberately broad:
it catches present forms ("refresh_token") and future variations
("ms_refresh_token", "azure_client_secret") without having to maintain
an exhaustive enumeration. Over-redaction is acceptable here — a
redacted "status_code" or "token_type" in a log line is a minor
annoyance; an un-redacted "refresh_token" is a security incident.

Recursion
---------
dict values that are themselves dicts or lists are walked so logging
an entire request body or a Microsoft token-exchange response yields
nested redaction. Tuples and other iterables are NOT walked — they're
rare in log extras and skipping them avoids performance surprises on
hot paths.
"""
from __future__ import annotations

from typing import Any


_REDACTED = "[REDACTED]"

_SENSITIVE_PATTERNS: tuple[str, ...] = (
    "client_secret",
    "refresh_token",
    "access_token",
    "id_token",
    "code_verifier",
    "password",
    "token",
    "secret",
    "code",
)


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(p in k for p in _SENSITIVE_PATTERNS)


def _scrub(value: Any) -> Any:
    """Return value with any nested sensitive keys redacted.

    Returns a new container for dict/list inputs (does not mutate).
    Returns the value unchanged for everything else.
    """
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_sensitive(k) else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def redact_secrets(record: dict[str, Any]) -> None:
    """Loguru patcher. Mutates record["extra"] in place to redact
    sensitive values keyed by names matching _SENSITIVE_PATTERNS."""
    extras = record.get("extra")
    if not isinstance(extras, dict):
        return
    for k, v in list(extras.items()):
        if _is_sensitive(k):
            extras[k] = _REDACTED
        else:
            extras[k] = _scrub(v)
