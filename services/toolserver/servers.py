"""MCP server definitions for the two registered endpoints (ADR-006).

Product tools (PRD v2 §6, `services.contracts.ToolName`) are registered here
with their business-parameter signatures (so `tools/list` advertises the true
schema) but every call is intercepted ABOVE the SDK's argument validation and
run through the guarded dispatcher. Reason (D-05, `docs/SECURITY.md`): the
pinned SDK drops undeclared `_`-prefixed keys and rejects declared ones, so
`_ctx` can never reach a handler body through the SDK path — verification must
see the raw arguments first. A registered body that ever executes raises: it
means the interceptor was bypassed (fail closed).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer

from services.authorization.ctx_envelope import InMemoryReplayGuard
from services.contracts import McpEndpoint, ToolExecutionStatus, ToolName, tool_spec
from services.toolserver import __version__
from services.toolserver.ledger import (
    DuplicateJti,
    Ledger,
    LedgerEntry,
    LedgerEntryId,
    verify_and_record,
)
from services.toolserver.schemas import (
    INTERNAL,
    INVALID_ARGUMENTS,
    OUT_OF_SCOPE,
    InvalidArguments,
    error_result,
    ok_result,
    parse_tool_arguments,
    rejection_from_ctx,
)
from services.toolserver.security import (
    CtxRejection,
    VerifiedCall,
    verify_tool_call,
)
from services.toolserver.tools_memory import PRODUCT_TOOLS

logger = logging.getLogger(__name__)

# Endpoint → bearer setting NAME that authenticates it (never a token value).
CREDENTIAL_IDS: dict[McpEndpoint, str] = {
    McpEndpoint.MEMORY: "toolserver_bearer_memory",
    McpEndpoint.WORKSPACE: "toolserver_bearer_workspace",
}


@dataclass
class ToolRuntime:
    """Per-endpoint dispatch state. `ledger` is None when no DSN is configured
    (local runs, most tests): verification still runs, with an in-memory
    replay guard and no attempt rows; every database-backed step fails closed."""

    endpoint: McpEndpoint
    ledger: Ledger | None
    pool: Any | None
    ctx_secret: str | None
    credential_id: str
    dev_guard: InMemoryReplayGuard


async def knowledge_search_team(query: str, limit: int) -> dict[str, Any]:
    """Schema declaration for `tools/list`; calls run through guarded dispatch."""
    raise RuntimeError("unreachable: product tools run through guarded dispatch")


_SCHEMA_BODIES: dict[ToolName, Callable[..., Awaitable[Any]]] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: knowledge_search_team,
}


def _dispatch_table(endpoint: McpEndpoint) -> dict[str, Any]:
    return {
        spec.name.value: spec
        for spec in PRODUCT_TOOLS.values()
        if tool_spec(spec.name).endpoint is endpoint
    }


def _ledger_entry(
    verified: VerifiedCall, validation_outcome: str, status: Any, error_class: str | None
) -> LedgerEntry:
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


async def _append_outcome(runtime: ToolRuntime, entry: LedgerEntry) -> LedgerEntryId | None:
    """Store an outcome row. Returns None when it could not be stored (logged).

    Callers keep their decided outcome either way: a missing row loses
    evidence, never changes the decision. The one exception is the executed
    path, which must never report EXECUTED without its row (handled there).
    """
    if runtime.ledger is None:
        logger.warning("ledger unavailable; outcome row not stored")
        return None
    try:
        return await anyio.to_thread.run_sync(runtime.ledger.append, entry)
    except DuplicateJti as duplicate:
        logger.warning("rejecting tool call: replay of entry %s", duplicate.existing_id.value)
        raise _ReplayAfterClaim(duplicate.existing_id) from None
    except Exception as exc:
        logger.error("ledger append failed (%s)", type(exc).__name__)
        return None


class _ReplayAfterClaim(Exception):
    """Append-time duplicate (near-impossible: verify claims first)."""

    def __init__(self, existing_id: LedgerEntryId) -> None:
        super().__init__(f"duplicate jti, original entry {existing_id.value}")
        self.existing_id = existing_id


class _OutOfScope(Exception):
    """Tier-ceiling denial (out_of_scope outcome). P2-08 raises this."""


def _install_guarded_dispatch(
    server: MCPServer, endpoint: McpEndpoint, holder: dict[str, Any]
) -> None:
    """Route product-tool calls through guarded dispatch with raw arguments."""
    table = _dispatch_table(endpoint)
    manager = server._tool_manager  # pinned SDK internals; see module docstring
    original_call_tool = manager.call_tool

    async def guarded_call_tool(
        name: str, arguments: Mapping[str, Any], context: Any, convert_result: bool = False
    ) -> Any:
        try:
            tool = ToolName(name)
        except ValueError:
            return await original_call_tool(name, arguments, context, convert_result)
        spec = table.get(name)
        if spec is None:
            return await original_call_tool(name, arguments, context, convert_result)
        runtime: ToolRuntime | None = holder.get("runtime")
        if runtime is None:
            return error_result(INTERNAL)
        raw = dict(arguments) if isinstance(arguments, Mapping) else {}
        if runtime.ledger is not None:
            try:
                verified = await anyio.to_thread.run_sync(
                    partial(
                        verify_and_record,
                        runtime.ledger,
                        endpoint.value,
                        name,
                        raw,
                        credential_id=runtime.credential_id,
                        ctx_secret=runtime.ctx_secret,
                    )
                )
            except CtxRejection as rejection:
                logger.warning("rejecting %s: %s", name, rejection.code)
                return rejection_from_ctx(rejection)
        else:
            try:
                verified = verify_tool_call(
                    endpoint.value,
                    name,
                    raw,
                    ctx_secret=runtime.ctx_secret,
                    replay_guard=runtime.dev_guard,
                )
            except CtxRejection as rejection:
                logger.warning("rejecting %s: %s", name, rejection.code)
                return rejection_from_ctx(rejection)
        try:
            args = parse_tool_arguments(tool, verified.business_arguments)
        except InvalidArguments:
            try:
                await _append_outcome(
                    runtime,
                    _ledger_entry(
                        verified,
                        INVALID_ARGUMENTS,
                        ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
                        None,
                    ),
                )
            except _ReplayAfterClaim:
                return error_result("replay")
            return error_result(INVALID_ARGUMENTS)
        # Member tier ceiling: only team-tier tools exist yet, and every
        # member may call them. P2-08 adds the elevated logic here.
        try:
            result_model = await spec.handler(verified, args, runtime)
        except Exception as exc:
            try:
                await _append_outcome(
                    runtime,
                    _ledger_entry(
                        verified,
                        OUT_OF_SCOPE if isinstance(exc, _OutOfScope) else INTERNAL,
                        ToolExecutionStatus.ERROR,
                        type(exc).__name__,
                    ),
                )
            except _ReplayAfterClaim:
                return error_result("replay")
            return error_result(INTERNAL)
        try:
            entry_id = await _append_outcome(
                runtime,
                _ledger_entry(verified, "accepted", ToolExecutionStatus.EXECUTED, None),
            )
        except _ReplayAfterClaim:
            return error_result("replay")
        if entry_id is None:
            # The tool ran but its row is not stored: never report EXECUTED.
            return error_result(INTERNAL)
        logger.info("executed %s (entry %s)", name, entry_id.value)
        return ok_result(result_model)

    manager.call_tool = guarded_call_tool  # type: ignore[method-assign]


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
    lifespan; guarded dispatch is always installed (fail closed without it)."""
    server: MCPServer = MCPServer(
        name=endpoint.value,
        version=__version__,
        instructions=f"ProxyLead {endpoint.value} tool server (foundation scaffold).",
    )
    if enable_diagnostic_tools:
        _register_diagnostic_ping(server, endpoint)
    for spec in PRODUCT_TOOLS.values():
        if tool_spec(spec.name).endpoint is not endpoint:
            continue
        server.tool(name=spec.name.value, description=spec.description)(_SCHEMA_BODIES[spec.name])
    _install_guarded_dispatch(server, endpoint, holder)
    return server
