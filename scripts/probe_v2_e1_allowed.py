"""E1 probe: one real planned `knowledge_search_team` call for Maya (P1-06, #7).

Drives the real `ArmorIQGateway.start_plan` / `invoke_step` path against the
deployed MCP tool server — never the raw SDK, never a direct MCP call
(ADR-018). Prints the E1 record with secrets redacted; the PASS/FAIL verdict
is assigned by the operator in `docs/EXPERIMENTS.md`, never by this script.

Usage (operator only, repository root):
    python scripts/probe_v2_e1_allowed.py

Configuration is read the way the API reads it: `apps.api.core.config.Settings`
(process environment first, then the repo-root `.env` and `apps/api/.env`).
Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, CTX_SECRET (must equal the
deployed tool server's). Optional: ARMORIQ_AGENT_ID (default `proxylead`).
DATABASE_URL_TOOLSERVER (ledger correlation) is read from the environment or
the same `.env` files, then `services/toolserver/.env`.

Exit codes: 0 the probe ran to completion (any observed outcome); 1 an
unexpected error stopped it; 2 prerequisites missing (settings unreadable or
required values unset: names only, never values).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# (API `Settings` attribute, environment variable name) of each required value.
REQUIRED_SETTINGS = (
    ("armoriq_api_key", "ARMORIQ_API_KEY"),
    ("persona_maya_email", "PERSONA_MAYA_EMAIL"),
    ("ctx_secret", "CTX_SECRET"),
)
REQUIRED_ENV = tuple(name for _, name in REQUIRED_SETTINGS)
# The tool server's own env file: read after the API's for DATABASE_URL_TOOLSERVER.
TOOLSERVER_ENV_FILE = REPO_ROOT / "services" / "toolserver" / ".env"

# E1 evidence record (P2-03 seed): the word `quokka` isolates this source for
# keyword search; the freeze protocol gives the semantic match.
E1_QUERY = "quokka Friday deploy freeze"
E1_LIMIT = 5
E1_TOOL = "knowledge_search_team"

# Ledger columns the record may show (P2-07 ledger). Never `arguments_sha256`,
# `prev_hash`, or `entry_hash`.
LEDGER_FIELDS = (
    "id",
    "jti",
    "request_id",
    "trace_id",
    "plan_id",
    "step_index",
    "tool_name",
    "requester_member_id",
    "validation_outcome",
    "execution_status",
    "created_at",
)


def load_settings() -> Any:
    """API settings exactly as the backend reads them (environment + `.env` files)."""
    from apps.api.core.config import Settings

    return Settings()


def secret_text(value: Any) -> str:
    """Plain text of a `SecretStr` (or str), for presence checks only; never printed."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    return raw if isinstance(raw, str) else ""


def missing_settings(settings: Any) -> list[str]:
    """Names (never values) of required settings that are unset or blank."""
    return [
        name
        for attr, name in REQUIRED_SETTINGS
        if not secret_text(getattr(settings, attr, None)).strip()
    ]


class _LedgerSettings(BaseSettings):
    """DATABASE_URL_TOOLSERVER only (the API `Settings` has no such field)."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    database_url_toolserver: SecretStr | None = None


def _env_files(settings_cls: Any) -> tuple[Any, ...]:
    configured = settings_cls.model_config.get("env_file")
    if configured is None:
        return ()
    if isinstance(configured, (str, Path)):
        return (configured,)
    return tuple(configured)


def load_ledger_dsn() -> str | None:
    """DATABASE_URL_TOOLSERVER from the environment, else the API's `.env` files,
    then `services/toolserver/.env` (a later file wins; the environment wins
    over every file). Never printed."""
    from apps.api.core.config import Settings

    files = (*_env_files(Settings), TOOLSERVER_ENV_FILE)
    value = _LedgerSettings(_env_file=files).database_url_toolserver  # type: ignore[call-arg]
    return secret_text(value).strip() or None


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


@dataclass(frozen=True)
class RunIdentifiers:
    """What this run sent (or was told), to check the ledger row against."""

    request_id: str
    trace_id: str
    plan_id: str
    requester_member_id: str
    jti: str | None
    ledger_entry_id: str | None
    step_index: int = 0
    tool_name: str = E1_TOOL


def safe_fields(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Allow-listed columns only; timestamps as ISO text."""
    safe: dict[str, Any] = {}
    for key in fields:
        if key in row:
            value = row[key]
            safe[key] = value.isoformat() if isinstance(value, datetime) else value
    return safe


def ledger_mismatches(row: Mapping[str, Any], run: RunIdentifiers) -> list[str]:
    """Columns where the ledger row disagrees with this run (empty = correlated).

    A value this run does not have (no jti, no `_meta.ledger_entry_id`) is a
    mismatch too: correlation is never assumed.
    """
    pairs: tuple[tuple[str, str, object], ...] = (
        ("id", "_meta.ledger_entry_id", run.ledger_entry_id),
        ("jti", "jti", run.jti),
        ("request_id", "ctx.request_id", run.request_id),
        ("plan_id", "handle.plan_id", run.plan_id),
        ("trace_id", "ctx.trace_id", run.trace_id),
        ("requester_member_id", "ctx.requester.member_id", run.requester_member_id),
        ("step_index", "step_index", run.step_index),
        ("tool_name", "tool", run.tool_name),
    )
    mismatches: list[str] = []
    for column, label, expected in pairs:
        actual = row.get(column)
        if expected is None or actual is None or str(actual) != str(expected):
            mismatches.append(f"row.{column}={actual!r} vs {label}={expected!r}")
    return mismatches


class NeonLedgerLookup:
    """Read-only ledger lookups over one private toolserver-role pool."""

    def __init__(self, dsn: str) -> None:
        from services.toolserver import db
        from services.toolserver.ledger import Ledger

        self._pool = db.create_pool(dsn, min_size=1, max_size=1)
        self._ledger = Ledger(pool=self._pool)

    def by_jti(self, jti: str) -> dict[str, Any] | None:
        return self._ledger.get_by_jti(jti)

    def close(self) -> None:
        self._pool.close()


def _empty_ledger_report(lookup: str) -> dict[str, Any]:
    return {"correlated": False, "lookup": lookup, "row": None, "mismatches": []}


def correlate_ledger(
    dsn: str | None,
    run: RunIdentifiers,
    *,
    lookup_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Find this run's ledger row by jti and check it against the run.

    `correlated` is True only when the row matches every identifier of this
    run (`ledger_mismatches` is empty). Output holds the allow-listed
    `LEDGER_FIELDS` only: never arguments, hashes, or the DSN.
    """
    if not dsn:
        return _empty_ledger_report("unavailable (no DATABASE_URL_TOOLSERVER)")
    if not run.jti:
        return _empty_ledger_report("no jti to correlate")
    factory = lookup_factory or NeonLedgerLookup
    try:
        lookup = factory(dsn)
        try:
            row = lookup.by_jti(run.jti)
        finally:
            lookup.close()
    except Exception as exc:
        return _empty_ledger_report(f"lookup error ({type(exc).__name__})")
    if row is None:
        report = _empty_ledger_report("no row for jti")
        report["mismatches"] = ["no ledger row for this run's jti"]
        return report
    mismatches = ledger_mismatches(row, run)
    return {
        "correlated": not mismatches,
        "lookup": "by jti",
        "row": safe_fields(row, LEDGER_FIELDS),
        "mismatches": mismatches,
    }


def pass_candidate(described: Mapping[str, Any], ledger: Mapping[str, Any]) -> bool:
    """PASS needs both: the gateway reports EXECUTED and the ledger row correlates."""
    return described.get("invocation_status") == "executed" and ledger.get("correlated") is True


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
        f"- Ledger correlation: correlated: {'true' if ledger['correlated'] else 'false'} "
        f"(lookup: {ledger['lookup']})",
        f"- Ledger row (safe subset): {ledger['row']}",
        f"- Ledger mismatches: {ledger['mismatches'] or 'none'}",
        "- PASS candidate (EXECUTED and correlated): "
        f"{'yes' if pass_candidate(described, ledger) else 'no'}",
        "- Credential path observed: see deployed tool-server request log "
        "(bearer accepted vs 401) plus tool_call_attempts.credential_id",
        "- `_ctx` arrival: see deployed tool-server log (verified tool call "
        "vs invalid_context/context_expired/replay) — names of `_` keys only",
        f"- Assessment: {described['assessment']}",
        "- Outcome: operator-assigned in docs/EXPERIMENTS.md (PASS needs EXECUTED, "
        "a correlated ledger row, and the matching Render request log)",
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


async def run_probe(
    settings: Any,
    *,
    ledger_dsn: str | None = None,
    gateway: Any = None,
    lookup_factory: Callable[[str], Any] | None = None,
) -> tuple[int, str]:
    """Execute the live probe; return (exit_code, record_text).

    `gateway` and `lookup_factory` are seams for offline tests only; the
    operator path builds the real gateway and the Neon ledger lookup.
    """
    from services.authorization import armoriq_gateway

    if gateway is None:
        gateway = armoriq_gateway.ArmorIQGateway(settings)
    ctx, plan = build_maya_request(settings)
    handle = await gateway.start_plan(ctx, plan)
    if handle.decision is not None:
        outcome = armoriq_gateway.StepOutcome(decision=handle.decision)
        described = describe_outcome(outcome)
        payload = None
        ledger = _empty_ledger_report("skipped (plan/mint did not reach invocation)")
    else:
        outcome = await gateway.invoke_step(handle, 0, ctx, plan)
        described = describe_outcome(outcome)
        payload = outcome.invocation.result if outcome.invocation is not None else None
        run = RunIdentifiers(
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=handle.plan_id,
            requester_member_id=ctx.requester.member_id,
            jti=described["jti"],
            ledger_entry_id=described["ledger_entry_id"],
        )
        ledger = await asyncio.to_thread(
            correlate_ledger, ledger_dsn, run, lookup_factory=lookup_factory
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
    try:
        settings = load_settings()
    except Exception as exc:
        # Type name only: a validation message can echo the offending value.
        print(
            f"probe_v2_e1_allowed: settings could not be loaded ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    missing = missing_settings(settings)
    if missing:
        print(
            "probe_v2_e1_allowed: missing required settings (environment or .env): "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    try:
        ledger_dsn = load_ledger_dsn()
    except Exception as exc:
        print(
            "probe_v2_e1_allowed: DATABASE_URL_TOOLSERVER could not be loaded "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    if ledger_dsn is None:
        print(
            "probe_v2_e1_allowed: DATABASE_URL_TOOLSERVER is not set; "
            "ledger correlation will be skipped",
            file=sys.stderr,
        )
    try:
        code, record = asyncio.run(run_probe(settings, ledger_dsn=ledger_dsn))
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
