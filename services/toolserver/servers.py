"""MCP server definitions for the two registered endpoints (ADR-006).

Product tools (PRD v2 §6, `services.contracts.ToolName`) are registered with
their enforced P2-02 argument schema (so `tools/list` advertises the real
limits), but every `tools/call` whose name is a `ToolName` is answered by the
guarded dispatcher, never by the SDK's tool path. Hook placement (D-05,
`docs/SECURITY.md`): the pinned SDK (`mcp==2.3.0`) drops undeclared
`_`-prefixed keys and rejects declared ones, so `_ctx` can never reach a
handler body through the SDK path — verification must see the raw arguments
first. The hook is the SDK's supported `tools/call` interceptor
(`Extension.intercept_tool_call`), which receives the validated JSON-RPC params
(raw tool arguments) before any argument model runs. The registered bodies are
fail-closed canaries: one that ever executes raises (the interceptor was
bypassed). Side effect: newer protocol versions may advertise the extension
identifier under `capabilities.extensions`; the 2024-11-05 handshake ArmorIQ
documents and 2025-11-25 do not list it (probed).

Guarded order (P2-06 §9 step 1): verify `_ctx` (+ attempt row) → validate
business arguments → member tier ceiling → handler → ledger row → MCP result
(`services/toolserver/schemas.py`, SHARED_INTERFACES §4). Every failure is an
MCP error result with a §4 code; no exception text ever reaches the client.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import anyio.to_thread
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Extension, MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolRequestParams, CallToolResult

from services.authorization.ctx_envelope import InMemoryReplayGuard
from services.contracts import (
    AppRole,
    DataTier,
    McpEndpoint,
    ToolExecutionStatus,
    ToolName,
    tool_spec,
)
from services.contracts.tool_args import TOOL_ARGUMENT_MODELS
from services.toolserver import __version__, tools_memory, tools_workspace
from services.toolserver.ledger import (
    ACCEPTED,
    DuplicateJti,
    Ledger,
    LedgerEntry,
    LedgerEntryId,
    ReplayRejection,
    verify_and_record,
)
from services.toolserver.schemas import (
    INTERNAL,
    INVALID_ARGUMENTS,
    NOT_FOUND,
    OUT_OF_SCOPE,
    InvalidArguments,
    NotFound,
    error_result,
    ok_result,
    parse_tool_arguments,
)
from services.toolserver.security import REPLAY, CtxRejection, VerifiedCall, verify_tool_call
from services.toolserver.tools_memory import ProductTool

logger = logging.getLogger(__name__)

# Endpoint → bearer setting NAME that authenticates it (never a token value).
CREDENTIAL_IDS: dict[McpEndpoint, str] = {
    McpEndpoint.MEMORY: "toolserver_bearer_memory",
    McpEndpoint.WORKSPACE: "toolserver_bearer_workspace",
}

# SEP-2133 identifier of the dispatch interceptor (advertised in `initialize`).
GUARDED_DISPATCH_ID = "dev.proxylead/guarded-dispatch"


@dataclass
class ToolRuntime:
    """Per-endpoint dispatch state, set in the app lifespan.

    `ledger` is None when no DSN is configured (local runs, most tests):
    verification and argument checks still run, so rejections keep their
    codes, but no product tool executes — an execution without a ledger row
    is never allowed (`internal`). `ctx_secret` is excluded from `repr`."""

    endpoint: McpEndpoint
    ledger: Ledger | None
    pool: Any | None
    ctx_secret: str | None = field(repr=False)
    credential_id: str
    dev_guard: InMemoryReplayGuard


class OutOfScope(Exception):
    """Member tier-ceiling denial (`out_of_scope`); raised before the handler."""


_ROLE_CEILING: dict[AppRole, DataTier] = {
    AppRole.MANAGER: DataTier.RESTRICTED,
    AppRole.SENIOR: DataTier.CONFIDENTIAL,
    AppRole.JUNIOR: DataTier.TEAM,
}
_TIER_RANK: dict[DataTier, int] = {
    DataTier.TEAM: 0,
    DataTier.CONFIDENTIAL: 1,
    DataTier.RESTRICTED: 2,
}


def check_tier_ceiling(tool: ToolName, verified: VerifiedCall) -> None:
    """P2-08 per-role ceiling (SHARED_INTERFACES §6) from the verified `_ctx`
    role, independent of ArmorIQ (R-26): manager → restricted, senior →
    confidential, junior → team. Unknown roles fail closed.

    Tier-less tools return here unchecked. P2-09's Maya-only maintenance
    tools check the manager role in their own handlers. The P2-10 workspace
    tools (`send_external_message`, `create_followup_task`) intentionally
    have NO tool-server role check, here or in their handlers: ArmorIQ policy
    is the deciding layer for them (ADR-025: it must BLOCK the honeypot for
    every persona, and that BLOCK is the demo's evidence), so no application
    rule may pre-empt or mask the ArmorIQ decision.
    """
    tier = tool_spec(tool).tier
    if tier is None:
        return
    try:
        ceiling = _ROLE_CEILING[AppRole(verified.ctx.app_role)]
    except ValueError:
        raise OutOfScope(tool.value) from None
    if _TIER_RANK[tier] > _TIER_RANK[ceiling]:
        raise OutOfScope(tool.value)


def advertised_input_schema(tool: ToolName) -> dict[str, Any]:
    """The `tools/list` input schema: the enforced P2-02 argument model.

    `additionalProperties: false`, length and range limits come from the
    model. `_`-prefixed keys (`_ctx`, the ArmorIQ SDK's `_policy_context`) are
    transport metadata that the server verifies and strips before business
    validation (D-05), so they are admitted by `patternProperties` but never
    declared — a strict client or proxy validating against this schema must
    not drop or refuse them.
    """
    schema = TOOL_ARGUMENT_MODELS[tool].model_json_schema()
    schema["patternProperties"] = {"^_": {}}
    return schema


def _guard_bypassed() -> None:
    """Registered body of every product tool (fail-closed canary)."""
    raise RuntimeError("product tools run only through guarded dispatch")


def _schema_tool(spec: ProductTool) -> Tool:
    tool = Tool.from_function(
        _guard_bypassed,
        name=spec.name.value,
        description=spec.description,
        structured_output=False,
    )
    return tool.model_copy(update={"parameters": advertised_input_schema(spec.name)})


def dispatch_table(endpoint: McpEndpoint) -> dict[ToolName, ProductTool]:
    """Implemented product tools served by `endpoint` (all behind dispatch).

    Merged live from both tool modules on every call, so tests may swap a
    handler via monkeypatch and P2-10-style registration stays one line.
    """
    combined = {**tools_memory.PRODUCT_TOOLS, **tools_workspace.PRODUCT_TOOLS}
    return {
        name: spec for name, spec in combined.items() if tool_spec(spec.name).endpoint is endpoint
    }


def _ledger_entry(
    verified: VerifiedCall,
    validation_outcome: str,
    status: ToolExecutionStatus,
    error_class: str | None,
) -> LedgerEntry:
    """Identity fields only from the verified `_ctx`, never from arguments."""
    ctx = verified.ctx
    return LedgerEntry(
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        tool_name=ctx.tool,
        jti=ctx.jti,
        execution_status=status,
        plan_id=ctx.plan_id,
        step_index=ctx.step_index,
        requester_member_id=ctx.requester_member_id,
        workspace_id=ctx.workspace_id,
        validation_outcome=validation_outcome,
        error_class=error_class,
        arguments=dict(verified.business_arguments),
    )


class _ReplayAfterClaim(Exception):
    """Append-time duplicate jti (near-impossible: verification claims first)."""

    def __init__(self, existing_id: LedgerEntryId) -> None:
        super().__init__("duplicate jti")
        self.existing_id = existing_id


async def _record(runtime: ToolRuntime, entry: LedgerEntry) -> LedgerEntryId | None:
    """Append one outcome row; None when it could not be stored (logged at
    error level). Callers never report success without a stored row."""
    if runtime.ledger is None:
        # Dev mode (no DSN, warned at startup): rejections only, never executions.
        logger.warning(
            "no ledger: %s row for %s not stored", entry.validation_outcome, entry.tool_name
        )
        return None
    try:
        return await anyio.to_thread.run_sync(runtime.ledger.append, entry)
    except DuplicateJti as duplicate:
        raise _ReplayAfterClaim(duplicate.existing_id) from None
    except ValueError:
        logger.error(
            "ledger refused an invalid %s/%s row for %s",
            entry.validation_outcome,
            entry.execution_status.value,
            entry.tool_name,
        )
        return None
    except Exception as exc:
        logger.error("ledger append failed (%s)", type(exc).__name__)
        return None


class GuardedDispatch(Extension):
    """Answers every `tools/call` whose name is a `ToolName`; other names
    (diagnostics, unknown tools) continue down the SDK path, where no product
    tool can run. Never raises: any failure becomes `internal`."""

    identifier = GUARDED_DISPATCH_ID

    def __init__(self, endpoint: McpEndpoint, holder: dict[str, Any]) -> None:
        self._endpoint = endpoint
        self._holder = holder
        self._table = dispatch_table(endpoint)

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        try:
            tool = ToolName(params.name)
        except ValueError:
            return await call_next(ctx)
        try:
            return await self._dispatch(tool, params.arguments)
        except _ReplayAfterClaim as replay:
            logger.warning("rejecting %s: replay at append", tool.value)
            return error_result(REPLAY, original_entry_id=replay.existing_id)
        except Exception as exc:
            # Class name only: driver errors can carry table names or values.
            logger.error("guarded dispatch failed for %s (%s)", tool.value, type(exc).__name__)
            return error_result(INTERNAL)

    async def _verify(
        self, runtime: ToolRuntime, tool: ToolName, raw: dict[str, Any]
    ) -> VerifiedCall:
        if runtime.ledger is not None:
            return await anyio.to_thread.run_sync(
                partial(
                    verify_and_record,
                    runtime.ledger,
                    self._endpoint.value,
                    tool.value,
                    raw,
                    credential_id=runtime.credential_id,
                    ctx_secret=runtime.ctx_secret,
                )
            )
        return verify_tool_call(
            self._endpoint.value,
            tool.value,
            raw,
            ctx_secret=runtime.ctx_secret,
            replay_guard=runtime.dev_guard,
        )

    async def _reject(
        self, runtime: ToolRuntime, verified: VerifiedCall, code: str
    ) -> CallToolResult:
        """Post-verification rejection: recorded with that validation outcome."""
        logger.warning("rejecting %s: %s", verified.ctx.tool, code)
        entry_id = await _record(
            runtime,
            _ledger_entry(verified, code, ToolExecutionStatus.REJECTED_BY_TOOL_SERVER, None),
        )
        return error_result(code, ledger_entry_id=entry_id)

    async def _dispatch(self, tool: ToolName, arguments: Any) -> CallToolResult:
        spec = self._table.get(tool)
        if spec is None:
            # A ToolName with no guarded handler here: never forwarded.
            logger.error("refusing %s: no guarded handler on %s", tool.value, self._endpoint.value)
            return error_result(INTERNAL)
        runtime: ToolRuntime | None = self._holder.get("runtime")
        if runtime is None:
            logger.error("refusing %s: tool runtime not initialised", tool.value)
            return error_result(INTERNAL)
        raw = dict(arguments) if isinstance(arguments, Mapping) else {}
        # 1.1 `_ctx` verification (+ attempt row with a ledger).
        try:
            verified = await self._verify(runtime, tool, raw)
        except CtxRejection as rejection:
            logger.warning("rejecting %s: %s", tool.value, rejection.code)
            original = (
                rejection.original_entry_id if isinstance(rejection, ReplayRejection) else None
            )
            return error_result(rejection.code, original_entry_id=original)
        # 1.2 business arguments (strict P2-02 model).
        try:
            args = parse_tool_arguments(tool, verified.business_arguments)
        except InvalidArguments:
            return await self._reject(runtime, verified, INVALID_ARGUMENTS)
        # 1.3 member tier ceiling.
        try:
            check_tier_ceiling(tool, verified)
        except OutOfScope:
            return await self._reject(runtime, verified, OUT_OF_SCOPE)
        if runtime.ledger is None:
            logger.error("refusing %s: no ledger, an execution could not be recorded", tool.value)
            return error_result(INTERNAL)
        # 1.4 handler.
        try:
            result_model = await spec.handler(verified, args, runtime)
        except NotFound:
            # Authorized lookup, nothing visible: an executed empty lookup.
            entry_id = await _record(
                runtime,
                _ledger_entry(verified, ACCEPTED, ToolExecutionStatus.EXECUTED, None),
            )
            return error_result(NOT_FOUND, ledger_entry_id=entry_id)
        except Exception as exc:
            logger.error("tool %s failed (%s)", tool.value, type(exc).__name__)
            entry_id = await _record(
                runtime,
                _ledger_entry(verified, ACCEPTED, ToolExecutionStatus.ERROR, type(exc).__name__),
            )
            return error_result(INTERNAL, ledger_entry_id=entry_id)
        # 1.5 ledger row; 1.6 MCP result. Never EXECUTED without its row.
        entry_id = await _record(
            runtime, _ledger_entry(verified, ACCEPTED, ToolExecutionStatus.EXECUTED, None)
        )
        if entry_id is None:
            return error_result(INTERNAL)
        logger.info("executed %s (entry %s)", tool.value, entry_id.value)
        return ok_result(result_model, ledger_entry_id=entry_id)


def _register_diagnostic_ping(server: MCPServer, endpoint: McpEndpoint) -> None:
    tool_name = f"{endpoint.value.removesuffix('-mcp')}_scaffold_ping"

    @server.tool(
        name=tool_name,
        description=(
            "Scaffold-only diagnostic. Returns static server metadata and reads no data. "
            "Not part of the ProxyLead product tool contract."
        ),
    )
    def scaffold_ping() -> dict[str, object]:
        return {"ok": True, "endpoint": endpoint.value, "server_version": __version__}


def build_server(
    endpoint: McpEndpoint,
    *,
    enable_diagnostic_tools: bool,
    holder: dict[str, Any],
) -> MCPServer:
    """Build one endpoint server. `holder` receives the ToolRuntime in the
    lifespan; until then every product-tool call fails closed (`internal`)."""
    server: MCPServer = MCPServer(
        name=endpoint.value,
        version=__version__,
        instructions=f"ProxyLead {endpoint.value} tool server (foundation scaffold).",
        tools=[_schema_tool(spec) for spec in dispatch_table(endpoint).values()],
        extensions=[GuardedDispatch(endpoint, holder)],
    )
    if enable_diagnostic_tools:
        _register_diagnostic_ping(server, endpoint)
    return server
