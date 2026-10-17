"""Append-only request / decision / delivery audit events (P3-01).

Execution evidence lives in the tool server ledger; a plan or ALLOW alone is
never proof that a tool executed. The backend stores execution *references*
(IDs and statuses reported by the orchestrator) in its own event tables —
it cannot read the ledger, and no audit query joins protected tables.

Write API: :mod:`services.audit.store`. Read API: :mod:`services.audit.queries`.
Redaction: :mod:`services.audit.redaction`. Database protocols:
:mod:`services.audit.protocols` (this package never imports ``apps``); the
pool-backed implementation is :mod:`apps.api.core.db` (``DATABASE_URL_BACKEND``
only). Every call raises an :class:`AuditError` subclass on failure.
"""

from services.audit.errors import (
    AuditError,
    AuditNotAuthorizedError,
    AuditNotFoundError,
    AuditStoreError,
    AuditTransitionError,
)
from services.audit.protocols import AuditCursor, AuditDatabase
from services.audit.queries import (
    PendingHold,
    RequestStatusView,
    RequestSummary,
    TimelineEntry,
    UnansweredItem,
    pending_holds,
    recent_requests,
    request_status,
    timeline,
    unanswered,
)
from services.audit.redaction import REDACTED, redact_payload, redact_question
from services.audit.store import (
    DeliveryStatus,
    SlackRejectionReason,
    StepOutcomeLike,
    UnansweredReason,
    add_unanswered,
    append_event,
    create_request,
    record_answer,
    record_approval,
    record_delivery,
    record_execution_ref,
    record_plan,
    record_slack_event_rejected,
    record_step_decision,
)

__all__ = [
    "REDACTED",
    "AuditCursor",
    "AuditDatabase",
    "AuditError",
    "AuditNotAuthorizedError",
    "AuditNotFoundError",
    "AuditStoreError",
    "AuditTransitionError",
    "DeliveryStatus",
    "PendingHold",
    "RequestStatusView",
    "RequestSummary",
    "SlackRejectionReason",
    "StepOutcomeLike",
    "TimelineEntry",
    "UnansweredItem",
    "UnansweredReason",
    "add_unanswered",
    "append_event",
    "create_request",
    "pending_holds",
    "recent_requests",
    "record_answer",
    "record_approval",
    "record_delivery",
    "record_execution_ref",
    "record_plan",
    "record_slack_event_rejected",
    "record_step_decision",
    "redact_payload",
    "redact_question",
    "request_status",
    "timeline",
    "unanswered",
]
