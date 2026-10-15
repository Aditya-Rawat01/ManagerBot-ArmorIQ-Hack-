"""Redaction before persistence (P3-01). Offline: no database needed.

Sample secrets below are obviously-fake test constants, never real credentials.
"""

import pytest

from services.audit.redaction import (
    MAX_DEPTH,
    MAX_STRING_CHARS,
    REDACTED,
    redact_payload,
    redact_question,
    scrub_text,
)

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
