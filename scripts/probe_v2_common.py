"""Shared helpers for the P1-08/P1-09/P1-10 probes (check_setup, E3, E4, E5).

Builds on the E1/E2 probes (`scripts/probe_v2_e1_allowed.py`,
`scripts/probe_v2_e2_offplan.py`) instead of re-implementing them: settings
come from the API `Settings` (environment, then `.env` files), every ArmorIQ
call goes through `ArmorIQGateway` (ADR-018; never the raw SDK), the SDK
client is wrapped through the gateway's `client_factory` seam for wire and
exception evidence, and the database is read only, with the toolserver role.

Nothing here runs on import or makes a network call by itself. Values of
secrets, tokens and e-mails are never printed: identities appear masked
(`w***@g***.com`), settings by NAME only.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from scripts import probe_v2_e1_allowed as e1
from scripts import probe_v2_e2_offplan as e2

EXIT_PASS = 0
EXIT_ERROR = 1
EXIT_PREREQUISITES = 2
EXIT_NOT_PASS = 3

PERSONA_ORDER = ("maya", "sam", "priya")
PERSONA_EMAIL_ENV = {
    "maya": "PERSONA_MAYA_EMAIL",
    "sam": "PERSONA_SAM_EMAIL",
    "priya": "PERSONA_PRIYA_EMAIL",
}
ENDPOINT_PATHS = {"memory-mcp": "/memory/mcp", "workspace-mcp": "/workspace/mcp"}

# Decision sources that may be cited as proof that the ArmorIQ policy applied
# (ArmorIQ evidence rule, P1-08 §10 / P1-09 §10). Everything else
# (`armoriq_sdk_plan_check`, `tool_server_rule`, `application_rule`,
# `unknown`) can prove fail-closed behavior, never the policy.
ARMORIQ_SOURCES = frozenset(
    {"armoriq_policy_decision", "armoriq_proxy_decision", "armoriq_issuance"}
)

# Harmless arguments per tool (team seed phrase; the honeypot's only
# destination, ADR-025). The tier is never an argument.
PROBE_QUERY = e1.E1_QUERY
PROBE_LIMIT = 3


def default_arguments(tool: str, tag: str) -> dict[str, Any]:
    """Planned arguments for one probe step (never a tier, identity or recipient)."""
    if tool == "send_external_message":
        return {"destination": "synthetic_sink", "message": f"{tag} probe: must never be delivered"}
    return {"query": PROBE_QUERY, "limit": PROBE_LIMIT}


class _ToolserverSettings(BaseSettings):
    """Tool-server values the API `Settings` does not carry (names only in output)."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")

    toolserver_bearer_memory: SecretStr | None = None
    toolserver_bearer_workspace: SecretStr | None = None


def load_bearers() -> dict[str, str | None]:
    """Both endpoint bearers from the environment, else the API `.env` files,
    then `services/toolserver/.env` (as `e1.load_ledger_dsn`). Never printed."""
    from apps.api.core.config import Settings

    files = (*e1._env_files(Settings), e1.TOOLSERVER_ENV_FILE)
    loaded = _ToolserverSettings(_env_file=files)  # type: ignore[call-arg]
    return {
        "memory-mcp": e1.secret_text(loaded.toolserver_bearer_memory).strip() or None,
        "workspace-mcp": e1.secret_text(loaded.toolserver_bearer_workspace).strip() or None,
    }


def missing_names(settings: Any, required: Iterable[tuple[str, str]]) -> list[str]:
    """Names (never values) of required `Settings` attributes that are unset or blank."""
    return [
        name for attr, name in required if not e1.secret_text(getattr(settings, attr, None)).strip()
    ]


def with_approval_wait(settings: Any, wait_s: float) -> Any:
    """A copy of `settings` whose SDK approval wait is `wait_s` seconds.

    The gateway always waits for approval (`wait_for_approval=True`) up to
    APPROVAL_TIMEOUT_MS; probes that must not wait 15 minutes on a HOLD use
    this copy. Shortening the wait keeps the startup invariant
    (TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000) true.
    """
    return settings.model_copy(update={"approval_timeout_ms": max(1, int(wait_s * 1000))})


def with_token_validity(settings: Any, validity_s: int) -> Any:
    """A copy of `settings` requesting a different intent-token lifetime (E4 expiry)."""
    return settings.model_copy(update={"token_validity_s": int(validity_s)})


# --- requests ------------------------------------------------------------------------


def new_request_id(prefix: str) -> str:
    now = datetime.now(UTC)
    return f"{prefix}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def build_member_request(
    settings: Any,
    persona: str,
    tool: str,
    *,
    prefix: str,
    summary: str,
    arguments: Mapping[str, Any] | None = None,
) -> tuple[Any, Any]:
    """One-step plan for `persona` calling `tool` (real identity path, as E1).

    The requester comes from the trusted server-side member table
    (`apps.api.identity.build_member_table`), never from input. The request
    id is unique per call; evidence is correlated by it and by the jti.
    """
    from apps.api import identity
    from services.contracts import (
        DEFAULT_WORKSPACE_ID,
        TOOL_CATALOG,
        AuthenticatedUser,
        EntryPoint,
        PersonaKey,
        PlannedToolCall,
        PlanProposal,
        RequestContext,
        ToolName,
    )

    record = identity.build_member_table(settings).get(PersonaKey(persona))
    if record is None:
        raise ValueError(f"{PERSONA_EMAIL_ENV[persona]} is not mapped")
    requester = AuthenticatedUser(
        member_id=record.member_id,
        persona=record.persona,
        display_name=record.display_name,
        app_role=record.app_role,
        masked_email=identity.mask_email(record.email),
        slack_user_id=record.slack_user_id,
    )
    name = ToolName(tool)
    request_id = new_request_id(prefix)
    ctx = RequestContext(
        request_id=request_id,
        trace_id=f"{request_id}-trace",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester=requester,
        entry_point=EntryPoint.WEB,
        received_at=datetime.now(UTC),
    )
    plan = PlanProposal(
        summary=summary,
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=TOOL_CATALOG[name].endpoint,
                tool=name,
                arguments=dict(
                    arguments if arguments is not None else default_arguments(tool, prefix)
                ),
                purpose=f"{prefix} probe: one planned {tool} call",
            )
        ],
    )
    return ctx, plan


# --- HOLD observation ---------------------------------------------------------------


class HoldRecorder:
    """`on_hold` observer for `ArmorIQGateway.invoke_step` (thread-safe).

    Keeps every pending `ApprovalStatus` the SDK reported and calls
    `announce` (if given) at once, so an operator can act in the console
    while the SDK is still waiting. Never raises.
    """

    def __init__(self, announce: Callable[[Any], None] | None = None) -> None:
        self._lock = threading.Lock()
        self.events: list[Any] = []
        self._announce = announce

    def __call__(self, approval: Any) -> None:
        with self._lock:
            self.events.append(approval)
        if self._announce is not None:
            with contextlib.suppress(Exception):
                self._announce(approval)

    @property
    def delegation_id(self) -> str | None:
        with self._lock:
            for event in self.events:
                value = getattr(event, "delegation_id", None)
                if value:
                    return str(value)
        return None


# --- what one step showed -------------------------------------------------------------


@dataclass
class StepObservation:
    """One planned step as observed: decision, layer, ArmorIQ evidence reference.

    ``observed`` is ALLOW / HOLD / BLOCK / UNVERIFIED. ``source`` is the
    layer of that observation (for a HOLD: the layer that HELD, even when the
    gateway's final verdict after the wait is a timeout). ``evidence_ref`` is
    an ArmorIQ evidence reference (SDK exception / delegation id / plan id),
    or None when there is none.
    """

    observed: str
    source: str
    gateway_outcome: str
    gateway_source: str
    reason: str | None
    evidence_ref: str | None
    at_mint: bool = False
    delegation_id: str | None = None
    approval_state: str | None = None
    invocation_status: str | None = None
    ledger_entry_id: str | None = None
    error_code: str | None = None
    jti: str | None = None
    gateway_reached: bool | None = None
    sdk_exceptions: list[str] = field(default_factory=list)
    invokes: int | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return self.invocation_status == "executed"

    @property
    def armoriq_evidenced(self) -> bool:
        """ArmorIQ evidence rule, parts 1 and 2 (part 3, no tool-server
        contact, needs the database: see `tool_server_contact`)."""
        return self.source in ARMORIQ_SOURCES and bool(self.evidence_ref)


_HOLD_STATES = frozenset({"pending", "approved", "rejected", "expired", "timeout"})


def observe_step(
    decision: Any,
    *,
    approval: Any = None,
    invocation: Any = None,
    holds: Iterable[Any] = (),
    window: Any = None,
    at_mint: bool = False,
) -> StepObservation:
    """Read one gateway result plus its evidence window (no re-decision).

    ``window`` is an `e2.Window` (SDK exceptions and wire requests captured
    through the gateway's `client_factory` seam), or None without hooks.
    """
    calls = list(getattr(window, "calls", None) or [])
    wire = list(getattr(window, "wire", None) or [])
    hooked = bool(getattr(window, "hooked", False))
    exceptions = [call.exception_class for call in calls if call.exception_class]
    invokes = [event for event in wire if event.is_invoke] if hooked else None
    hold_events = list(holds)
    held_ids = [str(event.delegation_id) for event in hold_events if event.delegation_id]
    delegation_id = (
        next(iter(held_ids), None)
        or getattr(approval, "delegation_id", None)
        or getattr(decision, "delegation_id", None)
    )
    approval_state = getattr(getattr(approval, "state", None), "value", None)
    outcome = decision.outcome.value
    gateway_source = decision.source.value
    hold_seen = (
        bool(hold_events)
        or bool(delegation_id)
        or approval_state in _HOLD_STATES
        or "PolicyHoldException" in exceptions
    )
    obs = StepObservation(
        observed=outcome.upper(),
        source=gateway_source,
        gateway_outcome=outcome,
        gateway_source=gateway_source,
        reason=e2.redact_text(decision.reason),
        evidence_ref=None,
        at_mint=at_mint,
        delegation_id=delegation_id,
        approval_state=approval_state,
        sdk_exceptions=exceptions,
        invokes=len(invokes) if invokes is not None else None,
    )
    if invocation is not None:
        obs.invocation_status = invocation.status.value
        obs.ledger_entry_id = invocation.ledger_entry_id
        obs.error_code = invocation.error_code
        obs.jti = invocation.jti
        obs.gateway_reached = invocation.tool_server_reached
    refs: list[str] = []
    if hold_seen:
        obs.observed = "HOLD"
        # The SDK reports a hold (on_hold / PolicyHoldException) only after the
        # ArmorIQ proxy answered with one; the delegation id comes from
        # ArmorIQ's delegation API. That is the layer that HELD.
        obs.source = "armoriq_policy_decision"
        if delegation_id:
            refs.append(f"delegation_id={delegation_id}")
        if hold_events:
            refs.append(f"SDK on_hold fired {len(hold_events)}x")
        if "PolicyHoldException" in exceptions:
            refs.append("SDK PolicyHoldException")
        if approval_state:
            refs.append(f"approval state={approval_state}")
    elif outcome == "allow":
        armoriq_plan_id = getattr(decision, "armoriq_plan_id", None)
        if armoriq_plan_id:
            refs.append(f"armoriq_plan_id={armoriq_plan_id}")
        if invokes:
            refs.append(f"proxy /invoke HTTP {e2._statuses(invokes)}")
    elif gateway_source == "armoriq_policy_decision":
        if "PolicyBlockedException" in exceptions:
            refs.append("SDK PolicyBlockedException")
        enforcement = getattr(decision, "enforcement_action", None)
        if enforcement:
            refs.append(f"enforcement_action={enforcement}")
        if at_mint and refs:
            refs.insert(0, "at plan mint")
        if not at_mint and hooked and not invokes:
            obs.flags.append("policy BLOCK reported but no request reached the proxy /invoke")
    elif gateway_source in ARMORIQ_SOURCES:
        refs.append(f"gateway {gateway_source} ({e2.redact_text(decision.reason)})")
    obs.evidence_ref = "; ".join(refs) or None
    return obs


# --- database evidence (read-only, toolserver role) ----------------------------------

_LEDGER_FOR_SQL = (
    "SELECT id, jti, request_id, trace_id, plan_id, step_index, tool_name, "
    "requester_member_id, validation_outcome, execution_status, created_at "
    "FROM public.tool_execution_ledger WHERE request_id = ANY(%s) OR jti = ANY(%s) "
    "ORDER BY id LIMIT 500"
)
_ATTEMPTS_FOR_SQL = (
    "SELECT id, credential_id, mcp_endpoint, tool_name, outcome, jti, request_id, "
    "created_at FROM public.tool_call_attempts WHERE request_id = ANY(%s) OR jti = ANY(%s) "
    "ORDER BY id LIMIT 500"
)


class ProbeEvidenceLookup(e2.E2EvidenceLookup):
    """Read-only lookups by request_id / jti, plus E2's time-window queries."""

    def ledger_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        return self._rows(_LEDGER_FOR_SQL, (request_ids, jtis))

    def attempts_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        return self._rows(_ATTEMPTS_FOR_SQL, (request_ids, jtis))


DB_SKEW_S = e2.DB_SKEW_S


def collect_evidence(
    dsn: str | None,
    *,
    request_ids: list[str],
    jtis: list[str],
    started_at: str,
    ended_at: str,
    tools: list[str],
    lookup_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Rows attributed to this run (by request_id / jti) and every row in the
    run window for `tools` (to spot unattributed executions). Errors by class
    name only, driver logs muted (never the DSN, host or role)."""
    report: dict[str, Any] = {
        "status": "not started",
        "ledger": [],
        "attempts": [],
        "egress": [],
        "ledger_window": [],
        "attempts_window": [],
        "egress_window": [],
        "notes": [],
    }
    if not dsn:
        report["status"] = "unavailable (no DATABASE_URL_TOOLSERVER)"
        return report
    start = e2._parse_utc(started_at) - timedelta(seconds=DB_SKEW_S)
    end = e2._parse_utc(ended_at) + timedelta(seconds=DB_SKEW_S)
    with e1.quiet_db_logs():
        try:
            lookup = (lookup_factory or ProbeEvidenceLookup)(dsn)
        except Exception as exc:
            report["status"] = f"lookup error ({type(exc).__name__})"
            return report
        try:
            try:
                report["ledger"] = lookup.ledger_for(request_ids, jtis)
                report["attempts"] = lookup.attempts_for(request_ids, jtis)
                report["ledger_window"] = lookup.ledger_window(start, end, tools)
                report["attempts_window"] = lookup.attempts_window(start, end, tools)
                report["status"] = "ok"
            except Exception as exc:
                report["status"] = f"lookup error ({type(exc).__name__})"
                return report
            try:
                report["egress"] = lookup.egress_for(request_ids, jtis)
                report["egress_window"] = lookup.egress_window(start, end)
            except Exception as exc:
                report["notes"].append(f"egress_sink lookup: {type(exc).__name__}")
        finally:
            with contextlib.suppress(Exception):
                lookup.close()
    return report


def rows_for(
    evidence: Mapping[str, Any], request_id: str, jti: str | None
) -> dict[str, list[dict]]:
    """This step's ledger / attempt / egress rows (by request_id or jti)."""

    def mine(row: Mapping[str, Any]) -> bool:
        return row.get("request_id") == request_id or (jti is not None and row.get("jti") == jti)

    return {
        "ledger": [dict(row) for row in evidence.get("ledger", []) if mine(row)],
        "attempts": [dict(row) for row in evidence.get("attempts", []) if mine(row)],
        "egress": [dict(row) for row in evidence.get("egress", []) if mine(row)],
    }


def tool_server_contact(evidence: Mapping[str, Any], mine: Mapping[str, list]) -> str:
    """yes / no / unknown: did the tool server record anything for this step?

    "no" needs a usable evidence database and zero ledger, attempt and
    egress_sink rows for the step's request_id and jti.
    """
    if evidence.get("status") != "ok":
        return "unknown (no DB evidence)"
    if mine["ledger"] or mine["attempts"] or mine["egress"]:
        return "yes"
    return "no"


def unattributed_rows(
    evidence: Mapping[str, Any], request_ids: Iterable[str], jtis: Iterable[str]
) -> list[str]:
    """Window rows (ledger / attempts / egress) that belong to no known step."""
    known_requests, known_jtis = set(request_ids), set(jtis)
    found: list[str] = []
    for kind in ("ledger_window", "attempts_window", "egress_window"):
        for row in evidence.get(kind, []):
            if row.get("request_id") in known_requests or row.get("jti") in known_jtis:
                continue
            found.append(
                f"{kind.removesuffix('_window')} #{row.get('id')} {row.get('tool_name')} "
                f"request_id={row.get('request_id')}"
            )
    return found


def ledger_check(
    mine: Mapping[str, list], run: e1.RunIdentifiers
) -> tuple[bool, list[str], dict | None]:
    """Exactly one ledger row for the step, matching every run identifier."""
    rows = mine["ledger"]
    if len(rows) != 1:
        return False, [f"{len(rows)} ledger row(s) for this step (expected exactly 1)"], None
    mismatches = e1.ledger_mismatches(rows[0], run)
    return not mismatches, mismatches, e1.safe_fields(rows[0], e1.LEDGER_FIELDS)


# --- direct tool-server calls (no ArmorIQ) -------------------------------------------


def member_ctx(
    secret: str,
    *,
    member_id: str,
    persona: str,
    app_role: str,
    tool: str,
    aud: str,
    request_id: str,
    lifetime_s: float = 300.0,
) -> tuple[str, str]:
    """A `_ctx` exactly as the gateway signs it (`sign_ctx_fields`), fresh jti.

    Returns (token, jti); the token is sent only, never printed.
    """
    from services.authorization.armoriq_gateway import sign_ctx_fields
    from services.contracts import DEFAULT_WORKSPACE_ID

    now = datetime.now(UTC)
    jti = uuid.uuid4().hex
    fields = {
        "request_id": request_id,
        "trace_id": f"{request_id}-trace",
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "requester_member_id": member_id,
        "armoriq_member_email_ref": PERSONA_EMAIL_ENV[persona],
        "app_role": app_role,
        "plan_id": f"{request_id}-direct",
        "step_index": 0,
        "tool": tool,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(seconds=lifetime_s),
        "jti": jti,
    }
    return sign_ctx_fields(fields, secret), jti


def post_mcp(
    client: Any, path: str, bearer: str | None, payload: Mapping[str, Any]
) -> tuple[int | None, str, str]:
    """POST one JSON-RPC message; (status, content type, body). Never raises."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        response = client.post(path, json=dict(payload), headers=headers)
    except Exception as exc:
        return None, "", f"transport error ({type(exc).__name__})"
    return response.status_code, response.headers.get("content-type", ""), response.text


def post_tool_call(
    client: Any, path: str, bearer: str | None, tool: str, arguments: dict, rpc_id: str
) -> e2.McpReply:
    """One direct `tools/call` (bearer only, no ArmorIQ), parsed like E2."""
    status, kind, text = post_mcp(
        client,
        path,
        bearer,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    if status is None:
        return e2.McpReply(None, None, text)
    return e2.parse_mcp_response(status, kind, text)


def list_tool_names(client: Any, path: str, bearer: str | None) -> tuple[list[str] | None, str]:
    """Live `tools/list` names for one endpoint, or (None, why). Never the bearer."""
    status, kind, text = post_mcp(
        client, path, bearer, {"jsonrpc": "2.0", "id": "check-setup", "method": "tools/list"}
    )
    if status is None:
        return None, text
    if status != 200:
        return None, f"HTTP {status}"
    media = (kind or "").split(";")[0].strip().lower()
    message = e2._sse_first_message(text) if media == "text/event-stream" else e2._json(text)
    result = message.get("result") if isinstance(message, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return None, "no tools/list result"
    names = [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]
    return names, "ok"


def masked_identity(settings: Any, persona: str) -> str:
    """`w***@g***.com` form of a persona's configured e-mail ("unset" if none)."""
    from apps.api.identity import mask_email

    raw = e1.secret_text(getattr(settings, f"persona_{persona}_email", None)).strip()
    return mask_email(raw) if raw else "unset"


def cell(value: Any) -> str:
    return e2._cell(value)
