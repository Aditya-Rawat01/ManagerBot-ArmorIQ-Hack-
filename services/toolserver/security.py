"""Service authentication for the MCP endpoints (ADR-010).

Scaffold scope: a static bearer credential per endpoint. Which credential path
ArmorIQ's hosted proxy actually uses is decided by experiment E1. The `_ctx`
identity envelope, underscore-key stripping and idempotency arrive here (P1-04);
strict tool schemas (P2-02/P2-06) and the execution ledger (P2-07) follow.
"""

import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from services.authorization.ctx_envelope import (
    CtxEnvelope,
    CtxError,
    ExpiredContext,
    ReplayGuard,
    verify_ctx,
)
from services.contracts import ErrorBody, ErrorResponse

logger = logging.getLogger(__name__)

# Rejection codes for tool-server verification (SHARED_INTERFACES §4). Messages
# stay generic: they never echo argument values or protected details.
INVALID_CONTEXT = "invalid_context"
CONTEXT_EXPIRED = "context_expired"
REPLAY = "replay"


async def _send_error(
    send: Send, status: int, code: str, message: str, extra_headers: list
) -> None:
    body = json.dumps(
        ErrorResponse(error=ErrorBody(code=code, message=message)).model_dump(mode="json")
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerTokenGuard:
    """Reject any HTTP request without the endpoint's bearer token.

    Fails closed: when no token is configured the endpoint answers 503 to
    every request rather than running unauthenticated.
    """

    def __init__(self, app: ASGIApp, *, token: SecretStr | None, endpoint: str) -> None:
        self.app = app
        self._expected = token.get_secret_value().encode() if token else None
        self._endpoint = endpoint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self._expected:
            await _send_error(
                send,
                503,
                "endpoint_not_configured",
                f"{self._endpoint} has no service credential configured.",
                [],
            )
            return

        authorization = next(
            (value for key, value in scope["headers"] if key == b"authorization"), b""
        )
        scheme, _, provided = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(provided, self._expected):
            await _send_error(
                send,
                401,
                "unauthorized",
                "Missing or invalid service credential.",
                [(b"www-authenticate", b'Bearer realm="proxylead-mcp"')],
            )
            return

        await self.app(scope, receive, send)


class CtxRejection(Exception):
    """A tool call failed verification. Carries a code, never argument values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedCall:
    """A tool call that passed service + `_ctx` verification (P1-04).

    ``business_arguments`` holds only non-underscore keys. ``trace_metadata``
    records the *names* (never values) of stripped underscore keys.
    """

    ctx: CtxEnvelope
    business_arguments: dict[str, Any] = field(default_factory=dict)
    trace_metadata: dict[str, Any] = field(default_factory=dict)


def verify_tool_call(
    endpoint: str,
    tool: str,
    raw_arguments: Mapping[str, Any],
    *,
    ctx_secret: str | None,
    replay_guard: ReplayGuard,
    now: float | None = None,
    leeway_s: float = 30.0,
) -> VerifiedCall:
    """Verify one product-tool call before any handler logic runs.

    Every product tool handler calls this first (wired by P2-06). Raises
    :class:`CtxRejection` with ``invalid_context`` / ``context_expired`` /
    ``replay``; the hook maps the code to the MCP error result. D-05 finding:
    ``mcp==2.3.0`` drops undeclared ``_`` keys before handler bodies run, so
    this MUST receive the raw arguments above SDK validation or `_ctx` is lost
    and every call fails closed (see docs/SECURITY.md).
    """
    current = time.time() if now is None else now
    if not ctx_secret:
        logger.warning("rejecting tool call: no ctx secret configured")
        raise CtxRejection(INVALID_CONTEXT)
    if not isinstance(raw_arguments, Mapping):
        logger.warning("rejecting tool call: arguments are not a mapping")
        raise CtxRejection(INVALID_CONTEXT)
    args = dict(raw_arguments)
    token = args.pop("_ctx", None)
    if not isinstance(token, str) or not token:
        logger.warning("rejecting tool call: missing _ctx")
        raise CtxRejection(INVALID_CONTEXT)
    try:
        envelope = verify_ctx(
            token,
            ctx_secret,
            expected_aud=endpoint,
            expected_tool=tool,
            now=current,
            leeway_s=leeway_s,
        )
    except ExpiredContext:
        logger.warning("rejecting tool call: context expired")
        raise CtxRejection(CONTEXT_EXPIRED) from None
    except CtxError:
        logger.warning("rejecting tool call: invalid context")
        raise CtxRejection(INVALID_CONTEXT) from None
    if replay_guard.seen_jti(envelope.jti):
        logger.warning("rejecting tool call: replay")
        raise CtxRejection(REPLAY)
    stripped = sorted(key for key in args if key.startswith("_"))
    business = {key: value for key, value in args.items() if not key.startswith("_")}
    logger.info("verified tool call (tool=%s request_id=%s)", tool, envelope.request_id)
    return VerifiedCall(
        ctx=envelope,
        business_arguments=business,
        trace_metadata={"stripped_underscore_keys": stripped},
    )
