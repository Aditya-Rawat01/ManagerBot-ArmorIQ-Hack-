"""Backend audit read API (P3-01, PRD v2 §10.1, Arch §7.2/§10).

Authorization model (callers: P4-01 web request routes, P4-04 manager console):

- ``request_status`` is owner-only: the caller passes the *trusted*
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
through a new reader.
"""

from dataclasses import dataclass
from datetime import datetime

from apps.api.core.db import AuditCursor, AuditDatabase
from services.audit.errors import (
    AuditNotAuthorizedError,
    AuditNotFoundError,
    AuditStoreError,
)
from services.audit.redaction import redact_payload
from services.contracts import AppRole, AuthenticatedUser

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_TIMELINE_LIMIT = 500


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
        plan_ids = tuple(row[0] for row in cursor.fetchall())  # type: ignore[union-attr]
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


def recent_requests(
    db: AuditDatabase, actor: AuthenticatedUser, limit: int = _DEFAULT_LIMIT
) -> list[RequestSummary]:
    """Newest requests first (Maya-only, for P4-04)."""
    _require_manager(actor)
    summaries: list[RequestSummary] = []
    with db.tx() as cursor:
        cursor.execute(
            "SELECT request_id, MAX(trace_id) AS trace_id, MAX(occurred_at) AS last_at "
            "FROM request_events GROUP BY request_id ORDER BY last_at DESC LIMIT %s",
            [_clamp_limit(limit)],
        )
        for request_id, trace_id, last_at in cursor.fetchall():
            origin = _request_origin(cursor, str(request_id))
            payload = origin["payload"]
            if not isinstance(payload, dict):
                raise AuditStoreError(f"request {request_id!r} has a corrupt origin event")
            summaries.append(
                RequestSummary(
                    request_id=str(request_id),
                    trace_id=str(trace_id),
                    workspace_id=str(payload.get("workspace_id", "")),
                    entry_point=str(payload.get("entry_point", "")),
                    requester_member_id=str(payload.get("requester_member_id", "")),
                    last_event_at=last_at,
                )
            )
        return summaries


def timeline(db: AuditDatabase, request_id: str, actor: AuthenticatedUser) -> list[TimelineEntry]:
    """Merged request/decision/delivery events, oldest first (Maya-only)."""
    _require_manager(actor)
    entries: list[TimelineEntry] = []
    with db.tx() as cursor:
        cursor.execute(
            "SELECT occurred_at, event_type, plan_id, step_index, jti, payload "
            "FROM request_events WHERE request_id = %s ORDER BY occurred_at "
            "LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        for occurred_at, event_type, plan_id, step, _jti, payload in cursor.fetchall():
            entries.append(
                TimelineEntry(
                    occurred_at=occurred_at,
                    kind="request",
                    event_type=str(event_type),
                    plan_id=plan_id,
                    step_index=step,
                    decision_source=None,
                    payload=redact_payload(payload if isinstance(payload, dict) else {}),
                )
            )
        cursor.execute(
            "SELECT occurred_at, event_type, plan_id, step_index, decision_source, "
            "payload FROM decision_events WHERE request_id = %s ORDER BY "
            "occurred_at LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        for occurred_at, event_type, plan_id, step, source, payload in cursor.fetchall():
            entries.append(
                TimelineEntry(
                    occurred_at=occurred_at,
                    kind="decision",
                    event_type=str(event_type),
                    plan_id=plan_id,
                    step_index=step,
                    decision_source=str(source) if source else None,
                    payload=redact_payload(payload if isinstance(payload, dict) else {}),
                )
            )
        cursor.execute(
            "SELECT occurred_at, delivery_channel, delivered_to_member_id, "
            "delivery_status, plan_id FROM delivery_events WHERE request_id = %s "
            "ORDER BY occurred_at LIMIT %s",
            [request_id, _TIMELINE_LIMIT],
        )
        for occurred_at, channel, delivered_to, status, plan_id in cursor.fetchall():
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
        for row in cursor.fetchall():
            holds.append(PendingHold(*row))
        return holds


def unanswered(
    db: AuditDatabase, actor: AuthenticatedUser, only_open: bool = True
) -> list[UnansweredItem]:
    """Abstention queue with reasons rejoined from events (Maya-only)."""
    _require_manager(actor)
    items: list[UnansweredItem] = []
    with db.tx() as cursor:
        cursor.execute(
            "SELECT id, request_id, question, status, created_at FROM "
            "unanswered_questions WHERE (%s OR status = 'open') "
            "ORDER BY created_at LIMIT %s",
            [not only_open, _MAX_LIMIT],
        )
        rows = cursor.fetchall()
        request_ids = sorted({row[1] for row in rows})
        reasons: dict[str, str | None] = {}
        if request_ids:
            placeholders = ", ".join(["%s"] * len(request_ids))
            # Placeholders are generated; IDs travel as bound params.
            cursor.execute(
                "SELECT DISTINCT ON (request_id) request_id, "  # noqa: S608
                "payload->>'reason' FROM request_events "
                "WHERE event_type = 'answer_abstained' AND request_id IN "
                f"({placeholders}) ORDER BY request_id, id DESC",
                request_ids,
            )
            reasons = {row[0]: row[1] for row in cursor.fetchall()}
        for question_id, request_id, question, status, created_at in rows:
            items.append(
                UnansweredItem(
                    question_id=question_id,
                    request_id=request_id,
                    # Stored redacted at write time; passed through unchanged.
                    question=question,
                    reason=reasons.get(request_id),
                    status=status,
                    created_at=created_at,
                )
            )
        return items


__all__ = [
    "PendingHold",
    "RequestStatusView",
    "RequestSummary",
    "TimelineEntry",
    "UnansweredItem",
    "pending_holds",
    "recent_requests",
    "request_status",
    "timeline",
    "unanswered",
]
