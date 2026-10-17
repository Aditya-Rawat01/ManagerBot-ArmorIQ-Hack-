"""Request orchestrator: one ArmorIQ plan per request, end to end (P3-04).

PRD v2 §7 / Arch §8 (the 14 lifecycle steps) behind one async entry point
shared by Slack (P3-05) and web (P4-01). Outcomes are ALLOW, BLOCK, HOLD,
ABSTAIN and ERROR/UNVERIFIED (Arch §1); nothing unverifiable ever becomes an
ALLOW.

Entry points (SHARED_INTERFACES §12)
-------------------------------------

- ``await orchestrator.handle(ctx, message, options=None) -> request_id``:
  validates the trusted :class:`RequestContext` (re-validated, requester
  checked against the configured member table), mints a fresh ``request_id``
  / ``trace_id``, writes ``create_request`` and returns at once. Everything
  else runs in a background task (the Slack ack is never blocked, R-43).
  ``handle(ctx, message)`` is the Slack ``OrchestratorHandle``. Slack never
  sets :class:`RequestOptions` (a Slack context carrying options is refused).
- ``await orchestrator.confirm(ctx, token, accept) -> request_id``: the
  trusted D-14 confirmation entry point (a new request). Entry points pass a
  token they received from a :data:`ConfirmationPresenter`; a token is never
  read from message text. ``handle(ctx, message,
  RequestOptions(confirmation_token=...))`` is the same as ``accept=True``.

Lifecycle (task §9; every transition is audited with ``request_id``,
``trace_id`` and, once known, ``plan_id`` / ``step_index`` / ``jti`` /
decision source)
-------------------------------------------------------------------------

1. ``create_request`` (``request_received``), then a background task, bounded
   by a semaphore (``max_concurrency``) and a global timeout.
2. Plan (P3-02): ``Rejected`` -> ``plan_rejected`` (``application_rule``) +
   the planner's safe message. ``Clarification`` -> ``answer_generated``
   (reason ``clarification``) + the prompt. ``Escalation`` -> a D-14
   confirmation is minted (``escalation_created``) and the prompt delivered.
   ``Failed`` -> ERROR + the planner's retry message.
3. ``gateway.start_plan`` exactly once, then ``record_plan`` with the
   gateway's ``plan_id`` (``plan_proposed``). A failed mint is an
   ``authorization_decided`` decision event with the gateway's label and the
   "authorization could not be verified" (or policy-block) message;
   otherwise ``plan_minted``.
4. Steps in order under that one handle. Each ``invoke_step`` gets the
   exact validated plan object (see "E2" below). Every decision is
   ``record_step_decision``; every invocation is ``record_execution_ref``
   (``jti``, ``ledger_entry_id``). BLOCK / UNVERIFIED stop the remaining
   steps and nothing is answered (no protected data, PRD §7.6). HOLD: the
   gateway waits (SDK delegation flow) and reports the pending hold through
   ``on_hold``; the orchestrator records it (``record_approval`` ->
   ``hold_created``) and DMs Maya (``notify_hold``) while the wait goes on,
   then records the resolution. Not approved (rejected / expired / timeout /
   unverifiable) -> no data, the requester is told to send a new request;
   there is never a new plan for the same request. ALLOW needs a
   ledger-backed ``executed`` invocation; its result is parsed with the P2-02
   result model and becomes evidence. ``highest_tier_returned`` is the max
   tier over EVERY parsed result, cited or not (PRD §7.12).
5. Responder (P3-03) over the evidence. A generator error is ERROR, never an
   abstention. Each ``requested_tools`` entry goes to
   ``gateway.attempt_off_plan`` only and is recorded with the label it
   returns; it is never executed. A tool that IS in the current plan is
   refused by trusted code without any SDK call: the SDK plan check binds
   names only, so a planned name with model-chosen arguments would execute
   (E2). ``rejected_tool_requests`` are recorded as ``application_rule``.
   The answer then tells the requester to send a new request if they want
   that action (a new, user-initiated plan; PRD §7.11).
6. Abstentions ``no_evidence`` / ``conflict`` go to ``add_unanswered`` with
   team-tier source IDs only (never titles or snippets).
   ``manager_decision_required`` / ``source_unavailable`` have no producer
   yet (P3-03 emits neither).
7. ``deliver(ctx, output, highest_tier_returned)`` (P3-06): the destination
   comes from ``ctx`` only; web answers are recorded ``pending`` (P3-06 gap).
8. An audit write failure on this path stops the request (no further
   protected step) and fails closed.
9. Global timeout and exception guard: ERROR outcome, recorded as an
   ``authorization_decided`` decision event with source ``unknown``,
   outcome ``unverified`` and ``request_outcome: "error"`` (Arch §1 groups
   ERROR with UNVERIFIED; no dedicated event type exists), then one generic
   message (never a second one once delivery started).
10. No database read of protected content: tool data arrives only through
    the gateway; the database is used only through ``services.audit`` and
    the planner's topic map.

E2 invariant (security-critical)
--------------------------------

ArmorIQ binds planned tool names, not argument values (E2: a changed query
under a valid plan executed). Arguments therefore come ONLY from the
validated, stored plan: the exact ``Planned.proposal`` object is handed to
``start_plan`` and to every ``invoke_step``; nothing re-reads, merges or
patches arguments afterwards, and responder output never reaches tool
arguments (its tool requests go to ``attempt_off_plan``, never
``invoke_step``). A fingerprint of the validated arguments is re-checked
before every step; any drift stops the request (ERROR) before the call.

D-14 confirmations (owned here)
-------------------------------

:class:`ConfirmationStore` is in-process (single uvicorn worker, D-27; a
restart forgets pending confirmations, which fails closed). A token is
``secrets.token_urlsafe(32)`` minted by the server only; only its sha256 is
stored, keyed with: workspace, member, original request ID, requested tier,
``plan_digest``, channel (entry point + Slack channel), expiry (5 minutes by
default, at most 10), a consumed flag, the original question and the stored
:class:`Escalation`. One pending confirmation per original request (a newer
one supersedes it). :meth:`ConfirmationStore.verify` is the planner's
``confirmation_verifier``: ``True`` only when the hash is known, not
expired, not consumed, not superseded, and the confirming context's member,
workspace and channel, the tier and the digest all match; the record is
marked consumed under an ``asyncio.Lock`` BEFORE ``True`` is returned; any
exception returns ``False``. The token is never logged, audited or
delivered: it leaves the store only inside a :class:`ConfirmationOffer`
handed to the entry point's registered presenter (P3-05 Slack button, P4-01
web), whose ``repr`` hides it. Audit: ``escalation_created`` and
``confirmation_resolved`` (PR #80 shapes). A confirmed run re-plans the
STORED proposal (no model call, P3-02) under a new request with a new
ArmorIQ plan, recorded with ``escalation_confirmed=True``.

``RequestOptions.simulate_hijacked_model`` (P3-07) is accepted for web
requests only and is inert here (logged); P3-07 adds the behaviour.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from apps.api.core.config import Settings
from apps.api.core.db import AuditDatabase
from apps.api.delivery import (
    DeliveryAuditAfterSendError,
    DeliveryError,
    DeliveryRecord,
    SlackSender,
)
from apps.api.delivery import deliver as default_deliver
from apps.api.delivery import notify_hold as default_notify_hold
from apps.api.identity import MemberRecord
from apps.api.planner import (
    Clarification,
    Escalation,
    EscalationConfirmation,
    Failed,
    ModelClient,
    Planned,
    PlanResult,
    Rejected,
)
from apps.api.planner import plan as default_plan
from apps.api.responder import (
    GenerateFn,
    ModelDraft,
    ResponderOutput,
    evidence_from_results,
)
from apps.api.responder import respond as default_respond
from services.audit import store as default_audit
from services.audit.errors import AuditError
from services.contracts import (
    ARMORIQ_POLICY_SOURCES,
    TOOL_CATALOG,
    TOOL_RESULT_MODELS,
    ApprovalState,
    ApprovalStatus,
    ArmorIQDecision,
    AuditEvent,
    AuditEventType,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    EntryPoint,
    ExternalMessageResult,
    FollowupTaskResult,
    PersonaKey,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    SaveTeamNoteResult,
    SourceCitation,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolKind,
    ToolName,
)

if TYPE_CHECKING:
    from services.authorization.armoriq_gateway import PlanHandle, StepOutcome

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 8
# Beyond the HOLD wait (APPROVAL_TIMEOUT_MS), the planner and responder calls
# (60 s each, one retry) and delivery need headroom.
_TIMEOUT_HEADROOM_S = 300.0
_SHUTDOWN_GRACE_S = 5.0
_FAIL_CLOSED_BUDGET_S = 30.0

DEFAULT_CONFIRMATION_TTL_S = 300
MAX_CONFIRMATION_TTL_S = 600
# Resolved/expired records are kept a little longer so a late presentation is
# reported truthfully (`expired` / `replayed`) instead of `invalid`.
_CONFIRMATION_RETENTION = timedelta(minutes=10)
_MAX_CONFIRMATIONS = 1000
# `secrets.token_urlsafe(32)`: 43 URL-safe base64 characters.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")

_TIER_RANK = {DataTier.TEAM: 0, DataTier.CONFIDENTIAL: 1, DataTier.RESTRICTED: 2}
# Abstentions that enter the unanswered queue (Arch §10). P3-03 emits only
# these two of the four queue reasons.
_QUEUED_ABSTENTIONS = {"no_evidence": "no_evidence", "conflict": "conflict"}
_MAX_SAFE_REFS = 20
_TOOL_SERVER_VALIDATION_CODES = frozenset(
    {"invalid_arguments", "out_of_scope", "precondition_failed"}
)

# --- fixed user-facing messages (never model text, never protected data) -----

ERROR_MESSAGE = (
    "Something went wrong and I couldn't complete this request. Nothing was shared. "
    "Please try again."
)
UNVERIFIED_MESSAGE = (
    "Authorization could not be verified, so nothing was run or returned. Please try again."
)
CONFIRMATION_INVALID_MESSAGE = (
    "That confirmation is no longer valid, so I didn't run anything. "
    "Please ask your question again."
)
CONFIRMATION_MISMATCH_MESSAGE = (
    "That confirmation can't be used here, so I didn't run anything. "
    "Please ask your question again."
)
CONFIRMATION_DECLINED_MESSAGE = "OK, I won't run that lookup."
OFF_PLAN_NOTE = (
    "Note: an additional action was suggested ({tools}), but it was not part of this "
    "request's plan, so it was not run. Send a new request if you need it."
)

_HOLD_MESSAGES = {
    ApprovalState.REJECTED: (
        "Maya did not approve this request in ArmorIQ, so no data was shared. "
        "You can send a new request."
    ),
    ApprovalState.EXPIRED: (
        "The approval request expired before a decision, so no data was shared. "
        "Please send a new request if you still need it."
    ),
    ApprovalState.TIMEOUT: (
        "Approval was not received in time, so no data was shared. "
        "Please send a new request if you still need it."
    ),
}
_HOLD_UNVERIFIED_MESSAGE = (
    "The approval could not be verified, so no data was shared. "
    "Please send a new request if you still need it."
)


# --- public types --------------------------------------------------------------


@dataclass(frozen=True)
class RequestOptions:
    """Per-request options set by trusted entry-point code (SHARED_INTERFACES §12).

    Slack never sets them (it calls ``handle(ctx, message)``). The web route
    (P4-01) may set ``confirmation_token`` (from the session's stored offer,
    never from message text) and ``simulate_hijacked_model`` (P3-07 hook,
    inert here). ``confirmation_token`` is hidden from ``repr``.
    """

    confirmation_token: str | None = field(default=None, repr=False)
    simulate_hijacked_model: bool = False


@dataclass(frozen=True)
class ConfirmationOffer:
    """A minted D-14 confirmation for the entry point that renders it.

    ``token`` is the only copy of the raw token: hand it to the user's
    confirm control (Slack button value, web session state) and to
    :meth:`Orchestrator.confirm`; never log, audit or post it as text.
    """

    request_id: str
    escalation_id: str
    requested_tier: DataTier
    expires_at: datetime
    token: str = field(repr=False)


ConfirmationPresenter = Callable[[RequestContext, ConfirmationOffer], Awaitable[None] | None]
"""Entry-point hook (P3-05 Slack, P4-01 web): show the confirm/decline control.

Called after the escalation prompt was delivered, with the original request's
context. It must not echo the token in text. Exceptions revoke the offer."""

SenderProvider = Callable[[], SlackSender | None]

ConfirmationState = Literal[
    "accepted", "rejected", "expired", "invalid", "replayed", "mismatch", "unbound"
]


class RequestRejectedError(ValueError):
    """``handle`` / ``confirm`` refused the call (invalid context or options).

    Raised synchronously; nothing was recorded. The message names the rule,
    never request content.
    """


class RequestNotRecordedError(RuntimeError):
    """``create_request`` failed: the request was not accepted (fail closed)."""


class PlanIntegrityError(RuntimeError):
    """The validated plan changed after minting (E2 invariant); nothing ran."""


class ResultShapeError(ValueError):
    """A tool result did not match the P2-02 result model of its step."""


class GatewayLike(Protocol):
    """The :class:`ArmorIQGateway` surface the orchestrator uses (P1-02)."""

    async def start_plan(self, ctx: RequestContext, plan: PlanProposal) -> PlanHandle: ...

    async def invoke_step(
        self,
        handle: PlanHandle,
        step_index: int,
        ctx: RequestContext,
        plan: PlanProposal,
        *,
        on_hold: Callable[[ApprovalStatus], None] | None = None,
    ) -> StepOutcome: ...

    async def attempt_off_plan(
        self,
        handle: PlanHandle,
        tool: ToolName,
        arguments: Mapping[str, Any],
        ctx: RequestContext,
    ) -> ArmorIQDecision: ...


class SlackSenderSlot:
    """Late-bound Slack sender for delivery (main.py fills it when Slack starts).

    The orchestrator exists before the Socket Mode client (Slack only starts
    once a handle exists), so delivery asks this slot for the running app's
    sender at send time. Empty slot: Slack deliveries are recorded as failed
    (``no_sender``); web delivery needs no sender.
    """

    def __init__(self) -> None:
        self._sender: SlackSender | None = None

    def set(self, sender: SlackSender | None) -> None:
        self._sender = sender

    def clear(self) -> None:
        self._sender = None

    def __call__(self) -> SlackSender | None:
        return self._sender


# --- helpers -------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _well_formed_token(token: object) -> bool:
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def confirmation_channel(ctx: RequestContext) -> tuple[str, str | None]:
    """The channel a confirmation is bound to: entry point + Slack channel ID."""
    channel = None if ctx.entry_point is EntryPoint.WEB else ctx.slack_channel_id
    return (ctx.entry_point.value, channel)


def plan_fingerprint(plan: PlanProposal) -> str:
    """sha256 over each step's tool and canonical arguments (E2 drift check)."""
    material = [
        {"index": step.step_index, "tool": step.tool.value, "arguments": step.arguments}
        for step in plan.steps
    ]
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _max_tier(current: DataTier, other: DataTier | None) -> DataTier:
    if other is None:
        return current
    return other if _TIER_RANK[other] > _TIER_RANK[current] else current


def parse_tool_result(step: PlannedToolCall, payload: object) -> BaseModel:
    """Parse an executed step's MCP ``CallToolResult`` with its P2-02 model.

    The text block of a success is the bare result model (SHARED_INTERFACES
    §4). The parsed ``tool`` must be the step's own tool (a result can never
    relabel itself into another tool or tier). Anything else raises
    :class:`ResultShapeError` (the request fails closed).
    """
    if not isinstance(payload, Mapping) or payload.get("isError") or payload.get("is_error"):
        raise ResultShapeError("tool result is not a successful CallToolResult")
    content = payload.get("content")
    first = content[0] if isinstance(content, list) and content else None
    text = first.get("text") if isinstance(first, Mapping) else None
    if not isinstance(text, str):
        raise ResultShapeError("tool result has no text block")
    model = TOOL_RESULT_MODELS[step.tool]
    try:
        parsed = model.model_validate_json(text)
    except ValidationError:
        raise ResultShapeError(f"tool result does not match {model.__name__}") from None
    if getattr(parsed, "tool", None) is not step.tool:
        raise ResultShapeError("tool result names another tool")
    return parsed


def _decision_message(decision: ArmorIQDecision) -> str:
    """Fixed text for a BLOCK / UNVERIFIED decision, labeled with its true layer."""
    if decision.outcome is not DecisionOutcome.BLOCK:
        return UNVERIFIED_MESSAGE
    source = decision.source
    if source in ARMORIQ_POLICY_SOURCES:
        layer = "ArmorIQ policy blocked"
    elif source is DecisionSource.ARMORIQ_SDK_PLAN_CHECK:
        layer = "The ArmorIQ plan check blocked"  # client-side only (ADR-026)
    elif source is DecisionSource.TOOL_SERVER_RULE:
        layer = "The tool server refused"
    elif source is DecisionSource.APPLICATION_RULE:
        layer = "ProxyLead refused"
    else:
        return UNVERIFIED_MESSAGE
    return f"{layer} this request ({source.value}). No data was returned."


def _hold_message(state: ApprovalState) -> str:
    return _HOLD_MESSAGES.get(state, _HOLD_UNVERIFIED_MESSAGE)


def _confirmation_message(state: ConfirmationState) -> str:
    if state == "rejected":
        return CONFIRMATION_DECLINED_MESSAGE
    if state == "mismatch":
        return CONFIRMATION_MISMATCH_MESSAGE
    return CONFIRMATION_INVALID_MESSAGE


def _action_summary(results: Sequence[_StepData]) -> str:
    """Trusted confirmation text for write/action steps (IDs and enums only)."""
    lines = []
    for item in results:
        parsed = item.parsed
        if isinstance(parsed, SaveTeamNoteResult):
            lines.append(
                f"Saved your note as a draft (record {parsed.record_id}, version "
                f"{parsed.version}). It becomes authoritative only after Maya confirms it."
            )
        elif isinstance(parsed, FollowupTaskResult):
            lines.append(
                f"Created follow-up task {parsed.task_id} for {parsed.owner.value} "
                f"({parsed.priority} priority)."
            )
        elif isinstance(parsed, ExternalMessageResult):
            lines.append("No external message was sent (synthetic sink only).")
        elif parsed is None:
            lines.append("The action returned no result.")
        else:
            lines.append("Done.")
    return "\n".join(lines)


async def _unavailable_generator(system: str, evidence: str, question: str) -> ModelDraft:
    """Stand-in when no answer model is configured: the responder reports
    ``error`` (-> ERROR outcome). Never falls back to process-wide settings."""
    raise RuntimeError("no answer model configured")


# --- D-14 confirmation store ---------------------------------------------------


@dataclass
class _PendingConfirmation:
    escalation_id: str
    token_sha256: str
    workspace_id: str
    member_id: str
    request_id: str
    channel: tuple[str, str | None]
    requested_tier: DataTier
    plan_digest: str
    escalation: Escalation = field(repr=False)
    question: str = field(repr=False)
    created_at: datetime
    expires_at: datetime
    consumed: bool = False
    superseded: bool = False


@dataclass(frozen=True)
class ConfirmationLookup:
    """Read-only classification of a presented token (nothing is consumed)."""

    state: Literal["pending", "expired", "invalid", "replayed", "mismatch", "unbound"]
    escalation_id: str | None = None
    _record: _PendingConfirmation | None = field(default=None, repr=False)


class ConfirmationStore:
    """In-process D-14 confirmation store (single worker, D-27). See module doc."""

    def __init__(
        self,
        *,
        ttl_s: int = DEFAULT_CONFIRMATION_TTL_S,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, int):
            raise ValueError("ttl_s must be an integer number of seconds")
        if not 0 < ttl_s <= MAX_CONFIRMATION_TTL_S:
            raise ValueError(f"ttl_s must be in 1..{MAX_CONFIRMATION_TTL_S} seconds")
        self._ttl = timedelta(seconds=ttl_s)
        self._clock = clock
        self._by_hash: dict[str, _PendingConfirmation] = {}
        self._by_request: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._by_hash)

    async def mint(
        self, ctx: RequestContext, escalation: Escalation, question: str
    ) -> ConfirmationOffer:
        """Mint a single-use confirmation for ``escalation`` (supersedes older ones)."""
        if escalation.requested_tier is DataTier.TEAM:
            raise ValueError("only an elevated tier needs a confirmation")
        token = secrets.token_urlsafe(32)
        now = self._clock()
        record = _PendingConfirmation(
            escalation_id=_new_id("esc"),
            token_sha256=_token_sha256(token),
            workspace_id=ctx.workspace_id,
            member_id=ctx.requester.member_id,
            request_id=ctx.request_id,
            channel=confirmation_channel(ctx),
            requested_tier=escalation.requested_tier,
            plan_digest=escalation.plan_digest,
            escalation=escalation,
            question=question,
            created_at=now,
            expires_at=now + self._ttl,
        )
        async with self._lock:
            self._prune(now)
            previous = self._by_request.get(ctx.request_id)
            if previous is not None and previous in self._by_hash:
                self._by_hash[previous].superseded = True
            self._by_hash[record.token_sha256] = record
            self._by_request[ctx.request_id] = record.token_sha256
        return ConfirmationOffer(
            request_id=ctx.request_id,
            escalation_id=record.escalation_id,
            requested_tier=record.requested_tier,
            expires_at=record.expires_at,
            token=token,
        )

    async def lookup(self, token: object, ctx: RequestContext) -> ConfirmationLookup:
        """Classify a presented token for ``ctx`` without consuming it."""
        if not isinstance(token, str) or not _well_formed_token(token):
            return ConfirmationLookup(state="invalid")
        async with self._lock:
            now = self._clock()
            self._prune(now)
            record = self._by_hash.get(_token_sha256(token))
            state = self._classify(record, ctx, now)
        if record is None:
            return ConfirmationLookup(state="invalid")
        return ConfirmationLookup(state=state, escalation_id=record.escalation_id, _record=record)

    async def verify(
        self, confirmation: EscalationConfirmation, ctx: RequestContext, plan_digest: str
    ) -> bool:
        """The planner's ``confirmation_verifier`` (D-14). Single use; fail closed."""
        try:
            token = confirmation.token
            if not _well_formed_token(token):
                return False
            async with self._lock:
                record = self._by_hash.get(_token_sha256(token))
                state = self._classify(
                    record, ctx, self._clock(), tier=confirmation.tier, digest=plan_digest
                )
                if state != "pending" or record is None:
                    return False
                record.consumed = True  # consumed BEFORE True is returned
            return True
        except Exception as exc:
            logger.warning("confirmation verifier failed closed error=%s", type(exc).__name__)
            return False

    async def consume(self, lookup: ConfirmationLookup) -> None:
        """Spend a confirmation without running it (declined or refused)."""
        record = lookup._record
        if record is None:
            return
        async with self._lock:
            record.consumed = True

    async def revoke(self, escalation_id: str) -> None:
        """Forget an offer that could not be presented."""
        async with self._lock:
            for digest, record in list(self._by_hash.items()):
                if record.escalation_id == escalation_id:
                    del self._by_hash[digest]
                    if self._by_request.get(record.request_id) == digest:
                        del self._by_request[record.request_id]

    async def refusal_state(
        self, lookup: ConfirmationLookup, result: PlanResult
    ) -> ConfirmationState:
        """Why the confirmed path did not produce a confirmed plan."""
        if isinstance(result, Rejected) and result.reason_code == "confirmation_unbound":
            return "unbound"
        record = lookup._record
        if record is None:
            return "invalid"
        async with self._lock:
            if record.consumed:
                return "replayed"
            if self._clock() >= record.expires_at:
                return "expired"
        return "mismatch"

    @staticmethod
    def _classify(
        record: _PendingConfirmation | None,
        ctx: RequestContext,
        now: datetime,
        *,
        tier: DataTier | None = None,
        digest: str | None = None,
    ) -> Literal["pending", "expired", "invalid", "replayed", "mismatch", "unbound"]:
        if record is None:
            return "invalid"
        if record.consumed:
            return "replayed"
        if record.superseded:
            return "unbound"
        if now >= record.expires_at:
            return "expired"
        if (
            record.workspace_id != ctx.workspace_id
            or record.member_id != ctx.requester.member_id
            or record.channel != confirmation_channel(ctx)
        ):
            return "mismatch"
        if tier is not None and tier is not record.requested_tier:
            return "mismatch"
        if digest is not None and not (
            isinstance(digest, str) and hmac.compare_digest(digest, record.plan_digest)
        ):
            return "mismatch"
        return "pending"

    def _prune(self, now: datetime) -> None:
        stale = [
            digest
            for digest, record in self._by_hash.items()
            if record.expires_at + _CONFIRMATION_RETENTION <= now
        ]
        overflow = len(self._by_hash) - len(stale) - _MAX_CONFIRMATIONS
        if overflow > 0:
            live = sorted(
                (item for item in self._by_hash.items() if item[0] not in stale),
                key=lambda item: item[1].created_at,
            )
            stale.extend(digest for digest, _ in live[:overflow])
        for digest in stale:
            record = self._by_hash.pop(digest)
            if self._by_request.get(record.request_id) == digest:
                del self._by_request[record.request_id]


# --- per-request state -----------------------------------------------------------


@dataclass
class _Run:
    ctx: RequestContext
    stage: str = "accepted"
    plan_id: str | None = None  # set only once record_plan succeeded (FK-safe)
    highest_tier: DataTier = DataTier.TEAM
    delivery_started: bool = False


@dataclass(frozen=True)
class _StepData:
    step: PlannedToolCall
    parsed: BaseModel | None


@dataclass(frozen=True)
class _StepOutcomeView:
    """``StepOutcome``-shaped value for ``record_step_decision`` (structural)."""

    decision: ArmorIQDecision
    approval: ApprovalStatus | None
    invocation: ToolInvocationResult | None


class _AuditWriteFailed(Exception):
    """An audit write failed on the protected path: stop and fail closed."""

    def __init__(self, operation: str, error_class: str) -> None:
        super().__init__(f"audit write failed: {operation} ({error_class})")
        self.operation = operation
        self.error_class = error_class


class _StepStop(Exception):
    """A step ended the request with a fixed user message (BLOCK, HOLD, ...)."""

    def __init__(self, message: str) -> None:
        super().__init__("request stopped")
        self.message = message


class _HoldWatch:
    """Records a HOLD reported by the gateway while its wait is still running.

    ``observe`` may be called from the SDK worker thread: it only schedules
    work on the API loop. The first valid pending report for this step
    starts one task (``record_approval`` + ``notify_hold``); :meth:`settle`
    awaits it and re-raises its failure, so an unrecorded HOLD fails closed.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        run: _Run,
        step: PlannedToolCall,
        plan_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._orchestrator = orchestrator
        self._run = run
        self._step = step
        self._plan_id = plan_id
        self._loop = loop
        self._task: asyncio.Task[None] | None = None
        self.recorded: ApprovalStatus | None = None

    def observe(self, approval: ApprovalStatus) -> None:
        try:
            self._loop.call_soon_threadsafe(self.start, approval)
        except RuntimeError:  # loop closed during shutdown
            logger.warning("hold not recorded: loop closed request_id=%s", self._run.ctx.request_id)

    def start(self, approval: ApprovalStatus) -> None:
        if self.recorded is not None:
            return
        if not (
            isinstance(approval, ApprovalStatus)
            and approval.state is ApprovalState.PENDING
            and approval.request_id == self._run.ctx.request_id
            and approval.plan_id == self._plan_id
            and approval.step_index == self._step.step_index
            and approval.tool is self._step.tool
        ):
            logger.warning(
                "hold report ignored (does not match the step) request_id=%s step=%d",
                self._run.ctx.request_id,
                self._step.step_index,
            )
            return
        self.recorded = approval
        self._task = self._loop.create_task(self._orchestrator._record_hold(self._run, approval))

    async def settle(self) -> None:
        await asyncio.sleep(0)  # let a just-scheduled `start` run first
        if self._task is not None:
            await self._task

    async def drain(self) -> None:
        """Wait for a started hold record without raising (the step already failed)."""
        await asyncio.sleep(0)
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:
                logger.warning(
                    "hold record failed request_id=%s error=%s",
                    self._run.ctx.request_id,
                    type(exc).__name__,
                )


# --- orchestrator ------------------------------------------------------------------


class Orchestrator:
    """Request lifecycle shared by Slack and web (P3-04). See the module docstring.

    Collaborators are injected (offline tests use fakes; ``main.py`` wires
    the real gateway, Anthropic clients, audit store and P3-06 delivery).
    ``audit`` is any object with the ``services.audit.store`` write
    functions; the blocking store and delivery calls run in worker threads.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        gateway: GatewayLike,
        db: AuditDatabase,
        plan_model: ModelClient,
        members: Mapping[PersonaKey, MemberRecord],
        generate: GenerateFn | None = None,
        sender_provider: SenderProvider | None = None,
        confirmations: ConfirmationStore | None = None,
        audit: Any = default_audit,
        plan_fn: Callable[..., Awaitable[PlanResult]] = default_plan,
        respond_fn: Callable[..., Awaitable[ResponderOutput]] = default_respond,
        deliver_fn: Callable[..., DeliveryRecord] = default_deliver,
        notify_hold_fn: Callable[..., DeliveryRecord] = default_notify_hold,
        request_timeout_s: float | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._settings = settings
        self._gateway = gateway
        self._db = db
        self._plan_model = plan_model
        self._members = dict(members)
        self._generate: GenerateFn = generate if generate is not None else _unavailable_generator
        self._sender_provider: SenderProvider = (
            sender_provider if sender_provider is not None else (lambda: None)
        )
        # Not `or`: an empty store has len() == 0 and is falsy.
        self._confirmations = confirmations if confirmations is not None else ConfirmationStore()
        self._audit = audit
        self._plan_fn = plan_fn
        self._respond_fn = respond_fn
        self._deliver_fn = deliver_fn
        self._notify_hold_fn = notify_hold_fn
        self._timeout_s = (
            float(request_timeout_s)
            if request_timeout_s is not None
            else settings.approval_timeout_ms / 1000 + _TIMEOUT_HEADROOM_S
        )
        self._slots = asyncio.Semaphore(max_concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._presenters: dict[EntryPoint, ConfirmationPresenter] = {}

    # -- public API ----------------------------------------------------------

    @property
    def confirmations(self) -> ConfirmationStore:
        return self._confirmations

    @property
    def confirmation_verifier(
        self,
    ) -> Callable[[EscalationConfirmation, RequestContext, str], Awaitable[bool]]:
        """The planner's D-14 ``confirmation_verifier`` (single use, fail closed)."""
        return self._confirmations.verify

    def set_confirmation_presenter(
        self, entry_point: EntryPoint, presenter: ConfirmationPresenter | None
    ) -> None:
        """Register how an entry point shows confirm/decline (P3-05, P4-01)."""
        if presenter is None:
            self._presenters.pop(entry_point, None)
        else:
            self._presenters[entry_point] = presenter

    async def handle(
        self, ctx: RequestContext, message: str, options: RequestOptions | None = None
    ) -> str:
        """Accept one request and return its new ``request_id`` immediately."""
        options = self._checked_options(options)
        ctx = self._accepted_context(ctx, options)
        if not isinstance(message, str):
            raise RequestRejectedError("message must be text")
        if options.confirmation_token is not None:
            return await self._start(ctx, self._confirmation_flow, options.confirmation_token, True)
        return await self._start(ctx, self._request_flow, message, options)

    async def confirm(self, ctx: RequestContext, token: str, accept: bool) -> str:
        """Present a D-14 confirmation (a new request); returns its ``request_id``."""
        if not isinstance(accept, bool):
            raise RequestRejectedError("accept must be a bool")
        ctx = self._accepted_context(ctx, RequestOptions())
        return await self._start(ctx, self._confirmation_flow, token, accept)

    async def wait_idle(self, timeout_s: float | None = None) -> None:
        """Wait for every background request to finish (tests, shutdown)."""
        while self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout_s)
            if timeout_s is not None:
                return

    async def aclose(self, grace_s: float = _SHUTDOWN_GRACE_S) -> None:
        """Shutdown: give in-flight requests ``grace_s``, then cancel them."""
        tasks = list(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=grace_s)
            logger.warning("orchestrator shutdown cancelled %d in-flight request(s)", len(pending))

    # -- acceptance ------------------------------------------------------------

    @staticmethod
    def _checked_options(options: RequestOptions | None) -> RequestOptions:
        if options is None:
            return RequestOptions()
        if not isinstance(options, RequestOptions):
            raise RequestRejectedError("options must be RequestOptions")
        if not isinstance(options.simulate_hijacked_model, bool):
            raise RequestRejectedError("simulate_hijacked_model must be a bool")
        if options.confirmation_token is not None and not isinstance(
            options.confirmation_token, str
        ):
            raise RequestRejectedError("confirmation_token must be text")
        return options

    def _accepted_context(self, ctx: RequestContext, options: RequestOptions) -> RequestContext:
        if not isinstance(ctx, RequestContext):
            raise RequestRejectedError("ctx must be a RequestContext")
        try:
            checked = RequestContext.model_validate(ctx.model_dump())
        except ValidationError:
            raise RequestRejectedError("invalid request context") from None
        requester = checked.requester
        record = self._members.get(requester.persona)
        if (
            record is None
            or record.member_id != requester.member_id
            or record.app_role is not requester.app_role
            or (
                requester.slack_user_id is not None
                and requester.slack_user_id != record.slack_user_id
            )
        ):
            raise RequestRejectedError("requester is not a configured member")
        if checked.entry_point is not EntryPoint.WEB and (
            options.simulate_hijacked_model or options.confirmation_token is not None
        ):
            raise RequestRejectedError("Slack requests never carry request options")
        return checked

    async def _start(
        self, ctx: RequestContext, flow: Callable[..., Awaitable[None]], *args: Any
    ) -> str:
        ctx = ctx.model_copy(update={"request_id": _new_id("req"), "trace_id": _new_id("trace")})
        try:
            await asyncio.to_thread(self._audit.create_request, self._db, ctx)
        except Exception as exc:
            logger.error(
                "request not recorded; refused request_id=%s error=%s",
                ctx.request_id,
                type(exc).__name__,
            )
            raise RequestNotRecordedError("the request could not be recorded") from None
        run = _Run(ctx=ctx)
        task = asyncio.create_task(
            self._guarded(run, flow, *args), name=f"proxylead-{ctx.request_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            "request accepted request_id=%s trace_id=%s entry=%s member=%s",
            ctx.request_id,
            ctx.trace_id,
            ctx.entry_point.value,
            ctx.requester.member_id,
        )
        return ctx.request_id

    async def _guarded(self, run: _Run, flow: Callable[..., Awaitable[None]], *args: Any) -> None:
        """Global timeout + exception guard: anything unexpected is ERROR."""
        try:
            async with asyncio.timeout(self._timeout_s), self._slots:
                await flow(run, *args)
        except asyncio.CancelledError:
            logger.warning(
                "request cancelled request_id=%s stage=%s", run.ctx.request_id, run.stage
            )
            raise
        except _AuditWriteFailed as exc:
            await self._fail_closed(run, exc.error_class, operation=exc.operation)
        except Exception as exc:
            await self._fail_closed(run, type(exc).__name__)

    # -- flows -----------------------------------------------------------------

    async def _request_flow(self, run: _Run, message: str, options: RequestOptions) -> None:
        if options.simulate_hijacked_model:
            logger.info(
                "simulate_hijacked_model requested; inert until P3-07 request_id=%s",
                run.ctx.request_id,
            )
        run.stage = "plan"
        result = await self._plan_fn(run.ctx, message, model=self._plan_model, db=self._db)
        if isinstance(result, Planned):
            await self._execute(run, result, message)
        elif isinstance(result, Rejected):
            await self._plan_rejected(run, result)
            await self._deliver_text(run, result.user_message)
        elif isinstance(result, Clarification):
            run.stage = "clarification"
            await self._write(
                "record_answer",
                request_id=run.ctx.request_id,
                trace_id=run.ctx.trace_id,
                citations=[],
                highest_tier=DataTier.TEAM,
                abstained=False,
                reason="clarification",
            )
            await self._deliver_text(run, result.prompt)
        elif isinstance(result, Escalation):
            await self._escalate(run, result, message)
        elif isinstance(result, Failed):
            await self._record_error(run, "planner_failed")
            await self._deliver_text(run, result.user_message)
        else:
            raise TypeError(f"unexpected plan result {type(result).__name__}")

    async def _escalate(self, run: _Run, escalation: Escalation, question: str) -> None:
        run.stage = "escalation"
        offer = await self._confirmations.mint(run.ctx, escalation, question)
        try:
            await self._write(
                "append_event",
                event=self._event(
                    run,
                    AuditEventType.ESCALATION_CREATED,
                    payload={
                        "escalation_id": offer.escalation_id,
                        "requested_tier": escalation.requested_tier.value,
                        "plan_digest": escalation.plan_digest,
                    },
                ),
            )
        except _AuditWriteFailed:
            await self._confirmations.revoke(offer.escalation_id)
            raise
        logger.info(
            "escalation offered request_id=%s escalation_id=%s tier=%s",
            run.ctx.request_id,
            offer.escalation_id,
            escalation.requested_tier.value,
        )
        record = await self._deliver_text(run, escalation.prompt)
        if record.status == "failed":
            await self._confirmations.revoke(offer.escalation_id)
            return
        presenter = self._presenters.get(run.ctx.entry_point)
        if presenter is None:
            logger.info(
                "no confirmation presenter for entry=%s; offer not shown escalation_id=%s",
                run.ctx.entry_point.value,
                offer.escalation_id,
            )
            return
        try:
            shown = presenter(run.ctx, offer)
            if inspect.isawaitable(shown):
                await shown
        except Exception as exc:
            logger.warning(
                "confirmation presenter failed; offer revoked escalation_id=%s error=%s",
                offer.escalation_id,
                type(exc).__name__,
            )
            await self._confirmations.revoke(offer.escalation_id)

    async def _confirmation_flow(self, run: _Run, token: object, accept: bool) -> None:
        run.stage = "confirmation"
        lookup = await self._confirmations.lookup(token, run.ctx)
        if lookup.state != "pending":
            await self._confirmation_resolved(run, lookup.state, lookup.escalation_id)
            await self._deliver_text(run, _confirmation_message(lookup.state))
            return
        record = lookup._record
        if record is None or not isinstance(token, str):  # a pending lookup has both
            raise RuntimeError("pending confirmation without its record")
        if not accept:
            await self._confirmations.consume(lookup)
            await self._confirmation_resolved(run, "rejected", lookup.escalation_id)
            await self._deliver_text(run, CONFIRMATION_DECLINED_MESSAGE)
            return
        run.stage = "plan"
        result = await self._plan_fn(
            run.ctx,
            record.question,
            EscalationConfirmation(tier=record.requested_tier, token=token),
            model=self._plan_model,
            db=self._db,
            confirmation_verifier=self._confirmations.verify,
            pending=record.escalation,
        )
        if isinstance(result, Planned) and result.escalation_confirmed:
            await self._confirmation_resolved(run, "accepted", lookup.escalation_id)
            await self._execute(run, result, record.question)
            return
        state = await self._confirmations.refusal_state(lookup, result)
        await self._confirmations.consume(lookup)
        await self._confirmation_resolved(run, state, lookup.escalation_id)
        if isinstance(result, Rejected):
            await self._plan_rejected(run, result)
        await self._deliver_text(run, _confirmation_message(state))

    # -- plan execution -----------------------------------------------------------

    async def _execute(self, run: _Run, planned: Planned, question: str) -> None:
        # E2: `plan` is the exact validated object; it is never rebuilt,
        # merged or patched, and every gateway call receives this object.
        plan = planned.proposal
        fingerprint = plan_fingerprint(plan)
        run.stage = "start_plan"
        handle = await self._gateway.start_plan(run.ctx, plan)
        run.stage = "record_plan"
        await self._write(
            "record_plan",
            request_id=run.ctx.request_id,
            trace_id=run.ctx.trace_id,
            plan=plan,
            armoriq_plan_id=handle.armoriq_plan_id,
            matched_topic_rule_id=planned.matched_topic_rule_id,
            chosen_tier=planned.chosen_tier,
            escalation_confirmed=planned.escalation_confirmed,
            plan_id=handle.plan_id,
            token_expires_at=handle.token_expires_at,
        )
        run.plan_id = handle.plan_id
        if handle.decision is not None:
            await self._write(
                "append_event",
                event=self._decision_event(run, handle.decision, stage="start_plan"),
            )
            await self._deliver_text(run, _decision_message(handle.decision))
            return
        await self._write(
            "append_event",
            event=self._event(
                run,
                AuditEventType.PLAN_MINTED,
                payload={
                    "armoriq_plan_id": handle.armoriq_plan_id,
                    "step_count": len(plan.steps),
                    "intent_expires_at": (
                        handle.token_expires_at.isoformat() if handle.token_expires_at else None
                    ),
                    "outcome": DecisionOutcome.ALLOW.value,
                    "decision_source": DecisionSource.ARMORIQ_ISSUANCE.value,
                },
            ),
        )
        returned: list[_StepData] = []
        try:
            for step in plan.steps:
                if plan_fingerprint(plan) != fingerprint:
                    raise PlanIntegrityError("validated plan arguments changed after minting")
                returned.append(await self._run_step(run, handle, plan, step))
        except _StepStop as stop:
            await self._deliver_text(run, stop.message)
            return
        await self._answer(run, handle, plan, returned, question)

    async def _run_step(
        self, run: _Run, handle: PlanHandle, plan: PlanProposal, step: PlannedToolCall
    ) -> _StepData:
        run.stage = f"step_{step.step_index}"
        watch = _HoldWatch(self, run, step, handle.plan_id, asyncio.get_running_loop())
        try:
            outcome = await self._gateway.invoke_step(
                handle, step.step_index, run.ctx, plan, on_hold=watch.observe
            )
        except BaseException:
            await watch.drain()
            raise
        await watch.settle()
        decision = outcome.decision
        approval = outcome.approval
        invocation = outcome.invocation

        if decision.outcome is DecisionOutcome.HOLD:
            # The gateway returned a HOLD without waiting for it: the hold is
            # recorded and Maya notified, but nothing can resume it here.
            pending = (
                approval
                if approval is not None and approval.state is ApprovalState.PENDING
                else None
            )
            if watch.recorded is None:
                watch.start(pending or self._pending_hold(run, handle, step, decision))
                await watch.settle()
            held = watch.recorded or pending
            stopped = ApprovalStatus(
                request_id=run.ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                state=ApprovalState.INTERRUPTED,
                delegation_id=(held.delegation_id if held else None) or decision.delegation_id,
                requested_at=held.requested_at if held else _utcnow(),
                resolved_at=_utcnow(),
            )
            await self._record_approval(run, stopped)
            await self._record_step(run, handle, step, decision, stopped, invocation)
            raise _StepStop(_HOLD_UNVERIFIED_MESSAGE)

        if approval is not None and approval.state is not ApprovalState.PENDING:
            await self._record_approval(run, approval)
        await self._record_step(run, handle, step, decision, approval, invocation)

        if decision.outcome is DecisionOutcome.ALLOW:
            if invocation is None or invocation.status is not ToolExecutionStatus.EXECUTED:
                # No ledger-backed execution: unverifiable, never used.
                raise _StepStop(UNVERIFIED_MESSAGE)
            if invocation.result is None:
                return _StepData(step=step, parsed=None)  # executed empty lookup
            parsed = parse_tool_result(step, invocation.result)
            run.highest_tier = _max_tier(run.highest_tier, TOOL_CATALOG[step.tool].tier)
            return _StepData(step=step, parsed=parsed)
        if approval is not None and approval.state is not ApprovalState.APPROVED:
            raise _StepStop(_hold_message(approval.state))
        raise _StepStop(_decision_message(decision))

    def _pending_hold(
        self, run: _Run, handle: PlanHandle, step: PlannedToolCall, decision: ArmorIQDecision
    ) -> ApprovalStatus:
        return ApprovalStatus(
            request_id=run.ctx.request_id,
            plan_id=handle.plan_id,
            step_index=step.step_index,
            tool=step.tool,
            state=ApprovalState.PENDING,
            delegation_id=decision.delegation_id,
            requested_at=_utcnow(),
        )

    async def _record_hold(self, run: _Run, approval: ApprovalStatus) -> None:
        """HOLD observed while the gateway waits: persist, then DM Maya."""
        await self._record_approval(run, approval)
        logger.info(
            "hold recorded request_id=%s plan_id=%s step=%d delegation_id=%s",
            run.ctx.request_id,
            approval.plan_id,
            approval.step_index,
            approval.delegation_id,
        )
        try:
            await asyncio.to_thread(
                self._notify_hold_fn,
                run.ctx,
                approval,
                sender=self._sender_provider(),
                db=self._db,
                members=self._members,
                settings=self._settings,
            )
        except DeliveryAuditAfterSendError:
            raise _AuditWriteFailed("notify_hold", "audit_failed") from None
        except DeliveryError as exc:
            logger.warning(
                "hold notification not sent request_id=%s error=%s",
                run.ctx.request_id,
                type(exc).__name__,
            )

    async def _record_approval(self, run: _Run, approval: ApprovalStatus) -> None:
        await self._write(
            "record_approval",
            request_id=run.ctx.request_id,
            trace_id=run.ctx.trace_id,
            approval=approval,
            requested_by_member_id=run.ctx.requester.member_id,
        )

    async def _record_step(
        self,
        run: _Run,
        handle: PlanHandle,
        step: PlannedToolCall,
        decision: ArmorIQDecision,
        approval: ApprovalStatus | None,
        invocation: ToolInvocationResult | None,
    ) -> None:
        await self._write(
            "record_step_decision",
            request_id=run.ctx.request_id,
            trace_id=run.ctx.trace_id,
            plan_id=handle.plan_id,
            step_index=step.step_index,
            outcome=_StepOutcomeView(decision=decision, approval=approval, invocation=invocation),
        )
        logger.info(
            "step decided request_id=%s plan_id=%s step=%d outcome=%s source=%s jti=%s",
            run.ctx.request_id,
            handle.plan_id,
            step.step_index,
            decision.outcome.value,
            decision.source.value,
            invocation.jti if invocation else None,
        )
        if invocation is None:
            return
        executed = invocation.status is ToolExecutionStatus.EXECUTED
        code = invocation.error_code
        await self._write(
            "record_execution_ref",
            request_id=run.ctx.request_id,
            trace_id=run.ctx.trace_id,
            jti=invocation.jti,
            ledger_entry_id=invocation.ledger_entry_id,
            status=invocation.status,
            plan_id=handle.plan_id,
            step_index=step.step_index,
            error_class=None if executed else code,
            tool_server_reached=invocation.tool_server_reached,
            validation_outcome=(
                "accepted"
                if executed
                else (code if code in _TOOL_SERVER_VALIDATION_CODES else None)
            ),
        )

    # -- answer, off-plan, delivery -------------------------------------------------

    async def _answer(
        self,
        run: _Run,
        handle: PlanHandle,
        plan: PlanProposal,
        returned: Sequence[_StepData],
        question: str,
    ) -> None:
        reads = [
            item
            for item in returned
            if TOOL_CATALOG[item.step.tool].kind is ToolKind.READ and item.parsed is not None
        ]
        actions = [
            item for item in returned if TOOL_CATALOG[item.step.tool].kind is not ToolKind.READ
        ]
        evidence = evidence_from_results([item.parsed for item in reads if item.parsed is not None])
        for item in evidence:
            run.highest_tier = _max_tier(run.highest_tier, item.tier)

        answers_question = any(TOOL_CATALOG[step.tool].kind is ToolKind.READ for step in plan.steps)
        if answers_question:
            run.stage = "respond"
            output = await self._respond_fn(
                run.ctx,
                question,
                evidence,
                self._settings.min_score,
                generate=self._generate,
                manager_name=self._manager_name(),
            )
            if not isinstance(output, ResponderOutput) or output.request_id != run.ctx.request_id:
                raise TypeError("responder output does not belong to this request")
            if output.abstain_reason == "error":
                await self._record_error(run, "responder_error")
                await self._deliver_text(run, ERROR_MESSAGE)
                return
        else:
            output = ResponderOutput(
                request_id=run.ctx.request_id, answer_text=_action_summary(actions)
            )

        run.stage = "off_plan"
        tried = await self._route_off_plan(run, handle, plan, output)

        run.stage = "record_answer"
        by_id = {item.source_id: item for item in evidence}
        citations = []
        for source_id in output.citations:
            item = by_id.get(source_id)
            if item is None:
                raise ResultShapeError("a citation is not in this request's evidence")
            # record_answer stores only source_id + tier per citation.
            citations.append(SourceCitation.model_construct(source_id=source_id, tier=item.tier))
        await self._write(
            "record_answer",
            request_id=run.ctx.request_id,
            trace_id=run.ctx.trace_id,
            citations=citations,
            highest_tier=run.highest_tier,
            abstained=output.abstained,
            reason=output.abstain_reason,
            plan_id=run.plan_id,
        )
        queued = _QUEUED_ABSTENTIONS.get(output.abstain_reason or "")
        if output.abstained and queued is not None:
            safe_refs = list(
                dict.fromkeys(item.source_id for item in evidence if item.tier is DataTier.TEAM)
            )[:_MAX_SAFE_REFS]
            await self._write(
                "add_unanswered",
                request_id=run.ctx.request_id,
                trace_id=run.ctx.trace_id,
                workspace_id=run.ctx.workspace_id,
                requester_member_id=run.ctx.requester.member_id,
                question=question,
                reason=queued,
                safe_refs=safe_refs,
            )

        parts = [output.answer_text]
        if answers_question and actions:
            parts.append(_action_summary(actions))
        if tried:
            parts.append(OFF_PLAN_NOTE.format(tools=", ".join(tried)))
        final = output.model_copy(update={"answer_text": "\n\n".join(parts)})
        run.stage = "deliver"
        await self._deliver(run, final)

    async def _route_off_plan(
        self, run: _Run, handle: PlanHandle, plan: PlanProposal, output: ResponderOutput
    ) -> list[str]:
        """Model tool requests: recorded, never executed (PRD §6.2, §7.11)."""
        planned = {step.tool for step in plan.steps}
        tried: list[str] = []
        for request in output.requested_tools:
            try:
                tool = ToolName(request.tool)
            except ValueError:
                decision = self._app_block("unknown tool requested by the model; never executed")
                await self._off_plan_event(run, decision, None, "unknown_tool")
                tried.append("an unknown tool")
                continue
            if tool in planned:
                # E2: the SDK plan check binds names only, so re-invoking a
                # planned name with model-chosen arguments would execute.
                decision = self._app_block(
                    "planned tool requested again after retrieval; never re-invoked"
                )
            else:
                try:
                    decision = await self._gateway.attempt_off_plan(
                        handle, tool, dict(request.arguments), run.ctx
                    )
                except Exception as exc:
                    decision = ArmorIQDecision(
                        outcome=DecisionOutcome.UNVERIFIED,
                        source=DecisionSource.UNKNOWN,
                        reason=f"off-plan check failed: {type(exc).__name__}; never executed",
                        decided_at=_utcnow(),
                    )
                if decision.outcome is DecisionOutcome.ALLOW:
                    decision = ArmorIQDecision(
                        outcome=DecisionOutcome.BLOCK,
                        source=DecisionSource.UNKNOWN,
                        reason="off-plan allow is never honored; never executed",
                        decided_at=_utcnow(),
                    )
            await self._off_plan_event(run, decision, tool, None)
            tried.append(tool.value)
        for rejected in output.rejected_tool_requests:
            try:
                known: ToolName | None = ToolName(rejected.tool)
            except ValueError:
                known = None  # model text is never recorded as a tool name
            decision = self._app_block("tool request failed validation; never executed")
            await self._off_plan_event(run, decision, known, rejected.reason)
            tried.append(known.value if known else "an unknown tool")
        return list(dict.fromkeys(tried))

    @staticmethod
    def _app_block(reason: str) -> ArmorIQDecision:
        return ArmorIQDecision(
            outcome=DecisionOutcome.BLOCK,
            source=DecisionSource.APPLICATION_RULE,
            reason=reason,
            decided_at=_utcnow(),
        )

    async def _off_plan_event(
        self,
        run: _Run,
        decision: ArmorIQDecision,
        tool: ToolName | None,
        rejection: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "outcome": decision.outcome.value,
            "reason": decision.reason,
            "off_plan": True,
            "executed": False,
        }
        if tool is not None:
            payload["tool_name"] = tool.value
        if rejection is not None:
            payload["rejection"] = rejection
        await self._write(
            "append_event",
            event=self._event(
                run,
                AuditEventType.AUTHORIZATION_DECIDED,
                decision_source=decision.source,
                payload=payload,
            ),
        )
        logger.info(
            "off-plan request recorded request_id=%s tool=%s outcome=%s source=%s",
            run.ctx.request_id,
            tool.value if tool else None,
            decision.outcome.value,
            decision.source.value,
        )

    async def _deliver_text(self, run: _Run, text: str) -> DeliveryRecord:
        return await self._deliver(
            run, ResponderOutput(request_id=run.ctx.request_id, answer_text=text)
        )

    async def _deliver(self, run: _Run, output: ResponderOutput) -> DeliveryRecord:
        """P3-06 delivery at ``highest_tier_returned``; the route comes from ``ctx``."""
        run.delivery_started = True
        try:
            record = await asyncio.to_thread(
                self._deliver_fn,
                run.ctx,
                output,
                run.highest_tier,
                sender=self._sender_provider(),
                db=self._db,
                plan_id=run.plan_id,
            )
        except DeliveryAuditAfterSendError:
            # Content is out but its audit row is missing: never re-send.
            raise _AuditWriteFailed("deliver", "audit_failed") from None
        logger.info(
            "delivery request_id=%s channel=%s tier=%s status=%s error=%s",
            run.ctx.request_id,
            record.channel.value,
            record.tier_delivered.value,
            record.status,
            record.error_class,
        )
        return record

    # -- audit helpers ----------------------------------------------------------------

    async def _write(self, operation: str, /, **kwargs: Any) -> Any:
        """One audit write in a worker thread; a failure stops the request."""
        function = getattr(self._audit, operation)
        try:
            return await asyncio.to_thread(function, self._db, **kwargs)
        except AuditError as exc:
            raise _AuditWriteFailed(operation, type(exc).__name__) from None

    def _event(
        self,
        run: _Run,
        event_type: AuditEventType,
        *,
        payload: dict[str, Any],
        decision_source: DecisionSource | None = None,
        step_index: int | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            event_id=_new_id("evt"),
            occurred_at=_utcnow(),
            event_type=event_type,
            request_id=run.ctx.request_id,
            trace_id=run.ctx.trace_id,
            plan_id=run.plan_id,
            step_index=step_index,
            decision_source=decision_source,
            payload=payload,
        )

    def _decision_event(self, run: _Run, decision: ArmorIQDecision, *, stage: str) -> AuditEvent:
        return self._event(
            run,
            AuditEventType.AUTHORIZATION_DECIDED,
            decision_source=decision.source,
            payload={
                "outcome": decision.outcome.value,
                "reason": decision.reason,
                "enforcement_action": decision.enforcement_action,
                "armoriq_plan_id": decision.armoriq_plan_id,
                "stage": stage,
            },
        )

    async def _plan_rejected(self, run: _Run, result: Rejected) -> None:
        await self._write(
            "append_event",
            event=self._event(
                run,
                AuditEventType.PLAN_REJECTED,
                decision_source=result.decision_source,
                payload={
                    "reason_code": result.reason_code,
                    "decision_source": result.decision_source.value,
                },
            ),
        )

    async def _confirmation_resolved(
        self, run: _Run, state: ConfirmationState, escalation_id: str | None
    ) -> None:
        payload: dict[str, Any] = {"confirmation_state": state}
        if escalation_id is not None:
            payload["escalation_id"] = escalation_id
        await self._write(
            "append_event",
            event=self._event(run, AuditEventType.CONFIRMATION_RESOLVED, payload=payload),
        )
        logger.info(
            "confirmation resolved request_id=%s state=%s escalation_id=%s",
            run.ctx.request_id,
            state,
            escalation_id,
        )

    async def _record_error(self, run: _Run, error_class: str) -> None:
        """The ERROR outcome as a decision event (source ``unknown``, never ALLOW)."""
        await self._write(
            "append_event",
            event=self._event(
                run,
                AuditEventType.AUTHORIZATION_DECIDED,
                decision_source=DecisionSource.UNKNOWN,
                payload={
                    "outcome": DecisionOutcome.UNVERIFIED.value,
                    "request_outcome": "error",
                    "stage": run.stage,
                    "error_class": error_class,
                    "reason": "request failed closed; nothing further was authorized",
                },
            ),
        )

    async def _fail_closed(self, run: _Run, error_class: str, operation: str | None = None) -> None:
        """ERROR outcome: best-effort audit, then one generic message."""
        logger.error(
            "request failed closed request_id=%s stage=%s error=%s audit_operation=%s",
            run.ctx.request_id,
            run.stage,
            error_class,
            operation,
        )
        try:
            async with asyncio.timeout(_FAIL_CLOSED_BUDGET_S):
                try:
                    await self._record_error(run, error_class)
                except Exception as exc:
                    logger.warning(
                        "error outcome not recorded request_id=%s error=%s",
                        run.ctx.request_id,
                        type(exc).__name__,
                    )
                if run.delivery_started:
                    return  # a message is (or may be) out already: never a second one
                await self._deliver_text(run, ERROR_MESSAGE)
        except Exception as exc:
            logger.warning(
                "error message not delivered request_id=%s error=%s",
                run.ctx.request_id,
                type(exc).__name__,
            )

    def _manager_name(self) -> str:
        maya = self._members.get(PersonaKey.MAYA)
        return maya.display_name if maya is not None else "your manager"


# --- construction from settings (main.py) -----------------------------------------

REQUIRED_SETTINGS: tuple[tuple[str, str], ...] = (
    ("armoriq_api_key", "ARMORIQ_API_KEY"),
    ("persona_maya_email", "PERSONA_MAYA_EMAIL"),
    ("persona_sam_email", "PERSONA_SAM_EMAIL"),
    ("persona_priya_email", "PERSONA_PRIYA_EMAIL"),
    ("ctx_secret", "CTX_SECRET"),
    ("database_url_backend", "DATABASE_URL_BACKEND"),
    ("anthropic_api_key", "ANTHROPIC_API_KEY"),
)


def missing_orchestrator_settings(settings: Settings) -> list[str]:
    """Names (never values) of the settings the orchestrator needs but lacks."""
    missing = []
    for attribute, name in REQUIRED_SETTINGS:
        value = getattr(settings, attribute, None)
        getter = getattr(value, "get_secret_value", None)
        raw = getter() if callable(getter) else value
        if not isinstance(raw, str) or not raw.strip():
            missing.append(name)
    return missing


def build_orchestrator(
    settings: Settings, *, sender_provider: SenderProvider | None = None
) -> tuple[Orchestrator, Any]:
    """Real singletons for the API process (no network call happens here).

    Returns ``(orchestrator, db)``; the caller owns ``db`` and closes it at
    shutdown. Raises ``ValueError`` naming missing settings (names only).
    """
    missing = missing_orchestrator_settings(settings)
    if missing:
        raise ValueError(f"orchestrator settings missing: {', '.join(missing)}")
    from apps.api.core.db import BackendDatabase
    from apps.api.identity import build_member_table
    from apps.api.planner import plan_client_from_settings
    from apps.api.responder import AnthropicGenerator
    from services.authorization.armoriq_gateway import ArmorIQGateway

    plan_model = plan_client_from_settings(settings)
    if plan_model is None:  # pragma: no cover - guarded by missing_orchestrator_settings
        raise ValueError("orchestrator settings missing: ANTHROPIC_API_KEY")
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    db = BackendDatabase.from_settings(settings)
    orchestrator = Orchestrator(
        settings=settings,
        gateway=ArmorIQGateway(settings),
        db=db,
        plan_model=plan_model,
        members=build_member_table(settings),
        generate=AnthropicGenerator(key, settings.llm_model),
        sender_provider=sender_provider,
    )
    return orchestrator, db


__all__ = [
    "DEFAULT_CONFIRMATION_TTL_S",
    "DEFAULT_MAX_CONCURRENCY",
    "ERROR_MESSAGE",
    "MAX_CONFIRMATION_TTL_S",
    "REQUIRED_SETTINGS",
    "UNVERIFIED_MESSAGE",
    "ConfirmationLookup",
    "ConfirmationOffer",
    "ConfirmationPresenter",
    "ConfirmationStore",
    "GatewayLike",
    "Orchestrator",
    "PlanIntegrityError",
    "RequestNotRecordedError",
    "RequestOptions",
    "RequestRejectedError",
    "ResultShapeError",
    "SlackSenderSlot",
    "build_orchestrator",
    "confirmation_channel",
    "missing_orchestrator_settings",
    "parse_tool_result",
    "plan_fingerprint",
]
