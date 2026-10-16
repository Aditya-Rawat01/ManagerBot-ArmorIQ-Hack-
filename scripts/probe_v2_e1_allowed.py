"""E1 probe: one real planned `knowledge_search_team` call for Maya (P1-06, #7).

Drives the real `ArmorIQGateway.start_plan` / `invoke_step` path against the
deployed MCP tool server — never the raw SDK, never a direct MCP call
(ADR-018). Prints the E1 record with secrets redacted; the PASS/FAIL verdict
is assigned by the operator in `docs/EXPERIMENTS.md`, never by this script.

Usage (operator only, repository root, real values in the environment):
    python scripts/probe_v2_e1_allowed.py

Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, CTX_SECRET (must match the
deployed tool server). Optional: ARMORIQ_AGENT_ID (default `proxylead`),
DATABASE_URL_TOOLSERVER (ledger correlation via `Ledger.get_by_jti`).

Exit codes: 0 the probe ran to completion (any observed outcome); 1 an
unexpected error stopped it; 2 prerequisites missing (names only, no values).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REQUIRED_ENV = ("ARMORIQ_API_KEY", "PERSONA_MAYA_EMAIL", "CTX_SECRET")

# E1 evidence record (P2-03 seed): the word `quokka` isolates this source for
# keyword search; the freeze protocol gives the semantic match.
E1_QUERY = "quokka Friday deploy freeze"
E1_LIMIT = 5
E1_TOOL = "knowledge_search_team"


def missing_env() -> list[str]:
    """Names (never values) of required variables that are unset."""
    return [name for name in REQUIRED_ENV if not os.environ.get(name)]


def summarize_value(value: Any, depth: int = 0) -> Any:
    """Redacted shape of one result value: type names and sizes, no content."""
    if isinstance(value, dict):
        if depth > 1:
            return {"type": "dict", "keys": len(value)}
        return {
            "type": "dict",
            "keys": sorted(str(key) for key in value),
            "detail": {str(key): summarize_value(item, depth + 1) for key, item in value.items()},
        }
    if isinstance(value, (list, tuple)):
        kinds = sorted({type(item).__name__ for item in value})
        return {"type": "list", "length": len(value), "item_types": kinds}
    if isinstance(value, str):
        return {"type": "str", "length": len(value)}
    return {"type": type(value).__name__}


def summarize_result(payload: Any) -> Any:
    """Redacted shape of the tool result: structure only, never the body."""
    if isinstance(payload, dict):
        return {str(key): summarize_value(value) for key, value in payload.items()}
    return summarize_value(payload)


def describe_outcome(outcome: Any) -> dict[str, Any]:
    """Presentation read of a real gateway `StepOutcome` (no re-decision).

    Every classification below repeats the gateway's own verdict verbatim;
    the E1 assessment notes only what an operator must still confirm.
    """
    decision = outcome.decision
    invocation = outcome.invocation
    described: dict[str, Any] = {
        "decision_outcome": decision.outcome.value,
        "decision_source": decision.source.value,
        "decision_reason": decision.reason,
        "tool_server_reached": None,
        "invocation_status": None,
        "ledger_entry_id": None,
        "error_code": None,
        "jti": None,
    }
    if invocation is None:
        described["assessment"] = "no invocation record; tool server reach unknown"
        return described
    described["tool_server_reached"] = invocation.tool_server_reached
    described["invocation_status"] = invocation.status.value
    described["ledger_entry_id"] = invocation.ledger_entry_id
    described["error_code"] = invocation.error_code
    described["jti"] = invocation.jti
    if invocation.status.value == "executed" and invocation.ledger_entry_id:
        described["assessment"] = (
            "gateway reports EXECUTED with a ledger reference; "
            "PASS still needs the server-side request log plus "
            "Ledger.get_by_jti correlation"
        )
    elif invocation.status.value == "unknown":
        described["assessment"] = (
            "ambiguous: proxy dropped the ledger reference; record UNKNOWN, never EXECUTED"
        )
    elif invocation.status.value == "rejected_by_tool_server":
        described["assessment"] = (
            f"tool-server rejection ({invocation.error_code}); "
            "enforcing layer is the tool server, not ArmorIQ policy"
        )
    elif invocation.status.value == "not_reached":
        described["assessment"] = (
            "tool server not reached; enforcing layer is the reported "
            "decision source, subject to evidence rules"
        )
    else:
        described["assessment"] = "unclassified invocation state; investigate, do not claim"
    return described


def correlate_ledger(jti: str | None) -> dict[str, Any]:
    """Correlate `jti` via `Ledger.get_by_jti` when a toolserver DSN exists.

    Returns a safe subset (no arguments, no hashes); never the DSN.
    """
    dsn = os.environ.get("DATABASE_URL_TOOLSERVER")
    if not dsn:
        return {"ledger": "unavailable (no DATABASE_URL_TOOLSERVER)"}
    if not jti:
        return {"ledger": "no jti to correlate"}
    try:
        from services.toolserver.ledger import Ledger

        ledger = Ledger(dsn)
        try:
            row = ledger.get_by_jti(jti)
        finally:
            ledger.close()
    except Exception as exc:
        return {"ledger": f"lookup error ({type(exc).__name__})"}
    if row is None:
        return {"ledger": "no row for jti"}
    return {
        "ledger": {
            key: row.get(key)
            for key in ("id", "request_id", "tool_name", "execution_status", "created_at")
            if key in row
        }
    }


def build_record(
    *,
    run_at: str,
    sdk_version: str,
    agent_id: str,
    endpoint_url: str,
    request_id: str,
    masked_email: str,
    plan_id: str | None,
    armoriq_plan_id: str | None,
    query: str,
    described: dict[str, Any],
    result_shape: Any,
    ledger: dict[str, Any],
) -> str:
    """Render the E1 record in the `docs/EXPERIMENTS.md` template format."""
    lines = [
        "### E1 — planned ArmorIQ call reaches the MCP tool server",
        f"- Run at (UTC): {run_at}",
        f"- SDK version: {sdk_version}",
        f"- Agent / MCP names as registered: {agent_id} / memory-mcp (tool {E1_TOOL})",
        f"- Endpoint URL (no secrets): {endpoint_url}",
        f"- Plan / session IDs: plan_id={plan_id} armoriq_plan_id={armoriq_plan_id}",
        f"- Requester: Maya ({masked_email}); request_id={request_id}",
        f'- Query: "{query}" (E1 seed phrase)',
        f"- Decision: {described['decision_outcome']} "
        f"(source={described['decision_source']}; reason={described['decision_reason']})",
        f"- Tool server reached: {described['tool_server_reached']}",
        f"- Invocation: status={described['invocation_status']} "
        f"ledger_entry_id={described['ledger_entry_id']} "
        f"error_code={described['error_code']} jti={described['jti']}",
        f"- Result shape (redacted): {result_shape}",
        f"- Ledger correlation: {ledger}",
        "- Credential path observed: see deployed tool-server request log "
        "(bearer accepted vs 401) plus tool_call_attempts.credential_id",
        "- `_ctx` arrival: see deployed tool-server log (verified tool call "
        "vs invalid_context/context_expired/replay) — names of `_` keys only",
        f"- Assessment: {described['assessment']}",
        "- Outcome: operator-assigned in docs/EXPERIMENTS.md "
        "(PASS needs correlated request + ledger evidence)",
        "- Evidence: Render request log excerpt; Neon ledger/attempt rows "
        "(collected privately, secrets redacted)",
        "- Fallback chosen / next action: per PRD §11 step 9 on failure",
    ]
    return "\n".join(lines)


def build_maya_request(settings: Any) -> tuple[Any, Any]:
    """One-step Maya plan for `knowledge_search_team` (real identity path)."""
    from apps.api import identity
    from services.contracts import (
        DEFAULT_WORKSPACE_ID,
        AuthenticatedUser,
        EntryPoint,
        McpEndpoint,
        PersonaKey,
        PlannedToolCall,
        PlanProposal,
        RequestContext,
        ToolName,
    )

    table = identity.build_member_table(settings)
    record = table.get(PersonaKey.MAYA)
    if record is None:
        raise ValueError("Maya has no mapped ArmorIQ email")
    requester = AuthenticatedUser(
        member_id=record.member_id,
        persona=record.persona,
        display_name=record.display_name,
        app_role=record.app_role,
        masked_email=identity.mask_email(record.email),
        slack_user_id=record.slack_user_id,
    )
    now = datetime.now(UTC)
    request_id = f"e1-{now.strftime('%Y%m%dT%H%M%S')}"
    ctx = RequestContext(
        request_id=request_id,
        trace_id=f"{request_id}-trace",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester=requester,
        entry_point=EntryPoint.WEB,
        received_at=now,
    )
    plan = PlanProposal(
        summary="E1: team-tier search for the known seed phrase",
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={"query": E1_QUERY, "limit": E1_LIMIT},
                purpose="E1 probe: reach memory-mcp through ArmorIQ",
            )
        ],
    )
    return ctx, plan


async def run_probe() -> tuple[int, str]:
    """Execute the live probe; return (exit_code, record_text)."""
    from apps.api.core.config import Settings
    from services.authorization import armoriq_gateway

    settings = Settings()
    gateway = armoriq_gateway.ArmorIQGateway(settings)
    ctx, plan = build_maya_request(settings)
    handle = await gateway.start_plan(ctx, plan)
    if handle.decision is not None:
        described = describe_outcome(
            type(
                "Outcome",
                (),
                {"decision": handle.decision, "invocation": None},
            )()
        )
        record = build_record(
            run_at=datetime.now(UTC).isoformat(),
            sdk_version=armoriq_gateway.SDK_VERSION,
            agent_id=settings.armoriq_agent_id,
            endpoint_url="(deployed tool-server URL; recorded by operator)",
            request_id=ctx.request_id,
            masked_email=ctx.requester.masked_email,
            plan_id=handle.plan_id,
            armoriq_plan_id=handle.armoriq_plan_id,
            query=E1_QUERY,
            described=described,
            result_shape=None,
            ledger={"ledger": "plan/mint did not reach invocation"},
        )
        return 0, record
    outcome = await gateway.invoke_step(handle, 0, ctx, plan)
    described = describe_outcome(outcome)
    payload = outcome.invocation.result if outcome.invocation is not None else None
    ledger = correlate_ledger(described["jti"])
    record = build_record(
        run_at=datetime.now(UTC).isoformat(),
        sdk_version=armoriq_gateway.SDK_VERSION,
        agent_id=settings.armoriq_agent_id,
        endpoint_url="(deployed tool-server URL; recorded by operator)",
        request_id=ctx.request_id,
        masked_email=ctx.requester.masked_email,
        plan_id=handle.plan_id,
        armoriq_plan_id=handle.armoriq_plan_id,
        query=E1_QUERY,
        described=described,
        result_shape=summarize_result(payload),
        ledger=ledger,
    )
    return 0, record


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("probe_v2_e1_allowed: no arguments accepted", file=sys.stderr)
        return 2
    missing = missing_env()
    if missing:
        print(
            "probe_v2_e1_allowed: missing required environment: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    try:
        code, record = asyncio.run(run_probe())
    except Exception as exc:
        print(
            f"probe_v2_e1_allowed: probe error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    print(record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
