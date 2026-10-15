"""Web demo session endpoint tests (P1-03). Offline unit tests."""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from apps.api.identity import SESSION_COOKIE_NAME, SESSION_TTL_S, mint_session_token
from services.contracts import ErrorResponse, PersonaKey
from tests.api.conftest import make_settings

PASSCODE = "demo-passcode-for-tests-only"
EMAIL = "maya@example.com"


def make_client(**overrides: object) -> TestClient:
    from apps.api.main import create_app

    values: dict[str, object] = {
        "demo_passcode": PASSCODE,
        "session_secret": "test-session-secret-not-real",
        "persona_maya_email": EMAIL,
        "persona_sam_email": "sam@example.com",
        "persona_priya_email": "priya@example.com",
    }
    values.update(overrides)
    return TestClient(create_app(make_settings(**values)), raise_server_exceptions=False)


def sign_in(client: TestClient, persona: str = "maya", passcode: str = PASSCODE):
    return client.post("/api/v1/session", json={"persona": persona, "passcode": passcode})


def test_sign_in_sets_cookie_with_required_flags() -> None:
    response = sign_in(make_client())
    assert response.status_code == 200
    body = response.json()
    assert body["persona"] == "maya"
    assert body["masked_email"] == "m***@e***.com"
    assert EMAIL not in response.text
    raw = response.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}=" in raw
    assert "HttpOnly" in raw
    assert "SameSite=Lax" in raw or "SameSite=lax" in raw
    # Test env runs over plain HTTP, so Secure must be off here (on in prod).
    assert "Secure" not in raw


def test_secure_flag_on_in_production() -> None:
    response = sign_in(make_client(app_env="production"))
    assert response.status_code == 200
    assert "Secure" in response.headers.get("set-cookie", "")


def test_wrong_passcode_rejected_without_oracle() -> None:
    client = make_client()
    bad_secret = sign_in(client, passcode="wrong")
    unknown_persona = client.post("/api/v1/session", json={"persona": "maya", "passcode": "wrong"})
    for response in (bad_secret, unknown_persona):
        assert response.status_code == 401
        assert ErrorResponse.model_validate(response.json()).error.code == "unauthenticated"
    # Same code and message either way (request_ids differ per request, as designed).
    assert bad_secret.json()["error"]["code"] == unknown_persona.json()["error"]["code"]
    assert bad_secret.json()["error"]["message"] == unknown_persona.json()["error"]["message"]
    assert EMAIL not in bad_secret.text


def test_non_ascii_passcode_is_a_plain_401() -> None:
    response = sign_in(make_client(), passcode="päss")
    assert response.status_code == 401
    assert ErrorResponse.model_validate(response.json()).error.code == "unauthenticated"


def test_sign_in_disabled_without_secrets() -> None:
    for kwargs in ({"demo_passcode": None}, {"session_secret": None}):
        response = sign_in(make_client(**kwargs))
        assert response.status_code == 503
        assert ErrorResponse.model_validate(response.json()).error.code == "sign_in_unavailable"


def test_app_starts_and_health_ok_without_secrets() -> None:
    from apps.api.main import create_app

    client = TestClient(create_app(make_settings()), raise_server_exceptions=False)
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 200


def test_session_round_trip_and_sign_out() -> None:
    # Browser flow: the client jar carries the cookie, no manual headers.
    client = make_client()
    assert sign_in(client).status_code == 200
    me = client.get("/api/v1/session")
    assert me.status_code == 200
    assert me.json()["persona"] == "maya"
    assert EMAIL not in me.text
    out = client.delete("/api/v1/session")
    assert out.status_code == 204
    cleared = out.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in cleared
    assert "Max-Age=0" in cleared or "expires=" in cleared.lower()
    # Jar no longer holds a usable session.
    assert client.get("/api/v1/session").status_code == 401


def _cookie_header(token: str) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={token}"}


def _tamper(token: str) -> str:
    """Flip a payload byte without re-signing. Deterministic: unlike flipping the
    last base64 char (which may only touch spare padding bits and verify fine),
    a middle payload byte always changes the HMAC input."""
    import base64

    payload_b64, _, mac_b64 = token.partition(".")
    raw = bytearray(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    raw[len(raw) // 2] ^= 0x01
    forged_payload = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    return f"{forged_payload}.{mac_b64}"


def test_tampered_and_expired_cookie_rejected() -> None:
    # Fresh client: the jar is empty, so only the explicit header is evaluated.
    secret = "test-session-secret-not-real"
    valid = mint_session_token(PersonaKey.MAYA, secret, time.time())
    expired = mint_session_token(PersonaKey.MAYA, secret, time.time() - SESSION_TTL_S - 60.0)
    for bad in (_tamper(valid), expired, "not-a-token"):
        client = make_client()
        response = client.get("/api/v1/session", headers=_cookie_header(bad))
        assert response.status_code == 401
        assert ErrorResponse.model_validate(response.json()).error.code == "unauthenticated"


def test_missing_session_is_401() -> None:
    assert make_client().get("/api/v1/session").status_code == 401


def test_no_email_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    client = make_client()
    with caplog.at_level(logging.INFO):
        sign_in(client)
        sign_in(client, passcode="wrong")
        client.get("/api/v1/session")
    assert EMAIL not in caplog.text
