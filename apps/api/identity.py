"""Trusted requester identity (P1-03, Arch §5.4 layer 1).

Identity comes only from server-side mapping: a Slack user ID or a signed web
session cookie resolves to a member whose raw ArmorIQ email lives in settings.
Chat text, display names, and request bodies are never identity inputs (R-10).

Member IDs are stable ``m-<persona>`` values from env (D-20/ADR-021); the P2-03
seed must reuse them for its ``members`` rows. Raw emails never leave this
module except through :func:`armoriq_email_for`, which only
``services/authorization/armoriq_gateway.py`` may call (test-enforced).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from apps.api.core.config import Settings
from services.contracts import AppRole, AuthenticatedUser, PersonaKey
from services.contracts.api_session import SessionInfo

SESSION_COOKIE_NAME = "proxylead_session"
SESSION_TTL_S = 12 * 3600


@dataclass(frozen=True)
class MemberRecord:
    """One demo member as configured. Raw email stays server-side only."""

    persona: PersonaKey
    member_id: str
    display_name: str
    app_role: AppRole
    email: str
    email_ref: str
    slack_user_id: str | None


_PERSONAS: tuple[tuple[PersonaKey, str, AppRole, str, str], ...] = (
    (PersonaKey.MAYA, "Maya", AppRole.MANAGER, "PERSONA_MAYA_EMAIL", "SLACK_USER_MAYA_ID"),
    (PersonaKey.SAM, "Sam", AppRole.SENIOR, "PERSONA_SAM_EMAIL", "SLACK_USER_SAM_ID"),
    (PersonaKey.PRIYA, "Priya", AppRole.JUNIOR, "PERSONA_PRIYA_EMAIL", "SLACK_USER_PRIYA_ID"),
)


def build_member_table(settings: Settings) -> dict[PersonaKey, MemberRecord]:
    """Build the immutable member table. A member without an email is unavailable."""
    env = settings.model_dump(mode="python")
    table: dict[PersonaKey, MemberRecord] = {}
    for persona, display_name, app_role, email_key, slack_key in _PERSONAS:
        email = env.get(email_key.lower())
        raw_email = email.get_secret_value() if email is not None else ""
        if not raw_email:
            continue
        slack_id = env.get(slack_key.lower()) or None
        table[persona] = MemberRecord(
            persona=persona,
            member_id=f"m-{persona.value}",
            display_name=display_name,
            app_role=app_role,
            email=raw_email,
            email_ref=email_key,
            slack_user_id=slack_id,
        )
    return table


def mask_email(address: str) -> str:
    """Mask an address to first-char form (``s***@e***.com``). Never echoes raw input on failure."""
    try:
        local, _, domain = address.partition("@")
        labels = domain.split(".")
        if not local or len(labels) < 2 or not all(labels):
            return "***"
        masked_labels = [f"{label[0]}***" for label in labels[:-1]] + [labels[-1]]
        return f"{local[0]}***@{'.'.join(masked_labels)}"
    except Exception:
        return "***"


def _authenticated(record: MemberRecord) -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id=record.member_id,
        persona=record.persona,
        display_name=record.display_name,
        app_role=record.app_role,
        masked_email=mask_email(record.email),
        slack_user_id=record.slack_user_id,
    )


def resolve_slack_user(
    members: dict[PersonaKey, MemberRecord],
    team_id: str | None,
    slack_user_id: str | None,
    expected_team_id: str | None,
) -> AuthenticatedUser | None:
    """Resolve a Slack event sender. Anything unmapped (or a wrong team) is None."""
    if not expected_team_id or team_id != expected_team_id or not slack_user_id:
        return None
    for record in members.values():
        if record.slack_user_id and record.slack_user_id == slack_user_id:
            return _authenticated(record)
    return None


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii") + b"=" * (-len(data) % 4))


def mint_session_token(
    persona: PersonaKey, secret: str, now: float, ttl_s: float = SESSION_TTL_S
) -> str:
    """Mint a signed session token. Raises ValueError when ``secret`` is empty."""
    if not secret:
        raise ValueError("refusing to mint a session with an empty secret")
    payload = json.dumps(
        {"persona": persona.value, "exp": now + ttl_s},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    mac = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(mac)}"


def resolve_web_session(
    members: dict[PersonaKey, MemberRecord],
    cookie_value: str | None,
    secret: str | None,
    now: float | None = None,
) -> AuthenticatedUser | None:
    """Verify a session cookie and return its member. Tampered/expired → None."""
    if not cookie_value or not secret:
        return None
    try:
        encoded_payload, _, encoded_mac = cookie_value.partition(".")
        payload = _b64decode(encoded_payload)
        mac = _b64decode(encoded_mac)
    except Exception:
        return None
    if not hmac.compare_digest(mac, hmac.new(secret.encode(), payload, hashlib.sha256).digest()):
        return None
    try:
        body = json.loads(payload)
        persona = PersonaKey(body["persona"])
    except Exception:
        return None
    current = time.time() if now is None else now
    try:
        if current > float(body["exp"]):
            return None
    except Exception:
        return None
    record = members.get(persona)
    return _authenticated(record) if record else None


def armoriq_email_for(members: dict[PersonaKey, MemberRecord], member_id: str) -> str:
    """Return a member's raw ArmorIQ email. Backend-internal: the ArmorIQ gateway
    is the only caller (any other call site fails the guardrail test)."""
    for record in members.values():
        if record.member_id == member_id:
            return record.email
    raise LookupError(f"unknown member: {member_id}")


def session_info_for(record: MemberRecord) -> SessionInfo:
    """Public session view of a member. Carries the masked email, never raw."""
    return SessionInfo(
        persona=record.persona,
        display_name=record.display_name,
        app_role=record.app_role,
        masked_email=mask_email(record.email),
    )
