"""MCP result framing for product tools (P2-06, SHARED_INTERFACES §4).

Canonical shape (what the gateway, P1-02, parses):

- Success: ``CallToolResult(isError=false)`` with one text block carrying the
  JSON of the P2-02 result model, and ``_meta.ledger_entry_id`` = the decimal
  string of the ``tool_execution_ledger.id`` row that records the execution.
- Rejection or failure: ``CallToolResult(isError=true)`` with one text block
  carrying ``{"code", "message"}`` (codes from SHARED_INTERFACES §4).
  ``_meta.ledger_entry_id`` is present when this call has its own ledger row
  (``invalid_arguments``, ``out_of_scope``, handler errors);
  ``_meta.original_entry_id`` is present on ``replay`` when the original
  call's ledger row exists.

Messages are fixed per code: they never echo argument values, exception text,
or protected details.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from services.contracts import ToolName, tool_args
from services.toolserver.ledger import LedgerEntryId
from services.toolserver.security import CONTEXT_EXPIRED, INVALID_CONTEXT, REPLAY

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


def _ref(entry_id: LedgerEntryId) -> str:
    return str(entry_id.value)


def error_result(
    code: str,
    *,
    ledger_entry_id: LedgerEntryId | None = None,
    original_entry_id: LedgerEntryId | None = None,
) -> CallToolResult:
    """isError result for a rejection code (unknown codes become `internal`)."""
    normalized = code if code in _ERROR_MESSAGE else INTERNAL
    meta: dict[str, Any] = {}
    if ledger_entry_id is not None:
        meta["ledger_entry_id"] = _ref(ledger_entry_id)
    if original_entry_id is not None and normalized == REPLAY:
        meta["original_entry_id"] = _ref(original_entry_id)
    return CallToolResult(
        is_error=True,
        content=[
            TextContent(
                type="text",
                text=json.dumps({"code": normalized, "message": _ERROR_MESSAGE[normalized]}),
            )
        ],
        meta=meta or None,
    )


def ok_result(model: BaseModel, *, ledger_entry_id: LedgerEntryId) -> CallToolResult:
    """Success result: result-model JSON as text, ledger reference in `_meta`."""
    return CallToolResult(
        is_error=False,
        content=[TextContent(type="text", text=model.model_dump_json())],
        meta={"ledger_entry_id": _ref(ledger_entry_id)},
    )


class InvalidArguments(Exception):
    """Business-argument validation failed (maps to `invalid_arguments`)."""


def parse_tool_arguments(tool: ToolName, business_arguments: dict[str, Any]) -> BaseModel:
    """Validate business arguments against the tool's P2-02 model."""
    try:
        return tool_args.validate_arguments(tool, business_arguments)
    except ValidationError as exc:
        raise InvalidArguments(str(exc)) from None
