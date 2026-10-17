"""E2 probe: off-plan and tampered-call enforcement layers (P1-07, #8).

Drives the real `ArmorIQGateway` (never the raw SDK, ADR-018) and direct
tool-server calls, and prints one evidence row per attempt (attempt /
expected / observed / enforcing layer / evidence) plus per-attempt details,
in the `docs/EXPERIMENTS.md` E2 format. The verdict is the operator's.
Attempts (22, see docs/EXPERIMENTS.md "E2 record"): a planned baseline
whose signed `_ctx` is replayed; direct calls (no bearer, no / forged /
wrong-secret / cross-tool / expired `_ctx`, replay); unplanned tool names
under a team-only plan (SDK plan check); tampering with the PLANNED step
(changed query; `tier`, `user_email`, `_approvals`, `_delegation_id`,
`_policy_context` first through the validator, then injected after it).
Every label is checked independently: wire counts and SDK exception form
(through the gateway's `client_factory` seam), the signed jti (its
`ctx_signer` seam), and read-only ledger / attempt / egress_sink rows.
Never claims server-side plan binding without evidence; the proxy-shape
attempt stays `not tested` (no verified direct-proxy request shape).
Nothing runs on import or in CI; offline tests use fakes only.

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

Exit codes (as E1):
    0  every attempt has an evidenced layer (wire, exception form, DB rows;
       the proxy-shape row is "not tested" by design) and zero unintended
       executions are confirmed: a candidate for the operator's verdict
    1  an unexpected error stopped the probe (exception type only)
    2  prerequisites missing or unusable: arguments given, settings
       unreadable or unset, a bad base URL, the tool server not answering
       `/health` 200, or the evidence database unusable (names only, never
       values). Checked BEFORE any ArmorIQ call.
    3  the probe ran, but some attempt lacks evidence, a label is
       contradicted (flag), or zero executions is not confirmed; read the
       record
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
EXIT_NOT_COMPLETE = 3


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


# --- independent evidence (I2): wire, SDK exceptions, timestamps -------------------

PRE_NETWORK_LABEL = "armoriq_sdk_plan_check"
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+/=.]{32,}")


def utc_now() -> str:
    """UTC timestamp with milliseconds, e.g. `2026-10-17T06:37:17.123Z`."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_text(text: str | None, limit: int = 240) -> str | None:
    """Exception/reason text safe to print: e-mails masked (`e1.mask_emails`),
    token-like runs (>= 32 chars) replaced, length capped."""
    if not text:
        return text
    masked = e1.mask_emails(text) or ""
    return _TOKENISH.sub("<redacted>", masked)[:limit]


@dataclass
class WireEvent:
    """One HTTP request the SDK client sent: method, host, path, status, and
    for `/invoke` the NAMES of the params sent (never values, headers, or
    bodies) plus whether each watched injected value went out unchanged."""

    at: str
    method: str
    host: str
    path: str
    status: int | None = None
    params_keys: tuple[str, ...] | None = None
    injected_sent: dict[str, bool] = field(default_factory=dict)

    @property
    def is_invoke(self) -> bool:
        return self.method == "POST" and self.path.endswith("/invoke")


@dataclass
class SdkCall:
    """One gateway call into the SDK client and how it ended (no values)."""

    method: str
    at: str
    exception_class: str | None = None
    # IntentMismatchException only: the SDK sets `action` when it raises on a
    # proxy HTTP 409 (client.py:976-981), not on its pre-network plan check
    # (client.py:814-819).
    proxy_form: bool | None = None
    message: str | None = None
    status_code: int | None = None


class SdkRecorder:
    """Evidence about the SDK client, collected through the gateway's
    `client_factory` seam: httpx event hooks on the client's `http_client`
    count every request; `RecordingClient` records every exception."""

    def __init__(self) -> None:
        self.events: list[WireEvent] = []
        self.calls: list[SdkCall] = []
        self.watch: dict[str, Any] = {}
        self.hooked = False
        self._pending: dict[int, WireEvent] = {}

    def attach(self, http_client: Any) -> bool:
        hooks = getattr(http_client, "event_hooks", None)
        if not isinstance(hooks, Mapping):
            return False
        http_client.event_hooks = {
            "request": [*hooks.get("request", []), self._on_request],
            "response": [*hooks.get("response", []), self._on_response],
        }
        self.hooked = True
        return True

    def _on_request(self, request: Any) -> None:
        event = WireEvent(
            at=utc_now(),
            method=str(request.method),
            host=str(request.url.host),
            path=str(request.url.path),
        )
        if event.is_invoke:
            try:
                body = json.loads(request.content)
            except Exception:
                body = None
            params = body.get("params") if isinstance(body, Mapping) else None
            if isinstance(params, Mapping):
                event.params_keys = tuple(sorted(str(key) for key in params))
                for key, value in self.watch.items():
                    event.injected_sent[key] = key in params and params[key] == value
        self.events.append(event)
        self._pending[id(request)] = event

    def _on_response(self, response: Any) -> None:
        event = self._pending.pop(id(response.request), None)
        if event is not None:
            event.status = int(response.status_code)


class RecordingClient:
    """Wraps the gateway's SDK client (never imports the SDK, ADR-018):
    attaches the wire hooks and records how each `invoke` /
    `invoke_with_policy` ended. Everything else passes through."""

    def __init__(self, inner: Any, recorder: SdkRecorder) -> None:
        self._inner = inner
        self._recorder = recorder
        recorder.attach(getattr(inner, "http_client", None))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _record(self, method: str, *args: Any, **kwargs: Any) -> Any:
        call = SdkCall(method=method, at=utc_now())
        self._recorder.calls.append(call)
        try:
            return getattr(self._inner, method)(*args, **kwargs)
        except Exception as exc:
            call.exception_class = type(exc).__name__
            if call.exception_class == "IntentMismatchException":
                call.proxy_form = getattr(exc, "action", None) is not None
            call.message = redact_text(str(exc))
            status = getattr(exc, "status_code", None)
            call.status_code = status if isinstance(status, int) else None
            raise

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._record("invoke", *args, **kwargs)

    def invoke_with_policy(self, *args: Any, **kwargs: Any) -> Any:
        return self._record("invoke_with_policy", *args, **kwargs)


@dataclass
class Window:
    """What happened during one attempt: UTC start/end, SDK requests, SDK
    calls, and the `_ctx` envelopes the gateway signed."""

    started_at: str
    ended_at: str = ""
    wire: list[WireEvent] = field(default_factory=list)
    calls: list[SdkCall] = field(default_factory=list)
    signed: list[CapturedCtx] = field(default_factory=list)
    hooked: bool = False


class Observer:
    """Opens one evidence window per attempt (recorder/capture optional, so
    the direct-call helpers also work on their own)."""

    def __init__(self, recorder: SdkRecorder | None = None, capture: Any = None) -> None:
        self.recorder = recorder
        self.capture = capture

    @contextlib.contextmanager
    def window(self, watch: Mapping[str, Any] | None = None) -> Iterator[Window]:
        recorder, capture = self.recorder, self.capture
        marks = (
            len(recorder.events) if recorder else 0,
            len(recorder.calls) if recorder else 0,
            len(capture.captured) if capture else 0,
        )
        if recorder:
            recorder.watch = dict(watch or {})
        window = Window(started_at=utc_now(), hooked=bool(recorder and recorder.hooked))
        try:
            yield window
        finally:
            window.ended_at = utc_now()
            if recorder:
                window.wire = recorder.events[marks[0] :]
                window.calls = recorder.calls[marks[1] :]
                window.hooked = recorder.hooked
                recorder.watch = {}
            if capture:
                window.signed = capture.captured[marks[2] :]


def _statuses(events: list[WireEvent]) -> str:
    return ", ".join(str(event.status) for event in events) or "no response"


def check_wire(item: dict[str, Any]) -> None:
    """Cross-check a gateway label against the wire and the SDK exception.

    A pre-network label (`armoriq_sdk_plan_check`) needs zero requests to
    the proxy `/invoke` and a pre-network-form IntentMismatchException; a
    request where the label claims none is flagged (a proxy 409 names the
    proxy). A reported tool-server result needs a proxy request.
    """
    window: Window | None = item.get("window")
    label = item.get("gateway_label")
    if window is None or label is None:
        return
    flags: list[str] = item.setdefault("flags", [])
    invokes = [event for event in window.wire if event.is_invoke]
    mismatch = next(
        (call for call in window.calls if call.exception_class == "IntentMismatchException"),
        None,
    )
    if label == PRE_NETWORK_LABEL:
        if not window.hooked:
            flags.append(
                "no wire hooks on the SDK client: the pre-network claim of "
                "armoriq_sdk_plan_check cannot be checked"
            )
            item["layer"] = "unknown (no wire evidence)"
        elif invokes:
            flags.append(
                "the label armoriq_sdk_plan_check claims a pre-network rejection, but "
                f"{len(invokes)} request(s) went to the proxy /invoke (HTTP "
                f"{_statuses(invokes)}): gateway ICR"
            )
            if any(event.status == 409 for event in invokes):
                item["layer"] = (
                    "armoriq_proxy_decision (HTTP 409 from the proxy; gateway label "
                    "armoriq_sdk_plan_check: ICR)"
                )
            else:
                item["layer"] = "unknown (gateway label contradicted by the wire)"
        elif mismatch is None or mismatch.proxy_form:
            flags.append("no pre-network IntentMismatchException was observed for this label")
            item["layer"] = "unknown (no SDK exception evidence)"
        else:
            item["wire_ok"] = True
        return
    reached = item.get("status") in ("executed", "rejected_by_tool_server", "unknown")
    proxy_decided = label == "armoriq_policy_decision" and item.get("gateway_outcome") == "block"
    if (reached or proxy_decided) and window.hooked and not invokes:
        flags.append("a proxy/tool-server result was reported but no request reached /invoke")
    elif reached or proxy_decided:
        item["wire_ok"] = window.hooked


E2_TOOL = "knowledge_search_team"
E2_QUERY = e1.E1_QUERY  # the P2-03 seed phrase (team tier)
E2_LIMIT = e1.E1_LIMIT
# A different, equally harmless team-tier query: the argument-tamper case.
TAMPERED_QUERY = "quokka release checklist"
# Injected AFTER validation into the planned step (key, value). Key names may
# be printed; values never are.
TAMPER_INJECTIONS: tuple[tuple[str, Any], ...] = (
    ("tier", "restricted"),
    ("user_email", "attacker@example.invalid"),
    # 999 is what the SDK itself sends on an approved retry (client.py:1765).
    ("_approvals", 999),
    ("_delegation_id", "dlg-forged-e2"),
    ("_policy_context", {"forged-policy": True, "allow": True}),
)
_TAMPER_EXPECTED = {
    "tier": "tool server `invalid_arguments` (strict schema), rejected row, no execution",
    "user_email": "tool server `invalid_arguments` (strict schema), rejected row, no execution",
    "_approvals": "stripped by the tool server (no effect): executes exactly as planned",
    "_delegation_id": "stripped by the tool server (no effect): executes exactly as planned",
    "_policy_context": (
        "overwritten by the SDK before sending, stripped by the tool server: "
        "executes exactly as planned"
    ),
}


REQUEST_ID_PREFIX = "e2"
PLAN_SUMMARY = "E2: one planned team-tier search (off-plan and tamper probe)"


def planned_arguments(query: str = E2_QUERY) -> dict[str, Any]:
    return {"query": query, "limit": E2_LIMIT}


def build_maya_request(
    settings: Any,
    *,
    prefix: str = REQUEST_ID_PREFIX,
    summary: str = PLAN_SUMMARY,
    query: str = E2_QUERY,
) -> tuple[Any, Any]:
    """One-step Maya plan for `knowledge_search_team` (real identity path, as
    E1's builder), with the request-id prefix and plan summary as parameters.
    The request id is unique per call (the evidence is correlated by it)."""
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

    record = identity.build_member_table(settings).get(PersonaKey.MAYA)
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
    request_id = f"{prefix}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    ctx = RequestContext(
        request_id=request_id,
        trace_id=f"{request_id}-trace",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester=requester,
        entry_point=EntryPoint.WEB,
        received_at=now,
    )
    plan = PlanProposal(
        summary=summary,
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments=planned_arguments(query),
                purpose="E2 probe: one planned team-tier search",
            )
        ],
    )
    return ctx, plan


def plan_failure_row(
    handle: Any, ctx: Any, *, group: str, case: str, attempt: str, expected: str
) -> dict[str, Any]:
    """A case whose plan/mint failed: rendered through the gateway's own
    `StepOutcome(decision=...)` and `e1.describe_outcome` (masked reason)."""
    from services.authorization.armoriq_gateway import StepOutcome

    described = e1.describe_outcome(StepOutcome(decision=handle.decision))
    return row(
        group=group,
        attempt=attempt,
        expected=expected,
        observed=(
            f"not run: plan/mint {described['decision_outcome']} ({described['decision_source']})"
        ),
        layer="unknown",
        evidence="gateway log",
        case=case,
        request_id=ctx.request_id,
        **gateway_facts(handle.decision),
    )


def tampered_plan(plan: Any, arguments: Mapping[str, Any]) -> Any:
    """The same one-step plan with step 0's arguments replaced, built with
    `model_construct` so the PlannedToolCall validator does NOT run: what a
    compromised caller could hand the gateway after validation."""
    from services.contracts import PlannedToolCall, PlanProposal

    step = plan.steps[0]
    forged = PlannedToolCall.model_construct(
        step_index=step.step_index,
        mcp=step.mcp,
        tool=step.tool,
        arguments=dict(arguments),
        purpose=step.purpose,
    )
    return PlanProposal.model_construct(
        summary=plan.summary,
        steps=[forged],
        escalation_user_confirmed=False,
        matched_topic_rule_id=None,
    )


async def start_case_plan(
    gateway: Any, settings: Any, case: str, attempt: str, expected: str
) -> tuple[Any, Any, Any, dict[str, Any] | None]:
    """A FRESH one-step team plan for one case (no step is ever invoked twice
    under one plan, so step reuse cannot confound a result). Returns (ctx,
    plan, handle, failure_row)."""
    ctx, plan = build_maya_request(settings)
    handle = await gateway.start_plan(ctx, plan)
    if handle.decision is None:
        return ctx, plan, handle, None
    failed = plan_failure_row(
        handle, ctx, group="setup", case=case, attempt=attempt, expected=expected
    )
    return ctx, plan, handle, failed


def gateway_facts(decision: Any) -> dict[str, Any]:
    """The gateway's own verdict, kept apart from the evidence-checked layer."""
    return {
        "gateway_label": decision.source.value,
        "gateway_outcome": decision.outcome.value,
        "reason": e1.mask_emails(decision.reason),
    }


async def group_off_plan(
    gateway: Any, settings: Any, observer: Observer | None = None
) -> list[dict[str, Any]]:
    """Group 1 (§9.1): unplanned tool NAMES under a team-only plan.

    `attempt_off_plan` calls the SDK's plain `invoke`, which compares the
    action name with the plan's steps before any network call
    (client.py:804-819); the Merkle proof covers only `/steps/[i]/action`,
    so this group tests names, never arguments (that is group 3).
    """
    from services.contracts import ToolName

    attempts = (
        ("offplan-confidential", ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, planned_arguments()),
        (
            "offplan-honeypot",
            ToolName.SEND_EXTERNAL_MESSAGE,
            {"message": "E2 probe: must never be delivered"},
        ),
    )
    expected = "BLOCK armoriq_sdk_plan_check before any network call; never executed"
    ctx, _, handle, failed = await start_case_plan(
        gateway, settings, attempts[0][0], "off-plan attempts", expected
    )
    observer = observer or Observer()
    rows = []
    for case, tool, args in attempts:
        attempt = f"unplanned `{tool.value}` under a knowledge_search_team-only plan"
        if failed is not None:
            rows.append({**failed, "case": case, "attempt": attempt})
            continue
        with observer.window() as window:
            decision = await gateway.attempt_off_plan(handle, tool, dict(args), ctx)
        rows.append(
            row(
                group="off-plan",
                attempt=attempt,
                expected=expected,
                observed=f"{decision.outcome.value} ({decision.source.value})",
                layer=decision.source.value,
                evidence="gateway decision + 0 proxy requests (wire hooks) + SDK exception form",
                case=case,
                request_id=ctx.request_id,
                tool=tool.value,
                window=window,
                **gateway_facts(decision),
            )
        )
    return rows


def validator_row(key: str, value: Any, observer: Observer | None = None) -> dict[str, Any]:
    """§9.3 first half: the PlannedToolCall validator on the injected key.

    Local only (the window shows no SDK request). Prints the pydantic error
    types, never the offending value.
    """
    from pydantic import ValidationError

    from services.contracts import McpEndpoint, PlannedToolCall, ToolName

    attempt = f"planned step with `{key}` added, built through the PlannedToolCall validator"
    expected = "rejected by the validator (application_rule) before any network call"
    error: ValidationError | None = None
    with (observer or Observer()).window() as window:
        try:
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={**planned_arguments(), key: value},
                purpose="E2 tamper probe",
            )
        except ValidationError as exc:
            error = exc
    if error is not None:
        kinds = ", ".join(sorted({str(item.get("type")) for item in error.errors()}))
        return row(
            group="tamper",
            attempt=attempt,
            expected=expected,
            observed=f"rejected locally: ValidationError ({kinds})",
            layer="application_rule",
            evidence="PlannedToolCall validator (services/contracts/models.py); 0 requests",
            case=f"validator-{key}",
            key=key,
            window=window,
            local_ok=not window.wire,
        )
    return row(
        group="tamper",
        attempt=attempt,
        expected=expected,
        observed="ACCEPTED by the validator (unexpected)",
        layer="unknown",
        evidence="none",
        case=f"validator-{key}",
        key=key,
        window=window,
    )


def invoke_layer(described: Mapping[str, Any], *, strips: bool = False) -> str:
    """Layer for a tampered planned invoke, from what the call returned.

    A tool-server code names the tool server; an executed call with forged
    `_` keys means the tool server stripped them (`strips`); an executed call
    otherwise means no layer stopped it.
    """
    status = described.get("invocation_status")
    if status == "rejected_by_tool_server":
        return "tool_server_rule"
    if status == "executed":
        return "tool_server_rule (stripped `_` keys)" if strips else "none (executed)"
    if status in (None, "not_reached"):
        return str(described.get("decision_source"))
    return "unknown"


def _invoke_observed(described: Mapping[str, Any]) -> str:
    """ArmorIQ's part and the tool server's part of one invoke, separately."""
    status = described.get("invocation_status")
    source = described.get("decision_source")
    if status in ("executed", "rejected_by_tool_server", "unknown"):
        armoriq = "ArmorIQ: allowed (the proxy forwarded the call)"
    elif source == "armoriq_policy_decision":
        armoriq = f"ArmorIQ: {described['decision_outcome']} (armoriq_policy_decision)"
    else:
        armoriq = f"gateway: {described['decision_outcome']} ({source})"
    return (
        f"{armoriq}; tool server: status={status} "
        f"error_code={described['error_code']} "
        f"ledger_entry_id={described['ledger_entry_id']}"
    )


async def run_tampered_invoke(
    gateway: Any,
    settings: Any,
    *,
    case: str,
    attempt: str,
    expected: str,
    arguments: Mapping[str, Any],
    strips: bool,
    watch: Mapping[str, Any] | None = None,
    observer: Observer | None = None,
) -> dict[str, Any]:
    """Invoke step 0 of a fresh valid plan with ``arguments`` swapped in
    (`gateway.invoke_step(handle, 0, ctx, tampered_plan)`): the SDK finds the
    step by its (unchanged) action name, so the request goes to the proxy.
    ``watch`` names injected values to look for in the `/invoke` body."""
    ctx, plan, handle, failed = await start_case_plan(gateway, settings, case, attempt, expected)
    if failed is not None:
        return failed
    with (observer or Observer()).window(watch) as window:
        outcome = await gateway.invoke_step(handle, 0, ctx, tampered_plan(plan, arguments))
    described = e1.describe_outcome(outcome)
    return row(
        group="tamper",
        attempt=attempt,
        expected=expected,
        observed=_invoke_observed(described),
        layer=invoke_layer(described, strips=strips),
        evidence="gateway result + tool-server code / `_meta.ledger_entry_id`",
        case=case,
        request_id=ctx.request_id,
        jti=described["jti"],
        status=described["invocation_status"],
        error_code=described["error_code"],
        ledger_entry_id=described["ledger_entry_id"],
        business_arguments={k: v for k, v in arguments.items() if not k.startswith("_")},
        window=window,
        **gateway_facts(outcome.decision),
    )


async def group_tamper(
    gateway: Any, settings: Any, observer: Observer | None = None
) -> list[dict[str, Any]]:
    """Group 3 (§9.3): tampering with the PLANNED step under a valid plan.

    The changed query asks the server-side argument-binding question. Each
    injected key is first put through the validator (application_rule),
    then injected after validation and invoked, recording both the ArmorIQ
    decision and the tool-server result.
    """
    rows = [
        await run_tampered_invoke(
            gateway,
            settings,
            case="tamper-query",
            attempt="planned step invoked with a CHANGED query (same tool, same tier)",
            expected=(
                "ArmorIQ refuses changed arguments (proxy evidence), or the changed query "
                "executes: server-side argument binding disproven"
            ),
            arguments=planned_arguments(TAMPERED_QUERY),
            strips=False,
            observer=observer,
        )
    ]
    for key, value in TAMPER_INJECTIONS:
        rows.append(validator_row(key, value, observer))
        rows.append(
            await run_tampered_invoke(
                gateway,
                settings,
                case=f"inject-{key}",
                attempt=f"`{key}` injected into the planned step after validation",
                expected=_TAMPER_EXPECTED[key],
                arguments={**planned_arguments(), key: value},
                strips=key.startswith("_"),
                watch={key: value},
                observer=observer,
            )
        )
    return rows


def argument_binding_verdict(rows: list[dict[str, Any]]) -> str:
    """Server-side argument binding, from the changed-query case only, read
    from the evidence-checked layer (run after `check_wire` and the DB
    evidence), never from the gateway label alone."""
    item = next((entry for entry in rows if entry.get("case") == "tamper-query"), None)
    if item is None or (item.get("status") is None and item.get("layer") == "unknown"):
        return "arguments: NOT DETERMINED (the changed-query case did not complete)"
    layer = str(item.get("layer"))
    if item.get("status") == "executed":
        hashed = {True: "yes", False: "NO"}.get(item.get("hash_match"), "not checked")
        return (
            "arguments: DISPROVEN: the proxy forwarded a changed query under a valid plan "
            f"and the tool server executed it (ledger entry {item.get('ledger_entry_id')}; "
            f"business-argument hash = tampered arguments: {hashed})"
        )
    if layer.startswith("armoriq_proxy_decision"):
        return (
            "arguments: PROVEN at the proxy: the changed query was refused (HTTP 409) "
            "before the tool server (gateway label contradicted: ICR)"
        )
    if layer == "armoriq_policy_decision":
        return (
            "arguments: NOT DETERMINED: an ArmorIQ policy decision blocked the changed "
            "query (a policy outcome, not evidence of plan binding)"
        )
    if item.get("status") == "rejected_by_tool_server":
        return (
            "arguments: NOT DETERMINED: the changed query reached the tool server and was "
            f"rejected there ({item.get('error_code')}), not by ArmorIQ"
        )
    return f"arguments: NOT DETERMINED ({item.get('observed')}; layer {layer})"


def group_proxy_shape() -> list[dict[str, Any]]:
    """Group 2 (§9.2): recorded not-tested — no verified shape exists."""
    return [
        row(
            group="proxy-shape",
            attempt="direct proxy off-plan request bypassing the SDK check",
            expected="not tested absent a verified request shape",
            observed="not tested",
            layer="not tested",
            evidence="none: no verified direct-proxy shape exists in-tree (Day-1 doc rule)",
            case="proxy-shape",
            window=Window(started_at=utc_now(), ended_at=utc_now()),
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


@dataclass(frozen=True)
class CapturedCtx:
    """One `_ctx` the gateway signed (captured at its `ctx_signer` seam).

    ``token`` is kept in memory only, for the replay case; it is excluded
    from `repr` and never printed.
    """

    token: str = field(repr=False)
    jti: str
    tool: str
    aud: str
    request_id: str
    issued_at: float
    expires_at: float


def _epoch(value: Any) -> float:
    return value.timestamp() if isinstance(value, datetime) else float(value)


class CtxCapture:
    """`ctx_signer` for the gateway: signs with the real signer (P1-04
    `sign_ctx` via `armoriq_gateway.sign_ctx_fields`) and remembers what it
    signed, so the probe knows each planned call's jti and can replay one
    `_ctx` byte for byte. Tests inject a fake inner signer."""

    def __init__(self, inner: Any = None) -> None:
        self._inner = inner
        self.captured: list[CapturedCtx] = []

    def __call__(self, fields: Mapping[str, Any], secret: str) -> str:
        inner = self._inner
        if inner is None:
            from services.authorization.armoriq_gateway import sign_ctx_fields

            inner = sign_ctx_fields
        token = inner(fields, secret)
        self.captured.append(
            CapturedCtx(
                token=token,
                jti=str(fields["jti"]),
                tool=str(fields["tool"]),
                aud=str(fields["aud"]),
                request_id=str(fields["request_id"]),
                issued_at=_epoch(fields["iat"]),
                expires_at=_epoch(fields["exp"]),
            )
        )
        return token

    def since(self, mark: int) -> list[CapturedCtx]:
        return self.captured[mark:]


@dataclass(frozen=True)
class ReplaySource:
    """A `_ctx` that already executed once, to POST again within its lifetime."""

    token: str = field(repr=False)
    jti: str
    ledger_entry_id: str
    issued_at: float
    expires_at: float
    arguments: Mapping[str, Any]


# Maya's identity as the gateway signs it (`_sign_step_ctx`; ADR-020: the
# env-key reference, never the e-mail). Used only for probe-minted envelopes.
MAYA_MEMBER_ID = "m-maya"
MAYA_EMAIL_REF = "PERSONA_MAYA_EMAIL"
EXPIRED_CTX_AGE_S = 700.0  # iat this far in the past ...
EXPIRED_CTX_LIFETIME_S = 300.0  # ... with the gateway's lifetime: exp is 400 s ago
REPLAY_MARGIN_S = 15.0  # never replay a capture this close to its expiry


def mint_ctx(
    secret: str,
    *,
    tool: str,
    request_id: str,
    iat: float,
    exp: float,
    aud: str = "memory-mcp",
) -> tuple[str, str]:
    """A well-formed `_ctx` for Maya (fresh jti), signed with ``secret``.

    Returns (token, jti). The token is never printed.
    """
    from services.authorization.ctx_envelope import CtxEnvelope, sign_ctx
    from services.contracts import DEFAULT_WORKSPACE_ID

    jti = uuid.uuid4().hex
    envelope = CtxEnvelope(
        request_id=request_id,
        trace_id=f"{request_id}-trace",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester_member_id=MAYA_MEMBER_ID,
        armoriq_member_email_ref=MAYA_EMAIL_REF,
        app_role="manager",
        plan_id=f"{request_id}-direct",
        step_index=0,
        tool=tool,
        aud=aud,
        iat=iat,
        exp=exp,
        jti=jti,
    )
    return sign_ctx(envelope, secret), jti


def observed_call(
    observer: Observer | None,
    client: Any,
    bearer: str | None,
    tool: str,
    arguments: dict,
    case: str,
) -> tuple[McpReply, Window]:
    """One direct call inside its own evidence window (UTC timestamps)."""
    with (observer or Observer()).window() as window:
        reply = post_tool_call(client, bearer, tool, arguments, case)
    return reply, window


def direct_replay_row(
    client: Any, bearer: str, source: ReplaySource, observer: Observer | None = None
) -> dict[str, Any]:
    """Replay (§9.4): POST an already-executed, still-valid `_ctx` again.

    Expected `replay` with `_meta.original_entry_id` equal to the first
    call's `_meta.ledger_entry_id`. A capture at or past its expiry is not
    sent (it would test expiry, not replay).
    """
    attempt = "replayed `_ctx` (same jti, same arguments, within CTX_TTL_S)"
    expected = "replay with original_entry_id = the first call's ledger entry"
    age = time.time() - source.issued_at
    lifetime = source.expires_at - source.issued_at
    if time.time() >= source.expires_at - REPLAY_MARGIN_S:
        return row(
            group="direct",
            attempt=attempt,
            expected=expected,
            observed=(
                f"not sent: the captured `_ctx` is {age:.0f} s old (lifetime {lifetime:.0f} s)"
            ),
            layer="unknown",
            evidence="none (re-run: replay must follow the planned call within CTX_TTL_S)",
            case="direct-replay",
            code=None,
            jti=source.jti,
            window=Window(started_at=utc_now(), ended_at=utc_now()),
        )
    reply, window = observed_call(
        observer,
        client,
        bearer,
        "knowledge_search_team",
        {**dict(source.arguments), "_ctx": source.token},
        "direct-replay",
    )
    item = direct_row("direct-replay", attempt, expected, reply)
    item["window"] = window
    same = reply.original_entry_id == source.ledger_entry_id
    item["observed"] += (
        f"; replayed {age:.0f} s after signing; original_entry_id "
        + ("matches the first call's ledger entry" if same else "does NOT match the first call")
        + f" ({source.ledger_entry_id})"
    )
    item["jti"] = source.jti
    item["matches_original"] = same
    return item


def group_direct(
    client: Any,
    bearer: str,
    *,
    ctx_secret: str,
    request_id: str,
    observer: Observer | None = None,
    spacing_s: float = 0.0,
) -> list[dict[str, Any]]:
    """Group 4 (§9.4): direct calls to the tool server (no ArmorIQ).

    ``client`` is an httpx-style client rooted at the tool-server base URL
    (`build_direct_client`; tests pass a `TestClient`). The bearer, the
    secrets, and every token are sent only and never printed. The replay
    case runs separately (`direct_replay_row`), right after the planned
    call it replays.
    """
    team = "knowledge_search_team"
    args = {"query": "quokka", "limit": 3}
    now = time.time()
    wrong_secret, wrong_jti = mint_ctx(
        secrets.token_hex(32), tool=team, request_id=f"{request_id}-ws", iat=now, exp=now + 300
    )
    cross_tool, cross_jti = mint_ctx(
        ctx_secret, tool=team, request_id=f"{request_id}-ct", iat=now, exp=now + 300
    )
    expired, expired_jti = mint_ctx(
        ctx_secret,
        tool=team,
        request_id=f"{request_id}-ex",
        iat=now - EXPIRED_CTX_AGE_S,
        exp=now - EXPIRED_CTX_AGE_S + EXPIRED_CTX_LIFETIME_S,
    )
    calls = (
        ("direct-no-bearer", "direct call, no bearer", "401 unauthorized (bearer guard)", None,
         team, dict(args)),
        ("direct-no-ctx", "direct call, bearer only, no `_ctx`", "invalid_context", bearer,
         team, dict(args)),
        ("direct-forged-ctx", "direct call, forged (malformed) `_ctx`", "invalid_context", bearer,
         team, {**args, "_ctx": "forged.token"}),
        ("direct-wrong-secret", "direct call, `_ctx` signed with a wrong secret",
         "invalid_context (HMAC mismatch)", bearer, team, {**args, "_ctx": wrong_secret}),
        ("direct-cross-tool",
         "direct call, `_ctx` minted for knowledge_search_team used on "
         "knowledge_search_confidential",
         "invalid_context (tool mismatch); never executed", bearer,
         "knowledge_search_confidential", {**args, "_ctx": cross_tool}),
        ("direct-expired", "direct call, expired `_ctx` (exp 400 s ago, lifetime 300 s)",
         "context_expired", bearer, team, {**args, "_ctx": expired}),
    )  # fmt: skip
    minted = {
        "direct-wrong-secret": wrong_jti,
        "direct-cross-tool": cross_jti,
        "direct-expired": expired_jti,
    }
    rows = []
    for case, attempt, expected, key, tool, arguments in calls:
        reply, window = observed_call(observer, client, key, tool, arguments, case)
        rows.append(
            {
                **direct_row(case, attempt, expected, reply),
                "window": window,
                "tool": tool,
                "minted_jti": minted.get(case),
            }
        )
        time.sleep(spacing_s)
    return rows


# --- zero-execution evidence (I3): ledger, attempts, egress sink -------------------

E2_TOOLS = ("knowledge_search_team", "knowledge_search_confidential", "send_external_message")
DB_SKEW_S = 5.0  # local clock vs Neon `now()` / `clock_timestamp()`
# Column lists are allow-listed: `arguments_sha256` is read for a local
# comparison only (never printed); no message, arguments, or hash-chain column.
_LEDGER_WINDOW_SQL = (
    "SELECT id, jti, request_id, trace_id, plan_id, step_index, tool_name, "
    "requester_member_id, validation_outcome, execution_status, arguments_sha256, "
    "created_at FROM public.tool_execution_ledger "
    "WHERE created_at BETWEEN %s AND %s AND tool_name = ANY(%s) ORDER BY id LIMIT 500"
)
_ATTEMPTS_WINDOW_SQL = (
    "SELECT id, credential_id, mcp_endpoint, tool_name, outcome, jti, request_id, "
    "original_entry_id, created_at FROM public.tool_call_attempts "
    "WHERE created_at BETWEEN %s AND %s AND tool_name = ANY(%s) ORDER BY id LIMIT 500"
)
_EGRESS_WINDOW_SQL = (
    "SELECT id, request_id, tool_name, created_at FROM public.egress_sink "
    "WHERE created_at BETWEEN %s AND %s ORDER BY id LIMIT 100"
)
# `jti` exists since migration 0012 (P2-10 follow-up).
_EGRESS_BY_ID_SQL = (
    "SELECT id, request_id, tool_name, jti, created_at FROM public.egress_sink "
    "WHERE request_id = ANY(%s) OR jti = ANY(%s) ORDER BY id LIMIT 100"
)
# Codes the tool server records as its own ledger row (post-verification).
_LEDGER_REJECTION_CODES = frozenset({"invalid_arguments", "out_of_scope", "precondition_failed"})
# Cases allowed to execute: the planned baseline, the argument-binding
# question, and the forged `_`-key cases (which must execute AS PLANNED).
EXECUTION_ALLOWED = frozenset(
    {
        "planned-baseline",
        "tamper-query",
        "inject-_approvals",
        "inject-_delegation_id",
        "inject-_policy_context",
    }
)


class E2EvidenceLookup(e1.NeonLedgerLookup):
    """Read-only window queries over one private toolserver-role pool."""

    def ping(self) -> None:
        self._rows("SELECT 1 AS ok", ())

    def ledger_window(self, start: datetime, end: datetime, tools: list[str]) -> list[dict]:
        return self._rows(_LEDGER_WINDOW_SQL, (start, end, tools))

    def attempts_window(self, start: datetime, end: datetime, tools: list[str]) -> list[dict]:
        return self._rows(_ATTEMPTS_WINDOW_SQL, (start, end, tools))

    def egress_window(self, start: datetime, end: datetime) -> list[dict]:
        return self._rows(_EGRESS_WINDOW_SQL, (start, end))

    def egress_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        return self._rows(_EGRESS_BY_ID_SQL, (request_ids, jtis))


def _parse_utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def collect_db_evidence(
    dsn: str | None,
    *,
    started_at: str,
    ended_at: str,
    request_ids: list[str],
    jtis: list[str],
    lookup_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Run the read-only evidence queries for the run window (+- DB_SKEW_S).

    Errors are reported by class name only, with driver logs muted
    (`e1.quiet_db_logs`): never the DSN, host, or role.
    """
    from datetime import timedelta

    start = _parse_utc(started_at) - timedelta(seconds=DB_SKEW_S)
    end = _parse_utc(ended_at) + timedelta(seconds=DB_SKEW_S)
    report: dict[str, Any] = {
        "status": "not started",
        "window": (start.isoformat(), end.isoformat()),
        "ledger": [],
        "attempts": [],
        "egress_window": [],
        "egress_ids": [],
        "notes": [],
    }
    if not dsn:
        report["status"] = "unavailable (no DATABASE_URL_TOOLSERVER)"
        return report
    with e1.quiet_db_logs():
        try:
            lookup = (lookup_factory or E2EvidenceLookup)(dsn)
        except Exception as exc:
            report["status"] = f"lookup error ({type(exc).__name__})"
            return report
        try:
            try:
                report["ledger"] = lookup.ledger_window(start, end, list(E2_TOOLS))
                report["attempts"] = lookup.attempts_window(start, end, list(E2_TOOLS))
                report["egress_window"] = lookup.egress_window(start, end)
                report["status"] = "ok"
            except Exception as exc:
                report["status"] = f"lookup error ({type(exc).__name__})"
                return report
            try:
                report["egress_ids"] = lookup.egress_for(request_ids, jtis)
            except Exception as exc:
                # Without migration 0012 the sink has no jti column.
                report["notes"].append(f"egress_sink by request_id/jti: {type(exc).__name__}")
        finally:
            with contextlib.suppress(Exception):
                lookup.close()
    return report


def _business_hash(arguments: Mapping[str, Any]) -> str:
    from services.toolserver.ledger import arguments_sha256

    return arguments_sha256(arguments)


def _attempt_text(row_: Mapping[str, Any]) -> str:
    jti = row_.get("jti")
    text = (
        f"attempt row #{row_.get('id')} ({row_.get('tool_name')}, {row_.get('outcome')}, "
        + (f"jti {jti}" if jti else "jti NULL")
        + ")"
    )
    if row_.get("original_entry_id") is not None:
        text += f" original_entry_id={row_['original_entry_id']}"
    return text


def _ledger_text(row_: Mapping[str, Any]) -> str:
    return (
        f"ledger row #{row_.get('id')} ({row_.get('tool_name')}, "
        f"validation_outcome={row_.get('validation_outcome')}, "
        f"execution_status={row_.get('execution_status')})"
    )


def _row_jtis(item: Mapping[str, Any]) -> list[str]:
    window: Window | None = item.get("window")
    jtis = [entry.jti for entry in window.signed] if window else []
    for key in ("jti", "minted_jti"):
        if item.get(key) and item[key] not in jtis:
            jtis.append(str(item[key]))
    return jtis


def apply_db_evidence(rows: list[dict[str, Any]], db: Mapping[str, Any]) -> dict[str, Any]:
    """Attribute every ledger / attempt / egress row of the window to an
    attempt and corroborate what each attempt reported.

    Ledger rows and verified attempt rows (`accepted`, `replay`) carry the
    jti; rejected attempts (`invalid_context`, `context_expired`) carry NULL
    identity (0008), so they are matched by (tool, outcome) in run order.
    Sets ``db_ok`` / ``db_lines`` per row; returns the run summary with the
    unattributed rows and every reason zero execution is not confirmed.
    """
    summary: dict[str, Any] = {"status": db.get("status"), "unintended": [], "unattributed": []}
    if db.get("status") != "ok":
        summary["unintended"].append(f"DB evidence {db.get('status')}")
        return summary
    ledger = list(db["ledger"])
    attempts = list(db["attempts"])
    used_attempts: set[Any] = set()
    used_ledger: set[Any] = set()

    def take_attempt(tool: str, outcome: str, jti: str | None) -> dict | None:
        for candidate in attempts:
            if candidate.get("id") in used_attempts:
                continue
            if candidate.get("tool_name") != tool or candidate.get("outcome") != outcome:
                continue
            if (candidate.get("jti") or None) != jti:
                continue
            used_attempts.add(candidate.get("id"))
            return candidate
        return None

    def expect_attempt(lines: list[str], tool: str, outcome: str, jti: str | None) -> dict | None:
        found = take_attempt(tool, outcome, jti)
        lines.append(_attempt_text(found) if found else f"expected {outcome} attempt row not found")
        return found

    for number, item in enumerate(rows, start=1):
        lines: list[str] = item.setdefault("db_lines", [])
        case = str(item.get("case"))
        tool = str(item.get("tool") or E2_TOOL)
        jtis = _row_jtis(item)
        own_ledger = [entry for entry in ledger if entry.get("jti") in jtis]
        status, code = item.get("status"), item.get("code") or item.get("error_code")
        ok = True
        if case == "direct-replay" or (status == "rejected_by_tool_server" and code == "replay"):
            found = expect_attempt(lines, tool, "replay", str(item.get("jti")))
            ok = found is not None and str(found.get("original_entry_id")) == str(
                item.get("original_entry_id")
            )
            # The replayed jti's only ledger row is the original call's.
            own_ledger = [
                entry
                for entry in own_ledger
                if str(entry.get("id")) != str(item.get("original_entry_id"))
            ]
            ok = ok and not own_ledger
        elif code in ("invalid_context", "context_expired"):
            ok = expect_attempt(lines, tool, str(code), None) is not None and not own_ledger
        elif item.get("group") == "direct":
            if code in BEARER_GUARD_CODES:
                lines.append("no attempt row (the bearer guard answers before verification)")
            else:
                ok = False
                lines.append(f"no corroboration rule for this reply (code {code})")
            ok = ok and not own_ledger
        elif status in ("executed", "rejected_by_tool_server"):
            found = expect_attempt(lines, tool, "accepted", str(item.get("jti")))
            entry = next(
                (
                    row_
                    for row_ in own_ledger
                    if str(row_.get("id")) == str(item["ledger_entry_id"])
                ),
                None,
            )
            lines.append(_ledger_text(entry) if entry else "ledger row for this jti not found")
            ok = found is not None and entry is not None
            if entry is not None and status == "executed":
                ok = ok and entry.get("execution_status") == "executed"
                expected = item.get("business_arguments")
                if expected is not None:
                    which = "tampered" if case == "tamper-query" else "planned"
                    match = entry.get("arguments_sha256") == _business_hash(expected)
                    item["hash_match"] = match
                    lines.append(
                        f"business-argument hash = {which} arguments: {'yes' if match else 'NO'}"
                    )
                    ok = ok and match
            elif entry is not None:
                ok = ok and (
                    code in _LEDGER_REJECTION_CODES
                    and entry.get("validation_outcome") == code
                    and entry.get("execution_status") == "rejected_by_tool_server"
                )
        elif status == "unknown":
            # Allowed result without a ledger reference: reach is claimed, but
            # nothing ties it to a row; never corroborated (E1: never EXECUTED).
            found = take_attempt(tool, "accepted", str(item.get("jti")))
            ok = False
            lines.append(
                (_attempt_text(found) if found else "no accepted attempt row")
                + "; the reply carried no ledger reference, so execution is unproven"
            )
            lines.extend(_ledger_text(entry) for entry in own_ledger)
        elif item.get("gateway_label") is not None:
            # Not reached (pre-network or ArmorIQ block): no row may carry its jti.
            own_attempts = [entry for entry in attempts if entry.get("jti") in jtis]
            ok = not own_ledger and not own_attempts
            lines.append(
                "no ledger or attempt row for its jti(s)"
                if ok
                else "rows found for a call reported as not reaching the tool server"
            )
        used_ledger.update(entry.get("id") for entry in own_ledger)
        if own_ledger and item.get("group") == "direct":
            lines.extend(_ledger_text(entry) for entry in own_ledger)
        item["db_ok"] = ok
        if case not in EXECUTION_ALLOWED:
            for entry in own_ledger:
                if entry.get("execution_status") == "executed":
                    summary["unintended"].append(f"#{number} {case} executed")
    stray_ledger = [entry for entry in ledger if entry.get("id") not in used_ledger]
    stray_attempts = [entry for entry in attempts if entry.get("id") not in used_attempts]
    summary["unattributed"] = [_ledger_text(entry) for entry in stray_ledger] + [
        _attempt_text(entry) for entry in stray_attempts
    ]
    for entry in stray_ledger:
        if entry.get("execution_status") == "executed":
            summary["unintended"].append(
                f"unattributed executed ledger row #{entry.get('id')} ({entry.get('tool_name')})"
            )
    egress, egress_ids = len(db["egress_window"]), len(db["egress_ids"])
    if egress:
        summary["unintended"].append(f"{egress} egress_sink row(s) in the window")
    if egress_ids:
        summary["unintended"].append(f"{egress_ids} egress_sink row(s) carry this run's ids")
    for item in rows:
        if item.get("group") != "off-plan":
            continue
        tool = str(item.get("tool"))
        stray = [entry for entry in stray_ledger + stray_attempts if entry.get("tool_name") == tool]
        sink = tool == "send_external_message" and bool(egress or egress_ids)
        item["db_ok"] = not stray and not sink
        item["db_lines"] = [
            f"no ledger, attempt, or egress_sink row for {tool} outside attributed cases"
            if item["db_ok"]
            else f"{len(stray)} unattributed row(s) for {tool}"
            + ("; egress_sink rows present" if sink else "")
        ]
    summary["counts"] = (
        f"ledger rows: {len(ledger)}; attempt rows: {len(attempts)}; "
        f"egress_sink rows: {egress} in the window, {egress_ids} by request_id/jti"
    )
    return summary


# --- verdict and exit code (I4) ------------------------------------------------------


class PrerequisiteError(Exception):
    """A prerequisite is unusable (exit 2). The message names it, never a value."""


def preflight(client: Any, ledger_dsn: str | None, lookup_factory: Any = None) -> None:
    """Before any ArmorIQ call: the tool server answers `/health` 200 (Render
    awake, embedding model ready) and the evidence database is usable, so a
    run never creates live rows it cannot then prove."""
    try:
        response = client.get("/health")
    except Exception as exc:
        raise PrerequisiteError(f"tool server /health unreachable ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise PrerequisiteError(
            f"tool server /health returned HTTP {response.status_code} "
            "(wake the Render service, wait for 200, and retry)"
        )
    if not ledger_dsn:
        return
    with e1.quiet_db_logs():
        try:
            lookup = (lookup_factory or E2EvidenceLookup)(ledger_dsn)
            try:
                lookup.ping()
            finally:
                with contextlib.suppress(Exception):
                    lookup.close()
        except Exception as exc:
            raise PrerequisiteError(
                f"DATABASE_URL_TOOLSERVER unusable ({type(exc).__name__})"
            ) from None


NOT_TESTED_BY_DESIGN = frozenset({"proxy-shape"})


def evidence_verdict(item: dict[str, Any]) -> bool | None:
    """Whether this attempt's layer is evidenced independently of the gateway
    label (None: not tested by design). Rewrites the evidence cell to say so."""
    case = str(item.get("case"))
    if case in NOT_TESTED_BY_DESIGN:
        item["evidence"] = f"not tested by design: {item['evidence']}"
        return None
    group, layer = item.get("group"), str(item.get("layer"))
    missing: list[str] = []
    if group == "tamper" and case.startswith("validator-"):
        basis = "validator exception, 0 SDK requests"
        if layer != "application_rule" or not item.get("local_ok"):
            missing.append("validator did not reject locally")
    elif group == "direct":
        basis = "tool-server reply code + attributed attempt row"
        if layer != "tool_server_rule":
            missing.append("no tool-server code in the reply")
        if case == "direct-replay" and not item.get("matches_original"):
            missing.append("original_entry_id does not match the first call")
        if not item.get("db_ok"):
            missing.append("no DB corroboration")
    elif group == "off-plan":
        basis = "0 proxy requests + pre-network IntentMismatchException + no DB rows"
        if not item.get("wire_ok"):
            missing.append("no wire/exception evidence")
        if not item.get("db_ok"):
            missing.append("no DB corroboration")
    else:
        basis = "proxy request on the wire + attributed ledger/attempt rows"
        if item.get("hash_match") is not None:
            basis += " + business-argument hash"
        if layer.startswith("unknown") or layer == "None":
            missing.append("no layer")
        if not item.get("wire_ok"):
            missing.append("no wire evidence")
        if not item.get("db_ok"):
            missing.append("no DB corroboration")
    if item.get("flags"):
        missing.append("flagged")
    ok = not missing
    item["evidenced"] = ok
    item["evidence"] = (
        f"evidenced: {basis}" if ok else f"NOT evidenced ({'; '.join(missing)}); {item['evidence']}"
    )
    return ok


def _cell(value: Any) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def render_table(rows: list[dict[str, Any]]) -> str:
    """One Markdown row per attempt; the case id leads the attempt cell."""
    header = "| # | attempt | expected | observed | enforcing layer | evidence reference |"
    lines = [
        "### E2 evidence rows (one per attempt)",
        "",
        header,
        "|---|---|---|---|---|---|",
    ]
    for number, item in enumerate(rows, start=1):
        attempt = _cell(item["attempt"])
        if item.get("case"):
            attempt = f"`{item['case']}` {attempt}"
        lines.append(
            f"| {number} | {attempt} | {_cell(item['expected'])} | {_cell(item['observed'])} | "
            f"{_cell(item['layer'])} | {_cell(item['evidence'])} |"
        )
    return "\n".join(lines)


def _wire_line(window: Window) -> str:
    if not window.hooked:
        return "wire: no hooks on an SDK client (no SDK traffic expected, or hooks unavailable)"
    invokes = [event for event in window.wire if event.is_invoke]
    if not invokes:
        return f"wire: {len(window.wire)} request(s) during the attempt (0 to the proxy /invoke)"
    keys = sorted({key for event in invokes for key in (event.params_keys or ())})
    line = (
        f"wire: {len(invokes)} request(s) to the proxy /invoke (HTTP {_statuses(invokes)}); "
        f"{len(window.wire)} request(s) during the attempt in total; params keys sent: {keys}"
    )
    sent: dict[str, bool] = {}
    for event in invokes:
        for key, value in event.injected_sent.items():
            sent[key] = sent.get(key, False) or value
    for key, value in sent.items():
        line += f"; `{key}` sent as injected: {'yes' if value else 'no (replaced before sending)'}"
    return line


def _sdk_line(call: SdkCall) -> str:
    if call.exception_class is None:
        return f"SDK: {call.method} returned (no exception)"
    form = ""
    if call.proxy_form is not None:
        form = (
            " (proxy-response form: action attribute set)"
            if call.proxy_form
            else " (pre-network form: no action attribute)"
        )
    status = f", status_code={call.status_code}" if call.status_code is not None else ""
    return f'SDK: {call.method} raised {call.exception_class}{form}{status}: "{call.message}"'


def render_details(rows: list[dict[str, Any]]) -> str:
    """Per-attempt evidence: UTC window, gateway verdict and masked reason,
    SDK exception, wire counts, signed jti(s), tool-server reply, flags."""
    lines = ["#### Attempt details (UTC; identifiers only, never tokens or values)"]
    for number, item in enumerate(rows, start=1):
        window: Window | None = item.get("window")
        span = f"[{window.started_at} → {window.ended_at}]" if window else "[not timed]"
        head = f"- #{number} `{item.get('case')}` {span}"
        if item.get("request_id"):
            head += f" request_id={item['request_id']}"
        lines.append(head)
        if item.get("gateway_label"):
            lines.append(
                f"  - gateway: {item.get('gateway_outcome')} ({item['gateway_label']}); "
                f"reason: {redact_text(item.get('reason'))}"
            )
        if window is not None:
            lines.extend(f"  - {_sdk_line(call)}" for call in window.calls)
            if item.get("gateway_label"):
                lines.append(f"  - {_wire_line(window)}")
            elif window.hooked:
                lines.append(
                    f"  - SDK client requests during the attempt: {len(window.wire)} "
                    "(ArmorIQ not involved)"
                )
            if window.signed:
                lines.append(
                    "  - signed `_ctx` jti(s): " + ", ".join(entry.jti for entry in window.signed)
                )
        if item.get("source"):
            lines.append(f"  - tool server reply: {item['observed']}")
        if item.get("db_lines"):
            lines.append("  - DB: " + "; ".join(item["db_lines"]))
        lines.extend(f"  - FLAG: {flag}" for flag in item.get("flags", []))
    return "\n".join(lines)


GATEWAY_ICR_NOTE = (
    "Gateway ICR (not changed by this PR; steward of "
    "services/authorization/armoriq_gateway.py): `invoke_step` and "
    "`attempt_off_plan` map every IntentMismatchException to "
    "`armoriq_sdk_plan_check`, but armoriq-sdk 0.6.10 raises it both on its "
    "pre-network plan check (client.py:814-819, no `action` attribute) and on a "
    "proxy HTTP 409 (client.py:976-981, `action` set). The gateway should tell "
    "them apart; until then this probe decides from wire evidence and the "
    "exception form, and flags any contradiction."
)


async def run_baseline(
    gateway: Any, settings: Any, capture: CtxCapture, observer: Observer | None = None
) -> tuple[dict[str, Any], ReplaySource | None]:
    """The planned call (ALLOW expected): its signed `_ctx` is the replay source."""
    ctx, plan = build_maya_request(settings)
    handle = await gateway.start_plan(ctx, plan)
    attempt = "planned knowledge_search_team for Maya (replay source)"
    expected = "ALLOW; EXECUTED with a ledger row (E1 path)"
    requester = {
        "requester_masked": ctx.requester.masked_email,
        "requester_member_id": ctx.requester.member_id,
    }
    if handle.decision is not None:
        failed = plan_failure_row(
            handle,
            ctx,
            group="baseline",
            case="planned-baseline",
            attempt=attempt,
            expected=expected,
        )
        return {**failed, **requester}, None
    with (observer or Observer(capture=capture)).window() as window:
        outcome = await gateway.invoke_step(handle, 0, ctx, plan)
    described = e1.describe_outcome(outcome)
    signed = [item for item in window.signed if item.jti == described["jti"]]
    source = None
    if described["invocation_status"] == "executed" and described["ledger_entry_id"] and signed:
        source = ReplaySource(
            token=signed[-1].token,
            jti=signed[-1].jti,
            ledger_entry_id=str(described["ledger_entry_id"]),
            issued_at=signed[-1].issued_at,
            expires_at=signed[-1].expires_at,
            arguments=dict(plan.steps[0].arguments),
        )
    item = row(
        group="baseline",
        attempt=attempt,
        expected=expected,
        observed=_invoke_observed(described),
        layer=invoke_layer(described),
        evidence="gateway result + `_meta.ledger_entry_id`",
        case="planned-baseline",
        jti=described["jti"],
        request_id=ctx.request_id,
        status=described["invocation_status"],
        error_code=described["error_code"],
        ledger_entry_id=described["ledger_entry_id"],
        business_arguments=dict(plan.steps[0].arguments),
        described=described,
        window=window,
        **gateway_facts(outcome.decision),
        **requester,
    )
    return item, source


def _replay_not_run(reason: str) -> dict[str, Any]:
    return row(
        group="direct",
        attempt="replayed `_ctx` (same jti, same arguments, within CTX_TTL_S)",
        expected="replay with original_entry_id = the first call's ledger entry",
        observed=f"not run: {reason}",
        layer="unknown",
        evidence="none",
        case="direct-replay",
        window=Window(started_at=utc_now(), ended_at=utc_now()),
    )


async def run_probe(
    settings: Any,
    *,
    direct: DirectConfig,
    ledger_dsn: str | None,
    sdk_client_factory: Callable[[str], Any] | None = None,
    direct_client: Any = None,
    ctx_signer_inner: Any = None,
    lookup_factory: Callable[[str], Any] | None = None,
    spacing_s: float = 1.0,
) -> tuple[int, str]:
    """Execute all attempt groups; return (exit_code, report_text).

    ``sdk_client_factory``, ``direct_client``, ``ctx_signer_inner`` and
    ``lookup_factory`` are seams for offline tests only; the operator path
    builds the gateway's own SDK client, a real HTTP client for the base
    URL, the real signer, and the Neon evidence lookup. Either way the SDK
    client is wrapped (`RecordingClient`) through the gateway's
    `client_factory` seam, so every label is checked on the wire.
    """
    from services.authorization import armoriq_gateway

    capture = CtxCapture(ctx_signer_inner)
    recorder = SdkRecorder()
    observer = Observer(recorder, capture)
    holder: dict[str, Any] = {}

    def recording_factory(member_id: str) -> Any:
        inner = sdk_client_factory or holder["gateway"]._build_client
        return RecordingClient(inner(member_id), recorder)

    gateway = armoriq_gateway.ArmorIQGateway(
        settings, client_factory=recording_factory, ctx_signer=capture
    )
    holder["gateway"] = gateway
    client = direct_client if direct_client is not None else build_direct_client(direct.base_url)
    try:
        await asyncio.to_thread(preflight, client, ledger_dsn, lookup_factory)
        run_started = utc_now()
        baseline, replay_source = await run_baseline(gateway, settings, capture, observer)
        rows: list[dict[str, Any]] = [baseline]
        if replay_source is not None:
            rows.append(direct_replay_row(client, direct.bearer or "", replay_source, observer))
        else:
            rows.append(_replay_not_run("the planned call did not execute with a ledger entry"))
        await asyncio.sleep(spacing_s)
        rows.extend(
            group_direct(
                client,
                direct.bearer or "",
                ctx_secret=e1.secret_text(settings.ctx_secret),
                request_id=str(baseline.get("request_id") or "e2-direct"),
                observer=observer,
                spacing_s=spacing_s,
            )
        )
    finally:
        if direct_client is None:
            client.close()
    rows.extend(await group_off_plan(gateway, settings, observer))
    rows.extend(group_proxy_shape())
    rows.extend(await group_tamper(gateway, settings, observer))
    run_ended = utc_now()
    for item in rows:
        check_wire(item)
    request_ids = sorted({str(item["request_id"]) for item in rows if item.get("request_id")})
    jtis = sorted({jti for item in rows for jti in _row_jtis(item)})
    db = await asyncio.to_thread(
        collect_db_evidence,
        ledger_dsn,
        started_at=run_started,
        ended_at=run_ended,
        request_ids=request_ids,
        jtis=jtis,
        lookup_factory=lookup_factory,
    )
    summary = apply_db_evidence(rows, db)
    flagged = [item for item in rows if item.get("flags")]
    unintended = summary["unintended"]
    unevidenced = [
        f"#{number} {item['case']}"
        for number, item in enumerate(rows, start=1)
        if evidence_verdict(item) is False
    ]
    # E1 rule: 0 only when every attempt has an evidenced layer and zero
    # unintended executions are confirmed; everything else that completed is 3.
    code = EXIT_COMPLETE if not (flagged or unintended or unevidenced) else EXIT_NOT_COMPLETE
    baseline_described = baseline.get("described")
    d05_answer, d05_reason = (
        e1.d05_finding(baseline_described)
        if baseline_described
        else ("n.a.", "the planned call did not run")
    )
    report = [
        "### E2 — off-plan and tampered-call enforcement (probe record)",
        f"- Run at (UTC): {run_started} → {run_ended}",
        "- Operator: (fill in)",
        "- Deployed tool-server SHA (Render → Events): (fill in; must be 82363f8 or later)",
        f"- SDK version: armoriq-sdk {armoriq_gateway.SDK_VERSION}",
        f"- Agent / MCP names used by the probe: {settings.armoriq_agent_id} / memory-mcp "
        "(tool knowledge_search_team); workspace-mcp is not needed: the honeypot attempt is "
        "stopped by the SDK before any network call (confirm registrations in the console)",
        f"- Tool-server base URL (no secrets): {direct.base_url} ({direct.base_url_source})",
        f"- Requester: Maya ({baseline.get('requester_masked')}), member "
        f"{baseline.get('requester_member_id')}; request_id prefix {REQUEST_ID_PREFIX}- "
        "(one-step team plans; a fresh plan per invoked case)",
        "- D-05 (`_ctx` forwarded by the proxy): yes per the E1 live run (2026-10-17); "
        f"this run: {d05_answer} ({d05_reason})",
        "- Server-side plan binding: tool names: not tested (no verified direct-proxy "
        "shape; the SDK stops unplanned names client-side); " + argument_binding_verdict(rows),
        f"- DB evidence (read-only, toolserver role): {db['status']}; window "
        f"{db['window'][0]} → {db['window'][1]} (run window +- {DB_SKEW_S:.0f} s); "
        f"tools {list(E2_TOOLS)}"
        + (f"; {summary['counts']}" if summary.get("counts") else "")
        + (f"; notes: {db['notes']}" if db["notes"] else ""),
        "- DB unattributed rows: " + ("; ".join(summary["unattributed"]) or "none"),
        "- Zero unintended executions: "
        + ("CONFIRMED" if not unintended else "NOT CONFIRMED (" + "; ".join(unintended) + ")"),
        f"- {GATEWAY_ICR_NOTE}",
        "- Flags: "
        + (", ".join(f"#{rows.index(item) + 1} {item['case']}" for item in flagged) or "none"),
        "- Attempts without an evidenced layer: " + (", ".join(unevidenced) or "none"),
        f"- Probe exit code: {code} (0 = every attempt has an evidenced layer and zero "
        "unintended executions are confirmed; 3 = completed otherwise)",
        "- Outcome: operator-assigned in docs/EXPERIMENTS.md (PASS needs exit 0 and the "
        "matching Render log lines; #8 stays open until a live record exists)",
        "",
        render_table(rows),
        "",
        render_details(rows),
    ]
    return code, "\n".join(report)


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
        # Only the gateway's own INFO/WARNING lines (request_id, jti, ledger
        # reference, exception class) go to stderr, as in E1.
        with e1.gateway_logs_to_stderr():
            code, report = asyncio.run(run_probe(settings, direct=direct, ledger_dsn=ledger_dsn))
    except PrerequisiteError as exc:
        return _fail(str(exc))
    except Exception as exc:
        print(f"probe_v2_e2_offplan: probe error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERROR
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
