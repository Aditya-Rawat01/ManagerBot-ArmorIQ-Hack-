"""Tier-based delivery routing and HOLD notification (P3-06, PRD §7.12-7.14).

Disclosure control after authorization (Arch §5.4 layer 7). Destinations come
only from the trusted :class:`RequestContext` plus the orchestrator-supplied
``highest_tier`` (computed over ALL returned tool evidence, including uncited
data — never recomputed here from answer citations). Model output, tool
arguments, and message text never choose a recipient.

Routing (task §9.1):

- ``slack_mention`` + team → thread reply, content in the thread.
- ``slack_mention`` + confidential/restricted → content to the requester's own
  DM, plus the fixed generic notice in the thread (no titles, snippets,
  citations, counts, or tiers).
- ``slack_dm`` → requester's own DM, any tier.
- ``web`` → ``web_private`` audit record only; the answer text itself is NOT
  persisted here (``delivery_events`` holds metadata only). Retaining the
  private answer for ``GET /api/v1/requests/{id}`` is P3-04/P4-01's contract
  (pending ICR — flagged, not invented here).

Slack transport arrives as a :class:`SlackSender` (P3-05's agreed shape:
``post_in_thread`` / ``send_dm``, raising on failure). P3-05 is not merged,
so this module defines the structural Protocol and tests with fakes; the
real sender plugs in unchanged. A failed DM is recorded ``delivery_failed``
and never falls back to a thread post (R-64).

Audit (SHARED_INTERFACES §10): only the answer is written with
``record_delivery`` — a ``delivered`` row advances ``plans.status`` to
``delivered`` and makes ``request_status().delivered`` true. Non-answer
messages (the generic thread notice, the DM-failure notice, the HOLD DM) are
audited with ``append_event`` as ``delivery_attempted`` events whose payload
carries ``purpose`` / ``channel`` / ``recipient_member_id`` / ``status`` /
``error_class`` and never the message text, so they cannot mark a request
delivered.

HOLD notification (R-40, R-44): DM to Maya's mapped Slack ID only, strict
metadata allowlist (requester persona, tool name, request ID, console base
URL). No approve/reject buttons, no approval actions, no protected content.
D-22 (exact console page) is OPEN: the DM links only the FIXED console base
URL, never a fabricated deep link; final link acceptance stays pending.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Literal, Protocol

from apps.api.core.db import AuditDatabase
from apps.api.identity import MemberRecord
from services.audit.errors import AuditError
from services.audit.store import DeliveryStatus, append_event, record_delivery
from services.contracts import (
    ApprovalStatus,
    AuditEvent,
    AuditEventType,
    DataTier,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    RequestContext,
)

GENERIC_THREAD_NOTICE = "I sent you a private response."
DM_FAILURE_NOTICE = "I couldn't deliver that privately. Please send me a direct message."

# Non-answer messages. They are audited as ``delivery_attempted`` events
# (``append_event``), NEVER as ``record_delivery`` rows: a ``delivered``
# delivery row advances ``plans.status`` to ``delivered`` and makes
# ``request_status().delivered`` true, which only the answer itself may do.
NoticePurpose = Literal["private_response_notice", "dm_failure_notice", "hold_notice"]
NoticeStatus = Literal["sent", "failed"]

CONSOLE_BASE_URL_DEFAULT = "https://platform.armoriq.ai"  # FIXED base (D-22: page TBD)
_MAYA_MEMBER_ID = "m-maya"  # stable D-20/ADR-021 format, used only if unresolvable


class DeliveryError(Exception):
    """Delivery cannot proceed or be recorded. Callers fail closed."""


class SlackSender(Protocol):
    """P3-05 sender shape (SHARED_INTERFACES §11): sends or raises, no fallback."""

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None: ...
    def send_dm(self, slack_user_id: str, text: str) -> None: ...


@dataclass(frozen=True)
class DeliveryRecord:
    """One delivery outcome. ``delivery_ids`` are the answer's
    ``delivery_events`` rows (notices and HOLD DMs have none: they are
    ``delivery_attempted`` events); ``notice_posted`` tracks whether the
    generic thread notice went out alongside a DM."""

    request_id: str
    channel: DeliveryChannel
    recipient_member_id: str
    tier_delivered: DataTier
    status: DeliveryStatus
    error_class: str | None = None
    delivery_ids: tuple[int, ...] = ()
    notice_posted: bool = False


def _record(
    db: AuditDatabase,
    *,
    request_id: str,
    trace_id: str,
    channel: DeliveryChannel,
    recipient_member_id: str,
    tier: DataTier,
    status: DeliveryStatus,
    error_class: str | None,
) -> int | None:
    """Persist one send; None when the audit write itself fails (fail closed)."""
    try:
        return record_delivery(
            db,
            request_id=request_id,
            trace_id=trace_id,
            channel=channel,
            recipient_member_id=recipient_member_id,
            tier_delivered=tier,
            status=status,
            error_class=error_class,
        )
    except AuditError:
        return None


def _failed(
    ctx: RequestContext,
    channel: DeliveryChannel,
    recipient_member_id: str,
    tier: DataTier,
) -> DeliveryRecord:
    return DeliveryRecord(
        request_id=ctx.request_id,
        channel=channel,
        recipient_member_id=recipient_member_id,
        tier_delivered=tier,
        status="failed",
        error_class="audit_failed",
    )


def _send_dm(
    sender: SlackSender | None,
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    slack_user_id: str | None,
    recipient_member_id: str,
    text: str,
    tier: DataTier,
    error_no_mapping: str,
) -> tuple[DeliveryStatus, str | None, int | None]:
    """Send one DM; returns (status, error_class, delivery_id). Never raises."""
    if sender is None:
        delivery_id = _record(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id=recipient_member_id,
            tier=tier,
            status="failed",
            error_class="no_sender",
        )
        if delivery_id is None:
            return ("failed", "audit_failed", None)
        return ("failed", "no_sender", delivery_id)
    if not slack_user_id:
        delivery_id = _record(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id=recipient_member_id,
            tier=tier,
            status="failed",
            error_class=error_no_mapping,
        )
        if delivery_id is None:
            return ("failed", "audit_failed", None)
        return ("failed", error_no_mapping, delivery_id)
    try:
        sender.send_dm(slack_user_id, text)
    except Exception as exc:
        delivery_id = _record(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id=recipient_member_id,
            tier=tier,
            status="failed",
            error_class=type(exc).__name__,
        )
        if delivery_id is None:
            return ("failed", "audit_failed", None)
        return ("failed", type(exc).__name__, delivery_id)
    delivery_id = _record(
        db,
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=recipient_member_id,
        tier=tier,
        status="delivered",
        error_class=None,
    )
    if delivery_id is None:
        return ("failed", "audit_failed", None)
    return ("delivered", None, delivery_id)


def _notice_event(
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    purpose: NoticePurpose,
    channel: DeliveryChannel,
    recipient_member_id: str,
    status: NoticeStatus,
    error_class: str | None,
) -> bool:
    """Audit one non-answer message as a ``delivery_attempted`` event.

    Metadata only (purpose, channel, recipient, status, error class) — never
    the message text. Writes no ``delivery_events`` row and never touches
    ``plans``. The tier is always ``team``: a fixed notice or the HOLD DM
    carries no elevated content, whatever tier the answer or the held tool
    has. Returns False when the audit write itself fails.
    """
    try:
        append_event(
            db,
            event=AuditEvent(
                event_id=f"dlv-{uuid.uuid4().hex}",
                occurred_at=datetime.now(UTC),
                event_type=AuditEventType.DELIVERY_ATTEMPTED,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                payload={
                    "purpose": purpose,
                    "channel": channel.value,
                    "recipient_member_id": recipient_member_id,
                    "tier_delivered": DataTier.TEAM.value,
                    "status": status,
                    "error_class": error_class,
                },
            ),
        )
    except AuditError:
        return False
    return True


def _send_notice(
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    purpose: NoticePurpose,
    channel: DeliveryChannel,
    recipient_member_id: str,
    send: Callable[[], None] | None,
    unavailable: str = "unavailable",
) -> tuple[bool, str | None]:
    """Send one non-answer message and audit it; returns (sent, error_class).

    ``send`` is None when nothing can be sent; ``unavailable`` names why
    (recorded as a failed attempt). A failed send or audit write is recorded
    or reported, never swallowed silently. Never raises.
    """

    def audit(status: NoticeStatus, error_class: str | None) -> bool:
        return _notice_event(
            db,
            ctx=ctx,
            purpose=purpose,
            channel=channel,
            recipient_member_id=recipient_member_id,
            status=status,
            error_class=error_class,
        )

    if send is None:
        return (False, unavailable if audit("failed", unavailable) else "audit_failed")
    try:
        send()
    except Exception as exc:
        error = type(exc).__name__
        return (False, error if audit("failed", error) else "audit_failed")
    if not audit("sent", None):
        return (False, "audit_failed")
    return (True, None)


def _post_thread_notice(
    sender: SlackSender | None,
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    text: str,
    purpose: NoticePurpose,
) -> bool:
    """Post a fixed generic notice in the originating thread; returns posted.

    Recorded via :func:`_notice_event` (``delivery_attempted``), so a notice
    can never mark the request delivered. Never raises.
    """
    send: Callable[[], None] | None = None
    unavailable = "no_sender"
    if sender is not None:
        if ctx.entry_point is EntryPoint.SLACK_MENTION and ctx.slack_channel_id:
            send = partial(sender.post_in_thread, ctx.slack_channel_id, ctx.slack_thread_ts, text)
        else:
            unavailable = "no_thread_context"
    posted, _ = _send_notice(
        db,
        ctx=ctx,
        purpose=purpose,
        channel=DeliveryChannel.SLACK_THREAD,
        recipient_member_id=ctx.requester.member_id,
        send=send,
        unavailable=unavailable,
    )
    return posted


def deliver(
    ctx: RequestContext,
    answer: object,
    highest_tier: DataTier,
    *,
    sender: SlackSender | None = None,
    db: AuditDatabase | None = None,
    members: dict[PersonaKey, MemberRecord] | None = None,
) -> DeliveryRecord:
    """Route one answer by entry point and highest tier (SHARED_INTERFACES §11).

    ``answer`` is the responder's output (its ``request_id`` must match
    ``ctx``; anything else raises). ``members`` is accepted for interface
    symmetry but unused here — destinations resolve from ``ctx`` alone.
    ``db`` is required: without persistence there is no delivery.
    """
    from apps.api.responder import ResponderOutput

    if db is None:
        raise DeliveryError("no audit store: refusing undocumented delivery")
    if not isinstance(answer, ResponderOutput):
        raise DeliveryError("answer must be a ResponderOutput")
    if answer.request_id != ctx.request_id:
        raise DeliveryError("answer belongs to another request: refusing to deliver")

    elevated = highest_tier in (DataTier.CONFIDENTIAL, DataTier.RESTRICTED)
    recipient = ctx.requester.member_id

    if ctx.entry_point is EntryPoint.WEB:
        delivery_id = _record(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.WEB_PRIVATE,
            recipient_member_id=recipient,
            tier=highest_tier,
            status="delivered",
            error_class=None,
        )
        if delivery_id is None:
            return _failed(ctx, DeliveryChannel.WEB_PRIVATE, recipient, highest_tier)
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=DeliveryChannel.WEB_PRIVATE,
            recipient_member_id=recipient,
            tier_delivered=highest_tier,
            status="delivered",
            delivery_ids=(delivery_id,),
        )

    if ctx.entry_point is EntryPoint.SLACK_DM or not elevated:
        if ctx.entry_point is EntryPoint.SLACK_DM:
            return _deliver_dm(
                ctx,
                answer.answer_text,
                highest_tier,
                sender=sender,
                db=db,
                recipient=recipient,
            )
        return _deliver_thread(
            ctx,
            answer.answer_text,
            highest_tier,
            sender=sender,
            db=db,
            recipient=recipient,
        )
    return _deliver_dm_with_notice(
        ctx, answer.answer_text, highest_tier, sender=sender, db=db, recipient=recipient
    )


def _deliver_dm(
    ctx: RequestContext,
    text: str,
    tier: DataTier,
    *,
    sender: SlackSender | None,
    db: AuditDatabase,
    recipient: str,
) -> DeliveryRecord:
    status, error, delivery_id = _send_dm(
        sender,
        db,
        ctx=ctx,
        slack_user_id=ctx.requester.slack_user_id,
        recipient_member_id=recipient,
        text=text,
        tier=tier,
        error_no_mapping="no_slack_mapping",
    )
    return DeliveryRecord(
        request_id=ctx.request_id,
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=recipient,
        tier_delivered=tier,
        status=status,
        error_class=error,
        delivery_ids=(delivery_id,) if delivery_id else (),
    )


def _deliver_thread(
    ctx: RequestContext,
    text: str,
    tier: DataTier,
    *,
    sender: SlackSender | None,
    db: AuditDatabase,
    recipient: str,
) -> DeliveryRecord:
    if sender is None or not ctx.slack_channel_id:
        error = "no_sender" if sender is None else "no_thread_context"
        delivery_id = _record(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_THREAD,
            recipient_member_id=recipient,
            tier=tier,
            status="failed",
            error_class=error,
        )
        if delivery_id is None:
            return _failed(ctx, DeliveryChannel.SLACK_THREAD, recipient, tier)
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=DeliveryChannel.SLACK_THREAD,
            recipient_member_id=recipient,
            tier_delivered=tier,
            status="failed",
            error_class=error,
            delivery_ids=(delivery_id,),
        )
    try:
        sender.post_in_thread(ctx.slack_channel_id, ctx.slack_thread_ts, text)
    except Exception as exc:
        delivery_id = _record(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_THREAD,
            recipient_member_id=recipient,
            tier=tier,
            status="failed",
            error_class=type(exc).__name__,
        )
        if delivery_id is None:
            return _failed(ctx, DeliveryChannel.SLACK_THREAD, recipient, tier)
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=DeliveryChannel.SLACK_THREAD,
            recipient_member_id=recipient,
            tier_delivered=tier,
            status="failed",
            error_class=type(exc).__name__,
            delivery_ids=(delivery_id,),
        )
    delivery_id = _record(
        db,
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        channel=DeliveryChannel.SLACK_THREAD,
        recipient_member_id=recipient,
        tier=tier,
        status="delivered",
        error_class=None,
    )
    if delivery_id is None:
        return _failed(ctx, DeliveryChannel.SLACK_THREAD, recipient, tier)
    return DeliveryRecord(
        request_id=ctx.request_id,
        channel=DeliveryChannel.SLACK_THREAD,
        recipient_member_id=recipient,
        tier_delivered=tier,
        status="delivered",
        delivery_ids=(delivery_id,),
    )


def _deliver_dm_with_notice(
    ctx: RequestContext,
    text: str,
    tier: DataTier,
    *,
    sender: SlackSender | None,
    db: AuditDatabase,
    recipient: str,
) -> DeliveryRecord:
    status, error, dm_id = _send_dm(
        sender,
        db,
        ctx=ctx,
        slack_user_id=ctx.requester.slack_user_id,
        recipient_member_id=recipient,
        text=text,
        tier=tier,
        error_no_mapping="no_slack_mapping",
    )
    ids = (dm_id,) if dm_id else ()
    if status == "delivered":
        # Content is private; the thread learns only that it exists.
        posted = _post_thread_notice(
            sender, db, ctx=ctx, text=GENERIC_THREAD_NOTICE, purpose="private_response_notice"
        )
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id=recipient,
            tier_delivered=tier,
            status="delivered",
            delivery_ids=ids,
            notice_posted=posted,
        )
    # DM failed: never post the content; optionally post the generic failure
    # notice (no content), and report failure regardless. The notice is not
    # the answer, so it never marks the request delivered.
    posted = _post_thread_notice(
        sender, db, ctx=ctx, text=DM_FAILURE_NOTICE, purpose="dm_failure_notice"
    )
    return DeliveryRecord(
        request_id=ctx.request_id,
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=recipient,
        tier_delivered=tier,
        status="failed",
        error_class=error,
        delivery_ids=ids,
        notice_posted=posted,
    )


def _maya_slack_id(
    members: dict[PersonaKey, MemberRecord] | None,
) -> tuple[str, str]:
    """Maya's (member_id, slack_user_id). Missing mapping is a safe failure."""
    record = (members or {}).get(PersonaKey.MAYA)
    if record is None:
        return (_MAYA_MEMBER_ID, "")
    return (record.member_id, record.slack_user_id or "")


def notify_hold(
    ctx: RequestContext,
    approval: ApprovalStatus,
    *,
    sender: SlackSender | None = None,
    db: AuditDatabase | None = None,
    members: dict[PersonaKey, MemberRecord] | None = None,
    console_base_url: str = CONSOLE_BASE_URL_DEFAULT,
) -> DeliveryRecord:
    """DM Maya a minimal HOLD notice (SHARED_INTERFACES §11, R-44).

    Strict metadata allowlist: requester persona, tool name, request ID, and
    the FIXED console base URL. No query text, excerpts, answers, titles,
    emails, tokens, approve buttons, or deep links (D-22 open — the exact
    console page stays pending, so only the base URL is linked).
    The recorded tier is ``team``: the DM carries metadata only, never
    content of the held tool's tier (the held tool is on the HOLD record).
    """
    if db is None:
        raise DeliveryError("no audit store: refusing undocumented delivery")
    maya_member_id, maya_slack_id = _maya_slack_id(members)
    text = (
        "ProxyLead needs your review.\n"
        f"Request {ctx.request_id} from {ctx.requester.persona.value} "
        f"needs a decision on {approval.tool.value}.\n"
        "Review pending approvals in the ArmorIQ console: "
        f"{console_base_url}"
    )
    send: Callable[[], None] | None = None
    unavailable = "no_sender"
    if sender is not None:
        if maya_slack_id:
            send = partial(sender.send_dm, maya_slack_id, text)
        else:
            unavailable = "no_maya_mapping"
    # The HOLD DM is not the requester's answer: it is audited as a
    # ``delivery_attempted`` event, never a delivery row, so the request is
    # not marked delivered while the HOLD is still pending.
    sent, error = _send_notice(
        db,
        ctx=ctx,
        purpose="hold_notice",
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=maya_member_id,
        send=send,
        unavailable=unavailable,
    )
    return DeliveryRecord(
        request_id=ctx.request_id,
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=maya_member_id,
        tier_delivered=DataTier.TEAM,
        status="delivered" if sent else "failed",
        error_class=error,
    )


__all__ = [
    "DM_FAILURE_NOTICE",
    "GENERIC_THREAD_NOTICE",
    "DeliveryError",
    "DeliveryRecord",
    "SlackSender",
    "deliver",
    "notify_hold",
]
