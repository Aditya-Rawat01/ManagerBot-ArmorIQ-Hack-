"""Backend audit read API (P3-01, PRD v2 §10.1, Arch §7.2/§10).

Authorization model (callers: P4-01 web request routes, P4-04 manager console):

- ``request_status`` and ``request_detail`` (P4-01 addition) are
  owner-only: the caller passes the *trusted*
  ``member_id`` resolved from the session (P1-03), never a client-supplied
  string. A bare ``member_id`` proves nothing on its own — the route must
  resolve it from the signed session first.
- ``recent_requests`` / ``timeline`` / ``pending_holds`` / ``unanswered``
  are Maya-only: they take the trusted :class:`AuthenticatedUser` and refuse
  non-managers inside (fail closed), so a missing caller check cannot leak.

No query touches protected knowledge tables or the execution ledger — only
``plans``, ``plan_steps``, ``approval_requests``, ``unanswered_questions``
and the three event tables. Returned rows are bounded (explicit limits) and
payloads are re-redacted defensively, so even pre-redaction rows cannot leak
through a new reader. Every function raises an ``AuditError`` subclass on
failure (driver errors are wrapped as ``AuditStoreError``).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from services.audit.errors import (
    AuditNotAuthorizedError,
    AuditNotFoundError,
    AuditStoreError,
    fail_closed,
)
from services.audit.protocols import AuditCursor, AuditDatabase
from services.audit.redaction import redact_payload
from services.contracts import ARMORIQ_POLICY_SOURCES, AppRole, AuthenticatedUser

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_TIMELINE_LIMIT = 500

_ARMORIQ_POLICY_SOURCE_VALUES = frozenset(source.value for source in ARMORIQ_POLICY_SOURCES)


@dataclass(frozen=True)
class RequestSummary:
    request_id: str
    trace_id: str
    workspace_id: str
    entry_point: str
    requester_member_id: str
    last_event_at: datetime


@dataclass(frozen=True)
class RequestStatusView:
    request_id: str
    trace_id: str
    workspace_id: str
    entry_point: str
    requester_member_id: str
    plan_ids: tuple[str, ...]
    pending_holds: int
    delivered: bool
    last_event_at: datetime | None


@dataclass(frozen=True)
class TimelineEntry:
    occurred_at: datetime
    kind: str  # "request" | "decision" | "delivery"
    event_type: str
    plan_id: str | None
    step_index: int | None
    decision_source: str | None
    payload: dict[str, object]
    # Planned tool for (plan_id, step_index), joined from plan_steps; for
    # off-plan decisions, the payload's tool_name when one was recorded.
    tool_name: str | None = None

    @property
    def is_armoriq_policy(self) -> bool:
        """True only for decisions an ArmorIQ policy layer made (ADR-026).

        ``ARMORIQ_POLICY_SOURCES`` only: the client-side SDK plan check,
        tool-server rules, application rules and ``unknown`` decisions are
        never shown as ArmorIQ policy outcomes.
        """
        return self.kind == "decision" and self.decision_source in _ARMORIQ_POLICY_SOURCE_VALUES


@dataclass(frozen=True)
class PendingHold:
    approval_id: int
    request_id: str
    plan_id: str
    step_index: int
    tool_name: str
    delegation_id: str | None
    requested_at: datetime
    requested_by_member_id: str | None


@dataclass(frozen=True)
class UnansweredItem:
    question_id: int
    request_id: str
    question: str
    reason: str | None
    status: str
    created_at: datetime


def _require_manager(actor: AuthenticatedUser) -> None:
    if actor.app_role is not AppRole.MANAGER:
        raise AuditNotAuthorizedError("manager console queries require the Maya role")


def _clamp_limit(limit: int) -> int:
    return max(1, min(_MAX_LIMIT, limit))


def _rows(cursor: AuditCursor) -> list[Sequence[object]]:
    return [row for row in cursor.fetchall() if isinstance(row, Sequence)]


def _request_origin(cursor: AuditCursor, request_id: str) -> dict[str, object]:
    cursor.execute(
        "SELECT trace_id, payload FROM request_events "
        "WHERE request_id = %s AND event_type = 'request_received' "
        "ORDER BY id LIMIT 1",
        [request_id],
    )
    row = cursor.fetchone()
    if row is None:
        raise AuditNotFoundError(f"request {request_id!r} not found")
    payload = row[1] if isinstance(row, (list, tuple)) else None
    if not isinstance(payload, dict):
        raise AuditStoreError(f"request {request_id!r} has a corrupt origin event")
    return {"trace_id": row[0], "payload": payload}


@fail_closed
def request_status(db: AuditDatabase, request_id: str, member_id: str) -> RequestStatusView:
    """Owner-only request status for P4-01.

    ``member_id`` must be the session-resolved member (P1-03); equality with
    the stored requester is enforced here.
    """
    with db.tx() as cursor:
        origin = _request_origin(cursor, request_id)
        payload = origin["payload"]
        if not isinstance(payload, dict):
            raise AuditStoreError(f"request {request_id!r} has a corrupt origin event")
        if payload.get("requester_member_id") != member_id:
            raise AuditNotAuthorizedError("request belongs to another member")
        cursor.execute(
            "SELECT id FROM plans WHERE request_id = %s ORDER BY created_at",
            [request_id],
        )
        plan_ids = tuple(str(row[0]) for row in _rows(cursor))
        cursor.execute(
            "SELECT COUNT(*) FROM approval_requests ar JOIN plans p ON p.id = ar.plan_id "
            "WHERE p.request_id = %s AND ar.state = 'pending'",
            [request_id],
        )
        holds_row = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*) FROM delivery_events "
            "WHERE request_id = %s AND delivery_status = 'delivered'",
            [request_id],
        )
        delivered_row = cursor.fetchone()
        cursor.execute(
            "SELECT MAX(occurred_at) FROM request_events WHERE request_id = %s",
            [request_id],
        )
        last_row = cursor.fetchone()
        return RequestStatusView(
            request_id=request_id,
            trace_id=str(origin["trace_id"]),
            workspace_id=str(payload.get("workspace_id", "")),
            entry_point=str(payload.get("entry_point", "")),
            requester_member_id=member_id,
            plan_ids=plan_ids,
            pending_holds=int(holds_row[0]) if holds_row else 0,
            delivered=bool(delivered_row and delivered_row[0]),
            last_event_at=last_row[0] if last_row else None,
        )


@dataclass(frozen=True)
class StepRecord:
    """One planned step as persisted (P4-01 status view)."""

    step_index: int
    tool_name: str | None
    outcome: str | None = None
    decision_source: str | None = None
    approval_state: str | None = None
    execution_status: str | None = None


@dataclass(frozen=True)
class OffPlanRecord:
    """One off-plan decision event (``off_plan: true``; never executed)."""

    tool_name: str | None
    outcome: str
    decision_source: str
    rejection: str | None
    simulated: bool


@dataclass(frozen=True)
class AnswerRecord:
    """The persisted ``record_answer`` event: citation IDs + tiers only."""

    abstained: bool
    reason: str | None
    citations: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RequestDetail:
    """Owner-only persisted state of one request (P4-01 GET).

    Built only from ``request_events``, ``decision_events``, ``plan_steps``,
    ``approval_requests`` and ``delivery_events`` (never protected tables
    or tool-server tables, which the backend role cannot read).
    ``honeypot_executions`` counts this request's ``tool_executed``
    references (ledger-backed by construction) for ``send_external_message``
    steps: the only way the honeypot can write its synthetic sink.
    """

    request_id: str
    entry_point: str
    steps: tuple[StepRecord, ...]
    off_plan: tuple[OffPlanRecord, ...]
    plan_decision: tuple[str, str] | None  # (outcome, source) of a failed mint
    error: bool
    plan_rejected: bool
    escalated: bool
    simulation_active: bool
    answer: AnswerRecord | None
    delivery_channel: str | None
    delivery_status: str | None
    pending_holds: int
    honeypot_executions: int
    last_event_at: datetime | None


_HONEYPOT_TOOL = "send_external_message"


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@fail_closed
def request_detail(db: AuditDatabase, request_id: str, member_id: str) -> RequestDetail:
    """Owner-only persisted detail for P4-01 (same owner rule as ``request_status``).

    ``member_id`` must be the session-resolved member (P1-03). Another
    member's request raises ``AuditNotAuthorizedError``; a missing one
    ``AuditNotFoundError``. Payloads are re-redacted before use.
    """
    with db.tx() as cursor:
        origin = _request_origin(cursor, request_id)
        payload = _dict(origin["payload"])
        if payload.get("requester_member_id") != member_id:
            raise AuditNotAuthorizedError("request belongs to another member")
        cursor.execute(
            "SELECT ps.step_index, ps.tool_name FROM plan_steps ps "
            "JOIN plans p ON p.id = ps.plan_id WHERE p.request_id = %s "
            "ORDER BY p.created_at, ps.step_index LIMIT %s",
            [request_id, _MAX_LIMIT],
        )
        tools: dict[int, str | None] = {}
        for step_index, tool_name in _rows(cursor):
            if isinstance(step_index, int):
                tools[step_index] = _text(tool_name)
        steps: dict[int, dict[str, str | None]] = {index: {} for index in tools}

        cursor.execute(
            "SELECT event_type, step_index, decision_source, payload FROM decision_events "
            "WHERE request_id = %s ORDER BY occurred_at, id LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        off_plan: list[OffPlanRecord] = []
        plan_decision: tuple[str, str] | None = None
        error = False
        executed_honeypots = 0
        for event_type, step_index, source, raw in _rows(cursor):
            data = redact_payload(_dict(raw))
            if event_type in ("tool_executed", "tool_rejected"):
                if isinstance(step_index, int) and step_index in steps:
                    status = _text(data.get("execution_status"))
                    if event_type == "tool_executed":
                        status = "executed"
                        if tools.get(step_index) == _HONEYPOT_TOOL:
                            executed_honeypots += 1
                    steps[step_index]["execution_status"] = status
                continue
            if event_type != "authorization_decided":
                continue
            outcome = _text(data.get("outcome")) or "unverified"
            label = _text(source) or "unknown"
            if data.get("request_outcome") == "error":
                error = True
            elif data.get("off_plan") is True:
                off_plan.append(
                    OffPlanRecord(
                        tool_name=_text(data.get("tool_name")),
                        outcome=outcome,
                        decision_source=label,
                        rejection=_text(data.get("rejection")),
                        simulated=data.get("simulation") == "hijacked_model",
                    )
                )
            elif isinstance(step_index, int) and step_index in steps:
                steps[step_index]["outcome"] = outcome
                steps[step_index]["decision_source"] = label
                approval = _text(data.get("approval_state"))
                if approval is not None:
                    steps[step_index]["approval_state"] = approval
            elif data.get("stage") == "start_plan":
                plan_decision = (outcome, label)

        cursor.execute(
            "SELECT ar.step_index, ar.state FROM approval_requests ar "
            "JOIN plans p ON p.id = ar.plan_id WHERE p.request_id = %s ORDER BY ar.id LIMIT %s",
            [request_id, _MAX_LIMIT],
        )
        latest_approval: dict[int, str] = {}
        for step_index, state in _rows(cursor):
            if isinstance(step_index, int) and isinstance(state, str):
                latest_approval[step_index] = state
        for step_index, state in latest_approval.items():
            if step_index in steps:
                steps[step_index]["approval_state"] = state

        cursor.execute(
            "SELECT event_type, payload, occurred_at FROM request_events "
            "WHERE request_id = %s ORDER BY occurred_at, id LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        answer: AnswerRecord | None = None
        plan_rejected = escalated = simulation_active = False
        last_event_at: datetime | None = None
        for event_type, raw, occurred_at in _rows(cursor):
            if isinstance(occurred_at, datetime):
                last_event_at = occurred_at
            data = redact_payload(_dict(raw))
            if event_type == "plan_rejected":
                plan_rejected = True
            elif event_type == "escalation_created":
                escalated = True
            elif event_type == "simulation_activated":
                simulation_active = True
            elif event_type in ("answer_generated", "answer_abstained"):
                citations = []
                listed = data.get("citations")
                for item in listed if isinstance(listed, list) else []:
                    entry = _dict(item)
                    source_id, tier = _text(entry.get("source_id")), _text(entry.get("tier"))
                    if source_id and tier:
                        citations.append((source_id, tier))
                answer = AnswerRecord(
                    abstained=event_type == "answer_abstained",
                    reason=_text(data.get("reason")),
                    citations=tuple(citations),
                )

        cursor.execute(
            "SELECT delivery_channel, delivery_status FROM delivery_events "
            "WHERE request_id = %s ORDER BY occurred_at DESC, id DESC LIMIT 1",
            [request_id],
        )
        delivery = cursor.fetchone()
        channel = status = None
        if isinstance(delivery, Sequence) and len(delivery) == 2:
            channel, status = _text(delivery[0]), _text(delivery[1])

        return RequestDetail(
            request_id=request_id,
            entry_point=str(payload.get("entry_point", "")),
            steps=tuple(
                StepRecord(
                    step_index=index,
                    tool_name=tools.get(index),
                    outcome=values.get("outcome"),
                    decision_source=values.get("decision_source"),
                    approval_state=values.get("approval_state"),
                    execution_status=values.get("execution_status"),
                )
                for index, values in sorted(steps.items())
            ),
            off_plan=tuple(off_plan),
            plan_decision=plan_decision,
            error=error,
            plan_rejected=plan_rejected,
            escalated=escalated,
            simulation_active=simulation_active,
            answer=answer,
            delivery_channel=channel,
            delivery_status=status,
            pending_holds=sum(1 for state in latest_approval.values() if state == "pending"),
            honeypot_executions=executed_honeypots,
            last_event_at=last_event_at,
        )


@fail_closed
def recent_requests(
    db: AuditDatabase, actor: AuthenticatedUser, limit: int = _DEFAULT_LIMIT
) -> list[RequestSummary]:
    """Newest requests first (Maya-only, for P4-04).

    One bounded query (no per-request round trips). Only IDs that have a
    ``request_received`` event are requests: rows without one (e.g. Slack
    ``slack_event_rejected`` rows keyed ``rej-<event_id>``, P3-05) are never
    listed and never consume the limit. A request whose origin payload is
    unreadable is still listed, with empty identity fields, rather than
    failing the whole console page.
    """
    _require_manager(actor)
    summaries: list[RequestSummary] = []
    with db.tx() as cursor:
        cursor.execute(
            "WITH latest AS ("
            "SELECT e.request_id, MAX(e.trace_id) AS trace_id, MAX(e.occurred_at) AS last_at "
            "FROM request_events e WHERE EXISTS ("
            "SELECT 1 FROM request_events s WHERE s.request_id = e.request_id "
            "AND s.event_type = 'request_received') "
            "GROUP BY e.request_id ORDER BY last_at DESC LIMIT %s) "
            "SELECT l.request_id, COALESCE(o.trace_id, l.trace_id), o.payload, l.last_at "
            "FROM latest l LEFT JOIN LATERAL ("
            "SELECT r.trace_id, r.payload FROM request_events r "
            "WHERE r.request_id = l.request_id AND r.event_type = 'request_received' "
            "ORDER BY r.id LIMIT 1) o ON TRUE "
            "ORDER BY l.last_at DESC",
            [_clamp_limit(limit)],
        )
        for request_id, trace_id, payload, last_at in _rows(cursor):
            origin = payload if isinstance(payload, dict) else {}
            summaries.append(
                RequestSummary(
                    request_id=str(request_id),
                    trace_id=str(trace_id or ""),
                    workspace_id=str(origin.get("workspace_id") or ""),
                    entry_point=str(origin.get("entry_point") or ""),
                    requester_member_id=str(origin.get("requester_member_id") or ""),
                    last_event_at=last_at,
                )
            )
        return summaries


def _tool_name(joined: object, payload: object) -> str | None:
    if isinstance(joined, str) and joined:
        return joined
    if isinstance(payload, dict):
        recorded = payload.get("tool_name")
        if isinstance(recorded, str) and recorded:
            return recorded
    return None


@fail_closed
def timeline(db: AuditDatabase, request_id: str, actor: AuthenticatedUser) -> list[TimelineEntry]:
    """Merged request/decision/delivery events, oldest first (Maya-only).

    Request and decision events carry the planned ``tool_name`` for their
    plan step (``plan_steps`` join), so the console shows which tool each
    decision was for.
    """
    _require_manager(actor)
    entries: list[TimelineEntry] = []
    with db.tx() as cursor:
        cursor.execute(
            "SELECT e.occurred_at, e.event_type, e.plan_id, e.step_index, e.jti, e.payload, "
            "ps.tool_name FROM request_events e LEFT JOIN plan_steps ps "
            "ON ps.plan_id = e.plan_id AND ps.step_index = e.step_index "
            "WHERE e.request_id = %s ORDER BY e.occurred_at, e.id LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        for occurred_at, event_type, plan_id, step, _jti, payload, tool in _rows(cursor):
            entries.append(
                TimelineEntry(
                    occurred_at=occurred_at,
                    kind="request",
                    event_type=str(event_type),
                    plan_id=plan_id,
                    step_index=step,
                    decision_source=None,
                    payload=redact_payload(payload if isinstance(payload, dict) else {}),
                    tool_name=_tool_name(tool, payload),
                )
            )
        cursor.execute(
            "SELECT d.occurred_at, d.event_type, d.plan_id, d.step_index, d.decision_source, "
            "d.payload, ps.tool_name FROM decision_events d LEFT JOIN plan_steps ps "
            "ON ps.plan_id = d.plan_id AND ps.step_index = d.step_index "
            "WHERE d.request_id = %s ORDER BY d.occurred_at, d.id LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        for occurred_at, event_type, plan_id, step, source, payload, tool in _rows(cursor):
            entries.append(
                TimelineEntry(
                    occurred_at=occurred_at,
                    kind="decision",
                    event_type=str(event_type),
                    plan_id=plan_id,
                    step_index=step,
                    decision_source=str(source) if source else None,
                    payload=redact_payload(payload if isinstance(payload, dict) else {}),
                    tool_name=_tool_name(tool, payload),
                )
            )
        cursor.execute(
            "SELECT occurred_at, delivery_channel, delivered_to_member_id, "
            "delivery_status, plan_id FROM delivery_events WHERE request_id = %s "
            "ORDER BY occurred_at, id LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        for occurred_at, channel, delivered_to, status, plan_id in _rows(cursor):
            entries.append(
                TimelineEntry(
                    occurred_at=occurred_at,
                    kind="delivery",
                    event_type=f"delivery_{status}",
                    plan_id=plan_id,
                    step_index=None,
                    decision_source=None,
                    payload=redact_payload(
                        {"channel": channel, "delivered_to_member_id": delivered_to}
                    ),
                )
            )
        entries.sort(key=lambda entry: entry.occurred_at)
        return entries


@fail_closed
def pending_holds(db: AuditDatabase, actor: AuthenticatedUser) -> list[PendingHold]:
    """Unresolved HOLDs with console correlation IDs (Maya-only)."""
    _require_manager(actor)
    holds: list[PendingHold] = []
    with db.tx() as cursor:
        cursor.execute(
            "SELECT ar.id, p.request_id, ar.plan_id, ar.step_index, ar.tool_name, "
            "ar.delegation_id, ar.requested_at, ar.requested_by_member_id "
            "FROM approval_requests ar JOIN plans p ON p.id = ar.plan_id "
            "WHERE ar.state = 'pending' AND ar.resolved_at IS NULL "
            "ORDER BY ar.requested_at LIMIT %s",
            [_MAX_LIMIT],
        )
        for row in _rows(cursor):
            holds.append(PendingHold(*row))
        return holds


@fail_closed
def unanswered(
    db: AuditDatabase, actor: AuthenticatedUser, only_open: bool = True
) -> list[UnansweredItem]:
    """Abstention queue with reasons rejoined from events (Maya-only).

    The reason comes from the companion ``answer_abstained`` event whose
    ``unanswered_question_id`` names the row; when ``add_unanswered`` did
    not append one (it would have duplicated ``record_answer``'s abstention),
    the request's latest abstention reason is used.
    """
    _require_manager(actor)
    items: list[UnansweredItem] = []
    with db.tx() as cursor:
        cursor.execute(
            "SELECT id, request_id, question, status, created_at FROM "
            "unanswered_questions WHERE (%s OR status = 'open') "
            "ORDER BY created_at LIMIT %s",
            [not only_open, _MAX_LIMIT],
        )
        rows = _rows(cursor)
        request_ids = sorted({str(row[1]) for row in rows})
        by_question: dict[str, str | None] = {}
        by_request: dict[str, str | None] = {}
        if request_ids:
            cursor.execute(
                "SELECT request_id, payload->>'unanswered_question_id', payload->>'reason' "
                "FROM request_events WHERE event_type = 'answer_abstained' "
                "AND request_id = ANY(%s) ORDER BY id",
                [request_ids],
            )
            for event_request, question_ref, reason in _rows(cursor):
                if question_ref is not None:
                    by_question[str(question_ref)] = reason
                else:
                    by_request[str(event_request)] = reason
        for question_id, request_id, question, status, created_at in rows:
            items.append(
                UnansweredItem(
                    question_id=question_id,
                    request_id=str(request_id),
                    # Stored redacted at write time; passed through unchanged.
                    question=question,
                    reason=by_question.get(str(question_id), by_request.get(str(request_id))),
                    status=str(status),
                    created_at=created_at,
                )
            )
        return items


__all__ = [
    "AnswerRecord",
    "OffPlanRecord",
    "PendingHold",
    "RequestDetail",
    "RequestStatusView",
    "RequestSummary",
    "StepRecord",
    "TimelineEntry",
    "UnansweredItem",
    "pending_holds",
    "recent_requests",
    "request_detail",
    "request_status",
    "timeline",
    "unanswered",
]
