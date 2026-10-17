"""Owner-scoped, in-process store for private web answers and offers (P4-01).

P3-06 records a web answer as a ``pending`` ``web_private`` delivery row but
keeps no answer text (``delivery_events`` / ``request_events`` are
metadata-only by design). This inbox holds what the requesting member's
session may read back through ``GET /api/v1/requests/{id}``:

- the delivered text, put by the orchestrator only AFTER P3-06 recorded the
  ``pending`` row (an unrecorded answer is never readable);
- the validated citations' titles (IDs and tiers are persisted);
- a pending D-14 :class:`ConfirmationOffer` (the web confirmation presenter).

Every item is bound to the request's member and workspace; :meth:`get`
returns nothing for anyone else, so another member cannot read it even with
the request ID. In-process only: one uvicorn worker (D-27), entries expire
after ``ttl_s`` (default 1 hour, above the 15-minute HOLD timeout) and at most
``max_items`` are kept (oldest dropped). A restart (deploy, crash, Render
idle spin-down) LOSES unread answers and offers: the status stays readable
from the database, but the text is gone and the user asks again. Nothing here
is logged, audited or persisted.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from services.contracts import DataTier, EntryPoint, RequestContext

DEFAULT_INBOX_TTL_S = 3600
DEFAULT_INBOX_MAX_ITEMS = 1000


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class InboxCitation:
    source_id: str
    tier: DataTier
    title: str


@dataclass(frozen=True)
class InboxOffer:
    escalation_id: str
    requested_tier: DataTier
    expires_at: datetime
    token: str = field(repr=False)


@dataclass(frozen=True)
class InboxItem:
    request_id: str
    member_id: str
    workspace_id: str
    stored_at: datetime
    answer_text: str | None = field(default=None, repr=False)
    citations: tuple[InboxCitation, ...] = field(default=(), repr=False)
    offer: InboxOffer | None = None


class WebInbox:
    """See the module docstring. Thread-safe (delivery runs in worker threads)."""

    def __init__(
        self,
        *,
        ttl_s: int = DEFAULT_INBOX_TTL_S,
        max_items: int = DEFAULT_INBOX_MAX_ITEMS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if ttl_s <= 0 or max_items <= 0:
            raise ValueError("ttl_s and max_items must be positive")
        self._ttl = timedelta(seconds=ttl_s)
        self._max = max_items
        self._clock = clock
        self._items: OrderedDict[str, InboxItem] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def put_answer(
        self, ctx: RequestContext, text: str, citations: Sequence[InboxCitation] = ()
    ) -> None:
        """Keep a web answer for its requester (never for a Slack request)."""
        if ctx.entry_point is not EntryPoint.WEB:
            raise ValueError("only web requests have a web inbox")
        self._upsert(ctx, answer_text=text, citations=tuple(citations))

    def put_offer(self, ctx: RequestContext, offer: object) -> None:
        """The web :data:`ConfirmationPresenter`: keep the offer for its requester."""
        if ctx.entry_point is not EntryPoint.WEB:
            raise ValueError("only web requests have a web inbox")
        if getattr(offer, "request_id", None) != ctx.request_id:
            raise ValueError("offer belongs to another request")
        self._upsert(
            ctx,
            offer=InboxOffer(
                escalation_id=offer.escalation_id,  # type: ignore[attr-defined]
                requested_tier=offer.requested_tier,  # type: ignore[attr-defined]
                expires_at=offer.expires_at,  # type: ignore[attr-defined]
                token=offer.token,  # type: ignore[attr-defined]
            ),
        )

    def get(self, request_id: str, member_id: str, workspace_id: str) -> InboxItem | None:
        """The item for the OWNER only; None for anyone else, expired or unknown."""
        with self._lock:
            self._prune(self._clock())
            item = self._items.get(request_id)
        if item is None or item.member_id != member_id or item.workspace_id != workspace_id:
            return None
        return item

    def _upsert(self, ctx: RequestContext, **changes: object) -> None:
        now = self._clock()
        with self._lock:
            self._prune(now)
            current = self._items.get(ctx.request_id)
            if current is not None and (
                current.member_id != ctx.requester.member_id
                or current.workspace_id != ctx.workspace_id
            ):
                raise ValueError("inbox item is bound to another member")
            base = current or InboxItem(
                request_id=ctx.request_id,
                member_id=ctx.requester.member_id,
                workspace_id=ctx.workspace_id,
                stored_at=now,
            )
            self._items[ctx.request_id] = replace(base, stored_at=now, **changes)  # type: ignore[arg-type]
            self._items.move_to_end(ctx.request_id)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def _prune(self, now: datetime) -> None:
        stale = [key for key, item in self._items.items() if item.stored_at + self._ttl <= now]
        for key in stale:
            del self._items[key]


__all__ = [
    "DEFAULT_INBOX_MAX_ITEMS",
    "DEFAULT_INBOX_TTL_S",
    "InboxCitation",
    "InboxItem",
    "InboxOffer",
    "WebInbox",
]
