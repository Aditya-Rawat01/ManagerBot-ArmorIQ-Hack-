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
metadata allowlist (requester persona, tool name, request ID, console link).
No approve/reject buttons, no approval actions, no protected content. The
link is ``ARMORIQ_CONSOLE_URL`` from settings (task §7), used verbatim only
if it is an ``https`` URL with a host and no credentials, query, or fragment;
otherwise the DM omits the link (neutral wording) — this module never
hardcodes a console URL. D-22 (exact console page) is OPEN and decided by E4:
until then the configured URL is linked as-is, never a fabricated deep link.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Literal, Protocol
from urllib.parse import urlsplit

from apps.api.core.config import Settings, get_settings
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
NoticeStatus = Literal["pending", "sent", "failed"]

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


class DeliveryAuditAfterSendError(DeliveryError):
    """A message reached Slack but the audit write AFTER the send failed.

    The message is already out: P3-04 must NOT re-send it (a retry would
    duplicate it) and must not report it undelivered. ``record`` describes
    what was sent (``error_class="audit_failed"``); handle it as an audit gap
    (ERROR outcome). Every send is preceded by a successful ``pending``
    audit write, so the attempt itself is always on record.
    """

    def __init__(self, record: DeliveryRecord) -> None:
        super().__init__("message sent but its audit record could not be written: do not re-send")
        self.record = record


def _record(
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    channel: DeliveryChannel,
    recipient_member_id: str,
    tier: DataTier,
    status: DeliveryStatus,
    error_class: str | None,
) -> int | None:
    """Persist one answer delivery row; None when the audit write fails."""
    try:
        return record_delivery(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=channel,
            recipient_member_id=recipient_member_id,
            tier_delivered=tier,
            status=status,
            error_class=error_class,
        )
    except AuditError:
        return None


def _deliver_answer(
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    channel: DeliveryChannel,
    recipient_member_id: str,
    tier: DataTier,
    send: Callable[[], None] | None,
    unavailable: str = "unavailable",
) -> DeliveryRecord:
    """Audit-first send of the answer to exactly one destination (R-64).

    1. ``send`` is None (no sender / mapping / thread): one ``failed`` row
       naming ``unavailable``; nothing is sent.
    2. A ``pending`` row is written BEFORE anything leaves; if that write
       fails nothing is sent (``audit_failed``).
    3. Send, then record ``delivered`` or ``failed`` (the send's error class).
    4. If the ``delivered`` write fails after a successful send, raise
       :class:`DeliveryAuditAfterSendError` (content is out: never re-send).
    """

    def write(status: DeliveryStatus, error_class: str | None = None) -> int | None:
        return _record(
            db,
            ctx=ctx,
            channel=channel,
            recipient_member_id=recipient_member_id,
            tier=tier,
            status=status,
            error_class=error_class,
        )

    def outcome(
        status: DeliveryStatus, error_class: str | None, ids: tuple[int, ...]
    ) -> DeliveryRecord:
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=channel,
            recipient_member_id=recipient_member_id,
            tier_delivered=tier,
            status=status,
            error_class=error_class,
            delivery_ids=ids,
        )

    if send is None:
        failed_id = write("failed", unavailable)
        if failed_id is None:
            return outcome("failed", "audit_failed", ())
        return outcome("failed", unavailable, (failed_id,))
    pending_id = write("pending")
    if pending_id is None:
        return outcome("failed", "audit_failed", ())  # nothing left ProxyLead
    try:
        send()
    except Exception as exc:
        error = type(exc).__name__
        failed_id = write("failed", error)
        if failed_id is None:
            return outcome("failed", "audit_failed", (pending_id,))
        return outcome("failed", error, (pending_id, failed_id))
    delivered_id = write("delivered")
    if delivered_id is None:
        raise DeliveryAuditAfterSendError(outcome("delivered", "audit_failed", (pending_id,)))
    return outcome("delivered", None, (pending_id, delivered_id))


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
    """Audit-first send of one non-answer message; returns (sent, error_class).

    Same order as the answer: a ``pending`` event before the send (no event,
    no send), then ``sent`` / ``failed``. ``send`` is None when nothing can
    be sent; ``unavailable`` names why (recorded as a failed attempt).
    ``(True, "audit_failed")`` means the message went out but its ``sent``
    event could not be written — callers raise
    :class:`DeliveryAuditAfterSendError`. Never raises.
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
    if not audit("pending", None):
        return (False, "audit_failed")  # nothing left ProxyLead
    try:
        send()
    except Exception as exc:
        error = type(exc).__name__
        return (False, error if audit("failed", error) else "audit_failed")
    if not audit("sent", None):
        return (True, "audit_failed")
    return (True, None)


def _post_thread_notice(
    sender: SlackSender | None,
    db: AuditDatabase,
    *,
    ctx: RequestContext,
    text: str,
    purpose: NoticePurpose,
) -> tuple[bool, str | None]:
    """Post a fixed generic notice in the originating thread.

    Recorded via :func:`_notice_event` (``delivery_attempted``), so a notice
    can never mark the request delivered. Returns ``_send_notice``'s
    (sent, error_class). Never raises.
    """
    send: Callable[[], None] | None = None
    unavailable = "no_sender"
    if sender is not None:
        if ctx.entry_point is EntryPoint.SLACK_MENTION and ctx.slack_channel_id:
            send = partial(sender.post_in_thread, ctx.slack_channel_id, ctx.slack_thread_ts, text)
        else:
            unavailable = "no_thread_context"
    return _send_notice(
        db,
        ctx=ctx,
        purpose=purpose,
        channel=DeliveryChannel.SLACK_THREAD,
        recipient_member_id=ctx.requester.member_id,
        send=send,
        unavailable=unavailable,
    )


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
    ``db`` is required: without persistence there is no delivery. Content
    leaves only after a ``pending`` audit row exists; a failed post-send
    audit write raises :class:`DeliveryAuditAfterSendError` (do not re-send).
    """
    from apps.api.responder import ResponderOutput

    if db is None:
        raise DeliveryError("no audit store: refusing undocumented delivery")
    if not isinstance(answer, ResponderOutput):
        raise DeliveryError("answer must be a ResponderOutput")
    if answer.request_id != ctx.request_id:
        raise DeliveryError("answer belongs to another request: refusing to deliver")
    if not isinstance(highest_tier, DataTier):
        # Fail closed: an unknown/missing tier must never reach a shared thread.
        raise DeliveryError("highest_tier must be a DataTier: refusing to route")

    recipient = ctx.requester.member_id

    if ctx.entry_point is EntryPoint.WEB:
        delivery_id = _record(
            db,
            ctx=ctx,
            channel=DeliveryChannel.WEB_PRIVATE,
            recipient_member_id=recipient,
            tier=highest_tier,
            status="delivered",
            error_class=None,
        )
        return DeliveryRecord(
            request_id=ctx.request_id,
            channel=DeliveryChannel.WEB_PRIVATE,
            recipient_member_id=recipient,
            tier_delivered=highest_tier,
            status="delivered" if delivery_id is not None else "failed",
            error_class=None if delivery_id is not None else "audit_failed",
            delivery_ids=(delivery_id,) if delivery_id is not None else (),
        )

    if ctx.entry_point is EntryPoint.SLACK_DM:
        return _deliver_dm(
            ctx,
            answer.answer_text,
            highest_tier,
            sender=sender,
            db=db,
            recipient=recipient,
        )
    if highest_tier is DataTier.TEAM:
        # The shared thread is allowed for team-tier data only (R-62).
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
    send: Callable[[], None] | None = None
    unavailable = "no_sender"
    if sender is not None:
        if ctx.requester.slack_user_id:
            send = partial(sender.send_dm, ctx.requester.slack_user_id, text)
        else:
            unavailable = "no_slack_mapping"
    return _deliver_answer(
        db,
        ctx=ctx,
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=recipient,
        tier=tier,
        send=send,
        unavailable=unavailable,
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
    send: Callable[[], None] | None = None
    unavailable = "no_sender"
    if sender is not None:
        if ctx.slack_channel_id:
            send = partial(sender.post_in_thread, ctx.slack_channel_id, ctx.slack_thread_ts, text)
        else:
            unavailable = "no_thread_context"
    return _deliver_answer(
        db,
        ctx=ctx,
        channel=DeliveryChannel.SLACK_THREAD,
        recipient_member_id=recipient,
        tier=tier,
        send=send,
        unavailable=unavailable,
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
    record = _deliver_dm(ctx, text, tier, sender=sender, db=db, recipient=recipient)
    purpose: NoticePurpose
    if record.status == "delivered":
        # Content is private; the thread learns only that it exists.
        notice, purpose = GENERIC_THREAD_NOTICE, "private_response_notice"
    else:
        # DM failed: never post the content; optionally post the generic
        # failure notice (no content), and report failure regardless. The
        # notice is not the answer, so it never marks the request delivered.
        notice, purpose = DM_FAILURE_NOTICE, "dm_failure_notice"
    posted, error = _post_thread_notice(sender, db, ctx=ctx, text=notice, purpose=purpose)
    record = replace(record, notice_posted=posted)
    if posted and error == "audit_failed":
        raise DeliveryAuditAfterSendError(record)
    return record


def _maya_slack_id(
    members: dict[PersonaKey, MemberRecord] | None,
) -> tuple[str, str]:
    """Maya's (member_id, slack_user_id). Missing mapping is a safe failure."""
    record = (members or {}).get(PersonaKey.MAYA)
    if record is None:
        return (_MAYA_MEMBER_ID, "")
    return (record.member_id, record.slack_user_id or "")


# Characters that would let a configured URL break out of a Slack link or
# message (``<url|label>``, mentions) or hide extra text.
_URL_FORBIDDEN = frozenset("<>|\"'`\\")


def _console_link(settings: Settings) -> str | None:
    """``ARMORIQ_CONSOLE_URL`` if it is a safe https link, else None.

    Required: ``https`` scheme, a host, no credentials, no query, no
    fragment, no whitespace/control or Slack-markup characters. Anything
    else (including unset/empty) yields None and the HOLD DM omits the link;
    there is deliberately no hardcoded fallback URL.
    """
    raw = settings.armoriq_console_url
    if not isinstance(raw, str) or not raw:
        return None
    if any(ch.isspace() or not ch.isprintable() or ch in _URL_FORBIDDEN for ch in raw):
        return None
    if "?" in raw or "#" in raw:
        return None
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        return None
    return raw


def _hold_text(ctx: RequestContext, approval: ApprovalStatus, link: str | None) -> str:
    """The HOLD DM: allowlisted metadata only (persona, tool, request ID, link)."""
    console = (
        f"Review pending approvals in the ArmorIQ console: {link}"
        if link
        else "Review pending approvals in the ArmorIQ console."
    )
    return (
        "ProxyLead needs your review.\n"
        f"Request {ctx.request_id} from {ctx.requester.persona.value} "
        f"needs a decision on {approval.tool.value}.\n"
        f"{console}"
    )


def notify_hold(
    ctx: RequestContext,
    approval: ApprovalStatus,
    *,
    sender: SlackSender | None = None,
    db: AuditDatabase | None = None,
    members: dict[PersonaKey, MemberRecord] | None = None,
    settings: Settings | None = None,
) -> DeliveryRecord:
    """DM Maya a minimal HOLD notice (SHARED_INTERFACES §11, R-44).

    Strict metadata allowlist: requester persona, tool name, request ID, and
    the console link from ``settings.armoriq_console_url`` (default
    :func:`get_settings`), only if it passes :func:`_console_link`; otherwise
    the DM says to use the ArmorIQ console without a link. No query text,
    excerpts, answers, titles, emails, tokens, approve buttons, or
    fabricated deep links (D-22 open, decided by E4).
    The recorded tier is ``team``: the DM carries metadata only, never
    content of the held tool's tier (the held tool is on the HOLD record).
    """
    if db is None:
        raise DeliveryError("no audit store: refusing undocumented delivery")
    maya_member_id, maya_slack_id = _maya_slack_id(members)
    text = _hold_text(ctx, approval, _console_link(settings or get_settings()))
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
    record = DeliveryRecord(
        request_id=ctx.request_id,
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id=maya_member_id,
        tier_delivered=DataTier.TEAM,
        status="delivered" if sent else "failed",
        error_class=error,
    )
    if sent and error == "audit_failed":
        raise DeliveryAuditAfterSendError(record)
    return record


__all__ = [
    "DM_FAILURE_NOTICE",
    "GENERIC_THREAD_NOTICE",
    "DeliveryAuditAfterSendError",
    "DeliveryError",
    "DeliveryRecord",
    "SlackSender",
    "deliver",
    "notify_hold",
]
