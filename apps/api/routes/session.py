"""Web demo session endpoints (P1-03): sign in, inspect, sign out.

The sign-in body is the only request body that may carry a persona (PRD §4.1).
Every later request reads identity from the session cookie only.
"""

import hmac
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from apps.api.core.config import Settings
from apps.api.core.errors import ApiError
from apps.api.deps import get_app_settings
from apps.api.identity import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_S,
    build_member_table,
    mint_session_token,
    resolve_web_session,
    session_info_for,
)
from apps.api.schemas import SessionCreateRequest, SessionInfo

router = APIRouter(tags=["session"])

_SIGN_IN_UNAVAILABLE = (
    "Web sign-in is not configured on this deployment. Set DEMO_PASSCODE and SESSION_SECRET."
)


def _cookie_flags(settings: Settings) -> dict[str, object]:
    # Local dev/test run over plain HTTP; anything else must be Secure so the
    # cookie survives the Vercel same-origin rewrite as first-party (ADR-030).
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.app_env not in ("development", "test"),
        "path": "/",
        "max_age": SESSION_TTL_S,
    }


@router.post("/session", response_model=SessionInfo, summary="Sign in to the web demo")
def create_session(
    body: SessionCreateRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> SessionInfo:
    passcode = settings.demo_passcode.get_secret_value() if settings.demo_passcode else ""
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    if not passcode or not secret:
        raise ApiError(503, "sign_in_unavailable", _SIGN_IN_UNAVAILABLE)
    record = build_member_table(settings).get(body.persona)
    if record is None or not hmac.compare_digest(body.passcode, passcode):
        # One generic 401: never reveal whether the persona exists or which
        # half of the credential was wrong.
        raise ApiError(401, "unauthenticated", "Invalid persona or passcode.")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        mint_session_token(body.persona, secret, time.time()),
        **_cookie_flags(settings),  # type: ignore[arg-type]
    )
    return session_info_for(record)


@router.get("/session", response_model=SessionInfo, summary="Current web session")
def read_session(
    request: Request, settings: Annotated[Settings, Depends(get_app_settings)]
) -> SessionInfo:
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    user = resolve_web_session(
        build_member_table(settings), request.cookies.get(SESSION_COOKIE_NAME), secret
    )
    if user is None:
        raise ApiError(401, "unauthenticated", "No valid session.")
    return SessionInfo(
        persona=user.persona,
        display_name=user.display_name,
        app_role=user.app_role,
        masked_email=user.masked_email,
    )


@router.delete("/session", status_code=204, summary="Sign out of the web demo")
def delete_session() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
