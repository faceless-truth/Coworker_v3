"""Loguru patcher that redacts secret-bearing fields from log record extras.

Wired in coworker.logging.setup_logging() via logger.configure(patcher=...)
so every log emission passes through this function before reaching any
sink. The patcher mutates record["extra"] in place — Loguru's contract
for patcher callables.

Match strategy
--------------
Exact case-insensitive key match against PATTERNS (a frozenset of
lowercase strings). A key is redacted iff key.lower() is in PATTERNS.
The clarity is the feature: there is no possibility of unexpectedly
redacting `status_code`, `error_code`, or `country_code` because they
share a substring with a sensitive name. The trade-off is that the
pattern list must enumerate every name we want to redact, including
variants — when a new field is added that needs redaction, add it
here.

PATTERNS includes both generic OAuth/auth field names (client_secret,
refresh_token, etc.) and the specific column/argument names this
codebase uses (azure_client_secret, ms_refresh_token_ciphertext,
etc.). The *_ciphertext entries are defensive: there is no legitimate
reason to log encrypted bytes, and including them removes one more
class of mistake from the worry budget.

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

PATTERNS: frozenset[str] = frozenset(
    {
        # Generic OAuth / auth field names
        "client_secret",
        "refresh_token",
        "access_token",
        "id_token",
        "code",
        "code_verifier",
        "password",
        "token",
        "secret",
        # Field names this codebase actually uses
        "azure_client_secret",
        "azure_client_secret_ciphertext",
        "ms_access_token_ciphertext",
        "ms_refresh_token_ciphertext",
        "session_jwt_secret",
        "master_encryption_key",
    }
)


def _is_sensitive(key: str) -> bool:
    return key.lower() in PATTERNS


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
    sensitive values keyed by names in PATTERNS (case-insensitive)."""
    extras = record.get("extra")
    if not isinstance(extras, dict):
        return
    for k, v in list(extras.items()):
        if _is_sensitive(k):
            extras[k] = _REDACTED
        else:
            extras[k] = _scrub(v)
