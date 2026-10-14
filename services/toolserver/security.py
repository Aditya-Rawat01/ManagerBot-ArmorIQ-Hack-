"""Service authentication for the MCP endpoints (ADR-010).

Scaffold scope: a static bearer credential per endpoint. Which credential path
ArmorIQ's hosted proxy actually uses is decided by experiment E1. The `_ctx`
identity envelope, underscore-key stripping, strict tool schemas, idempotency,
and the execution ledger arrive in Phase 2.
"""

import hmac
import json

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from services.contracts import ErrorBody, ErrorResponse


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
