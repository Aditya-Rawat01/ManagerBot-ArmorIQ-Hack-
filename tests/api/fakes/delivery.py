"""``deliver`` / ``notify_hold`` doubles bound to the real P3-06 signatures.

Every call is bound against the real function's signature first, so a drift
in how the orchestrator calls P3-06 fails here. Routing mirrors P3-06
(``apps/api/delivery.py``): web -> ``web_private`` (``pending``), DM ->
``slack_dm``, a mention at team tier -> ``slack_thread``, a mention above
team tier -> ``slack_dm``. Offline only.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from apps.api.delivery import DeliveryError, DeliveryRecord
from apps.api.delivery import deliver as real_deliver
from apps.api.delivery import notify_hold as real_notify_hold
from apps.api.responder import ResponderOutput
from services.contracts import (
    ApprovalState,
    ApprovalStatus,
    DataTier,
    DeliveryChannel,
    EntryPoint,
    RequestContext,
)

from .audit import RecordingAudit


@dataclass(frozen=True)
class Delivered:
    ctx: RequestContext
    answer: ResponderOutput
    tier: DataTier
    channel: DeliveryChannel
    plan_id: str | None
    sender: Any


@dataclass(frozen=True)
class HoldNotice:
    ctx: RequestContext
    approval: ApprovalStatus


class FakeDelivery:
    """Records deliveries and HOLD notices; optional audit timeline entries."""

    def __init__(
        self,
        audit: RecordingAudit | None = None,
        *,
        fail_status: bool = False,
        raise_on_deliver: BaseException | None = None,
        raise_on_notify: BaseException | None = None,
    ) -> None:
        self.audit = audit
        self.fail_status = fail_status
        self.raise_on_deliver = raise_on_deliver
        self.raise_on_notify = raise_on_notify
        self.deliveries: list[Delivered] = []
        self.holds: list[HoldNotice] = []

    def texts(self) -> list[str]:
        return [item.answer.answer_text for item in self.deliveries]

    def deliver(
        self,
        ctx: RequestContext,
        answer: ResponderOutput,
        highest_tier: DataTier,
        *,
        sender: Any = None,
        db: Any = None,
        plan_id: str | None = None,
    ) -> DeliveryRecord:
        inspect.signature(real_deliver).bind(
            ctx, answer, highest_tier, sender=sender, db=db, plan_id=plan_id
        )
        if self.raise_on_deliver is not None:
            raise self.raise_on_deliver
        if answer.request_id != ctx.request_id:
            raise DeliveryError("answer belongs to another request: refusing to deliver")
        if answer.abstain_reason == "error":
            raise DeliveryError("an error outcome is not deliverable")
        if not answer.answer_text.strip():
            raise DeliveryError("empty answer text: refusing to deliver")
        if not isinstance(highest_tier, DataTier):
            raise DeliveryError("highest_tier must be a DataTier: refusing to route")
        if ctx.entry_point is EntryPoint.WEB:
            channel, status = DeliveryChannel.WEB_PRIVATE, "pending"
        elif ctx.entry_point is EntryPoint.SLACK_DM or highest_tier is not DataTier.TEAM:
            channel, status = DeliveryChannel.SLACK_DM, "delivered"
        else:
            channel, status = DeliveryChannel.SLACK_THREAD, "delivered"
        if self.fail_status and status == "delivered":
            status = "failed"
        self.deliveries.append(
            Delivered(
                ctx=ctx,
                answer=answer,
                tier=highest_tier,
                channel=channel,
                plan_id=plan_id,
                sender=sender,
            )
        )
        if self.audit is not None:
            self.audit.timeline.append(
                {"pending": "delivery_attempted", "delivered": "delivery_succeeded"}.get(
                    status, "delivery_failed"
                )
            )
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=channel,
            recipient_member_id=ctx.requester.member_id,
            tier_delivered=highest_tier,
            status=status,  # type: ignore[arg-type]
            error_class="SlackError" if status == "failed" else None,
        )

    def notify_hold(
        self,
        ctx: RequestContext,
        approval: ApprovalStatus,
        *,
        sender: Any = None,
        db: Any = None,
        members: Any,
        settings: Any = None,
    ) -> DeliveryRecord:
        inspect.signature(real_notify_hold).bind(
            ctx, approval, sender=sender, db=db, members=members, settings=settings
        )
        if self.raise_on_notify is not None:
            raise self.raise_on_notify
        if approval.state is not ApprovalState.PENDING:
            raise DeliveryError("approval is not pending: refusing to notify")
        self.holds.append(HoldNotice(ctx=ctx, approval=approval))
        if self.audit is not None:
            self.audit.timeline.append("hold_notified")
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id="m-maya",
            tier_delivered=DataTier.TEAM,
            status="delivered",
        )


__all__ = ["Delivered", "FakeDelivery", "HoldNotice"]
