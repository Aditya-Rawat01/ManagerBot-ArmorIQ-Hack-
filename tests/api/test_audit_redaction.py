"""Redaction before persistence (P3-01). Offline: no database needed.

Sample secrets below are obviously-fake test constants, never real credentials.
"""

import base64
import json
import random

import pytest

from services.audit.redaction import (
    MAX_DEPTH,
    MAX_STRING_CHARS,
    REDACTED,
    redact_payload,
    redact_question,
    scrub_text,
)
from services.contracts import DataTier
from tests.api.audit_fakes import NOW

_FAKE_TOKEN = "ak_test_faketoken000000"
_FAKE_EMAIL = "someone@example.com"
_FAKE_CONN = "postgresql://app:s3cr3t@db.example.com:5432/proxylead"


def test_bearer_and_api_key_values_redacted() -> None:
    payload = {
        "authorization": "Bearer abc.def.ghi",
        "api_key": _FAKE_TOKEN,
        "nested": {"apiKey": _FAKE_TOKEN},
    }
    redacted = redact_payload(payload)
    assert redacted["authorization"] == REDACTED
    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["apiKey"] == REDACTED
    assert _FAKE_TOKEN not in str(redacted)


def test_passwords_credentials_and_connection_strings_redacted() -> None:
    payload = {
        "password": "hunter2",
        "db_credentials": {"user": "app", "passwd": "hunter2"},
        "DATABASE_URL_BACKEND": _FAKE_CONN,
        "dsn": _FAKE_CONN,
    }
    redacted = redact_payload(payload)
    assert redacted["password"] == REDACTED
    # A key named *credentials* redacts the whole subtree (bias to over-redact).
    assert redacted["db_credentials"] == REDACTED
    assert redacted["DATABASE_URL_BACKEND"] == REDACTED
    assert redacted["dsn"] == REDACTED
    assert _FAKE_CONN not in str(redacted)
    assert "hunter2" not in str(redacted)


def test_real_emails_redacted_even_without_suspicious_key() -> None:
    redacted = redact_payload({"note": f"contact {_FAKE_EMAIL} for access"})
    assert redacted["note"] != f"contact {_FAKE_EMAIL} for access"
    assert _FAKE_EMAIL not in str(redacted)


def test_ctx_and_underscore_keys_redacted() -> None:
    payload = {
        "_ctx": "signed-envelope-bytes",
        "_approvals": [{"id": 1}],
        "_policy_context": {"tier": "team"},
        "query": "atlas budget",
    }
    redacted = redact_payload(payload)
    assert redacted["_ctx"] == REDACTED
    assert redacted["_approvals"] == REDACTED
    assert redacted["_policy_context"] == REDACTED
    assert redacted["query"] == "atlas budget"


def test_nested_lists_and_objects_recursed() -> None:
    payload = {
        "steps": [
            {"tool": "search", "arguments": {"query": "q", "token": _FAKE_TOKEN}},
            {"tool": "read", "arguments": {"source_ref": "src-1"}},
        ]
    }
    redacted = redact_payload(payload)
    assert redacted["steps"][0]["arguments"]["token"] == REDACTED
    assert redacted["steps"][1]["arguments"]["source_ref"] == "src-1"
    assert _FAKE_TOKEN not in str(redacted)


def test_sensitive_values_inside_error_text_scrubbed() -> None:
    message = f"connection failed for {_FAKE_EMAIL} with Bearer {_FAKE_TOKEN}: {_FAKE_CONN}"
    scrubbed = scrub_text(message)
    assert _FAKE_EMAIL not in scrubbed
    assert _FAKE_TOKEN not in scrubbed
    assert _FAKE_CONN not in scrubbed
    assert REDACTED in scrubbed


def test_long_strings_truncated() -> None:
    redacted = redact_payload({"excerpt": "x" * (MAX_STRING_CHARS + 100)})
    assert len(redacted["excerpt"]) <= MAX_STRING_CHARS + len("...[truncated]")
    assert redacted["excerpt"].endswith("...[truncated]")


def test_deep_nesting_redacted_whole() -> None:
    nested: object = {"level": "deep", "token": _FAKE_TOKEN}
    for _ in range(MAX_DEPTH + 5):
        nested = {"child": nested}
    # The secret must be gone no matter how deep it sits.
    assert _FAKE_TOKEN not in str(redact_payload(nested))


def test_scalars_pass_through() -> None:
    assert redact_payload(7) == 7
    assert redact_payload(True) is True
    assert redact_payload(None) is None
    assert redact_payload("plain text") == "plain text"


def test_original_payload_not_mutated() -> None:
    payload = {"token": _FAKE_TOKEN, "nested": {"password": "x"}}
    redact_payload(payload)
    assert payload == {"token": _FAKE_TOKEN, "nested": {"password": "x"}}


def test_redact_question_strips_and_scrubs() -> None:
    assert redact_question(f"  what did {_FAKE_EMAIL} decide?  ").startswith("what did ")
    assert _FAKE_EMAIL not in redact_question(f"mail {_FAKE_EMAIL} me")


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "TOKEN",
        "session_secret",
        "CTX_SECRET",
        "password",
        "credentials",
        "authorization",
        "private_key",
    ],
)
def test_sensitive_key_names_case_insensitive(key: str) -> None:
    assert redact_payload({key: "value"})[key] == REDACTED


# --- P3-01 follow-up: redaction gaps -------------------------------------------
# Fake, structurally realistic credentials (never real): Slack, ArmorIQ- and
# Anthropic-style keys, a JWT, HTTP Basic, and credential URLs on any scheme.

_BARE_SECRETS = [
    "xoxb-1234567890-0987654321-AbCdEfGhIjKlMnOp",
    "xoxp-1111111111-2222222222-3333333333-abcdef",
    "xapp-1-A0123456789-1234567890123-abcdef0123456789",
    "xoxe.xoxp-1-Mi0yLTEyMzQ1Ng",
    "ak_live_0123456789abcdefABCDEF",
    "sk_live_0123456789abcdefABCDEF",
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
    "sk-0123456789abcdefghijklmnop",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtLXNhbSJ9.c2lnbmF0dXJlLWZha2U",
]


@pytest.mark.parametrize("secret", _BARE_SECRETS)
def test_bare_tokens_in_text_redacted(secret: str) -> None:
    redacted = redact_payload({"error": f"upstream rejected {secret} (retrying)"})
    assert secret not in str(redacted)
    assert REDACTED in redacted["error"]
    assert redacted["error"].startswith("upstream rejected ")


def test_basic_credentials_redacted_but_prose_kept() -> None:
    assert "dXNlcjpodW50ZXIy" not in scrub_text("Authorization: Basic dXNlcjpodW50ZXIy")
    assert scrub_text("a basic question about budgets") == "a basic question about budgets"


@pytest.mark.parametrize(
    "key",
    [
        "X-Api-Key",
        "x-api-key",
        "auth",
        "Auth",
        "Authorization",
        "cookie",
        "Cookie",
        "set-cookie",
        "Set-Cookie",
        "jwt",
        "JWT",
        "key",
        "secret",
        "password",
        "token",
        "authHeader",
        "accessToken",
        "client-secret",
        "Proxy-Authorization",
    ],
)
def test_more_sensitive_key_names_redacted(key: str) -> None:
    assert redact_payload({key: "opaque-value"})[key] == REDACTED


@pytest.mark.parametrize(
    "key",
    [
        "author",
        "author_member_id",
        "keyword",
        "slack_thread_ts",
        "matched_topic_rule_id",
        "source_id",
        "delegation_id",
        "ledger_entry_id",
        "jti",
        "tool_server_reached",
        "intent_expires_at",
    ],
)
def test_audit_field_names_not_over_redacted(key: str) -> None:
    assert redact_payload({key: "kept-value"})[key] == "kept-value"


@pytest.mark.parametrize(
    "url",
    [
        "postgres://app:hunter2pw@db/proxylead",
        "postgresql+psycopg://app:hunter2pw@db.internal:5432/x",
        "redis://:hunter2pw@cache:6379/0",
        "rediss://default:hunter2pw@cache.internal:6380",
        "amqp://guest:hunter2pw@rabbit/vhost",
        "https://svc:hunter2pw@intranet/api",
        "ftp://user:hunter2pw@files",
        "mongodb+srv://u:hunter2pw@cluster0",
    ],
)
def test_credential_urls_redacted_for_any_scheme_and_host(url: str) -> None:
    redacted = redact_payload({"detail": f"could not connect to {url} after 3 tries"})
    assert "hunter2pw" not in str(redacted)


def test_emails_and_tokens_used_as_dict_keys_redacted() -> None:
    payload = {
        "maya@example.com": {"count": 1},
        "sam@example.com": {"count": 2},
        "xoxb-1234567890-0987654321-AbCdEfGhIjKl": "seen",
    }
    redacted = redact_payload(payload)
    text = json.dumps(redacted)
    assert "@example.com" not in text and "xoxb-" not in text
    assert len(redacted) == 3  # masked keys never collapse into one entry
    counts = sorted(value["count"] for value in redacted.values() if isinstance(value, dict))
    assert counts == [1, 2]


def test_sets_frozensets_and_bytes_redacted_and_json_ready() -> None:
    payload = {
        "tags": {"plain", "xoxb-1234567890-0987654321-AbCdEfGhIjKl"},
        "frozen": frozenset({"someone@example.com", "ok"}),
        "raw": b"password=hunter2pw",
        "buffer": bytearray(b"Bearer abc.def.ghi"),
        "view": memoryview(b"sk-0123456789abcdefghijklmnop"),
        "when": NOW,
        "tier": DataTier.CONFIDENTIAL,
        "other": object(),
    }
    redacted = redact_payload(payload)
    text = json.dumps(redacted)  # JSON-ready: no sets/bytes/objects survive
    for leaked in ("xoxb-", "someone@example.com", "hunter2pw", "abc.def.ghi", "sk-0123"):
        assert leaked not in text
    assert "plain" in redacted["tags"] and "ok" in redacted["frozen"]
    assert redacted["raw"] == redacted["buffer"] == redacted["view"] == REDACTED
    assert redacted["when"] == NOW.isoformat()
    assert redacted["tier"] == "confidential"
    assert redacted["other"] == REDACTED


# --- seeded randomized property test (stdlib only) ------------------------------

_SENSITIVE_KEYS = (
    "token",
    "X-Api-Key",
    "auth",
    "Authorization",
    "set-cookie",
    "jwt",
    "key",
    "client_secret",
    "Password",
    "accessToken",
    "_ctx",
    "_approvals",
)
_BENIGN_KEYS = ("note", "detail", "summary", "items", "meta", "source_id", "author", "step")
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_URL_SHAPES = (
    ("postgresql", "db.internal:5432/proxylead"),
    ("redis", "cache:6379/0"),
    ("amqp", "rabbit/vhost"),
    ("https", "intranet/api"),
    ("mysql", "localhost"),
)


def _rand(rng: random.Random, size: int, alphabet: str = _ALNUM) -> str:
    return "".join(rng.choice(alphabet) for _ in range(size))


def _make_secret(rng: random.Random) -> tuple[str, str]:
    """(text to plant, needle that must never survive redaction)."""
    kind = rng.randrange(8 + len(_URL_SHAPES))
    if kind == 0:
        token = f"xox{rng.choice('bpar')}-{rng.randrange(10**9, 10**10)}-{_rand(rng, 24)}"
        return token, token
    if kind == 1:
        token = f"xapp-1-A{_rand(rng, 10)}-{rng.randrange(10**12)}-{_rand(rng, 32)}"
        return token, token
    if kind == 2:
        token = f"ak_live_{_rand(rng, 24)}"
        return token, token
    if kind == 3:
        token = f"sk-ant-api03-{_rand(rng, 32)}"
        return token, token
    if kind == 4:
        token = f"eyJ{_rand(rng, 20)}.eyJ{_rand(rng, 30)}.{_rand(rng, 24)}"
        return token, token
    if kind == 5:
        blob = base64.b64encode(f"user{rng.randrange(99)}:{_rand(rng, 14)}".encode()).decode()
        return f"Basic {blob}", blob
    if kind == 6:
        token = _rand(rng, 28)
        return f"Bearer {token}", token
    if kind == 7:
        email = f"{_rand(rng, 8).lower()}@example.com"
        return email, email
    password = f"pw{_rand(rng, 16)}"
    scheme, host = _URL_SHAPES[kind - 8]
    return f"{scheme}://svc:{password}@{host}", password


def _benign(rng: random.Random) -> object:
    return rng.choice(["alpha", 42, True, None, "src-001", 3.5, "plain words"])


def _plant(rng: random.Random, secret: str, depth: int) -> object:
    """Hide ``secret`` at a random spot of a random nested structure."""
    if depth == 0:
        mode = rng.randrange(6)
        if mode == 0:
            return {rng.choice(_SENSITIVE_KEYS): secret, rng.choice(_BENIGN_KEYS): _benign(rng)}
        if mode == 1:
            return {secret: _benign(rng)}  # the secret as a dict key
        if mode == 2:
            return {rng.choice(_BENIGN_KEYS): f"error near {secret} (retry {rng.randrange(9)})"}
        if mode == 3:
            return {secret, "plain"}
        if mode == 4:
            return secret.encode()
        return frozenset({f"x {secret} y"})
    container = rng.randrange(3)
    inner = _plant(rng, secret, depth - 1)
    noise = [_benign(rng) for _ in range(rng.randrange(3))]
    if container == 0:
        payload = {key: _benign(rng) for key in rng.sample(_BENIGN_KEYS, 2)}
        payload[rng.choice(_BENIGN_KEYS) + "_inner"] = inner
        return payload
    if container == 1:
        items = [*noise, inner]
        rng.shuffle(items)
        return items
    return (*noise, inner)


@pytest.mark.parametrize("seed", range(40))
def test_redaction_property_planted_secrets_never_survive(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not crypto
    planted: list[str] = []
    payload: dict[str, object] = {"marker": f"keep-me-{seed}"}
    for index in range(rng.randrange(3, 7)):
        text, needle = _make_secret(rng)
        planted.append(needle)
        payload[f"branch_{index}"] = _plant(rng, text, rng.randrange(0, 5))
    redacted = redact_payload(payload)
    serialized = json.dumps(redacted)  # always JSON-ready
    for needle in planted:
        assert needle not in serialized, f"seed {seed} leaked a planted secret"
    assert redacted["marker"] == f"keep-me-{seed}"  # benign data survives
