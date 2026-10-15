"""Recursive payload redaction before persistence (P3-01, PRD v2 §10.1).

Everything stored in audit-event payloads, unanswered-question records, and
logs passes through here first. Redaction is deliberately biased toward
over-redacting: a masked display string lost to ``[redacted]`` is a minor
inconvenience, while a stored token or email is a security incident.

Redacted (value replaced, key kept so the field is still identifiable):

- keys containing (case-insensitive): ``token``, ``secret``, ``passwd``,
  ``password``, ``credential``, ``api_key``, ``apikey``, ``authorization``,
  ``bearer``, ``private_key``, ``dsn``, ``connectionstring``;
- the exact key ``_ctx`` and any other ``_``-prefixed key (reserved for
  trusted identity envelopes and SDK metadata — never evidence);
- string values matching an email address, a ``Bearer`` credential, or a
  database connection string, wherever they appear (including error text).

Structure is preserved (dicts stay dicts, lists stay lists) so downstream
readers keep working. Strings longer than :data:`MAX_STRING_CHARS` are
truncated: audit payloads are metadata, never document content, and an
unbounded string is usually a pasted snippet. Nesting deeper than
:data:`MAX_DEPTH` redacts whole — audit payloads are shallow by construction.
"""

import re
from typing import Any

REDACTED = "[redacted]"
MAX_STRING_CHARS = 4000
MAX_DEPTH = 20

_KEY_SUBSTRINGS = (
    "token",
    "secret",
    "passwd",
    "password",
    "credential",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "private_key",
    "dsn",
    "connectionstring",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]+", re.IGNORECASE)
_CONN_RE = re.compile(
    r"\b(?:postgres(?:ql)?|postgresql|mysql|sqlite|mongodb)(?:\+\w+)?://[^\s'\";]+",
    re.IGNORECASE,
)


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if key == "_ctx" or key.startswith("_"):
        return True
    return any(part in lowered for part in _KEY_SUBSTRINGS)


def scrub_text(value: str) -> str:
    """Mask secret-bearing patterns inside free text (e.g. error messages)."""
    scrubbed = _EMAIL_RE.sub(REDACTED, value)
    scrubbed = _BEARER_RE.sub(REDACTED, scrubbed)
    scrubbed = _CONN_RE.sub(REDACTED, scrubbed)
    if len(scrubbed) > MAX_STRING_CHARS:
        return scrubbed[:MAX_STRING_CHARS] + "...[truncated]"
    return scrubbed


def redact_payload(value: Any, _depth: int = 0) -> Any:
    """Return a redacted deep copy of ``value`` (dicts, lists, scalars)."""
    if _depth > MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            name = key if isinstance(key, str) else str(key)
            if _sensitive_key(name):
                redacted[name] = REDACTED
            else:
                redacted[name] = redact_payload(item, _depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def redact_question(question: str) -> str:
    """Redact a user question before it reaches ``unanswered_questions``.

    The question itself is the requester's own text (not protected document
    content), but secrets or emails pasted into it must not persist.
    """
    return scrub_text(question.strip())
