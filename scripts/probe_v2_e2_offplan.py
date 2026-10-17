"""E2 probe: off-plan and tampered-call enforcement layers (P1-07, #8).

Exercises the real `ArmorIQGateway.attempt_off_plan` path plus direct
tool-server calls, and emits one evidence row per attempt (attempt /
expected / observed / enforcing layer / evidence reference). Never claims
server-side plan binding without evidence; the proxy-shape attempt stays
`not tested` (no verified direct-proxy request shape exists in-tree).

Usage (operator only, repository root):
    python scripts/probe_v2_e2_offplan.py

Configuration is read the way E1 reads it (`e1.load_settings`: the API
`Settings`, process environment first, then the repo-root `.env` and
`apps/api/.env`). Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, CTX_SECRET
(must equal the deployed tool server's), TOOLSERVER_BEARER_MEMORY (direct
calls), and DATABASE_URL_TOOLSERVER (read-only evidence queries, toolserver
role). The bearer and the DSN are also read from `services/toolserver/.env`.
Optional: TOOLSERVER_BASE_URL (default DEFAULT_TOOLSERVER_BASE_URL),
ARMORIQ_AGENT_ID (default `proxylead`). Values are never printed.

Exit codes: 0 the probe ran to completion (any observed outcome); 1 an
unexpected error stopped it; 2 prerequisites missing or unusable (names
only, never values).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import probe_v2_e1_allowed as e1

# The deployed Render tool server (docs/EXPERIMENTS.md, P1-05 record).
DEFAULT_TOOLSERVER_BASE_URL = "https://managerbot-armoriq-hack.onrender.com"

EXIT_COMPLETE = 0
EXIT_ERROR = 1
EXIT_PREREQUISITES = 2


class _DirectSettings(BaseSettings):
    """TOOLSERVER_BEARER_MEMORY only (the API `Settings` has no such field)."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    toolserver_bearer_memory: SecretStr | None = None


@dataclass(frozen=True)
class DirectConfig:
    """Direct-call settings: the memory bearer (never printed) and base URL."""

    bearer: str | None
    base_url: str
    base_url_source: str
    base_url_problem: str | None = None

    def __repr__(self) -> str:  # never show the bearer
        return f"DirectConfig(base_url={self.base_url!r}, bearer_set={self.bearer is not None})"


def _base_url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "TOOLSERVER_BASE_URL must be an http(s) URL with a host"
    if parts.username or parts.password:
        return "TOOLSERVER_BASE_URL must not embed credentials"
    return None


def load_direct_config(settings: Any) -> DirectConfig:
    """Memory bearer from the environment, else the API's `.env` files, then
    `services/toolserver/.env` (as `e1.load_ledger_dsn` reads the DSN); base
    URL from `Settings.toolserver_base_url`, else the documented default."""
    from apps.api.core.config import Settings

    files = (*e1._env_files(Settings), e1.TOOLSERVER_ENV_FILE)
    raw = _DirectSettings(_env_file=files).toolserver_bearer_memory  # type: ignore[call-arg]
    bearer = e1.secret_text(raw).strip() or None
    configured = (getattr(settings, "toolserver_base_url", None) or "").strip()
    if configured:
        url, source = configured.rstrip("/"), "TOOLSERVER_BASE_URL"
    else:
        url, source = DEFAULT_TOOLSERVER_BASE_URL, "documented default"
    problem = _base_url_problem(url)
    if problem is not None:
        url = "(unusable)"
    return DirectConfig(
        bearer=bearer, base_url=url, base_url_source=source, base_url_problem=problem
    )


def missing_required(settings: Any, direct: DirectConfig, ledger_dsn: str | None) -> list[str]:
    """Names (never values) of every required setting that is unset or blank."""
    missing = list(e1.missing_settings(settings))
    if direct.bearer is None:
        missing.append("TOOLSERVER_BEARER_MEMORY")
    if not ledger_dsn:
        missing.append("DATABASE_URL_TOOLSERVER")
    return missing


# §4 tool-server rejection codes (SHARED_INTERFACES §4; services/toolserver
# security.py + schemas.py): the server speaking for itself inside the MCP
# result. Anything else (HTTP status, transport error) is NOT a layer.
TOOL_SERVER_CODES = frozenset(
    {
        "invalid_context",
        "context_expired",
        "replay",
        "invalid_arguments",
        "out_of_scope",
        "precondition_failed",
        "internal",
    }
)
# Top-level `{"error": {"code": ...}}` codes of the tool server's bearer guard
# (services/toolserver/security.py `BearerTokenGuard`): 401 / 503 JSON.
BEARER_GUARD_CODES = frozenset({"unauthorized", "endpoint_not_configured"})
MEMORY_PATH = "/memory/mcp"
DIRECT_TIMEOUT_S = 90.0  # Render Free may cold-start (~22 s observed, P1-05)


@dataclass(frozen=True)
class McpReply:
    """What one direct HTTP reply says (never the result body itself).

    ``code``: the §4 code from the MCP result text, or the bearer guard's
    error code. ``source`` says which shape carried it.
    """

    http_status: int | None
    code: str | None
    source: str
    is_error: bool | None = None
    ledger_entry_id: str | None = None
    original_entry_id: str | None = None
    meta_keys: tuple[str, ...] = ()
    jsonrpc_error: int | None = None


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _sse_first_message(text: str) -> Any:
    """First `data:` frame of an SSE body as JSON (cf. tests/toolserver/conftest.py)."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            return _json(line.removeprefix("data:").strip())
    return None


def _from_jsonrpc(status: int | None, message: Any, source: str) -> McpReply:
    if not isinstance(message, dict):
        return McpReply(status, None, f"{source} (no JSON-RPC message)")
    error = message.get("error")
    if isinstance(error, dict):
        rpc_code = error.get("code")
        return McpReply(
            status,
            None,
            f"{source} (JSON-RPC error)",
            jsonrpc_error=rpc_code if isinstance(rpc_code, int) else None,
        )
    result = message.get("result")
    if not isinstance(result, dict):
        return McpReply(status, None, f"{source} (no result)")
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}

    def ref(key: str) -> str | None:
        value = meta.get(key)
        return value if isinstance(value, str) and value else None

    code: str | None = None
    is_error = result.get("isError") is True
    if is_error:
        content = result.get("content")
        first = content[0] if isinstance(content, list) and content else None
        text = first.get("text") if isinstance(first, dict) else None
        body = _json(text) if isinstance(text, str) else None
        found = body.get("code") if isinstance(body, dict) else None
        code = found if isinstance(found, str) and found in TOOL_SERVER_CODES else None
    return McpReply(
        status,
        code,
        source,
        is_error=is_error,
        ledger_entry_id=ref("ledger_entry_id"),
        original_entry_id=ref("original_entry_id"),
        meta_keys=tuple(sorted(str(key) for key in meta)),
    )


def parse_mcp_response(status: int | None, content_type: str, text: str) -> McpReply:
    """Read one tools/call reply. Never raises on hostile shapes.

    The deployed server answers MCP calls as SSE (`json_response=False`,
    services/toolserver/main.py): the JSON-RPC message is the `data:` frame.
    The bearer guard answers 401/503 with `{"error": {"code": ...}}` JSON.
    """
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind == "text/event-stream":
        return _from_jsonrpc(status, _sse_first_message(text or ""), "sse data frame")
    if kind == "application/json":
        body = _json(text or "")
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return McpReply(status, error["code"], "bearer guard json")
        return _from_jsonrpc(status, body, "json body")
    return McpReply(status, None, f"unparsed ({kind or 'no content type'})")


def direct_layer(reply: McpReply) -> str:
    """Enforcing layer for a direct tool-server call.

    Only the server's own code names the tool-server layer: a §4 code in the
    MCP result, or the bearer guard's code on its 401/503. Transport
    outcomes (timeouts, other statuses, unparsed bodies) stay unknown.
    """
    if reply.code in TOOL_SERVER_CODES:
        return "tool_server_rule"
    if reply.code in BEARER_GUARD_CODES and reply.http_status in (401, 503):
        return "tool_server_rule"
    return "unknown"


def describe_reply(reply: McpReply) -> str:
    """One-line observation (codes and references only, never the body)."""
    parts = [f"http={reply.http_status}", f"code={reply.code}"]
    if reply.jsonrpc_error is not None:
        parts.append(f"jsonrpc_error={reply.jsonrpc_error}")
    if reply.is_error is False:
        parts.append("isError=false")
    if reply.ledger_entry_id:
        parts.append(f"ledger_entry_id={reply.ledger_entry_id}")
    if reply.original_entry_id:
        parts.append(f"original_entry_id={reply.original_entry_id}")
    return " ".join(parts) + f" ({reply.source})"


def build_direct_client(base_url: str) -> Any:
    """Real HTTP client for direct calls (operator path only; tests inject one)."""
    import httpx

    return httpx.Client(base_url=base_url, timeout=DIRECT_TIMEOUT_S, follow_redirects=False)


def post_tool_call(
    client: Any, bearer: str | None, tool: str, arguments: dict[str, Any], rpc_id: str
) -> McpReply:
    """POST one JSON-RPC `tools/call` to `/memory/mcp` (bearer only, no ArmorIQ)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    try:
        response = client.post(MEMORY_PATH, json=payload, headers=headers)
    except Exception as exc:
        return McpReply(None, None, f"transport error ({type(exc).__name__})")
    return parse_mcp_response(
        response.status_code, response.headers.get("content-type", ""), response.text
    )


def row(
    group: str,
    attempt: str,
    expected: str,
    observed: str,
    layer: str,
    evidence: str,
    **facts: Any,
) -> dict[str, Any]:
    return {
        "group": group,
        "attempt": attempt,
        "expected": expected,
        "observed": observed,
        "layer": layer,
        "evidence": evidence,
        **facts,
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


def direct_row(case: str, attempt: str, expected: str, reply: McpReply) -> dict[str, Any]:
    """Evidence row for one direct call: the label comes only from the
    server's own code (SSE data frame or bearer-guard JSON)."""
    return row(
        group="direct",
        attempt=attempt,
        expected=expected,
        observed=describe_reply(reply),
        layer=direct_layer(reply),
        evidence=f"tool-server reply code ({reply.source}) + Render log",
        case=case,
        code=reply.code,
        http_status=reply.http_status,
        source=reply.source,
        ledger_entry_id=reply.ledger_entry_id,
        original_entry_id=reply.original_entry_id,
    )


def group_direct(client: Any, bearer: str) -> list[dict[str, Any]]:
    """Group 4 (§9.4): direct calls to the tool server (no ArmorIQ).

    ``client`` is an httpx-style client rooted at the tool-server base URL
    (`build_direct_client`; tests pass a `TestClient`). The bearer is sent
    as a header only and never printed.
    """
    team = "knowledge_search_team"
    args = {"query": "quokka", "limit": 3}
    calls = (
        (
            "direct-no-bearer",
            "direct call, no bearer",
            "401 unauthorized (bearer guard)",
            None,
            dict(args),
        ),
        (
            "direct-no-ctx",
            "direct call, bearer only, no `_ctx`",
            "invalid_context",
            bearer,
            dict(args),
        ),
        (
            "direct-forged-ctx",
            "direct call, forged (malformed) `_ctx`",
            "invalid_context",
            bearer,
            {**args, "_ctx": "forged.token"},
        ),
    )
    rows = [
        direct_row(case, attempt, expected, post_tool_call(client, key, team, arguments, case))
        for case, attempt, expected, key, arguments in calls
    ]
    rows.append(
        row(
            group="direct",
            attempt="replayed jti",
            expected="replay with original_entry_id",
            observed="pending: needs a prior live jti (e.g. from E1)",
            layer="unknown",
            evidence="operator replays the E1 jti after a live E1",
            case="direct-replay",
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


async def run_probe(
    settings: Any, *, direct: DirectConfig, ledger_dsn: str | None
) -> tuple[int, str]:
    """Execute all attempt groups; return (exit_code, report_text)."""
    from services.authorization import armoriq_gateway

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
    with build_direct_client(direct.base_url) as client:
        rows.extend(group_direct(client, direct.bearer or ""))
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


def _fail(message: str) -> int:
    print(f"probe_v2_e2_offplan: {message}", file=sys.stderr)
    return EXIT_PREREQUISITES


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        return _fail("no arguments accepted")
    try:
        settings = e1.load_settings()
        direct = load_direct_config(settings)
        ledger_dsn = e1.load_ledger_dsn()
    except Exception as exc:
        # Type name only: a validation message can echo the offending value.
        return _fail(f"settings could not be loaded ({type(exc).__name__})")
    missing = missing_required(settings, direct, ledger_dsn)
    if missing:
        return _fail("missing required settings (environment or .env): " + ", ".join(missing))
    if direct.base_url_problem is not None:
        return _fail(direct.base_url_problem)
    try:
        code, report = asyncio.run(run_probe(settings, direct=direct, ledger_dsn=ledger_dsn))
    except Exception as exc:
        print(f"probe_v2_e2_offplan: probe error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERROR
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
