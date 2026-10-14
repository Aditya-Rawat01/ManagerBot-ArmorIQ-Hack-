"""Consistent error envelope for every non-2xx response.

Shape (contracts `ErrorResponse`): {"error": {"code", "message", "request_id", "details"}}
Internal exception text is never returned to clients.
"""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from proxylead_contracts import ErrorBody, ErrorResponse

from .middleware import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

_HTTP_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "service_unavailable",
}


class ApiError(Exception):
    """Raise from route/service code for an expected, client-visible error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: JsonValue | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: JsonValue | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, request_id=request_id, details=details)
    )
    response_headers = dict(headers or {})
    if request_id:
        response_headers.setdefault(REQUEST_ID_HEADER, request_id)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=response_headers,
    )


async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc.status_code, exc.code, exc.message, exc.details)


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    try:
        default_message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        default_message = "HTTP error"
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else default_message
    return error_response(
        request, exc.status_code, code, message, headers=getattr(exc, "headers", None)
    )


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Echo only location/message/type — never the submitted input values.
    details: list[Any] = [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    return error_response(request, 422, "validation_error", "Request validation failed.", details)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error (request_id=%s)", _request_id(request), exc_info=exc)
    return error_response(request, 500, "internal_error", "An unexpected error occurred.")


def register_exception_handlers(app: FastAPI) -> None:
    # Starlette dispatches by exception type, so each handler receives its own type.
    app.add_exception_handler(ApiError, _handle_api_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected)
