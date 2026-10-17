"""E1 probe: one real planned `knowledge_search_team` call for Maya (P1-06, #7).

Drives the real `ArmorIQGateway.start_plan` / `invoke_step` path against the
deployed MCP tool server — never the raw SDK, never a direct MCP call
(ADR-018). Prints the E1 record with secrets redacted; the PASS/FAIL verdict
is assigned by the operator in `docs/EXPERIMENTS.md`, never by this script.
Nothing runs on import or in CI: only an operator running this file directly
(no arguments) makes a live call. Offline tests drive it with fakes only.

Usage (operator only, repository root):
    python scripts/probe_v2_e1_allowed.py

Configuration is read the way the API reads it: `apps.api.core.config.Settings`
(process environment first, then the repo-root `.env` and `apps/api/.env`).
Required: ARMORIQ_API_KEY, PERSONA_MAYA_EMAIL, CTX_SECRET (must equal the
deployed tool server's). Optional: ARMORIQ_AGENT_ID (default `proxylead`).
DATABASE_URL_TOOLSERVER (ledger correlation) is read from the environment or
the same `.env` files, then `services/toolserver/.env`.

Logs: only the gateway's own INFO/WARNING lines go to stderr (request_id,
jti, ledger reference, exception class), so a failed call keeps its jti.
SDK, httpx, and root logging are left alone.

Exit codes:
    0  EXECUTED and the ledger row correlates with this run (a PASS
       candidate: the operator still confirms the Render request log)
    1  an unexpected error stopped the probe (exception type only)
    2  prerequisites missing: arguments given, settings unreadable, or a
       required value unset (names only, never values)
    3  the probe ran, but the result is not EXECUTED + correlated (includes
       a run without DATABASE_URL_TOOLSERVER); read the record

HOLD note: Maya's plan is expected to ALLOW. If it HOLDs, the SDK waits up
to APPROVAL_TIMEOUT_MS (900 s by default) for the console decision, but the
signed `_ctx` expires CTX_TTL_S (300 s) after signing and is not re-signed
on resume: an approval later than about 5 minutes reaches the tool server
as `context_expired`. Approve within 5 minutes or re-run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
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

EXIT_PASS_CANDIDATE = 0
EXIT_ERROR = 1
EXIT_PREREQUISITES = 2
EXIT_NOT_PASS = 3

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
# `tool_call_attempts` columns (0008): `credential_id` is the NAME of the
# bearer setting that passed (D-06 evidence), never a token value.
ATTEMPT_FIELDS = (
    "id",
    "credential_id",
    "mcp_endpoint",
    "tool_name",
    "outcome",
    "jti",
    "request_id",
    "created_at",
)
_LEDGER_BY_REQUEST_SQL = (
    "SELECT id, jti, request_id, trace_id, plan_id, step_index, tool_name, "
    "requester_member_id, validation_outcome, execution_status, created_at "
    "FROM public.tool_execution_ledger WHERE request_id = %s ORDER BY id LIMIT 5"
)
_ATTEMPTS_SQL = (
    "SELECT id, credential_id, mcp_endpoint, tool_name, outcome, jti, request_id, "
    "created_at FROM public.tool_call_attempts "
    "WHERE jti = %s OR request_id = %s ORDER BY id LIMIT 5"
)

# Only the gateway's own logger is shown (request_id, jti, ledger reference,
# exception class): never the root, SDK, or httpx loggers.
GATEWAY_LOGGER = "services.authorization.armoriq_gateway"
# Driver/pool loggers muted during the ledger lookup: psycopg_pool's connect
# warnings quote the libpq error, which names the Neon host and role.
QUIET_DB_LOGGERS = ("psycopg", "psycopg.pool")


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


@contextlib.contextmanager
def gateway_logs_to_stderr() -> Iterator[None]:
    """Show the gateway's INFO+ lines on stderr for one run.

    Only `services.authorization.armoriq_gateway` gets the handler (its lines
    carry request_id, jti, the ledger reference, and exception class names:
    no secrets), so "gateway step ok ... jti=... ledger=..." and the
    no-invocation warnings keep their jti even when the call fails.
    """
    gateway_logger = logging.getLogger(GATEWAY_LOGGER)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("[gateway %(levelname)s] %(message)s"))
    previous_level = gateway_logger.level
    gateway_logger.addHandler(handler)
    gateway_logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        gateway_logger.removeHandler(handler)
        gateway_logger.setLevel(previous_level)


@contextlib.contextmanager
def quiet_db_logs() -> Iterator[None]:
    """Mute `QUIET_DB_LOGGERS` for the block (levels restored afterwards)."""
    saved = [(logging.getLogger(name), logging.getLogger(name).level) for name in QUIET_DB_LOGGERS]
    for db_logger, _ in saved:
        db_logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        for db_logger, level in saved:
            db_logger.setLevel(level)


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


def _key_types(mapping: Mapping[Any, Any]) -> dict[str, str]:
    return {str(key): type(value).__name__ for key, value in mapping.items()}


def summarize_first_content(payload: Any) -> Any:
    """Keys and value types of the first MCP `content` item (never values).

    For a text item whose text is a JSON object, the object's top-level keys
    and value types too (the P2-06 result model), so E1 step 7 can record the
    real result shape.
    """
    if not isinstance(payload, Mapping):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        return "no content items"
    first = content[0]
    if not isinstance(first, Mapping):
        return {"item_type": type(first).__name__}
    shape: dict[str, Any] = {"keys": _key_types(first)}
    text = first.get("text")
    if isinstance(text, str):
        try:
            body = json.loads(text)
        except (ValueError, RecursionError):
            body = None
        if isinstance(body, Mapping):
            shape["text_json_keys"] = _key_types(body)
        else:
            shape["text_json_keys"] = "text is not a JSON object"
    return shape


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def mask_emails(text: str | None) -> str | None:
    """Mask every e-mail address in free text (an SDK/ArmorIQ reason may quote one)."""
    if not text:
        return text
    from apps.api.identity import mask_email

    return _EMAIL.sub(lambda match: mask_email(match.group(0)), text)


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
        "decision_reason": mask_emails(decision.reason),
        "hold_observed": getattr(outcome, "approval", None) is not None
        or bool(getattr(decision, "delegation_id", None)),
        "tool_server_reached": None,
        "invocation_status": None,
        "ledger_entry_id": None,
        "error_code": None,
        "jti": None,
    }
    if invocation is None:
        described["assessment"] = (
            "no invocation record; tool server reach unknown (the gateway lines on "
            "stderr carry the jti and exception class; the ledger is searched by "
            "request_id)"
        )
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
            "ambiguous: allowed result without a ledger reference (cause unconfirmed); "
            "record UNKNOWN, never EXECUTED"
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


def meta_present(payload: Any, described: Mapping[str, Any]) -> tuple[str, str]:
    """Whether the result payload carries an MCP `_meta` object: yes / no / n.a.

    Key names only, never values.
    """
    if payload is None:
        if described.get("invocation_status") == "rejected_by_tool_server":
            return "n.a.", "the gateway keeps no payload for a rejection"
        return "n.a.", "no result payload"
    if isinstance(payload, Mapping) and isinstance(payload.get("_meta"), Mapping):
        return "yes", f"keys {sorted(str(key) for key in payload['_meta'])}"
    return "no", "the payload has no `_meta` object"


# Rejection codes raised only after `verify_tool_call` accepted `_ctx`
# (services/toolserver/servers.py, P2-06 dispatch).
_D05_AFTER_VERIFICATION = frozenset({"invalid_arguments", "out_of_scope", "precondition_failed"})
# `verify_ctx` checks the HMAC before expiry, and the replay check runs after
# verification: `_ctx` arrived intact for these too.
_D05_SIGNATURE_VERIFIED = frozenset({"context_expired", "replay"})


def d05_finding(described: Mapping[str, Any]) -> tuple[str, str]:
    """D-05 (did `_ctx` survive the proxy?): yes / ambiguous / n.a. (+ why).

    "no" is never inferred from the result: only the Render log line
    "rejecting tool call: missing _ctx" shows a stripped `_ctx`.
    """
    status = described.get("invocation_status")
    code = described.get("error_code")
    if status == "executed":
        return "yes", "EXECUTED: the tool server verified `_ctx` before the handler ran"
    if status == "rejected_by_tool_server":
        if code in _D05_AFTER_VERIFICATION:
            return "yes", f"{code} is raised only after `_ctx` verified"
        if code in _D05_SIGNATURE_VERIFIED:
            return "yes", f"{code}: the `_ctx` signature verified before this check"
        if code == "invalid_context":
            return "ambiguous", (
                "invalid_context: either the proxy stripped `_ctx` or CTX_SECRET differs "
                "from the deployed value; check the Render log at this time for "
                '"rejecting tool call: missing _ctx" (stripped: D-05 no) vs '
                '"rejecting tool call: invalid context" (secret or format mismatch)'
            )
        return "ambiguous", f"{code}: not decidable from the result; check the Render log"
    if status == "unknown":
        return "ambiguous", (
            'allowed result without a ledger reference; check the Render log for "verified '
            'tool call" at this time'
        )
    return "n.a.", "the tool server was not reached (or reach is unknown)"


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

    def _rows(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        from services.toolserver import db

        with db.transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            names = [column.name for column in cur.description or []]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def by_request_id(self, request_id: str) -> list[dict[str, Any]]:
        return self._rows(_LEDGER_BY_REQUEST_SQL, (request_id,))

    def attempts(self, jti: str | None, request_id: str) -> list[dict[str, Any]]:
        return self._rows(_ATTEMPTS_SQL, (jti, request_id))

    def close(self) -> None:
        self._pool.close()


def _empty_ledger_report(lookup: str) -> dict[str, Any]:
    return {
        "correlated": False,
        "lookup": lookup,
        "row": None,
        "mismatches": [],
        "rows_by_request_id": None,
        "attempts": None,
    }


def _query_ledger(
    dsn: str,
    run: RunIdentifiers,
    factory: Callable[[str], Any],
    report: dict[str, Any],
) -> Mapping[str, Any] | None:
    """Fill `report` (lookup, rows_by_request_id, attempts); return the row to
    check, if exactly one was found. Errors are reported by class name only."""
    row: Mapping[str, Any] | None = None
    try:
        lookup = factory(dsn)
    except Exception as exc:
        report["lookup"] = f"lookup error ({type(exc).__name__})"
        return None
    try:
        steps: list[str] = []
        try:
            if run.jti:
                row = lookup.by_jti(run.jti)
                steps.append("by jti" if row is not None else "no row for jti")
            else:
                steps.append("no jti returned")
            if row is None:
                rows = lookup.by_request_id(run.request_id)
                report["rows_by_request_id"] = [safe_fields(item, LEDGER_FIELDS) for item in rows]
                steps.append(f"by request_id: {len(rows)} row(s)")
                if len(rows) == 1:
                    row = rows[0]
            report["lookup"] = "; ".join(steps)
        except Exception as exc:
            report["lookup"] = f"lookup error ({type(exc).__name__})"
        try:
            attempts = lookup.attempts(run.jti, run.request_id)
            report["attempts"] = [safe_fields(item, ATTEMPT_FIELDS) for item in attempts]
        except Exception as exc:
            report["attempts"] = f"lookup error ({type(exc).__name__})"
    finally:
        with contextlib.suppress(Exception):
            lookup.close()
    return row


def correlate_ledger(
    dsn: str | None,
    run: RunIdentifiers,
    *,
    lookup_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Find this run's ledger row and check it against the run.

    Lookup by jti; when the gateway returned no jti (no invocation record)
    or no row has it, fall back to `request_id` (unique per run). Attempt
    rows (`credential_id`, verification outcome) are fetched by jti or
    request_id. `correlated` is True only when one row matches every
    identifier of this run (`ledger_mismatches` is empty). Output holds the
    allow-listed `LEDGER_FIELDS` / `ATTEMPT_FIELDS` only: never arguments,
    hashes, or the DSN.
    """
    if not dsn:
        return _empty_ledger_report("unavailable (no DATABASE_URL_TOOLSERVER)")
    report = _empty_ledger_report("not started")
    # Pool creation, queries, and close all run muted (M1: no host/role output).
    with quiet_db_logs():
        row = _query_ledger(dsn, run, lookup_factory or NeonLedgerLookup, report)
    if row is not None:
        report["row"] = safe_fields(row, LEDGER_FIELDS)
        report["mismatches"] = ledger_mismatches(row, run)
        report["correlated"] = not report["mismatches"]
    elif not str(report["lookup"]).startswith("lookup error"):
        report["mismatches"] = ["no single ledger row for this run"]
    return report


def pass_candidate(described: Mapping[str, Any], ledger: Mapping[str, Any]) -> bool:
    """PASS needs both: the gateway reports EXECUTED and the ledger row correlates."""
    return described.get("invocation_status") == "executed" and ledger.get("correlated") is True


def probe_exit_code(described: Mapping[str, Any], ledger: Mapping[str, Any]) -> int:
    """0 only for a PASS candidate; every other completed run exits 3."""
    return EXIT_PASS_CANDIDATE if pass_candidate(described, ledger) else EXIT_NOT_PASS


def hold_note(approval_timeout_ms: int) -> str:
    """Why a late approval fails: the SDK wait outlasts the `_ctx` lifetime."""
    from services.authorization.armoriq_gateway import CTX_TTL_S

    return (
        f"HOLD observed: the SDK waits up to {approval_timeout_ms // 1000} s for approval, "
        f"but `_ctx` expires {CTX_TTL_S} s after signing and is not re-signed on resume; "
        f"an approval later than about {CTX_TTL_S // 60} minutes reaches the tool server "
        "as `context_expired` (approve sooner or re-run; not a D-05 finding)"
    )


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
    payload: Any,
    ledger: dict[str, Any],
    approval_timeout_ms: int = 900_000,
) -> str:
    """Render the E1 record in the `docs/EXPERIMENTS.md` template format."""
    meta_answer, meta_reason = meta_present(payload, described)
    d05_answer, d05_reason = d05_finding(described)
    lines = [
        "### E1 — planned ArmorIQ call reaches the MCP tool server",
        f"- Run at (UTC): {run_at}",
        f"- SDK version: {sdk_version}",
        f"- Agent / MCP names used by the probe: {agent_id} / memory-mcp (tool {E1_TOOL}); "
        "confirm the registration in the ArmorIQ console",
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
        f"- `_meta` present: {meta_answer} ({meta_reason})",
        f"- Result shape (redacted): {summarize_result(payload)}",
        f"- First content item (keys/types only): {summarize_first_content(payload)}",
        "- SDK result fields not visible here: the gateway keeps only "
        "MCPInvocationResult.result; status, verified, metadata, and execution_time "
        "are dropped before the probe sees them",
        f"- Ledger correlation: correlated: {'true' if ledger['correlated'] else 'false'} "
        f"(lookup: {ledger['lookup']})",
        f"- Ledger row (safe subset): {ledger['row']}",
        f"- Ledger mismatches: {ledger['mismatches'] or 'none'}",
        *(
            [f"- Ledger rows by request_id (fallback): {ledger['rows_by_request_id']}"]
            if ledger.get("rows_by_request_id") is not None
            else []
        ),
        "- Attempt rows (tool_call_attempts; credential_id answers D-06): "
        f"{ledger.get('attempts')}",
        "- PASS candidate (EXECUTED and correlated): "
        f"{'yes' if pass_candidate(described, ledger) else 'no'} "
        f"(probe exit code {probe_exit_code(described, ledger)})",
        "- Credential path observed: see deployed tool-server request log "
        "(bearer accepted vs 401) plus tool_call_attempts.credential_id",
        f"- D-05 (`_ctx` forwarded): {d05_answer} ({d05_reason})",
        "- `_ctx` arrival detail: deployed tool-server log (verified tool call vs "
        "invalid_context/context_expired/replay; names of `_` keys only)",
        f"- Assessment: {described['assessment']}",
        *([f"- Note: {hold_note(approval_timeout_ms)}"] if described.get("hold_observed") else []),
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
    # Unique per run: the ledger fallback correlates by request_id.
    request_id = f"e1-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
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
        payload=payload,
        ledger=ledger,
        approval_timeout_ms=int(settings.approval_timeout_ms),
    )
    return probe_exit_code(described, ledger), record


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("probe_v2_e1_allowed: no arguments accepted", file=sys.stderr)
        return EXIT_PREREQUISITES
    try:
        settings = load_settings()
    except Exception as exc:
        # Type name only: a validation message can echo the offending value.
        print(
            f"probe_v2_e1_allowed: settings could not be loaded ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_PREREQUISITES
    missing = missing_settings(settings)
    if missing:
        print(
            "probe_v2_e1_allowed: missing required settings (environment or .env): "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return EXIT_PREREQUISITES
    try:
        ledger_dsn = load_ledger_dsn()
    except Exception as exc:
        print(
            "probe_v2_e1_allowed: DATABASE_URL_TOOLSERVER could not be loaded "
            f"({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_PREREQUISITES
    if ledger_dsn is None:
        print(
            "probe_v2_e1_allowed: DATABASE_URL_TOOLSERVER is not set; ledger "
            "correlation will be skipped, so the exit code cannot be 0",
            file=sys.stderr,
        )
    try:
        with gateway_logs_to_stderr():
            code, record = asyncio.run(run_probe(settings, ledger_dsn=ledger_dsn))
    except Exception as exc:
        print(
            f"probe_v2_e1_allowed: probe error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(record)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
