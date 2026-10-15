"""Backend audit write API (P3-01, PRD v2 §7.14/§10.1, Arch §7.2/§10).

Every function takes an :class:`AuditDatabase` (pool-backed or fake) and uses
only the backend role's tables: ``plans``, ``plan_steps``,
``approval_requests``, ``unanswered_questions`` (read/write) and
``request_events``, ``decision_events``, ``delivery_events`` (append-only).
Protected knowledge tables and the execution ledger are never named here —
the backend stores execution *references* (IDs reported by the orchestrator),
never ledger content, and audit SQL is covered by a test asserting that.

Schema mapping notes (no migration change was needed):

- There is no ``requests`` table: a request exists as its ``request_received``
  event. ``create_request`` is retry-safe (replays return the same ID).
- ``matched_topic_rule_id`` / ``chosen_tier`` / ``escalation_confirmed`` have
  no plan columns: they travel in the ``plan_proposed`` event payload.
- ``unanswered_questions`` has no reason column: ``reason`` and ``safe_refs``
  travel in the companion ``answer_abstained`` event; :mod:`queries` rejoins
  them in code (same allowed tables, no protected join).
- ``delivery_events`` has no tier/error columns: ``tier_delivered`` and
  ``error_class`` travel in the companion delivery event row.
- ``record_step_decision`` accepts the gateway ``StepOutcome`` structurally
  (``decision`` / ``approval`` / ``invocation`` attributes) via
  :class:`StepOutcomeLike`, so this module never imports the gateway or the
  ArmorIQ SDK. Pass ``services.authorization.armoriq_gateway.StepOutcome``.

Fail-closed contract for P3-04: any write failure raises
:class:`AuditStoreError` (fearless to catch and abort the request); failed
writes never leave partial multi-row records (one transaction per operation).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol

from apps.api.core.db import AuditCursor, AuditDatabase
from services.audit.errors import AuditNotFoundError, AuditStoreError, AuditTransitionError
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

_APPROVAL_TERMINAL = ("approved", "rejected", "expired", "timeout", "interrupted", "unverified")
_DELIVERY_ORDER = ("pending", "delivered", "failed")

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


def create_request(db: AuditDatabase, ctx: RequestContext) -> str:
    """Persist a request's trusted identity; retry-safe (replays are no-ops)."""
    with db.tx() as cursor:
        cursor.execute(
            "SELECT payload FROM request_events "
            "WHERE request_id = %s AND event_type = 'request_received' "
            "ORDER BY id LIMIT 1",
            [ctx.request_id],
        )
        if cursor.fetchone() is not None:
            return ctx.request_id
        payload = redact_payload(
            {
                "workspace_id": ctx.workspace_id,
                "requester_member_id": ctx.requester.member_id,
                "entry_point": ctx.entry_point.value,
                "slack_team_id": ctx.slack_team_id,
                "slack_channel_id": ctx.slack_channel_id,
                "slack_event_id": ctx.slack_event_id,
            }
        )
        cursor.execute(
            "INSERT INTO request_events "
            "(request_id, trace_id, event_type, payload, occurred_at) "
            "VALUES (%s, %s, 'request_received', %s, %s)",
            [ctx.request_id, ctx.trace_id, payload, _aware(ctx.received_at, "received_at")],
        )
        return ctx.request_id


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
    escalation_confirmed: bool = False,
) -> str:
    """Persist a validated plan, its steps, and the proposal event atomically."""
    plan_id = _new_id("plan")
    with db.tx() as cursor:
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
            spec_endpoint = step.mcp.value
            cursor.execute(
                "INSERT INTO plan_steps "
                "(plan_id, step_index, tool_name, mcp_endpoint, arguments, status) "
                "VALUES (%s, %s, %s, %s, %s, 'planned')",
                [
                    plan_id,
                    step.step_index,
                    step.tool.value,
                    spec_endpoint,
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
                        "matched_topic_rule_id": matched_topic_rule_id,
                        "chosen_tier": chosen_tier.value if chosen_tier else None,
                        "escalation_confirmed": escalation_confirmed,
                    }
                ),
                _now(),
            ],
        )
        return plan_id


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
    """
    decision = outcome.decision
    approval = outcome.approval
    invocation = outcome.invocation
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
                approval.delegation_id if approval else None,
                redact_payload(
                    {
                        "outcome": decision.outcome.value,
                        "reason": decision.reason,
                        "enforcement_action": decision.enforcement_action,
                        "armoriq_plan_id": decision.armoriq_plan_id,
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
            touch_updated_at=True,
        )


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
    for the plan step. Replays of the same state return the existing row.
    """
    with db.tx() as cursor:
        existing = None
        if approval.delegation_id:
            cursor.execute(
                "SELECT id, state FROM approval_requests WHERE delegation_id = %s",
                [approval.delegation_id],
            )
            existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                "SELECT id, state FROM approval_requests "
                "WHERE plan_id = %s AND step_index = %s AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                [approval.plan_id, approval.step_index],
            )
            existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                "INSERT INTO approval_requests (plan_id, step_index, tool_name, "
                "delegation_id, state, requested_at, resolved_at, "
                "requested_by_member_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                [
                    approval.plan_id,
                    approval.step_index,
                    approval.tool.value,
                    approval.delegation_id,
                    approval.state.value,
                    _aware(approval.requested_at, "requested_at"),
                    _aware(approval.resolved_at, "resolved_at") if approval.resolved_at else None,
                    requested_by_member_id,
                ],
            )
            row = cursor.fetchone()
            approval_id = row[0]
        else:
            approval_id, current = existing[0], existing[1]
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
                    "updated_at = now() WHERE id = %s AND state = 'pending'",
                    [
                        approval.state.value,
                        _aware(approval.resolved_at, "resolved_at")
                        if approval.resolved_at
                        else _now(),
                        approval_id,
                    ],
                )
                if cursor.rowcount != 1:
                    raise AuditTransitionError(f"approval {approval_id} already resolved")
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
                            "delegation_id": approval.delegation_id,
                        }
                    ),
                    _now(),
                ],
            )
        return approval_id


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
) -> None:
    """Store an execution *reference* (IDs/status only, never ledger content).

    The backend role cannot read the ledger; these IDs let auditors correlate
    with tool-server evidence held elsewhere. Only ``executed`` claims
    execution, and only together with a ledger entry ID.
    """
    executed = status is ToolExecutionStatus.EXECUTED
    if executed and not ledger_entry_id:
        raise AuditStoreError("executed execution refs require a ledger_entry_id")
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
                        "error_class": error_class,
                    }
                ),
                _now(),
            ],
        )
        if plan_id is not None and step_index is not None:
            terminal = (
                "executed"
                if executed
                else (
                    "rejected"
                    if status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER
                    else "failed"
                )
            )
            if status in (
                ToolExecutionStatus.EXECUTED,
                ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
                ToolExecutionStatus.ERROR,
            ):
                _advance(
                    cursor,
                    "plan_steps",
                    terminal,
                    "plan_id = %s AND step_index = %s",
                    [plan_id, step_index],
                    ("decided",),
                    f"plan step {plan_id}:{step_index}",
                    touch_updated_at=True,
                )


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
    """Persist a delivery attempt; forward-only (``pending`` -> terminal)."""
    if status not in _DELIVERY_ORDER:
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
                        "tier_delivered": tier_delivered.value,
                        "error_class": error_class,
                    }
                ),
                _now(),
            ],
        )
        if status in ("delivered", "failed"):
            cursor.execute(
                "UPDATE plans SET status = 'delivered', updated_at = now() "
                "WHERE request_id = %s AND status IN ('open', 'planned', 'decided', "
                "'answered')",
                [request_id],
            )
        return delivery_id


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
    refs are stored alongside — never protected snippets (Arch §10)."""
    if len(safe_refs) > 20:
        raise AuditStoreError("safe_refs is bounded to 20 source IDs")
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO unanswered_questions (request_id, workspace_id, "
            "requester_member_id, question, status) VALUES (%s, %s, %s, %s, 'open') "
            "RETURNING id",
            [
                request_id,
                workspace_id,
                requester_member_id,
                redact_question(question),
            ],
        )
        row = cursor.fetchone()
        question_id = row[0]
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
                        "safe_refs": list(safe_refs),
                    }
                ),
                _now(),
            ],
        )
        return question_id


def append_event(db: AuditDatabase, *, event: AuditEvent) -> None:
    """Append a generic audit event, routed by type (decision events carry sources)."""
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
    "record_step_decision",
]
