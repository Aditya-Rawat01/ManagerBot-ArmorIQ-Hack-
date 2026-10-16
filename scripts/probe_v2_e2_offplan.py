"""E2 probe: off-plan and tampered-call enforcement layers (P1-07, #8).

Exercises the real `ArmorIQGateway.attempt_off_plan` path plus direct
tool-server calls, and emits one evidence row per attempt (attempt /
expected / observed / enforcing layer / evidence reference). Never claims
server-side plan binding without evidence; the proxy-shape attempt stays
`not tested` (no verified direct-proxy request shape exists in-tree).

Usage (operator only, repository root, real values in the environment):
    python scripts/probe_v2_e2_offplan.py

Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, CTX_SECRET. Direct-call group
additionally needs TOOLSERVER_BASE_URL + TOOLSERVER_BEARER_MEMORY; without
them that group is reported skipped (never faked). Replay needs a prior
live jti (from E1); without one it is reported pending.

Exit codes: 0 the probe ran to completion (any observed outcome); 1 an
unexpected error stopped it; 2 prerequisites missing (names only, no values).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_v2_e1_allowed as e1

REQUIRED_ENV = ("ARMORIQ_API_KEY", "PERSONA_MAYA_EMAIL", "CTX_SECRET")
DIRECT_ENV = ("TOOLSERVER_BASE_URL", "TOOLSERVER_BEARER_MEMORY")

# §4 tool-server rejection codes (SHARED_INTERFACES §4): the server speaking
# for itself. Anything else (HTTP status, transport error) is NOT a layer.
TOOL_SERVER_CODES = frozenset(
    {
        "unauthenticated",
        "invalid_context",
        "context_expired",
        "replay",
        "invalid_arguments",
        "out_of_scope",
        "precondition_failed",
        "internal",
    }
)


def missing_env() -> list[str]:
    """Names (never values) of required variables that are unset."""
    return [name for name in REQUIRED_ENV if not os.environ.get(name)]


def direct_layer(http_status: int | None, code: str | None) -> str:
    """Enforcing layer for a direct tool-server call.

    Only a §4 code from the response body names the tool-server layer;
    transport outcomes (401/503/timeout) name nothing and stay unknown.
    """
    if code in TOOL_SERVER_CODES:
        return "tool_server_rule"
    return "unknown"


def parse_direct_result(body: Any) -> tuple[int | None, str | None]:
    """Extract (http_status, §4 code) from a tools/call round trip.

    Returns (None, None) on transport failure; the code only when the body
    carries a §4-shaped error. Never raises on hostile shapes.
    """
    status, payload = body
    if not isinstance(payload, dict):
        return status, None
    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        content = result.get("content")
        first = content[0] if isinstance(content, list) and content else None
        text = first.get("text") if isinstance(first, dict) else None
        try:
            parsed = json.loads(text) if isinstance(text, str) else None
        except ValueError:
            parsed = None
        code = parsed.get("code") if isinstance(parsed, dict) else None
        if isinstance(code, str) and code in TOOL_SERVER_CODES:
            return status, code
    if isinstance(payload.get("error"), dict):
        return status, None
    return status, None


def row(
    group: str,
    attempt: str,
    expected: str,
    observed: str,
    layer: str,
    evidence: str,
) -> dict[str, str]:
    return {
        "group": group,
        "attempt": attempt,
        "expected": expected,
        "observed": observed,
        "layer": layer,
        "evidence": evidence,
    }


async def group_off_plan(gateway: Any, handle: Any, ctx: Any) -> list[dict[str, str]]:
    """Group 1 (§9.1): off-plan attempts through the SDK plan-check path."""
    from services.contracts import ToolName

    rows = []
    for tool, args in (
        (ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, {"query": "runway", "limit": 3}),
        (ToolName.SEND_EXTERNAL_MESSAGE, {"message": "hello"}),
    ):
        decision = await gateway.attempt_off_plan(handle, tool, dict(args), ctx)
        rows.append(
            row(
                group="off-plan-sdk",
                attempt=f"{tool.value} under a team-only plan",
                expected="BLOCK armoriq_sdk_plan_check before any network call",
                observed=f"{decision.outcome.value} {decision.source.value}",
                layer=decision.source.value,
                evidence=(
                    "gateway log (mismatch exception type); operator confirms "
                    "Render log + ledger/attempt absence by request_id/jti"
                ),
            )
        )
    return rows


async def group_tamper(gateway: Any, handle: Any, ctx: Any) -> list[dict[str, str]]:
    """Group 3 (§9.3): forbidden keys and forged underscore fields via off-plan."""
    from services.contracts import ToolName

    rows = []
    cases = (
        ("forbidden tier key", {"query": "runway", "limit": 3, "tier": "restricted"}),
        ("forbidden identity key", {"query": "runway", "limit": 3, "user_email": "x"}),
        (
            "forged underscore fields",
            {
                "query": "runway",
                "limit": 3,
                "_approvals": "forged",
                "_delegation_id": "forged",
                "_policy_context": "forged",
            },
        ),
    )
    for label, args in cases:
        decision = await gateway.attempt_off_plan(
            handle, ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, dict(args), ctx
        )
        rows.append(
            row(
                group="tamper",
                attempt=f"off-plan confidential search with {label}",
                expected="BLOCK before execution; forged `_` fields grant nothing",
                observed=f"{decision.outcome.value} {decision.source.value}",
                layer=decision.source.value,
                evidence=(
                    "gateway log; tool-server stripping proven offline by "
                    "P1-04 tests, live by server log trace_metadata"
                ),
            )
        )
    return rows


def group_proxy_shape() -> list[dict[str, str]]:
    """Group 2 (§9.2): recorded not-tested — no verified shape exists."""
    return [
        row(
            group="proxy-shape",
            attempt="direct proxy off-plan request bypassing the SDK check",
            expected="not tested absent a verified request shape",
            observed="not tested",
            layer="unknown",
            evidence="none: no verified direct-proxy shape exists in-tree (Day-1 doc rule)",
        )
    ]


def group_direct(base_url: str, bearer: str) -> list[dict[str, str]]:
    """Group 4 (§9.4): bearer-only direct calls against the deployed server."""
    import httpx

    calls = (
        ("no _ctx", {"query": "runway", "limit": 3}),
        ("forged _ctx", {"query": "runway", "limit": 3, "_ctx": "forged-token"}),
    )
    rows = []
    for label, args in calls:
        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/memory/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": "e2-probe",
                    "method": "tools/call",
                    "params": {"name": "knowledge_search_team", "arguments": args},
                },
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            )
            try:
                body: Any = response.json()
            except ValueError:
                body = None
        except Exception as exc:
            rows.append(
                row(
                    group="direct",
                    attempt=f"direct call, {label}",
                    expected="§4 rejection code",
                    observed=f"transport failure ({type(exc).__name__})",
                    layer="unknown",
                    evidence="no response received; layer cannot be determined",
                )
            )
            continue
        status, code = parse_direct_result((response.status_code, body))
        rows.append(
            row(
                group="direct",
                attempt=f"direct call, {label}",
                expected="§4 rejection code (invalid_context)",
                observed=f"http={status} code={code}",
                layer=direct_layer(status, code),
                evidence="response body §4 code (server speaking) + Render log",
            )
        )
    rows.append(
        row(
            group="direct",
            attempt="replayed jti",
            expected="replay with original_entry_id",
            observed="pending: needs a prior live jti (e.g. from E1)",
            layer="unknown",
            evidence="operator replays the E1 jti after a live E1",
        )
    )
    return rows


def render_table(rows: list[dict[str, str]]) -> str:
    header = "| attempt | expected | observed | enforcing layer | evidence reference |"
    lines = [
        "### E2 evidence rows (one per attempt)",
        "",
        header,
        "|---|---|---|---|---|",
    ]
    for item in rows:
        lines.append(
            f"| {item['attempt']} | {item['expected']} | {item['observed']} | "
            f"{item['layer']} | {item['evidence']} |"
        )
    return "\n".join(lines)


async def run_probe() -> tuple[int, str]:
    """Execute all attempt groups; return (exit_code, report_text)."""
    from apps.api.core.config import Settings
    from services.authorization import armoriq_gateway

    settings = Settings()
    gateway = armoriq_gateway.ArmorIQGateway(settings)
    ctx, plan = e1.build_maya_request(settings)
    handle = await gateway.start_plan(ctx, plan)
    if handle.decision is not None:
        blocked = e1.describe_outcome(
            type(
                "Outcome",
                (),
                {"decision": handle.decision, "invocation": None},
            )()
        )
        return 0, "\n".join(
            [
                "### E2 — off-plan and tampered-call enforcement",
                f"- Plan/mint: {blocked['decision_outcome']} "
                f"({blocked['decision_source']}); no attempts ran.",
                "- Outcome: operator-assigned in docs/EXPERIMENTS.md.",
            ]
        )
    rows: list[dict[str, str]] = []
    rows.extend(await group_off_plan(gateway, handle, ctx))
    rows.extend(await group_tamper(gateway, handle, ctx))
    rows.extend(group_proxy_shape())
    direct_missing = [v for v in DIRECT_ENV if not os.environ.get(v)]
    if direct_missing:
        rows.append(
            row(
                group="direct",
                attempt="direct calls (no _ctx / forged _ctx / replay)",
                expected="§4 rejection codes",
                observed=f"skipped (missing: {', '.join(direct_missing)})",
                layer="unknown",
                evidence="rerun with TOOLSERVER_BASE_URL + TOOLSERVER_BEARER_MEMORY",
            )
        )
    else:
        base = os.environ.get("TOOLSERVER_BASE_URL", "")
        bearer = os.environ.get("TOOLSERVER_BEARER_MEMORY", "")
        rows.extend(group_direct(base, bearer))
    report = [
        "### E2 — off-plan and tampered-call enforcement",
        f"- Request: {ctx.request_id} (Maya, team-only plan)",
        "- Server-side zero-execution proof: operator correlates each "
        "request_id/jti via Render log + Ledger.get_by_jti + tool_call_attempts; "
        "this probe asserts no local execution only.",
        "- Server-side plan binding: not tested (no verified shape).",
        "",
        render_table(rows),
    ]
    return 0, "\n".join(report)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("probe_v2_e2_offplan: no arguments accepted", file=sys.stderr)
        return 2
    missing = missing_env()
    if missing:
        print(
            "probe_v2_e2_offplan: missing required environment: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    try:
        code, report = asyncio.run(run_probe())
    except Exception as exc:
        print(f"probe_v2_e2_offplan: probe error ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
