"""MCP result framing for product tools (P2-06, SHARED_INTERFACES §4).

Canonical shape (what the gateway, P1-02, parses):

- Success: ``CallToolResult(isError=false)`` with one text block carrying the
  JSON of the P2-02 result model, and ``_meta.ledger_entry_id`` = the decimal
  string of the ``tool_execution_ledger.id`` row that records the execution.
- Rejection or failure: ``CallToolResult(isError=true)`` with one text block
  carrying ``{"code", "message"}`` (codes from SHARED_INTERFACES §4).
  ``_meta.ledger_entry_id`` is present when this call has its own ledger row
  (``invalid_arguments``, ``out_of_scope``, handler errors, ``not_found``);
  ``_meta.original_entry_id`` is present on ``replay`` when the original
  call's ledger row exists.
- ``not_found`` is the one ``isError`` result that is NOT a rejection: the
  authorized lookup executed (ledger row ``accepted``/``executed``, always
  present in ``_meta``) and found nothing visible. Missing, wrong-tier, and
  inactive lookups share its body byte for byte (D-11). The gateway maps it to
  an executed lookup with no evidence (SHARED_INTERFACES §4).

Messages are fixed per code: they never echo argument values, exception text,
or protected details.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from services.contracts import ToolName, tool_args
from services.toolserver.ledger import LedgerEntryId
from services.toolserver.security import CONTEXT_EXPIRED, INVALID_CONTEXT, REPLAY

INTERNAL = "internal"
INVALID_ARGUMENTS = "invalid_arguments"
NOT_FOUND = "not_found"
OUT_OF_SCOPE = "out_of_scope"

_ERROR_MESSAGE: dict[str, str] = {
    INVALID_CONTEXT: "The call was rejected: invalid context.",
    CONTEXT_EXPIRED: "The call was rejected: context expired.",
    REPLAY: "The call was rejected: duplicate invocation.",
    INVALID_ARGUMENTS: "The call was rejected: invalid arguments.",
    NOT_FOUND: "No matching source found.",
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


class NotFound(Exception):
    """Authorized lookup with no visible result (maps to `not_found`).

    Missing, wrong-tier, and inactive sources share this response byte for
    byte: existence is never disclosed (R-55). The exception text is never
    sent to the client (the body is the fixed §4 message).
    """


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, Mapping):
        return any(_has_nul(key) or _has_nul(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(item) for item in value)
    return False


def parse_tool_arguments(tool: ToolName, business_arguments: dict[str, Any]) -> BaseModel:
    """Validate business arguments against the tool's P2-02 model.

    A NUL character in any string argument is `invalid_arguments`: PostgreSQL
    text cannot hold it, so it would otherwise surface as a driver error
    (`internal`) inside the handler. No stored value can contain NUL, so the
    rejection discloses nothing about existence.
    """
    try:
        model = tool_args.validate_arguments(tool, business_arguments)
    except ValidationError as exc:
        raise InvalidArguments(str(exc)) from None
    if _has_nul(model.model_dump()):
        raise InvalidArguments("NUL character in a string argument")
    return model
