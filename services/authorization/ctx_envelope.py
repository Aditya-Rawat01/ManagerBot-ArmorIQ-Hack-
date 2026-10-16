"""Backend-signed `_ctx` identity envelope (P1-04, D-04/ADR-020).

Shared by the backend (signs) and the MCP tool server (verifies). Imports only
the standard library and pydantic, so the toolserver test job can import this
module without ``armoriq_sdk`` installed.

Token shape is ``base64url(payload).base64url(mac)`` where ``payload`` is the
canonical JSON of :class:`CtxEnvelope` (sorted keys, no whitespace, UTF-8) and
``mac`` is HMAC-SHA256 over the encoded payload. Per D-04 the envelope carries
no raw member email, only ``armoriq_member_email_ref`` (the env-var name holding
it, e.g. ``PERSONA_MAYA_EMAIL``); only the gateway resolves that reference.

An empty or unset secret always rejects: :func:`sign_ctx` refuses to mint and
:func:`verify_ctx` rejects every token (fail closed).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Small clock-skew allowance between backend and tool server (P1-04 §14).
DEFAULT_LEEWAY_S = 30.0


class CtxError(Exception):
    """Base class for every `_ctx` mint/verification failure."""


class MalformedContext(CtxError):
    """Token is not ``payload.mac`` with a decodable, schema-valid payload."""


class BadSignature(CtxError):
    """HMAC does not match (wrong secret or tampered payload)."""


class WrongAudience(CtxError):
    """``aud`` does not equal the endpoint receiving the call."""


class WrongTool(CtxError):
    """``tool`` does not equal the tool being invoked."""


class NotYetValid(CtxError):
    """Call arrived before ``iat`` (beyond the skew leeway)."""


class ExpiredContext(CtxError):
    """``exp`` has passed (beyond the skew leeway)."""


class LifetimeTooLong(CtxError):
    """``exp - iat`` exceeds :data:`MAX_CTX_LIFETIME_S` (rejected as invalid)."""


# Upper bound on a token's validity window. The gateway mints 300 s tokens
# (armoriq_gateway.CTX_TTL_S); anything far longer is not one of ours and must
# not stay replayable-by-capture for hours. Generous headroom over 300 s keeps
# clock-skewed or test-minted tokens working.
MAX_CTX_LIFETIME_S = 900.0


class CtxEnvelope(BaseModel):
    """Identity envelope the backend signs per tool call (D-04/ADR-020)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    requester_member_id: str = Field(min_length=1)
    # Env-var NAME holding the member's ArmorIQ email (never the address itself).
    armoriq_member_email_ref: str = Field(min_length=1)
    # Informational label only; ArmorIQ policy (not this label) authorizes tools.
    app_role: Literal["manager", "senior", "junior"]
    plan_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    tool: str = Field(min_length=1)
    # Receiving MCP endpoint name (e.g. "memory-mcp").
    aud: str = Field(min_length=1)
    iat: float
    exp: float
    # Application-generated invocation ID (Arch §5.1); replay-checked by the
    # tool server, NOT an ArmorIQ token claim.
    jti: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_window(self) -> CtxEnvelope:
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        return self


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii") + b"=" * (-len(data) % 4))


def _canonical(envelope: CtxEnvelope) -> bytes:
    return json.dumps(
        envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_ctx(envelope: CtxEnvelope, secret: str) -> str:
    """Mint a ``_ctx`` token. Raises :class:`CtxError` when ``secret`` is empty."""
    if not secret:
        raise CtxError("refusing to sign _ctx with an empty secret")
    payload = _canonical(envelope)
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(mac)}"


def verify_ctx(
    token: str,
    secret: str,
    *,
    expected_aud: str,
    expected_tool: str,
    now: float | None = None,
    leeway_s: float = DEFAULT_LEEWAY_S,
) -> CtxEnvelope:
    """Verify a ``_ctx`` token, returning the envelope or raising a typed error.

    ``now`` defaults to :func:`time.time` and is injectable for tests.
    """
    if not secret:
        raise CtxError("no secret configured; rejecting _ctx")
    if not isinstance(token, str) or "." not in token:
        raise MalformedContext("token is not payload.mac")
    encoded_payload, _, encoded_mac = token.partition(".")
    try:
        payload = _b64decode(encoded_payload)
        mac = _b64decode(encoded_mac)
    except Exception as exc:
        raise MalformedContext("token parts are not base64url") from exc
    expected_mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise BadSignature("HMAC mismatch")
    try:
        envelope = CtxEnvelope.model_validate_json(payload)
    except Exception as exc:
        raise MalformedContext("payload is not a valid envelope") from exc
    if envelope.aud != expected_aud:
        raise WrongAudience("aud mismatch")
    if envelope.tool != expected_tool:
        raise WrongTool("tool mismatch")
    current = time.time() if now is None else now
    if current < envelope.iat - leeway_s:
        raise NotYetValid("context used before iat")
    if current > envelope.exp + leeway_s:
        raise ExpiredContext("context expired")
    # After the time checks, so an expired token still reports "expired".
    if envelope.exp - envelope.iat > MAX_CTX_LIFETIME_S:
        raise LifetimeTooLong("context lifetime exceeds the maximum")
    return envelope


class ReplayGuard(Protocol):
    """Ledger-backed in production (P2-07 ``seen_jti``); in-memory below for tests."""

    def seen_jti(self, jti: str) -> bool:
        """Return True when ``jti`` was already used (caller must reject: replay)."""
        ...


class InMemoryReplayGuard:
    """Non-production :class:`ReplayGuard` for tests. NOT for deployment."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen_jti(self, jti: str) -> bool:
        if jti in self._seen:
            return True
        self._seen.add(jti)
        return False
