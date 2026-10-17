"""Recursive payload redaction before persistence (P3-01, PRD v2 §10.1).

Everything stored in audit-event payloads, unanswered-question records, and
logs passes through here first. Redaction is deliberately biased toward
over-redacting: a masked display string lost to ``[redacted]`` is a minor
inconvenience, while a stored token or email is a security incident.

Redacted (value replaced, key kept so the field is still identifiable). Key
names are normalized first: camelCase is split, ``-`` and spaces become
``_``, and matching is case-insensitive (``X-Api-Key`` -> ``x_api_key``):

- keys containing ``token``, ``secret``, ``passwd``, ``password``,
  ``credential``, ``api_key``, ``apikey``, ``authorization``, ``bearer``,
  ``private_key``, ``privatekey``, ``dsn``, ``connectionstring``, ``cookie``,
  or ``jwt``;
- keys with a ``_``-separated part equal to ``auth``, ``key``, ``jwt``,
  ``cookie``, ``secret``, ``password``, or ``token`` (so ``auth``, ``key``,
  ``set-cookie``, ``auth_header`` are caught while ``author`` is not);
- the exact key ``_ctx`` and any other ``_``-prefixed key (reserved for
  trusted identity envelopes and SDK metadata — never evidence);
- anywhere in string values, dict KEYS, or error text: email addresses,
  ``Bearer``/``Basic`` credentials, bare Slack tokens (``xox?-``,
  ``xapp-``), ``ak_live_``/``sk_live_``-style, ``sk-`` and Google (``AIza``)
  API keys, JWTs
  (``eyJ…​.…​.…``), any ``scheme://user:password@host`` URL (any scheme, any
  host, TLD or not), and database/broker connection strings
  (``postgres://``, ``redis://``, …).

Structure is preserved (dicts stay dicts, lists stay lists; tuples, sets and
frozensets become lists so the result is JSON-serializable). Bytes are never
decoded — they redact whole. Unknown object types redact whole too (their
``str()`` could carry anything); datetimes become ISO-8601 strings and enums
their values. Strings longer than :data:`MAX_STRING_CHARS` are truncated:
audit payloads are metadata, never document content, and an unbounded string
is usually a pasted snippet. Nesting deeper than :data:`MAX_DEPTH` redacts
whole — audit payloads are shallow by construction.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
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
    "privatekey",
    "dsn",
    "connectionstring",
    "cookie",
    "jwt",
)
# Short names that are only sensitive as a whole `_`-separated part
# (`auth` must not catch `author`; `key` must not catch `keyword`).
_KEY_PARTS = frozenset({"auth", "key", "jwt", "cookie", "secret", "password", "token"})

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATORS = re.compile(r"[\s\-.]+")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]+", re.IGNORECASE)
# HTTP Basic: the blob must look like base64 (a digit, `+`, `/`, `=`, or an
# interior capital) so prose such as "basic question" survives. Only the
# scheme name is case-insensitive; the blob's classes must stay case-exact.
_BASIC_RE = re.compile(
    r"\b(?i:basic)\s+(?=[A-Za-z0-9+/]*(?:[0-9+/=]|[a-z][A-Z]))[A-Za-z0-9+/]{6,}={0,2}"
)
# Credentials embedded in any URL: scheme://user:password@host (user may be
# empty, as in redis://:password@host; host may have no TLD).
_URL_CREDENTIALS_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/?#@'\"<>]*:[^\s@'\"<>]*@[^\s'\"<>;,]*"
)
_CONN_RE = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|sqlite|mongodb|redis|rediss|amqps?)"
    r"(?:\+\w+)?://[^\s'\";]+",
    re.IGNORECASE,
)
_SLACK_TOKEN_RE = re.compile(r"\b(?:xox[a-z](?:\.xox[a-z])?|xapp)-[A-Za-z0-9-]{6,}")
_PREFIXED_KEY_RE = re.compile(r"\b(?:ak|sk|pk|rk)_(?:live|test|prod)_[A-Za-z0-9_\-]{6,}")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")
# Google API keys (`GEMINI_API_KEY`, ADR-032): "AIza" + 35 URL-safe characters.
_GOOGLE_KEY_RE = re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]*")

# Order matters: URL credentials before emails (user:pw@host looks like an
# email tail), whole connection strings before their pieces.
_VALUE_PATTERNS = (
    _URL_CREDENTIALS_RE,
    _CONN_RE,
    _EMAIL_RE,
    _BEARER_RE,
    _BASIC_RE,
    _SLACK_TOKEN_RE,
    _PREFIXED_KEY_RE,
    _SK_KEY_RE,
    _GOOGLE_KEY_RE,
    _JWT_RE,
)


def _normalize_key(key: str) -> str:
    return _KEY_SEPARATORS.sub("_", _CAMEL_BOUNDARY.sub("_", key)).lower()


def _sensitive_key(key: str) -> bool:
    if key.startswith("_"):  # includes `_ctx`
        return True
    normalized = _normalize_key(key)
    if any(part in normalized for part in _KEY_SUBSTRINGS):
        return True
    return any(part in _KEY_PARTS for part in normalized.split("_"))


def scrub_text(value: str) -> str:
    """Mask secret-bearing patterns inside free text (e.g. error messages)."""
    scrubbed = value
    for pattern in _VALUE_PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    if len(scrubbed) > MAX_STRING_CHARS:
        return scrubbed[:MAX_STRING_CHARS] + "...[truncated]"
    return scrubbed


def _redact_mapping(value: Mapping[Any, Any], depth: int) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        raw = key if isinstance(key, str) else str(key)
        # Keys are text too: an email or token used as a key is masked, and
        # collisions after masking keep every entry.
        name = scrub_text(raw)
        if name in redacted:
            suffix = 2
            while f"{name}#{suffix}" in redacted:
                suffix += 1
            name = f"{name}#{suffix}"
        redacted[name] = REDACTED if _sensitive_key(raw) else redact_payload(item, depth + 1)
    return redacted


def redact_payload(value: Any, _depth: int = 0) -> Any:
    """Return a redacted, JSON-ready deep copy of ``value``."""
    if _depth > MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Enum):
        return redact_payload(value.value, _depth + 1)
    if isinstance(value, Mapping):
        return _redact_mapping(value, _depth)
    if isinstance(value, list | tuple):
        return [redact_payload(item, _depth + 1) for item in value]
    if isinstance(value, set | frozenset):
        items = [redact_payload(item, _depth + 1) for item in value]
        return sorted(items, key=repr)
    if isinstance(value, datetime | date):
        return value.isoformat()
    # bytes / bytearray / memoryview and unknown objects: never decoded or
    # str()-ed — they may carry raw credentials.
    return REDACTED


def redact_question(question: str) -> str:
    """Redact a user question before it reaches ``unanswered_questions``.

    The question itself is the requester's own text (not protected document
    content), but secrets or emails pasted into it must not persist.
    """
    return scrub_text(question.strip())
