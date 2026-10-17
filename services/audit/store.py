"""Backend audit write API (P3-01, PRD v2 §7.14/§10.1, Arch §7.2/§10).

Every function takes an :class:`AuditDatabase` (pool-backed or fake) and uses
only the backend role's tables: ``plans``, ``plan_steps``,
``approval_requests``, ``unanswered_questions`` (read/write) and
``request_events``, ``decision_events``, ``delivery_events`` (append-only).
Protected knowledge tables and the execution ledger are never named here —
the backend stores execution *references* (IDs reported by the orchestrator),
never ledger content, and audit SQL is covered by a test asserting that.

Schema mapping notes (no column was added; migration 0009 only hardens FKs):

- There is no ``requests`` table: a request exists as its ``request_received``
  event, written only by :func:`create_request` from a trusted
  ``RequestContext``. Replays are serialized by a transaction advisory lock
  and must name the same requester/workspace/entry point.
- ``matched_topic_rule_id`` / ``chosen_tier`` / ``escalation_confirmed`` and
  the intent-token expiry have no plan columns: they travel in the
  ``plan_proposed`` event payload.
- ``unanswered_questions`` has no reason column: ``reason`` and ``safe_refs``
  travel in a companion ``answer_abstained`` event keyed by
  ``unanswered_question_id``; :mod:`queries` rejoins them in code (same
  allowed tables, no protected join).
- ``delivery_events`` has no tier/error columns: ``tier_delivered`` and
  ``error_class`` travel in the companion delivery event row.
- Slack events refused before they become requests (P3-05 ICR) are
  ``slack_event_rejected`` rows in ``request_events`` keyed
  ``rej-<slack_event_id>``, written only by
  :func:`record_slack_event_rejected` (``event_type`` has no CHECK, so no
  migration). They have no ``request_received`` origin, so read queries never
  list them as requests.
- Execution facts the ledger owns (tool server reached, validation outcome,
  result ID) are stored as *references* in the ``tool_executed`` /
  ``tool_rejected`` payload.
- ``record_step_decision`` accepts the gateway ``StepOutcome`` structurally
  (``decision`` / ``approval`` / ``invocation`` attributes) via
  :class:`StepOutcomeLike`, so this module never imports the gateway or the
  ArmorIQ SDK. Pass ``services.authorization.armoriq_gateway.StepOutcome``.

Fail-closed contract for P3-04: every function raises an ``AuditError``
subclass on any failure — invalid input, a missing row, a backwards
transition, or a driver/pool error (wrapped as :class:`AuditStoreError` by
:func:`fail_closed`). Failed writes never leave partial multi-row records
(one transaction per operation).

Correlation (SHARED_INTERFACES §13): pass the gateway's ``PlanHandle.plan_id``
to :func:`record_plan` (``plan_id=``) so ``ApprovalStatus`` /
``ToolInvocationResult`` rows from the gateway reference the same plan row.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, get_args

from services.audit.errors import (
    AuditNotFoundError,
    AuditStoreError,
    AuditTransitionError,
    fail_closed,
)
from services.audit.protocols import AuditCursor, AuditDatabase
from services.audit.redaction import redact_payload, redact_question
from services.contracts import (
    ApprovalStatus,
    ArmorIQDecision,
    AuditEvent,
    AuditEventType,
    DataTier,
    DeliveryChannel,
    PlanProposal,
    RequestContext,
    SourceCitation,
    ToolExecutionStatus,
    ToolInvocationResult,
)

DeliveryStatus = Literal["pending", "delivered", "failed"]
UnansweredReason = Literal[
    "no_evidence", "conflict", "manager_decision_required", "source_unavailable"
]
# Why the Slack adapter (P3-05) dropped an event before it became a request.
SlackRejectionReason = Literal[
    "invalid_envelope",
    "wrong_team",
    "channel_not_allowed",
    "unmapped_user",
    "db_unavailable",
    "duplicate",
]

_APPROVAL_TERMINAL = ("approved", "rejected", "expired", "timeout", "interrupted", "unverified")
_DELIVERY_STATUSES: tuple[str, ...] = get_args(DeliveryStatus)
_UNANSWERED_REASONS: tuple[str, ...] = get_args(UnansweredReason)
_SLACK_REJECTION_REASONS: tuple[str, ...] = get_args(SlackRejectionReason)
_MAX_SAFE_REFS = 20
_MAX_REF_CHARS = 200

# User-confirmation lifecycle events (P3-04 ICR, issue #25; SHARED_INTERFACES
# §10). Payloads are metadata only: an opaque escalation ID (never the token),
# plus the escalated tier, the plan digest, and the resolution state. The
# opaque ID and the token↔ID mapping live in P3-04's in-process store, so a
# raw confirmation token has no field to travel in. The key allowlists below
# are exhaustive, so a token cannot be smuggled under an unexpected key.
_ESCALATION_CREATED_KEYS = frozenset({"escalation_id", "requested_tier", "plan_digest"})
_CONFIRMATION_RESOLVED_KEYS = frozenset({"escalation_id", "confirmation_state"})
_CONFIRMATION_STATES = frozenset(
    {"accepted", "rejected", "expired", "invalid", "replayed", "mismatch", "unbound"}
)
_ESCALATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}")
# sha256 hex (P3-02 `plan_digest`); never secret, just a binding reference.
_PLAN_DIGEST_RE = re.compile(r"[A-Fa-f0-9]{32,128}")

# Slack event/team/channel IDs (``Ev…``, ``T…``, ``C…``/``D…``): short and
# printable, the same shape as plan IDs; never free text.
_SLACK_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}")

# Caller-supplied plan IDs (the gateway's ``p-<hex>``): short, printable,
# no whitespace — they are primary keys and correlation IDs, never content.
_PLAN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}")

# pg_advisory_xact_lock(int4, int4) namespaces (fixed, P3-01-owned): the
# second key is hashtext(<id>), so unrelated requests never contend.
_LOCK_NS_REQUEST = 3_010_001
_LOCK_NS_HOLD = 3_010_002

# Plan-step terminal label per reported execution status (plan_steps.status
# has no CHECK in 0004; every label is distinct and says what happened).
_STEP_LABELS: Mapping[ToolExecutionStatus, str] = {
    ToolExecutionStatus.EXECUTED: "executed",
    ToolExecutionStatus.REJECTED_BY_TOOL_SERVER: "rejected",
    ToolExecutionStatus.NOT_REACHED: "not_reached",
    ToolExecutionStatus.ERROR: "error",
    ToolExecutionStatus.UNKNOWN: "unknown",
}

_DECISION_EVENTS = frozenset(
    {
        AuditEventType.AUTHORIZATION_DECIDED,
        AuditEventType.TOOL_EXECUTED,
        AuditEventType.TOOL_REJECTED,
    }
)


class StepOutcomeLike(Protocol):
    """Structural shape of the gateway ``StepOutcome`` (no SDK import needed)."""

    @property
    def decision(self) -> ArmorIQDecision: ...
    @property
    def approval(self) -> ApprovalStatus | None: ...
    @property
    def invocation(self) -> ToolInvocationResult | None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _aware(moment: datetime | None, field: str) -> datetime:
    current = moment if moment is not None else _now()
    if current.tzinfo is None:
        raise AuditStoreError(f"{field} must be timezone-aware UTC, got naive datetime")
    return current


def _forward(current: str, new: str, order: Sequence[str], what: str) -> None:
    if new == current:
        return
    if order.index(new) <= order.index(current):
        raise AuditTransitionError(f"{what} cannot move {current!r} -> {new!r}")


def _xact_lock(cursor: AuditCursor, namespace: int, key: str) -> None:
    """Serialize writers on ``key`` until this transaction ends."""
    cursor.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", [namespace, key])


def _validated_plan_id(plan_id: str) -> str:
    if not isinstance(plan_id, str) or not _PLAN_ID_RE.fullmatch(plan_id):
        raise AuditStoreError(
            "plan_id must be 1-128 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return plan_id


def _payload(row: object) -> dict[str, object] | None:
    """First column of a fetched row as a JSON object, if it is one."""
    if not isinstance(row, Sequence) or isinstance(row, str) or not row:
        return None
    value = row[0]
    return value if isinstance(value, dict) else None


def _advance(
    cursor: AuditCursor,
    table: str,
    new_status: str,
    where: str,
    params: Sequence[object],
    allowed_prev: Sequence[str],
    what: str,
    touch_updated_at: bool,
) -> None:
    """Forward-only status UPDATE; raises NotFound/TransitionError otherwise."""
    assignment = "status = %s" + (", updated_at = now()" if touch_updated_at else "")
    placeholders = ", ".join(["%s"] * len(allowed_prev))
    # Table/column fragments are internal constants, never caller input.
    cursor.execute(
        f"UPDATE {table} SET {assignment} WHERE {where} AND status IN ({placeholders})",  # noqa: S608
        [new_status, *params, *allowed_prev],
    )
    if cursor.rowcount == 1:
        return
    cursor.execute(f"SELECT status FROM {table} WHERE {where}", list(params))  # noqa: S608
    row = cursor.fetchone()
    if row is None:
        raise AuditNotFoundError(f"{what} not found")
    raise AuditTransitionError(f"{what} cannot move {row[0]!r} -> {new_status!r}")


@fail_closed
def create_request(db: AuditDatabase, ctx: RequestContext) -> str:
    """Persist a request's trusted identity; retry-safe (replays are no-ops).

    Concurrent retries are serialized per ``request_id``; a replay that names
    a different requester, workspace, or entry point raises
    ``AuditStoreError`` instead of silently reusing someone else's request.
    """
    payload = redact_payload(
        {
            "workspace_id": ctx.workspace_id,
            "requester_member_id": ctx.requester.member_id,
            "entry_point": ctx.entry_point.value,
            "slack_team_id": ctx.slack_team_id,
            "slack_channel_id": ctx.slack_channel_id,
            "slack_thread_ts": ctx.slack_thread_ts,
            "slack_event_id": ctx.slack_event_id,
        }
    )
    with db.tx() as cursor:
        _xact_lock(cursor, _LOCK_NS_REQUEST, ctx.request_id)
        cursor.execute(
            "SELECT payload FROM request_events "
            "WHERE request_id = %s AND event_type = 'request_received' "
            "ORDER BY id LIMIT 1",
            [ctx.request_id],
        )
        row = cursor.fetchone()
        if row is not None:
            stored = _payload(row)
            identity = ("workspace_id", "requester_member_id", "entry_point")
            if stored is None or any(stored.get(key) != payload[key] for key in identity):
                raise AuditStoreError(
                    f"request {ctx.request_id!r} is already recorded for a different "
                    "requester, workspace, or entry point"
                )
            return ctx.request_id
        cursor.execute(
            "INSERT INTO request_events "
            "(request_id, trace_id, event_type, payload, occurred_at) "
            "VALUES (%s, %s, 'request_received', %s, %s)",
            [ctx.request_id, ctx.trace_id, payload, _aware(ctx.received_at, "received_at")],
        )
        return ctx.request_id


@fail_closed
def record_plan(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    plan: PlanProposal,
    armoriq_plan_id: str | None = None,
    plan_hash: str | None = None,
    matched_topic_rule_id: str | None = None,
    chosen_tier: DataTier | None = None,
    escalation_confirmed: bool | None = None,
    plan_id: str | None = None,
    token_expires_at: datetime | None = None,
) -> str:
    """Persist a validated plan, its steps, and the proposal event atomically.

    ``plan_id``: pass the gateway's ``PlanHandle.plan_id`` so later
    ``ApprovalStatus`` / ``ToolInvocationResult`` rows (which carry the
    gateway ID) reference this row; omitted, a ``plan-<hex>`` ID is minted.
    A supplied ID that already exists raises ``AuditTransitionError``.
    ``matched_topic_rule_id`` / ``escalation_confirmed`` default to the
    plan's own ``matched_topic_rule_id`` / ``escalation_user_confirmed``
    when not passed. ``token_expires_at`` (``PlanHandle.token_expires_at``)
    is recorded as ``intent_expires_at``.
    """
    supplied = plan_id is not None
    plan_id = _validated_plan_id(plan_id) if plan_id is not None else _new_id("plan")
    topic_rule = (
        matched_topic_rule_id if matched_topic_rule_id is not None else plan.matched_topic_rule_id
    )
    escalation = (
        escalation_confirmed if escalation_confirmed is not None else plan.escalation_user_confirmed
    )
    expires_at = _aware(token_expires_at, "token_expires_at") if token_expires_at else None
    with db.tx() as cursor:
        if supplied:
            cursor.execute("SELECT request_id FROM plans WHERE id = %s", [plan_id])
            if cursor.fetchone() is not None:
                raise AuditTransitionError(f"plan {plan_id!r} is already recorded")
        cursor.execute(
            "INSERT INTO plans (id, request_id, trace_id, workspace_id, "
            "requester_member_id, entry_point, armoriq_plan_id, plan_hash, status) "
            "SELECT %s, %s, %s, payload->>'workspace_id', payload->>'requester_member_id', "
            "payload->>'entry_point', %s, %s, 'planned' FROM request_events "
            "WHERE request_id = %s AND event_type = 'request_received' "
            "ORDER BY id LIMIT 1",
            [plan_id, request_id, trace_id, armoriq_plan_id, plan_hash, request_id],
        )
        if cursor.rowcount != 1:
            raise AuditNotFoundError(f"request {request_id!r} has no request_received event")
        for step in plan.steps:
            cursor.execute(
                "INSERT INTO plan_steps "
                "(plan_id, step_index, tool_name, mcp_endpoint, arguments, status) "
                "VALUES (%s, %s, %s, %s, %s, 'planned')",
                [
                    plan_id,
                    step.step_index,
                    step.tool.value,
                    step.mcp.value,
                    redact_payload(dict(step.arguments)),
                ],
            )
        cursor.execute(
            "INSERT INTO request_events "
            "(request_id, trace_id, plan_id, event_type, payload, occurred_at) "
            "VALUES (%s, %s, %s, 'plan_proposed', %s, %s)",
            [
                request_id,
                trace_id,
                plan_id,
                redact_payload(
                    {
                        "summary": plan.summary,
                        "step_count": len(plan.steps),
                        "tools": [step.tool.value for step in plan.steps],
                        "armoriq_plan_id": armoriq_plan_id,
                        "matched_topic_rule_id": topic_rule,
                        "chosen_tier": chosen_tier.value if chosen_tier else None,
                        "escalation_confirmed": escalation,
                        "intent_expires_at": expires_at.isoformat() if expires_at else None,
                    }
                ),
                _now(),
            ],
        )
        return plan_id


@fail_closed
def record_step_decision(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    plan_id: str,
    step_index: int,
    outcome: StepOutcomeLike,
) -> None:
    """Persist one step's authorization outcome; moves the step to ``decided``.

    A decision is never execution proof: the ledger row (P2-07) is the only
    execution evidence, referenced separately via :func:`record_execution_ref`.
    Replays are rejected (``TransitionError``): the orchestrator dedupes
    upstream, so a second decision for one step is a conflict, not a retry.
    The delegation ID comes from the approval, else from the decision itself
    (two different IDs are rejected).
    """
    decision = outcome.decision
    approval = outcome.approval
    invocation = outcome.invocation
    approval_delegation = approval.delegation_id if approval else None
    if (
        approval_delegation
        and decision.delegation_id
        and approval_delegation != decision.delegation_id
    ):
        raise AuditStoreError("decision and approval name different delegation IDs")
    delegation_id = approval_delegation or decision.delegation_id
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO decision_events (request_id, trace_id, plan_id, step_index, "
            "jti, event_type, decision_source, delegation_id, payload, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, 'authorization_decided', %s, %s, %s, %s)",
            [
                request_id,
                trace_id,
                plan_id,
                step_index,
                invocation.jti if invocation else None,
                decision.source.value,
                delegation_id,
                redact_payload(
                    {
                        "outcome": decision.outcome.value,
                        "reason": decision.reason,
                        "enforcement_action": decision.enforcement_action,
                        "armoriq_plan_id": decision.armoriq_plan_id,
                        "delegation_id": delegation_id,
                        "approval_state": approval.state.value if approval else None,
                    }
                ),
                _aware(decision.decided_at, "decided_at"),
            ],
        )
        _advance(
            cursor,
            "plan_steps",
            "decided",
            "plan_id = %s AND step_index = %s",
            [plan_id, step_index],
            ("open", "planned"),
            f"plan step {plan_id}:{step_index}",
            touch_updated_at=False,  # plan_steps has no updated_at column (0004)
        )


@fail_closed
def record_approval(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    approval: ApprovalStatus,
    requested_by_member_id: str | None = None,
) -> int:
    """Insert a HOLD or resolve it; forward-only (``pending`` -> terminal).

    Matches by ``delegation_id`` when present, else the latest unresolved row
    for the plan step — never a row that already belongs to another
    delegation (``AuditTransitionError``). A row created before the
    delegation existed gets its ``delegation_id`` filled in (never
    overwritten). Writers for one plan step are serialized, so concurrent
    HOLDs cannot create two pending rows. Replays of the same state return
    the existing row.
    """
    if approval.request_id != request_id:
        raise AuditStoreError("approval belongs to a different request")
    delegation = approval.delegation_id
    step_key = f"{approval.plan_id}:{approval.step_index}"
    with db.tx() as cursor:
        _xact_lock(cursor, _LOCK_NS_HOLD, step_key)
        existing = None
        if delegation:
            cursor.execute(
                "SELECT id, state, delegation_id, plan_id, step_index FROM approval_requests "
                "WHERE delegation_id = %s",
                [delegation],
            )
            existing = cursor.fetchone()
            if existing is not None and (existing[3], existing[4]) != (
                approval.plan_id,
                approval.step_index,
            ):
                raise AuditTransitionError(
                    f"delegation {delegation!r} is recorded for another plan step"
                )
        if existing is None:
            cursor.execute(
                "SELECT id, state, delegation_id, plan_id, step_index FROM approval_requests "
                "WHERE plan_id = %s AND step_index = %s AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                [approval.plan_id, approval.step_index],
            )
            existing = cursor.fetchone()
            if existing is not None and delegation and existing[2] and existing[2] != delegation:
                raise AuditTransitionError(
                    f"plan step {step_key} already has an unresolved HOLD for another delegation"
                )
        changed = False
        stored_delegation = existing[2] if existing is not None else None
        if existing is None:
            cursor.execute(
                "INSERT INTO approval_requests (plan_id, step_index, tool_name, "
                "delegation_id, state, requested_at, resolved_at, requested_by_member_id) "
                "SELECT id, %s, %s, %s, %s, %s, %s, %s FROM plans "
                "WHERE id = %s AND request_id = %s RETURNING id",
                [
                    approval.step_index,
                    approval.tool.value,
                    delegation,
                    approval.state.value,
                    _aware(approval.requested_at, "requested_at"),
                    _aware(approval.resolved_at, "resolved_at") if approval.resolved_at else None,
                    requested_by_member_id,
                    approval.plan_id,
                    request_id,
                ],
            )
            row = cursor.fetchone()
            if row is None:
                raise AuditNotFoundError(
                    f"plan {approval.plan_id!r} not found for request {request_id!r}"
                )
            approval_id = row[0]
        else:
            approval_id = existing[0]
            current = existing[1]
            changed = approval.state.value != current
            if changed:
                _forward(
                    current,
                    approval.state.value,
                    ("pending", *_APPROVAL_TERMINAL),
                    f"approval {approval_id}",
                )
                cursor.execute(
                    "UPDATE approval_requests SET state = %s, resolved_at = %s, "
                    "delegation_id = COALESCE(delegation_id, %s), updated_at = now() "
                    "WHERE id = %s AND state = 'pending'",
                    [
                        approval.state.value,
                        _aware(approval.resolved_at, "resolved_at")
                        if approval.resolved_at
                        else _now(),
                        delegation,
                        approval_id,
                    ],
                )
                if cursor.rowcount != 1:
                    raise AuditTransitionError(f"approval {approval_id} already resolved")
            elif delegation and not stored_delegation:
                cursor.execute(
                    "UPDATE approval_requests SET delegation_id = COALESCE(delegation_id, %s), "
                    "updated_at = now() WHERE id = %s",
                    [delegation, approval_id],
                )
        resolved = approval.state.value != "pending"
        if existing is None or changed:
            cursor.execute(
                "INSERT INTO request_events (request_id, trace_id, plan_id, step_index, "
                "event_type, payload, occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    request_id,
                    trace_id,
                    approval.plan_id,
                    approval.step_index,
                    "approval_resolved" if resolved else "hold_created",
                    redact_payload(
                        {
                            "approval_id": approval_id,
                            "state": approval.state.value,
                            "delegation_id": delegation or stored_delegation,
                            "tool_name": approval.tool.value,
                        }
                    ),
                    _now(),
                ],
            )
        return approval_id


@fail_closed
def record_execution_ref(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    jti: str,
    ledger_entry_id: str | None,
    status: ToolExecutionStatus,
    plan_id: str | None = None,
    step_index: int | None = None,
    error_class: str | None = None,
    tool_server_reached: bool | None = None,
    validation_outcome: str | None = None,
    result_id: str | None = None,
) -> None:
    """Store an execution *reference* (IDs/status only, never ledger content).

    The backend role cannot read the ledger; these IDs let auditors correlate
    with tool-server evidence held elsewhere. Only ``executed`` claims
    execution (event ``tool_executed``), and only together with a ledger
    entry ID; every other status is a ``tool_rejected`` event meaning "no
    ledger-backed execution claim" whose payload's ``execution_status`` says
    exactly what happened. With ``plan_id`` + ``step_index`` the step moves
    ``decided`` -> ``executed`` / ``rejected`` / ``not_reached`` / ``error`` /
    ``unknown``. ``tool_server_reached`` must agree with the status (the
    ``ToolInvocationResult`` rule); ``validation_outcome`` / ``result_id`` are
    the tool server's reported references.
    """
    status = ToolExecutionStatus(status)
    executed = status is ToolExecutionStatus.EXECUTED
    if executed and not ledger_entry_id:
        raise AuditStoreError("executed execution refs require a ledger_entry_id")
    if executed and tool_server_reached is False:
        raise AuditStoreError("an executed call must have reached the tool server")
    if status is ToolExecutionStatus.NOT_REACHED and (tool_server_reached or ledger_entry_id):
        raise AuditStoreError("not_reached refs cannot carry tool_server_reached or a ledger entry")
    if executed:
        tool_server_reached = True
    elif status is ToolExecutionStatus.NOT_REACHED:
        tool_server_reached = False
    if (plan_id is None) != (step_index is None):
        raise AuditStoreError("plan_id and step_index must be given together")
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO decision_events (request_id, trace_id, plan_id, step_index, "
            "jti, event_type, payload, occurred_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                request_id,
                trace_id,
                plan_id,
                step_index,
                jti,
                "tool_executed" if executed else "tool_rejected",
                redact_payload(
                    {
                        "execution_status": status.value,
                        "ledger_entry_id": ledger_entry_id,
                        "tool_server_reached": tool_server_reached,
                        "validation_outcome": validation_outcome,
                        "result_id": result_id,
                        "error_class": error_class,
                    }
                ),
                _now(),
            ],
        )
        if plan_id is not None and step_index is not None:
            _advance(
                cursor,
                "plan_steps",
                _STEP_LABELS[status],
                "plan_id = %s AND step_index = %s",
                [plan_id, step_index],
                ("decided",),
                f"plan step {plan_id}:{step_index}",
                touch_updated_at=False,  # plan_steps has no updated_at column (0004)
            )


@fail_closed
def record_answer(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    citations: Sequence[SourceCitation],
    highest_tier: DataTier,
    abstained: bool,
    reason: str | None,
    plan_id: str | None = None,
) -> None:
    """Persist the generated answer (citation IDs + tiers only, never snippets)."""
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO request_events (request_id, trace_id, plan_id, event_type, "
            "payload, occurred_at) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                request_id,
                trace_id,
                plan_id,
                "answer_abstained" if abstained else "answer_generated",
                redact_payload(
                    {
                        "citations": [
                            {"source_id": c.source_id, "tier": c.tier.value} for c in citations
                        ],
                        "highest_tier_returned": highest_tier.value,
                        "abstained": abstained,
                        "reason": reason,
                    }
                ),
                _now(),
            ],
        )
        cursor.execute(
            "UPDATE plans SET status = 'answered', updated_at = now() "
            "WHERE request_id = %s AND status IN ('open', 'planned', 'decided')",
            [request_id],
        )


@fail_closed
def record_delivery(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    channel: DeliveryChannel,
    recipient_member_id: str,
    tier_delivered: DataTier,
    status: DeliveryStatus,
    error_class: str | None = None,
    plan_id: str | None = None,
) -> int:
    """Append one delivery attempt (its own ``delivery_events`` row + event).

    Delivery rows are append-only — each attempt is a new row, nothing moves.
    Only a ``delivered`` attempt advances the request's plans to
    ``delivered``; ``failed`` (R-64: no fallback destination) and ``pending``
    leave plan status unchanged, so a failed DM is never reported as
    delivered.
    """
    if status not in _DELIVERY_STATUSES:
        raise AuditStoreError(f"unknown delivery status {status!r}")
    with db.tx() as cursor:
        delivered_at = _now() if status == "delivered" else None
        cursor.execute(
            "INSERT INTO delivery_events (request_id, trace_id, plan_id, "
            "delivery_channel, delivered_to_member_id, delivery_status, "
            "delivered_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            [
                request_id,
                trace_id,
                plan_id,
                channel.value,
                recipient_member_id,
                status,
                delivered_at,
            ],
        )
        row = cursor.fetchone()
        delivery_id = row[0]
        event_type = (
            "delivery_succeeded"
            if status == "delivered"
            else ("delivery_failed" if status == "failed" else "delivery_attempted")
        )
        cursor.execute(
            "INSERT INTO request_events (request_id, trace_id, plan_id, event_type, "
            "payload, occurred_at) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                request_id,
                trace_id,
                plan_id,
                event_type,
                redact_payload(
                    {
                        "delivery_id": delivery_id,
                        "channel": channel.value,
                        "recipient_member_id": recipient_member_id,
                        "tier_delivered": tier_delivered.value,
                        "error_class": error_class,
                    }
                ),
                _now(),
            ],
        )
        if status == "delivered":
            cursor.execute(
                "UPDATE plans SET status = 'delivered', updated_at = now() "
                "WHERE request_id = %s AND status IN ('open', 'planned', 'decided', "
                "'answered')",
                [request_id],
            )
        return delivery_id


def _validated_refs(safe_refs: Sequence[str]) -> list[str]:
    if isinstance(safe_refs, str):
        raise AuditStoreError("safe_refs must be a sequence of source IDs, not a string")
    refs = list(safe_refs)
    if len(refs) > _MAX_SAFE_REFS:
        raise AuditStoreError(f"safe_refs is bounded to {_MAX_SAFE_REFS} source IDs")
    for ref in refs:
        # Source IDs only: a ref with whitespace or unbounded length is a
        # snippet, and snippets never enter the queue (Arch §10).
        if (
            not isinstance(ref, str)
            or not ref
            or len(ref) > _MAX_REF_CHARS
            or ref != "".join(ref.split())
        ):
            raise AuditStoreError("safe_refs must be source IDs (no whitespace, <= 200 chars)")
    return refs


@fail_closed
def add_unanswered(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    workspace_id: str,
    requester_member_id: str,
    question: str,
    reason: UnansweredReason,
    safe_refs: Sequence[str] = (),
) -> int:
    """Queue an unanswered question. ``question`` is redacted; only source-ID
    refs are stored alongside — never protected snippets (Arch §10).

    Workspace and requester are taken from the trusted ``request_received``
    event; the parameters must match it (``AuditStoreError`` otherwise). The
    companion ``answer_abstained`` event (``unanswered_question_id``,
    ``reason``, ``safe_refs``) is appended unless it would duplicate the
    abstention ``record_answer`` already wrote for the request: same reason
    and no safe ref beyond that event's cited source IDs. The queue then
    reads the reason from that event (:func:`queries.unanswered`).
    """
    if reason not in _UNANSWERED_REASONS:
        raise AuditStoreError(f"unknown unanswered reason {reason!r}")
    refs = _validated_refs(safe_refs)
    with db.tx() as cursor:
        cursor.execute(
            "SELECT payload FROM request_events "
            "WHERE request_id = %s AND event_type = 'request_received' "
            "ORDER BY id LIMIT 1",
            [request_id],
        )
        origin = _payload(cursor.fetchone())
        if origin is None:
            raise AuditNotFoundError(f"request {request_id!r} has no request_received event")
        trusted_workspace = origin.get("workspace_id")
        trusted_requester = origin.get("requester_member_id")
        if (trusted_workspace, trusted_requester) != (workspace_id, requester_member_id):
            raise AuditStoreError("workspace/requester do not match the trusted request record")
        cursor.execute(
            "INSERT INTO unanswered_questions (request_id, workspace_id, "
            "requester_member_id, question, status) VALUES (%s, %s, %s, %s, 'open') "
            "RETURNING id",
            [request_id, trusted_workspace, trusted_requester, redact_question(question)],
        )
        question_id = cursor.fetchone()[0]
        # Only record_answer's abstention (no question ID) can make the
        # companion redundant; another question's companion never does.
        cursor.execute(
            "SELECT payload FROM request_events "
            "WHERE request_id = %s AND event_type = 'answer_abstained' "
            "AND payload->>'unanswered_question_id' IS NULL "
            "ORDER BY id DESC LIMIT 1",
            [request_id],
        )
        prior = _payload(cursor.fetchone())
        if prior is not None and prior.get("reason") == reason:
            cited = {
                str(item.get("source_id"))
                for item in prior.get("citations") or []
                if isinstance(item, dict)
            }
            if set(refs) <= cited:
                return question_id
        cursor.execute(
            "INSERT INTO request_events (request_id, trace_id, event_type, payload, "
            "occurred_at) VALUES (%s, %s, 'answer_abstained', %s, %s)",
            [
                request_id,
                trace_id,
                redact_payload(
                    {
                        "unanswered_question_id": question_id,
                        "reason": reason,
                        "safe_refs": refs,
                    }
                ),
                _now(),
            ],
        )
        return question_id


def _slack_ref(value: object, field: str, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _SLACK_REF_RE.fullmatch(value):
        raise AuditStoreError(
            f"{field} must be 1-128 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return value


@fail_closed
def record_slack_event_rejected(
    db: AuditDatabase,
    *,
    slack_event_id: str,
    reason: SlackRejectionReason,
    team_id: str | None,
    channel_id: str | None,
) -> str:
    """Record a Slack event dropped before it became a request (P3-05 ICR).

    Metadata only: the Slack event/team/channel IDs and a reason code, never
    message text and never a Slack user ID (an unmapped sender is not a
    member). The ``slack_event_rejected`` row is appended to
    ``request_events`` under the synthetic key ``rej-<slack_event_id>``
    (returned). It has no ``request_received`` origin, so it is never a
    request: :func:`services.audit.queries.recent_requests` skips it and
    ``timeline`` / ``request_status`` report the key as not found.

    Raises ``AuditStoreError`` on an unknown reason, a malformed ID, or a
    database failure. The caller has already refused the event, so a failed
    write never lets it through (fail closed).
    """
    event_id = _slack_ref(slack_event_id, "slack_event_id", required=True)
    if reason not in _SLACK_REJECTION_REASONS:
        raise AuditStoreError(f"reason must be one of {', '.join(_SLACK_REJECTION_REASONS)}")
    team = _slack_ref(team_id, "team_id", required=False)
    channel = _slack_ref(channel_id, "channel_id", required=False)
    key = f"rej-{event_id}"
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO request_events "
            "(request_id, trace_id, event_type, payload, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            [
                key,
                key,
                AuditEventType.SLACK_EVENT_REJECTED.value,
                redact_payload(
                    {
                        "reason": reason,
                        "slack_event_id": event_id,
                        "team_id": team,
                        "channel_id": channel,
                    }
                ),
                _now(),
            ],
        )
    return key


_CONFIRMATION_CREDENTIAL_MARKERS = (
    "token",
    "secret",
    "credential",
    "password",
    "bearer",
    "api_key",
    "apikey",
    "authorization",
)


def _check_confirmation_event(event: AuditEvent) -> None:
    """Validate a P3-04 confirmation-lifecycle payload (issue #25 ICR).

    Exhaustive key allowlists plus typed fields: a raw confirmation token or
    any other credential can never be recorded, under any key.
    """
    kind = event.event_type
    payload = dict(event.payload)
    if kind is AuditEventType.ESCALATION_CREATED:
        allowed = _ESCALATION_CREATED_KEYS
        required = {"escalation_id"}
    else:
        allowed = _CONFIRMATION_RESOLVED_KEYS
        required = {"escalation_id", "confirmation_state"}

    if any(marker in key.lower() for key in payload for marker in _CONFIRMATION_CREDENTIAL_MARKERS):
        raise AuditStoreError(f"{kind.value} payload must never carry a token or credential")
    extra = set(payload) - allowed
    if extra:
        raise AuditStoreError(f"{kind.value} has unsupported payload keys: {sorted(extra)}")
    missing = required - set(payload)
    if missing:
        raise AuditStoreError(f"{kind.value} payload is missing keys: {sorted(missing)}")

    escalation_id = payload["escalation_id"]
    if not isinstance(escalation_id, str) or not _ESCALATION_ID_RE.fullmatch(escalation_id):
        raise AuditStoreError("escalation_id must be a bounded opaque identifier")
    if kind is AuditEventType.ESCALATION_CREATED:
        digest = payload.get("plan_digest")
        if digest is not None and (
            not isinstance(digest, str) or not _PLAN_DIGEST_RE.fullmatch(digest)
        ):
            raise AuditStoreError("plan_digest must be a hex digest")
        tier = payload.get("requested_tier")
        if tier is not None and tier not in {member.value for member in DataTier}:
            raise AuditStoreError("requested_tier must be a DataTier value")
    else:
        state = payload.get("confirmation_state")
        if state not in _CONFIRMATION_STATES:
            raise AuditStoreError(
                "confirmation_state must be one of " + ", ".join(sorted(_CONFIRMATION_STATES))
            )


def _check_generic_event(event: AuditEvent) -> None:
    """Apply the typed writers' evidence rules to :func:`append_event`."""
    kind = event.event_type
    if kind in (AuditEventType.ESCALATION_CREATED, AuditEventType.CONFIRMATION_RESOLVED):
        _check_confirmation_event(event)
    if kind is AuditEventType.REQUEST_RECEIVED:
        raise AuditStoreError(
            "request_received is written only by create_request (trusted RequestContext)"
        )
    if kind is AuditEventType.SLACK_EVENT_REJECTED:
        raise AuditStoreError(
            "slack_event_rejected is written only by record_slack_event_rejected (metadata only)"
        )
    if kind is AuditEventType.AUTHORIZATION_DECIDED and event.decision_source is None:
        raise AuditStoreError(
            "authorization_decided needs a decision_source "
            "(use record_step_decision for planned steps)"
        )
    if kind in (AuditEventType.TOOL_EXECUTED, AuditEventType.TOOL_REJECTED) and not event.jti:
        raise AuditStoreError(f"{kind.value} needs a jti (use record_execution_ref)")
    if kind is AuditEventType.TOOL_EXECUTED:
        ledger_entry_id = event.payload.get("ledger_entry_id")
        status = event.payload.get("execution_status", ToolExecutionStatus.EXECUTED.value)
        if not isinstance(ledger_entry_id, str) or not ledger_entry_id:
            raise AuditStoreError(
                "tool_executed needs payload.ledger_entry_id (use record_execution_ref)"
            )
        if status != ToolExecutionStatus.EXECUTED.value:
            raise AuditStoreError("tool_executed payload must not report a non-executed status")


@fail_closed
def append_event(db: AuditDatabase, *, event: AuditEvent) -> None:
    """Append a generic audit event, routed by type (decision events carry sources).

    For transitions without a typed writer (e.g. ``plan_rejected``,
    ``plan_minted``, off-plan ``authorization_decided``). The typed writers'
    evidence rules still apply: ``request_received`` is refused (identity
    comes only from ``create_request``), ``authorization_decided`` needs a
    ``decision_source``, ``tool_executed`` needs a ``jti`` and a
    ``payload.ledger_entry_id``, ``tool_rejected`` needs a ``jti``.
    """
    _check_generic_event(event)
    occurred_at = _aware(event.occurred_at, "occurred_at")
    payload = redact_payload({**dict(event.payload), "event_id": event.event_id})
    with db.tx() as cursor:
        if event.event_type in _DECISION_EVENTS:
            cursor.execute(
                "INSERT INTO decision_events (request_id, trace_id, plan_id, "
                "step_index, jti, event_type, decision_source, payload, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    event.request_id,
                    event.trace_id,
                    event.plan_id,
                    event.step_index,
                    event.jti,
                    event.event_type.value,
                    event.decision_source.value if event.decision_source else None,
                    payload,
                    occurred_at,
                ],
            )
        else:
            cursor.execute(
                "INSERT INTO request_events (request_id, trace_id, plan_id, "
                "step_index, jti, event_type, payload, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    event.request_id,
                    event.trace_id,
                    event.plan_id,
                    event.step_index,
                    event.jti,
                    event.event_type.value,
                    payload,
                    occurred_at,
                ],
            )


__all__ = [
    "DeliveryStatus",
    "SlackRejectionReason",
    "StepOutcomeLike",
    "UnansweredReason",
    "add_unanswered",
    "append_event",
    "create_request",
    "record_answer",
    "record_approval",
    "record_delivery",
    "record_execution_ref",
    "record_plan",
    "record_slack_event_rejected",
    "record_step_decision",
]
