"""In-memory stand-in for the ``services.audit.store`` write API (offline only).

The orchestrator calls ``audit.<function>(db, **kwargs)``; this recorder has
the same function names and keyword arguments, keeps every accepted call in
order, and exposes the event-type sequence the real store would write
(:attr:`RecordingAudit.timeline`). ``append_event`` runs the store's real
evidence checks (confirmation payload allowlists included), so an event the
real store would refuse is refused here too. Failure injection:
``fail={"record_step_decision": 1}`` raises ``AuditStoreError`` on that
function's first call (1-based); a refused call is not recorded.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from services.audit import store as real_store
from services.audit.errors import AuditStoreError
from services.contracts import (
    ApprovalState,
    AuditEvent,
    AuditEventType,
    RequestContext,
    ToolExecutionStatus,
)


class RecordingAudit:
    """Records audit writes; never touches a database."""

    def __init__(self, *, fail: Mapping[str, int] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeline: list[str] = []
        self._fail = dict(fail or {})
        self._counts: Counter[str] = Counter()
        self._approval_ids = itertools.count(1)
        self._question_ids = itertools.count(1)
        self.requests: dict[str, RequestContext] = {}
        self.plans: set[str] = set()

    # -- bookkeeping ---------------------------------------------------------

    def _gate(self, name: str) -> None:
        self._counts[name] += 1
        if self._fail.get(name) == self._counts[name]:
            raise AuditStoreError(f"fake {name} failure (offline)")

    def _accept(self, name: str, kwargs: dict[str, Any], event_type: str) -> None:
        self.calls.append((name, kwargs))
        self.timeline.append(event_type)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for call, kwargs in self.calls if call == name]

    def events(self, event_type: AuditEventType | None = None) -> list[AuditEvent]:
        found = [kwargs["event"] for kwargs in self.named("append_event")]
        return [event for event in found if event_type is None or event.event_type is event_type]

    def text(self) -> str:
        """Everything recorded, as text (for leak assertions)."""
        return repr(self.calls)

    # -- store surface ---------------------------------------------------------

    def create_request(self, db: object, ctx: RequestContext) -> str:
        self._gate("create_request")
        self.requests[ctx.request_id] = ctx
        self._accept(
            "create_request", {"ctx": ctx, "request_id": ctx.request_id}, "request_received"
        )
        return ctx.request_id

    def record_plan(self, db: object, **kwargs: Any) -> str:
        self._gate("record_plan")
        if kwargs["request_id"] not in self.requests:
            raise AuditStoreError("request has no request_received event")
        plan_id = kwargs.get("plan_id") or "plan-fake"
        if plan_id in self.plans:
            raise AuditStoreError("plan already recorded")
        self.plans.add(plan_id)
        self._accept("record_plan", kwargs, "plan_proposed")
        return plan_id

    def record_step_decision(self, db: object, **kwargs: Any) -> None:
        self._gate("record_step_decision")
        if kwargs["plan_id"] not in self.plans:
            raise AuditStoreError("plan step not found")
        outcome = kwargs["outcome"]
        approval = outcome.approval
        decision = outcome.decision
        if (
            approval is not None
            and approval.delegation_id
            and decision.delegation_id
            and approval.delegation_id != decision.delegation_id
        ):
            raise AuditStoreError("decision and approval name different delegation IDs")
        self._accept("record_step_decision", kwargs, "authorization_decided")

    def record_approval(self, db: object, **kwargs: Any) -> int:
        self._gate("record_approval")
        approval = kwargs["approval"]
        if approval.request_id != kwargs["request_id"]:
            raise AuditStoreError("approval belongs to a different request")
        if approval.plan_id not in self.plans:
            raise AuditStoreError("plan not found for request")
        pending = approval.state is ApprovalState.PENDING
        self._accept("record_approval", kwargs, "hold_created" if pending else "approval_resolved")
        return next(self._approval_ids)

    def record_execution_ref(self, db: object, **kwargs: Any) -> None:
        self._gate("record_execution_ref")
        status = ToolExecutionStatus(kwargs["status"])
        executed = status is ToolExecutionStatus.EXECUTED
        if executed and not kwargs.get("ledger_entry_id"):
            raise AuditStoreError("executed execution refs require a ledger_entry_id")
        if status is ToolExecutionStatus.NOT_REACHED and (
            kwargs.get("tool_server_reached") or kwargs.get("ledger_entry_id")
        ):
            raise AuditStoreError("not_reached refs cannot carry reach or a ledger entry")
        self._accept(
            "record_execution_ref", kwargs, "tool_executed" if executed else "tool_rejected"
        )

    def record_answer(self, db: object, **kwargs: Any) -> None:
        self._gate("record_answer")
        kind = "answer_abstained" if kwargs["abstained"] else "answer_generated"
        self._accept("record_answer", kwargs, kind)

    def record_delivery(self, db: object, **kwargs: Any) -> int:  # pragma: no cover
        raise AssertionError("the orchestrator records deliveries only through deliver()")

    def add_unanswered(self, db: object, **kwargs: Any) -> int:
        self._gate("add_unanswered")
        if kwargs["reason"] not in real_store._UNANSWERED_REASONS:
            raise AuditStoreError("unknown unanswered reason")
        refs: Sequence[str] = kwargs.get("safe_refs", ())
        real_store._validated_refs(refs)
        origin = self.requests.get(kwargs["request_id"])
        if origin is None or (origin.workspace_id, origin.requester.member_id) != (
            kwargs["workspace_id"],
            kwargs["requester_member_id"],
        ):
            raise AuditStoreError("workspace/requester do not match the trusted request record")
        self._accept("add_unanswered", kwargs, "unanswered_queued")
        return next(self._question_ids)

    def append_event(self, db: object, *, event: AuditEvent) -> None:
        self._gate("append_event")
        real_store._check_generic_event(event)  # the store's own evidence rules
        if event.plan_id is not None and event.plan_id not in self.plans:
            raise AuditStoreError("event references an unrecorded plan")
        self._accept(
            "append_event",
            {"event": event, "request_id": event.request_id},
            event.event_type.value,
        )


__all__ = ["RecordingAudit"]
