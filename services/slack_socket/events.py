"""Slack event validation, deduplication, and dispatch (P3-05, PRD §4.2, Arch §4).

Every incoming event flows through :func:`on_app_mention` / :func:`on_direct_message`,
which acknowledge FIRST and then validate in task order: team → (mentions)
channel allowlist → mapped user → dedupe → trusted ``RequestContext``. Bot and
unsupported events are ignored without any database write (loop safety).
Rejected events get a metadata-only ``slack_event_rejected`` audit row via
``services.audit.store.record_slack_event_rejected`` (Slack event/team/channel
IDs and a reason code, never message bodies or user IDs). Those rows are not
requests, so the manager console's ``recent_requests`` never lists them.

Identity comes only from ``resolve_slack_user`` over the Slack user ID.
Message text — including "I am Maya" claims — never influences identity,
tier, or routing (R-10, R-11, SC-03).

Deduplication is atomic: ``INSERT ... ON CONFLICT DO NOTHING RETURNING``
makes concurrent duplicates collapse to exactly one processing attempt; the
row moves ``received`` → ``processed`` only after the orchestrator returns.
Any database/dedupe failure fails closed (the event is not processed).

``store this``: only THIS bot's own leading mention is stripped (its user ID
comes from the envelope's ``authorizations`` or Bolt's ``context``; a leading
mention of anyone else is kept). The rest is forwarded verbatim, so the P3-02
planner's ``^store this\\b`` rule (``apps.api.planner.STORE_THIS``, reused
here) decides; ``store_this_content`` is the same remainder as metadata
(``""`` for a bare ``store this``, which the planner answers with a
clarification). No ``handle`` signature change.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from apps.api.core.db import AuditDatabase
from apps.api.identity import MemberRecord, resolve_slack_user
from apps.api.planner import STORE_THIS
from services.audit import store as audit_store
from services.audit.store import SlackRejectionReason
from services.contracts import AuthenticatedUser, EntryPoint, PersonaKey, RequestContext

logger = logging.getLogger(__name__)

MappingEvent = dict[str, Any]

_BOT_SUBTYPES = frozenset({"bot_message", "message_changed", "message_deleted"})

OrchestratorHandle = Callable[[RequestContext, str], Awaitable[str]]
"""P3-04 entry point (fake until merged): returns the request ID it accepted."""

UNMAPPED_USER_REFUSAL = (
    "Sorry, I can only help members of this team who are set up in ProxyLead. "
    "If you think you should have access, please ask your manager."
)
"""Fixed polite refusal for unmapped senders (P3-05 §9.2.4). Never echoes input."""


class RefusalSender(Protocol):
    """The part of ``SlackSender`` (P3-06 protocol shape) the adapter uses."""

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None: ...

    def send_dm(self, slack_user_id: str, text: str) -> None: ...


@dataclass(frozen=True)
class IncomingSlackEvent:
    """A validated event ready for the orchestrator."""

    request_id: str
    trace_id: str
    ctx: RequestContext
    message: str
    store_this_content: str | None
    event_id: str


@dataclass(frozen=True)
class SlackRejection:
    """Why an event was dropped. Carries IDs only, never message text."""

    event_id: str
    reason_code: str


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _is_bot_event(event: MappingEvent) -> bool:
    return event.get("subtype") in _BOT_SUBTYPES or "bot_id" in event or "bot_profile" in event


def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Drop one leading mention of THIS bot (``<@Ubot>`` / ``<@Ubot|name>``).

    A leading mention of any other user is kept, and nothing is stripped
    when the bot's user ID is unknown (the planner then sees the raw text).
    """
    if not bot_user_id:
        return text
    pattern = re.compile(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>\s*")
    return pattern.sub("", text, count=1)


def _authorized_bot_user_id(body: dict[str, Any]) -> str | None:
    """The bot's user ID from the envelope's ``authorizations[0].user_id``."""
    authorizations = body.get("authorizations")
    if isinstance(authorizations, list) and authorizations:
        first = authorizations[0]
        if isinstance(first, dict):
            user_id = first.get("user_id")
            if isinstance(user_id, str) and user_id:
                return user_id
    return None


def parse_store_this(message: str) -> str | None:
    """Note content of a ``store this`` command, else None (the planner's rule).

    Same test as P3-02 (``apps.api.planner.STORE_THIS``: ``^store this\\b``,
    case-insensitive, on the stripped text) and the same remainder, so the
    adapter's flag always agrees with the planner: a bare ``store this`` is a
    command with empty content (``""``), ``store thisfoo`` is not one.
    """
    text = message.strip()
    found = STORE_THIS.match(text)
    if found is None:
        return None
    return text[found.end() :].lstrip(" :\n\t")


def resolve_workspace_id(db: AuditDatabase) -> str | None:
    """The single workspace ID, or None unless exactly one workspace exists."""
    try:
        with db.tx() as cursor:
            cursor.execute("SELECT id FROM workspaces ORDER BY id")
            rows = cursor.fetchall()
    except Exception:
        return None
    if len(rows) != 1:
        return None
    return str(rows[0][0])


def _audit_rejection(
    db: AuditDatabase | None,
    *,
    event_id: str,
    reason_code: SlackRejectionReason,
    team_id: str | None,
    channel_id: str | None,
) -> None:
    """Metadata-only ``slack_event_rejected`` row via the audit store.

    Never raises: the event is already refused, so an audit failure is
    logged (exception class only) and never lets the event through.
    """
    logger.info("slack event rejected event_id=%s reason=%s", event_id, reason_code)
    if db is None:
        return
    try:
        audit_store.record_slack_event_rejected(
            db,
            slack_event_id=event_id,
            reason=reason_code,
            team_id=team_id,
            channel_id=channel_id,
        )
    except Exception as exc:
        logger.warning(
            "slack rejection audit failed event_id=%s reason=%s error=%s",
            event_id,
            reason_code,
            type(exc).__name__,
        )


def _claim_dedupe(
    db: AuditDatabase,
    *,
    event_id: str,
    workspace_id: str,
    channel_id: str | None,
) -> bool:
    """Atomically claim an event ID. True exactly once per ID (even concurrently)."""
    with db.tx() as cursor:
        cursor.execute(
            "INSERT INTO slack_events (slack_event_id, workspace_id, channel_id) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (slack_event_id) DO NOTHING RETURNING id",
            [event_id, workspace_id, channel_id],
        )
        return cursor.fetchone() is not None


def mark_processed(db: AuditDatabase, event_id: str) -> None:
    """Move a claimed event to `processed` after successful dispatch."""
    with db.tx() as cursor:
        cursor.execute(
            "UPDATE slack_events SET status = 'processed', processed_at = now() "
            "WHERE slack_event_id = %s",
            [event_id],
        )


def _build_context(
    *,
    event: MappingEvent,
    event_id: str,
    entry_point: Literal["slack_mention", "slack_dm"],
    requester: AuthenticatedUser,
    workspace_id: str,
    team_id: str,
) -> tuple[RequestContext, str]:
    request_id, trace_id = _new_id("req"), _new_id("trace")
    ctx = RequestContext(
        request_id=request_id,
        trace_id=trace_id,
        workspace_id=workspace_id,
        requester=requester,
        entry_point=EntryPoint(entry_point),
        received_at=datetime.now(UTC),
        slack_team_id=team_id,
        slack_channel_id=event.get("channel"),
        slack_thread_ts=event.get("thread_ts") or event.get("ts"),
        slack_event_id=event_id,
    )
    return ctx, request_id


def process_event(
    event: MappingEvent,
    *,
    event_id: str | None,
    team_id: str | None,
    entry_point: Literal["slack_mention", "slack_dm"],
    members: dict[PersonaKey, MemberRecord],
    expected_team_id: str | None,
    allowed_channels: set[str],
    workspace_id: str | None,
    db: AuditDatabase | None,
    bot_user_id: str | None = None,
) -> IncomingSlackEvent | SlackRejection:
    """Validate one event (ack already sent by the caller). Never raises.

    ``event_id`` is the OUTER envelope's ``event_id`` (Events API / Socket
    Mode ``event_callback`` body); the inner ``event`` dict never carries it.
    """
    if not isinstance(event, dict) or not event_id or not isinstance(event_id, str):
        return SlackRejection(event_id=_new_id("rej"), reason_code="invalid_envelope")
    channel_id = event.get("channel")
    logger.info("slack event received event_id=%s", event_id)

    if _is_bot_event(event):
        return SlackRejection(event_id=event_id, reason_code="bot_ignored")
    if not expected_team_id or team_id != expected_team_id:
        _audit_rejection(
            db,
            event_id=event_id,
            reason_code="wrong_team",
            team_id=team_id,
            channel_id=channel_id,
        )
        return SlackRejection(event_id=event_id, reason_code="wrong_team")
    if entry_point == "slack_mention" and channel_id not in allowed_channels:
        _audit_rejection(
            db,
            event_id=event_id,
            reason_code="channel_not_allowed",
            team_id=team_id,
            channel_id=channel_id,
        )
        return SlackRejection(event_id=event_id, reason_code="channel_not_allowed")

    requester = resolve_slack_user(members, team_id, event.get("user"), expected_team_id)
    if requester is None:
        _audit_rejection(
            db,
            event_id=event_id,
            reason_code="unmapped_user",
            team_id=team_id,
            channel_id=channel_id,
        )
        return SlackRejection(event_id=event_id, reason_code="unmapped_user")
    if workspace_id is None or db is None:
        _audit_rejection(
            db,
            event_id=event_id,
            reason_code="db_unavailable",
            team_id=team_id,
            channel_id=channel_id,
        )
        return SlackRejection(event_id=event_id, reason_code="db_unavailable")

    try:
        first = _claim_dedupe(
            db, event_id=event_id, workspace_id=workspace_id, channel_id=channel_id
        )
    except Exception:
        _audit_rejection(
            db,
            event_id=event_id,
            reason_code="db_unavailable",
            team_id=team_id,
            channel_id=channel_id,
        )
        return SlackRejection(event_id=event_id, reason_code="db_unavailable")
    if not first:
        _audit_rejection(
            db,
            event_id=event_id,
            reason_code="duplicate",
            team_id=team_id,
            channel_id=channel_id,
        )
        return SlackRejection(event_id=event_id, reason_code="duplicate")

    raw_text = event.get("text")
    if not isinstance(raw_text, str):
        return SlackRejection(event_id=event_id, reason_code="invalid_envelope")
    message = _strip_bot_mention(raw_text, bot_user_id)
    store_this_content = parse_store_this(message)
    try:
        ctx, request_id = _build_context(
            event=event,
            event_id=event_id,
            entry_point=entry_point,
            requester=requester,
            workspace_id=workspace_id,
            team_id=team_id or "",
        )
    except Exception:
        return SlackRejection(event_id=event_id, reason_code="invalid_envelope")
    trace_id = ctx.trace_id
    logger.info(
        "slack event accepted event_id=%s request_id=%s trace_id=%s",
        event_id,
        request_id,
        trace_id,
    )
    return IncomingSlackEvent(
        request_id=request_id,
        trace_id=trace_id,
        ctx=ctx,
        message=message,
        store_this_content=store_this_content,
        event_id=event_id,
    )


def _refuse_unmapped(
    sender: RefusalSender | None,
    event: MappingEvent,
    *,
    entry_point: Literal["slack_mention", "slack_dm"],
    event_id: str,
) -> None:
    """Send the fixed polite refusal to an unmapped sender. Never raises.

    Mentions get a thread reply (the channel already passed the allowlist,
    which is checked before the user); DMs get a DM to the sender's Slack ID.
    The text is a constant: nothing from the event is echoed back. A send
    failure is logged by exception class only.
    """
    if sender is None:
        return
    try:
        if entry_point == "slack_mention":
            channel_id = event.get("channel")
            if not isinstance(channel_id, str) or not channel_id:
                return
            thread_ts = event.get("thread_ts") or event.get("ts")
            sender.post_in_thread(
                channel_id, thread_ts if isinstance(thread_ts, str) else None, UNMAPPED_USER_REFUSAL
            )
        else:
            user_id = event.get("user")
            if not isinstance(user_id, str) or not user_id:
                return
            sender.send_dm(user_id, UNMAPPED_USER_REFUSAL)
    except Exception as exc:
        logger.warning(
            "slack unmapped-user refusal not sent event_id=%s error=%s",
            event_id,
            type(exc).__name__,
        )


def on_app_mention(
    body: dict[str, Any],
    ack: Callable[[], None],
    *,
    members: dict[PersonaKey, MemberRecord],
    expected_team_id: str | None,
    allowed_channels: set[str],
    workspace_id: str | None,
    db: AuditDatabase | None,
    sender: RefusalSender | None = None,
    bot_user_id: str | None = None,
) -> IncomingSlackEvent | SlackRejection:
    """Bolt `app_mention` listener: ack immediately, then validate.

    An unmapped sender in an allowed channel gets the fixed polite refusal
    as a thread reply (via ``sender``); the event is audited, never processed.
    """
    ack()
    event = body.get("event") if isinstance(body, dict) else None
    if not isinstance(event, dict):
        return SlackRejection(event_id=_new_id("rej"), reason_code="invalid_envelope")
    result = process_event(
        event,
        event_id=body.get("event_id"),
        team_id=body.get("team_id"),
        entry_point="slack_mention",
        members=members,
        expected_team_id=expected_team_id,
        allowed_channels=allowed_channels,
        workspace_id=workspace_id,
        db=db,
        bot_user_id=bot_user_id or _authorized_bot_user_id(body),
    )
    if isinstance(result, SlackRejection) and result.reason_code == "unmapped_user":
        _refuse_unmapped(sender, event, entry_point="slack_mention", event_id=result.event_id)
    return result


def on_direct_message(
    body: dict[str, Any],
    ack: Callable[[], None],
    *,
    members: dict[PersonaKey, MemberRecord],
    expected_team_id: str | None,
    workspace_id: str | None,
    db: AuditDatabase | None,
    sender: RefusalSender | None = None,
    bot_user_id: str | None = None,
) -> IncomingSlackEvent | SlackRejection:
    """Bolt `message` listener for DMs: ack immediately, then validate.

    An unmapped sender gets the fixed polite refusal by DM (via ``sender``);
    the event is audited, never processed.
    """
    ack()
    event = body.get("event") if isinstance(body, dict) else None
    if not isinstance(event, dict):
        return SlackRejection(event_id=_new_id("rej"), reason_code="invalid_envelope")
    result = process_event(
        event,
        event_id=body.get("event_id"),
        team_id=body.get("team_id") or event.get("team"),
        entry_point="slack_dm",
        members=members,
        expected_team_id=expected_team_id,
        allowed_channels=set(),
        workspace_id=workspace_id,
        db=db,
        bot_user_id=bot_user_id or _authorized_bot_user_id(body),
    )
    if isinstance(result, SlackRejection) and result.reason_code == "unmapped_user":
        _refuse_unmapped(sender, event, entry_point="slack_dm", event_id=result.event_id)
    return result


async def dispatch(
    handle: OrchestratorHandle,
    incoming: IncomingSlackEvent,
    db: AuditDatabase,
) -> str:
    """Forward to the orchestrator, then mark the event processed.

    Runs on the API event loop (scheduled from Bolt's worker thread by
    :mod:`services.slack_socket.app`); the blocking ``slack_events`` UPDATE
    runs in a worker thread so it never stalls the loop. A handle failure
    propagates (no false `processed` row); P3-04 owns retry.
    """
    request_id = await handle(incoming.ctx, incoming.message)
    await asyncio.to_thread(mark_processed, db, incoming.event_id)
    return request_id


__all__ = [
    "UNMAPPED_USER_REFUSAL",
    "IncomingSlackEvent",
    "OrchestratorHandle",
    "RefusalSender",
    "SlackRejection",
    "dispatch",
    "mark_processed",
    "on_app_mention",
    "on_direct_message",
    "parse_store_this",
    "process_event",
    "resolve_workspace_id",
]
