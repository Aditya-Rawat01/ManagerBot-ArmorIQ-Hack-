"""MCP result framing for product tools (P2-06, SHARED_INTERFACES §4).

Success: ``CallToolResult(isError=False)`` with one text block carrying the
JSON of the P2-02 result model. Failure: ``CallToolResult(isError=True)`` with
one text block carrying ``{"code", "message"}`` (codes from SHARED_INTERFACES
§4). Messages never echo argument values or protected details.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from services.contracts import ToolName, tool_args
from services.toolserver.security import CONTEXT_EXPIRED, INVALID_CONTEXT, REPLAY, CtxRejection

INTERNAL = "internal"
INVALID_ARGUMENTS = "invalid_arguments"
OUT_OF_SCOPE = "out_of_scope"

_ERROR_MESSAGE: dict[str, str] = {
    INVALID_CONTEXT: "The call was rejected: invalid context.",
    CONTEXT_EXPIRED: "The call was rejected: context expired.",
    REPLAY: "The call was rejected: duplicate invocation.",
    INVALID_ARGUMENTS: "The call was rejected: invalid arguments.",
    OUT_OF_SCOPE: "The call was rejected: out of scope.",
    INTERNAL: "The tool call could not be completed.",
}


def error_result(code: str) -> CallToolResult:
    """isError result for a rejection code (unknown codes become `internal`)."""
    normalized = code if code in _ERROR_MESSAGE else INTERNAL
    return CallToolResult(
        isError=True,
        content=[
            TextContent(
                type="text",
                text=json.dumps({"code": normalized, "message": _ERROR_MESSAGE[normalized]}),
            )
        ],
    )


def ok_result(model: BaseModel) -> CallToolResult:
    """Success result carrying the result-model JSON as text."""
    return CallToolResult(
        isError=False,
        content=[TextContent(type="text", text=model.model_dump_json())],
    )


def rejection_from_ctx(rejection: CtxRejection) -> CallToolResult:
    return error_result(rejection.code)


class InvalidArguments(Exception):
    """Business-argument validation failed (maps to `invalid_arguments`)."""


def parse_tool_arguments(tool: ToolName, business_arguments: dict[str, Any]) -> BaseModel:
    """Validate business arguments against the tool's P2-02 model."""
    try:
        return tool_args.validate_arguments(tool, business_arguments)
    except ValidationError as exc:
        raise InvalidArguments(str(exc)) from None
