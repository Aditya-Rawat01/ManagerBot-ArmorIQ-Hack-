"""Request ID propagation (pure ASGI middleware)."""

import re
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Attach a request ID to `request.state.request_id` and the response header.

    A well-formed incoming `X-Request-ID` is reused for tracing; anything else
    is replaced so clients cannot inject arbitrary text into logs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next(
            (value.decode("latin-1") for key, value in scope["headers"] if key == b"x-request-id"),
            "",
        )
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if REQUEST_ID_HEADER not in headers:
                    headers.append(REQUEST_ID_HEADER, request_id)
            await send(message)

        await self.app(scope, receive, send_with_request_id)
