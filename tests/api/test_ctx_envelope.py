"""Signing-side tests for the `_ctx` envelope (P1-04). Offline unit tests."""

import sys

import pytest

from services.authorization.ctx_envelope import (
    BadSignature,
    CtxEnvelope,
    CtxError,
    ExpiredContext,
    MalformedContext,
    NotYetValid,
    WrongAudience,
    WrongTool,
    sign_ctx,
    verify_ctx,
)

SECRET = "test-ctx-secret-not-a-real-secret"
NOW = 1_700_000_000.0


def make_envelope(**overrides: object) -> CtxEnvelope:
    values: dict[str, object] = {
        "request_id": "req-1",
        "trace_id": "trace-1",
        "workspace_id": "ws-1",
        "requester_member_id": "member-sam",
        "armoriq_member_email_ref": "PERSONA_SAM_EMAIL",
        "app_role": "senior",
        "plan_id": "plan-1",
        "step_index": 0,
        "tool": "knowledge_search_team",
        "aud": "memory-mcp",
        "iat": NOW - 60.0,
        "exp": NOW + 600.0,
        "jti": "jti-1",
    }
    values.update(overrides)
    return CtxEnvelope(**values)  # type: ignore[arg-type]


def test_round_trip() -> None:
    envelope = make_envelope()
    assert (
        verify_ctx(
            sign_ctx(envelope, SECRET),
            SECRET,
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_team",
            now=NOW,
        )
        == envelope
    )


def test_signing_is_canonical() -> None:
    assert sign_ctx(make_envelope(), SECRET) == sign_ctx(make_envelope(), SECRET)


def test_tampered_payload_rejected() -> None:
    import base64

    token = sign_ctx(make_envelope(), SECRET)
    payload, _, mac = token.partition(".")
    # Flip a middle payload byte (not the last base64 char, which may only
    # touch spare padding bits and still verify) without re-signing.
    raw = bytearray(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    raw[len(raw) // 2] ^= 0x01
    forged = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    with pytest.raises(CtxError):
        verify_ctx(
            f"{forged}.{mac}",
            SECRET,
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_team",
            now=NOW,
        )


def test_wrong_secret_rejected() -> None:
    token = sign_ctx(make_envelope(), SECRET)
    with pytest.raises(BadSignature):
        verify_ctx(
            token,
            "another-secret",
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_team",
            now=NOW,
        )


def test_wrong_audience_and_tool_rejected() -> None:
    token = sign_ctx(make_envelope(), SECRET)
    with pytest.raises(WrongAudience):
        verify_ctx(
            token,
            SECRET,
            expected_aud="workspace-mcp",
            expected_tool="knowledge_search_team",
            now=NOW,
        )
    with pytest.raises(WrongTool):
        verify_ctx(
            token,
            SECRET,
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_confidential",
            now=NOW,
        )


def test_expiry_and_leeway() -> None:
    token = sign_ctx(make_envelope(), SECRET)
    with pytest.raises(ExpiredContext):
        verify_ctx(
            token,
            SECRET,
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_team",
            now=NOW + 631.0,
        )
    # Inside the 30 s skew leeway the same token still verifies.
    verify_ctx(
        token,
        SECRET,
        expected_aud="memory-mcp",
        expected_tool="knowledge_search_team",
        now=NOW + 629.0,
    )


def test_not_yet_valid_rejected() -> None:
    token = sign_ctx(make_envelope(iat=NOW + 600.0, exp=NOW + 1200.0), SECRET)
    with pytest.raises(NotYetValid):
        verify_ctx(
            token, SECRET, expected_aud="memory-mcp", expected_tool="knowledge_search_team", now=NOW
        )


def test_malformed_tokens_rejected() -> None:
    for bad in ["", "no-separator", ".", "!!!.!!!"]:
        with pytest.raises(CtxError):
            verify_ctx(
                bad,
                SECRET,
                expected_aud="memory-mcp",
                expected_tool="knowledge_search_team",
                now=NOW,
            )


def test_added_field_rejected() -> None:
    import base64
    import hashlib
    import hmac
    import json

    envelope = make_envelope()
    payload = json.loads(json.dumps(envelope.model_dump(mode="json")))
    payload["smuggled"] = "x"
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
    token = (
        f"{base64.urlsafe_b64encode(raw).rstrip(b'=').decode()}."
        f"{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    )
    with pytest.raises(MalformedContext):
        verify_ctx(
            token, SECRET, expected_aud="memory-mcp", expected_tool="knowledge_search_team", now=NOW
        )


def test_empty_secret_never_mints_or_verifies() -> None:
    with pytest.raises(CtxError):
        sign_ctx(make_envelope(), "")
    with pytest.raises(CtxError):
        verify_ctx(
            sign_ctx(make_envelope(), SECRET),
            "",
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_team",
            now=NOW,
        )


def test_ctx_envelope_does_not_import_armoriq_sdk() -> None:
    assert "armoriq_sdk" not in sys.modules
