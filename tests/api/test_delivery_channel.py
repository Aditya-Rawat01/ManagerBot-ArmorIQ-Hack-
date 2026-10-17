"""Delivery routing by entry point and tier (P3-06 §11).

Offline: `__offline_fake_slack` sender and fake audit store (no Slack, no
Neon). Leakage tests assert on the actual payloads handed to the sender —
absence of protected text, not just return values. The `test_live_*` tests
at the end need a LOCAL migrated and seeded database: `DATABASE_URL_BACKEND`
and `PROXYLEAD_DB_WRITE_TESTS=1` (the DSN is captured at import because the
API conftest scrubs settings variables, and is never printed).
"""

import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from apps.api.delivery import (
    DM_FAILURE_NOTICE,
    GENERIC_THREAD_NOTICE,
    DeliveryError,
    DeliveryRecord,
    deliver,
)
from apps.api.responder import ResponderOutput
from services.audit.errors import AuditStoreError
from services.audit.queries import request_status
from services.contracts import (
    AppRole,
    AuthenticatedUser,
    DataTier,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    RequestContext,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
_SECRET_TITLE = "Atlas SECRET Budget Cut Plan"


class FakeSender:
    """Offline fake Slack transport (`__offline_fake_slack`)."""

    def __init__(self, fail_dm: bool = False, fail_thread: bool = False) -> None:
        self.thread_posts: list[tuple[str, str | None, str]] = []
        self.dms: list[tuple[str, str]] = []
        self.fail_dm = fail_dm
        self.fail_thread = fail_thread

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None:
        if self.fail_thread:
            raise RuntimeError("fake thread post failed")
        self.thread_posts.append((channel_id, thread_ts, text))

    def send_dm(self, slack_user_id: str, text: str) -> None:
        if self.fail_dm:
            raise RuntimeError("fake DM failed")
        self.dms.append((slack_user_id, text))


class _SimCursor:
    """One transaction of :class:`AuditSim`; changes apply only on commit."""

    def __init__(self, sim: "AuditSim", fail: bool) -> None:
        self.sim = sim
        self.fail = fail
        self.rowcount = 1
        self._one: list[object] = []
        self._all: list[list[object]] = []
        self._rows: list[dict[str, object]] = []
        self._events: list[dict[str, object]] = []
        self._plans_delivered = False

    def execute(self, query: str, params: object = None) -> None:
        values = list(params or [])  # type: ignore[call-overload]
        self.sim.statements.append((query, values))
        if self.fail:
            raise AuditStoreError("fake audit write failed")
        if query.startswith("INSERT INTO delivery_events"):
            row_id = len(self.sim.delivery_rows) + len(self._rows) + 1
            self._rows.append(
                {
                    "id": row_id,
                    "request_id": values[0],
                    "plan_id": values[2],
                    "channel": values[3],
                    "recipient": values[4],
                    "status": values[5],
                }
            )
            self._one.append((row_id,))
        elif query.startswith("INSERT INTO request_events"):
            # record_delivery: (request, trace, plan, type, payload, at);
            # append_event: (request, trace, plan, step, jti, type, payload, at).
            kind, payload = (values[3], values[4]) if len(values) == 6 else (values[5], values[6])
            self._events.append(
                {"request_id": values[0], "plan_id": values[2], "type": kind, "payload": payload}
            )
        elif query.startswith("UPDATE plans SET status = 'delivered'"):
            self._plans_delivered = True
        elif "event_type = 'request_received'" in query:
            origin = {"requester_member_id": self.sim.requester, "workspace_id": "ws-1"}
            self._one.append(("trace-1", origin))
        elif query.startswith("SELECT id FROM plans"):
            self._all.append([("plan-1",)])
        elif "FROM approval_requests" in query:
            self._one.append((0,))
        elif "FROM delivery_events" in query and "'delivered'" in query:
            count = sum(1 for row in self.sim.delivery_rows if row["status"] == "delivered")
            self._one.append((count,))
        elif query.startswith("SELECT MAX(occurred_at)"):
            self._one.append((NOW,))
        else:
            raise AssertionError(f"unexpected SQL in delivery test: {query}")

    def fetchone(self) -> object:
        return self._one.pop(0) if self._one else None

    def fetchall(self) -> list[object]:
        return self._all.pop(0) if self._all else []

    def commit(self) -> None:
        self.sim.delivery_rows.extend(self._rows)
        self.sim.events.extend(self._events)
        if self._plans_delivered:
            self.sim.plan_status = "delivered"


class AuditSim:
    """Stateful stand-in for the backend audit tables (SQL captured, no database).

    Applies what ``record_delivery`` / ``append_event`` write and answers
    the ``request_status`` reads from that state, so tests can assert on the
    real read API. A transaction commits only if it does not raise.
    ``fail_tx`` lists transaction indexes (0-based) that fail; ``fail_all``
    fails every transaction.
    """

    def __init__(
        self,
        *,
        requester: str = "m-sam",
        fail_tx: tuple[int, ...] = (),
        fail_all: bool = False,
    ) -> None:
        self.requester = requester
        self.fail_tx = set(fail_tx)
        self.fail_all = fail_all
        self.statements: list[tuple[str, list[object]]] = []
        self.delivery_rows: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.plan_status = "answered"
        self._count = 0

    @contextmanager
    def tx(self):  # type: ignore[no-untyped-def]
        index, self._count = self._count, self._count + 1
        cursor = _SimCursor(self, fail=self.fail_all or index in self.fail_tx)
        yield cursor
        cursor.commit()

    def plans_delivered_updates(self) -> list[str]:
        return [sql for sql, _ in self.statements if "UPDATE plans SET status = 'delivered'" in sql]

    def attempts(
        self, purpose: str | None = None, status: str | None = None
    ) -> list[dict[str, object]]:
        """Committed non-answer ``delivery_attempted`` payloads (they carry a
        ``purpose``; an answer's ``pending`` row companion event does not)."""
        payloads = [
            event["payload"] for event in self.events if event["type"] == "delivery_attempted"
        ]
        return [
            p  # type: ignore[misc]
            for p in payloads
            if isinstance(p, dict)
            and "purpose" in p
            and (purpose is None or p["purpose"] == purpose)
            and (status is None or p.get("status") == status)
        ]

    def row_statuses(self) -> list[tuple[object, object]]:
        """Committed answer ``delivery_events`` rows as (channel, status)."""
        return [(row["channel"], row["status"]) for row in self.delivery_rows]

    def params_text(self) -> str:
        return str([params for _, params in self.statements])


def _requester(slack_id: str | None = "U-sam") -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id="m-sam",
        persona=PersonaKey.SAM,
        display_name="Sam",
        app_role=AppRole.SENIOR,
        masked_email="s***@example.com",
        slack_user_id=slack_id,
    )


def _ctx(
    entry: EntryPoint = EntryPoint.SLACK_MENTION,
    slack_id: str | None = "U-sam",
    thread_ts: str | None = "1700000000.0001",
) -> RequestContext:
    refs: dict[str, object] = (
        {}
        if entry is EntryPoint.WEB
        else {
            "slack_team_id": "T1",
            "slack_channel_id": "C1",
            "slack_event_id": "E1",
            "slack_thread_ts": thread_ts,
        }
    )
    return RequestContext(
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester=_requester(slack_id),
        entry_point=entry,
        received_at=NOW,
        **refs,  # type: ignore[arg-type]
    )


def _answer(text: str = "team update", citations: list[str] | None = None) -> ResponderOutput:
    return ResponderOutput(
        request_id="req-1",
        answer_text=text,
        citations=citations or [],
    )


# --- routing table ----------------------------------------------------------


def test__offline_fake_slack_mention_team_goes_to_thread() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(_ctx(), _answer("team update"), DataTier.TEAM, sender=sender, db=db)
    assert isinstance(record, DeliveryRecord)
    assert record.channel is DeliveryChannel.SLACK_THREAD
    assert record.status == "delivered"
    assert sender.thread_posts == [("C1", "1700000000.0001", "team update")]
    assert sender.dms == []


@pytest.mark.parametrize("tier", [DataTier.CONFIDENTIAL, DataTier.RESTRICTED])
def test__offline_fake_slack_mention_elevated_dm_plus_generic_notice(
    tier: DataTier,
) -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(),
        _answer(f"secret {tier.value} content"),
        tier,
        sender=sender,
        db=db,
    )
    assert record.channel is DeliveryChannel.SLACK_DM
    assert record.status == "delivered"
    assert record.notice_posted is True
    assert sender.dms == [("U-sam", f"secret {tier.value} content")]
    assert sender.thread_posts == [("C1", "1700000000.0001", GENERIC_THREAD_NOTICE)]


@pytest.mark.parametrize("tier", list(DataTier))
def test__offline_fake_slack_dm_always_private(tier: DataTier) -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(EntryPoint.SLACK_DM),
        _answer("any content"),
        tier,
        sender=sender,
        db=db,
    )
    assert record.channel is DeliveryChannel.SLACK_DM
    assert record.status == "delivered"
    assert sender.thread_posts == []
    assert sender.dms == [("U-sam", "any content")]


@pytest.mark.parametrize("tier", list(DataTier))
def test__offline_fake_slack_web_private_only(tier: DataTier) -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(EntryPoint.WEB),
        _answer("private content"),
        tier,
        sender=sender,  # even with a sender available, web never touches Slack
        db=db,
    )
    assert record.channel is DeliveryChannel.WEB_PRIVATE
    assert sender.dms == [] and sender.thread_posts == []


@pytest.mark.parametrize("tier", list(DataTier))
def test__offline_fake_slack_web_answer_recorded_pending_not_delivered(tier: DataTier) -> None:
    # I7: the web answer text is stored nowhere yet (ICR pending), so it must
    # not be reported delivered.
    db = AuditSim()
    record = deliver(_ctx(EntryPoint.WEB), _answer("private content"), tier, sender=None, db=db)
    assert record.status == "pending"
    assert record.error_class is None
    assert db.row_statuses() == [("web_private", "pending")]
    assert db.plans_delivered_updates() == []
    assert request_status(db, "req-1", "m-sam").delivered is False
    assert "private content" not in db.params_text()  # metadata only


def test__offline_fake_slack_web_audit_down_reports_failed() -> None:
    record = deliver(_ctx(EntryPoint.WEB), _answer("x"), DataTier.TEAM, db=AuditSim(fail_all=True))
    assert record.status == "failed"
    assert record.error_class == "audit_failed"


# --- leakage proofs ---------------------------------------------------------


def test__offline_fake_slack_notice_is_fixed_constant() -> None:
    assert GENERIC_THREAD_NOTICE == "I sent you a private response."


def test__offline_fake_slack_thread_notice_carries_no_content() -> None:
    sender, db = FakeSender(), AuditSim()
    answer_text = f"{_SECRET_TITLE} [src-9] allege 3 layoffs (confidence 0.9)"
    deliver(
        _ctx(),
        _answer(answer_text, citations=["src-9"]),
        DataTier.CONFIDENTIAL,
        sender=sender,
        db=db,
    )
    (channel, _, notice) = sender.thread_posts[0]
    assert channel == "C1"
    for leaked in ("SECRET", "src-9", "layoffs", "0.9", "confidential"):
        assert leaked not in notice


def test__offline_fake_slack_uncited_elevated_still_dm() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(),
        _answer("uncited confidential text"),
        DataTier.CONFIDENTIAL,
        sender=sender,
        db=db,
    )
    assert record.channel is DeliveryChannel.SLACK_DM
    assert sender.thread_posts == [("C1", "1700000000.0001", GENERIC_THREAD_NOTICE)]


def test__offline_fake_slack_citations_do_not_drive_routing() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(),
        _answer("team answer [src-1]", citations=["src-1"]),
        DataTier.TEAM,
        sender=sender,
        db=db,
    )
    assert record.channel is DeliveryChannel.SLACK_THREAD


# --- I3: the tier check fails closed ------------------------------------------


@pytest.mark.parametrize("entry", list(EntryPoint))
@pytest.mark.parametrize("bad_tier", [None, "team", "confidential", "", 0, object()])
def test__offline_fake_slack_non_datatier_refuses(entry: EntryPoint, bad_tier: object) -> None:
    sender, db = FakeSender(), AuditSim()
    with pytest.raises(DeliveryError):
        deliver(_ctx(entry), _answer("secret"), bad_tier, sender=sender, db=db)  # type: ignore[arg-type]
    assert sender.thread_posts == [] and sender.dms == []
    assert db.statements == []  # refused before any write


# --- failure handling -------------------------------------------------------


def test__offline_fake_slack_failed_dm_never_falls_back_to_thread() -> None:
    sender, db = FakeSender(fail_dm=True), AuditSim()
    secret = "confidential content must not leak"
    record = deliver(_ctx(), _answer(secret), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert record.status == "failed"
    assert record.error_class is not None
    for _, _, posted in sender.thread_posts:
        assert secret not in posted
        assert posted in (GENERIC_THREAD_NOTICE, DM_FAILURE_NOTICE)


_ROUTES = [
    (EntryPoint.SLACK_MENTION, DataTier.TEAM),
    (EntryPoint.SLACK_MENTION, DataTier.CONFIDENTIAL),
    (EntryPoint.SLACK_DM, DataTier.RESTRICTED),
]


@pytest.mark.parametrize(("entry", "tier"), _ROUTES)
def test__offline_fake_slack_audit_down_sends_nothing(entry: EntryPoint, tier: DataTier) -> None:
    # I2: with the audit store down, nothing may leave (no undocumented send).
    sender, db = FakeSender(), AuditSim(fail_all=True)
    record = deliver(_ctx(entry), _answer("team update"), tier, sender=sender, db=db)
    assert record.status == "failed"
    assert record.error_class == "audit_failed"
    assert sender.thread_posts == [] and sender.dms == []


class AuditCheckingSender(FakeSender):
    """Fails the test if anything is sent before a ``pending`` audit record."""

    def __init__(self, db: AuditSim) -> None:
        super().__init__()
        self.db = db

    def _assert_pending_recorded(self) -> None:
        answer_pending = bool(self.db.delivery_rows) and self.db.delivery_rows[-1]["status"] == (
            "pending"
        )
        notice_pending = bool(self.db.attempts()) and self.db.attempts()[-1]["status"] == (
            "pending"
        )
        assert answer_pending or notice_pending, "sent before its pending audit record"

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None:
        self._assert_pending_recorded()
        super().post_in_thread(channel_id, thread_ts, text)

    def send_dm(self, slack_user_id: str, text: str) -> None:
        self._assert_pending_recorded()
        super().send_dm(slack_user_id, text)


@pytest.mark.parametrize(("entry", "tier"), _ROUTES)
def test__offline_fake_slack_pending_recorded_before_send(
    entry: EntryPoint, tier: DataTier
) -> None:
    db = AuditSim()
    sender = AuditCheckingSender(db)
    record = deliver(_ctx(entry), _answer("content"), tier, sender=sender, db=db)
    assert record.status == "delivered"
    assert sender.thread_posts or sender.dms
    pending_then_delivered = [status for _, status in db.row_statuses()]
    assert pending_then_delivered == ["pending", "delivered"]
    assert record.delivery_ids == (1, 2)


def _expected_channel(entry: EntryPoint, tier: DataTier) -> str:
    if entry is EntryPoint.SLACK_MENTION and tier is DataTier.TEAM:
        return "slack_thread"
    return "slack_dm"


@pytest.mark.parametrize(("entry", "tier"), _ROUTES)
def test__offline_fake_slack_audit_fails_after_send_raises_distinct_error(
    entry: EntryPoint, tier: DataTier
) -> None:
    from apps.api.delivery import DeliveryAuditAfterSendError

    # tx 0 = pending (ok), tx 1 = delivered (fails): the content is out.
    sender, db = FakeSender(), AuditSim(fail_tx=(1,))
    with pytest.raises(DeliveryAuditAfterSendError) as raised:
        deliver(_ctx(entry), _answer("content"), tier, sender=sender, db=db)
    assert isinstance(raised.value, DeliveryError)  # still fails closed for P3-04
    assert raised.value.record.status == "delivered"  # sent: P3-04 must not re-send
    assert raised.value.record.error_class == "audit_failed"
    assert len(sender.thread_posts) + len(sender.dms) == 1  # sent exactly once
    assert db.row_statuses() == [(_expected_channel(entry, tier), "pending")]


def test__offline_fake_slack_notice_not_posted_without_pending_event() -> None:
    # tx 0/1 = answer pending/delivered, tx 2 = notice pending (fails).
    sender, db = FakeSender(), AuditSim(fail_tx=(2,))
    record = deliver(_ctx(), _answer("secret"), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert record.status == "delivered"
    assert record.notice_posted is False
    assert sender.thread_posts == []


def test__offline_fake_slack_notice_audit_fails_after_post_raises() -> None:
    from apps.api.delivery import DeliveryAuditAfterSendError

    # tx 2 = notice pending (ok), tx 3 = notice sent (fails).
    sender, db = FakeSender(), AuditSim(fail_tx=(3,))
    with pytest.raises(DeliveryAuditAfterSendError) as raised:
        deliver(_ctx(), _answer("secret"), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert raised.value.record.status == "delivered"
    assert raised.value.record.notice_posted is True
    assert sender.thread_posts == [("C1", "1700000000.0001", GENERIC_THREAD_NOTICE)]


def test__offline_fake_slack_missing_mapping_fails_safely() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(slack_id=None),
        _answer("dm content"),
        DataTier.CONFIDENTIAL,
        sender=sender,
        db=db,
    )
    assert record.status == "failed"
    assert record.error_class == "no_slack_mapping"
    assert sender.dms == []


def test__offline_fake_slack_request_mismatch_refuses() -> None:
    other = ResponderOutput(request_id="req-OTHER", answer_text="x")
    with pytest.raises(DeliveryError):
        deliver(_ctx(), other, DataTier.TEAM, sender=FakeSender(), db=AuditSim())


def test__offline_fake_slack_non_responder_answer_refuses() -> None:
    sender, db = FakeSender(), AuditSim()
    fake = {"request_id": "req-1", "answer_text": "x"}
    with pytest.raises(DeliveryError):
        deliver(_ctx(), fake, DataTier.TEAM, sender=sender, db=db)  # type: ignore[arg-type]
    assert sender.thread_posts == [] and db.statements == []


def test__offline_fake_slack_deliver_has_no_members_parameter() -> None:
    # Destinations resolve from the trusted ctx alone: no unused recipient table.
    with pytest.raises(TypeError):
        deliver(_ctx(), _answer("x"), DataTier.TEAM, db=AuditSim(), members={})  # type: ignore[call-arg]


def test__offline_fake_slack_no_store_refuses() -> None:
    with pytest.raises(DeliveryError):
        deliver(_ctx(), _answer("x"), DataTier.TEAM, sender=FakeSender(), db=None)


def test__offline_fake_slack_recipient_is_mapped_identity() -> None:
    sender, db = FakeSender(), AuditSim()
    deliver(_ctx(), _answer("x"), DataTier.RESTRICTED, sender=sender, db=db)
    # No recipient input exists anywhere: destination is the trusted mapping.
    assert [user for user, _ in sender.dms] == ["U-sam"]


# --- B1: non-answer messages never mark the request delivered ---------------


def test__offline_fake_slack_dm_failure_notice_never_marks_request_delivered() -> None:
    sender, db = FakeSender(fail_dm=True), AuditSim()
    record = deliver(_ctx(), _answer("secret"), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert record.status == "failed"
    assert sender.thread_posts == [("C1", "1700000000.0001", DM_FAILURE_NOTICE)]
    # The notice reached the thread, the answer reached nobody: no
    # plans->delivered UPDATE, no delivered row, and the read API agrees.
    assert db.plans_delivered_updates() == []
    assert db.plan_status != "delivered"
    assert "delivered" not in [status for _, status in db.row_statuses()]
    assert not any(channel == "slack_thread" for channel, _ in db.row_statuses())
    assert request_status(db, "req-1", "m-sam").delivered is False
    (notice,) = db.attempts("dm_failure_notice", status="sent")
    assert notice["channel"] == "slack_thread"
    assert DM_FAILURE_NOTICE not in db.params_text()  # metadata only, never the text


def test__offline_fake_slack_private_notice_is_not_a_delivery_row() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(_ctx(), _answer("secret"), DataTier.RESTRICTED, sender=sender, db=db)
    assert record.status == "delivered" and record.notice_posted is True
    # Exactly one delivered row: the DM answer. The thread notice is an event.
    assert [row for row in db.row_statuses() if row[1] != "pending"] == [("slack_dm", "delivered")]
    assert not any(channel == "slack_thread" for channel, _ in db.row_statuses())
    (notice,) = db.attempts("private_response_notice", status="sent")
    assert notice["recipient_member_id"] == "m-sam"
    assert GENERIC_THREAD_NOTICE not in db.params_text()


def test__offline_fake_slack_failed_notice_post_is_recorded() -> None:
    sender, db = FakeSender(fail_thread=True), AuditSim()
    record = deliver(_ctx(), _answer("secret"), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert record.status == "delivered"
    assert record.notice_posted is False
    failed = db.attempts("private_response_notice", status="failed")
    assert [n["error_class"] for n in failed] == ["RuntimeError"]


# --- I1: notices carry team tier, never the answer's elevated tier ----------


@pytest.mark.parametrize("fail_dm", [False, True])
def test__offline_fake_slack_notice_records_team_tier(fail_dm: bool) -> None:
    sender, db = FakeSender(fail_dm=fail_dm), AuditSim()
    deliver(_ctx(), _answer("secret"), DataTier.RESTRICTED, sender=sender, db=db)
    notices = db.attempts()
    assert notices, "the notice attempt must be audited"
    assert {n["tier_delivered"] for n in notices} == {"team"}
    # The answer's own delivery event keeps the true (restricted) tier.
    answer_tiers = {
        e["payload"]["tier_delivered"]  # type: ignore[index]
        for e in db.events
        if e["type"] in ("delivery_succeeded", "delivery_failed")
    }
    assert answer_tiers == {"restricted"}


# --- I6: model text cannot inject Slack control sequences -------------------

_INJECTION = "Hi <!channel> <!here> <@U999> <https://evil.example.test|click> & &lt;"
_ESCAPED = (
    "Hi &lt;!channel&gt; &lt;!here&gt; &lt;@U999&gt; "
    "&lt;https://evil.example.test|click&gt; &amp; &amp;lt;"
)


@pytest.mark.parametrize(("entry", "tier"), _ROUTES)
def test__offline_fake_slack_model_text_is_slack_escaped(entry: EntryPoint, tier: DataTier) -> None:
    sender, db = FakeSender(), AuditSim()
    deliver(_ctx(entry), _answer(_INJECTION), tier, sender=sender, db=db)
    answers = [text for _, _, text in sender.thread_posts if text != GENERIC_THREAD_NOTICE]
    answers += [text for _, text in sender.dms]
    assert answers == [_ESCAPED]
    for _, _, text in sender.thread_posts + [("", "", t) for _, t in sender.dms]:
        assert "<" not in text and ">" not in text


def test__offline_fake_slack_fixed_notice_stays_literal() -> None:
    sender, db = FakeSender(), AuditSim()
    deliver(_ctx(), _answer(_INJECTION), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert sender.thread_posts == [("C1", "1700000000.0001", GENERIC_THREAD_NOTICE)]
    assert sender.dms == [("U-sam", _ESCAPED)]


# --- minor: missing thread reference, empty and error answers fail closed ---


def test__offline_fake_slack_mention_without_thread_ts_never_posts_top_level() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(thread_ts=None), _answer("team update"), DataTier.TEAM, sender=sender, db=db
    )
    assert record.status == "failed"
    assert record.error_class == "no_thread_context"
    assert sender.thread_posts == [] and sender.dms == []  # no top-level post, no fallback
    assert db.row_statuses() == [("slack_thread", "failed")]


def test__offline_fake_slack_elevated_without_thread_ts_skips_and_records_notice() -> None:
    sender, db = FakeSender(), AuditSim()
    record = deliver(
        _ctx(thread_ts=None), _answer("secret"), DataTier.CONFIDENTIAL, sender=sender, db=db
    )
    assert record.status == "delivered"  # the private DM still goes out
    assert record.notice_posted is False
    assert sender.thread_posts == []
    (notice,) = db.attempts("private_response_notice")
    assert (notice["status"], notice["error_class"]) == ("failed", "no_thread_context")


@pytest.mark.parametrize("entry", list(EntryPoint))
@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test__offline_fake_slack_empty_answer_refuses(entry: EntryPoint, text: str) -> None:
    sender, db = FakeSender(), AuditSim()
    with pytest.raises(DeliveryError):
        deliver(_ctx(entry), _answer(text), DataTier.TEAM, sender=sender, db=db)
    assert sender.thread_posts == [] and sender.dms == [] and db.statements == []


@pytest.mark.parametrize("entry", list(EntryPoint))
def test__offline_fake_slack_error_outcome_is_not_delivered(entry: EntryPoint) -> None:
    sender, db = FakeSender(), AuditSim()
    error = ResponderOutput(
        request_id="req-1",
        answer_text="Something went wrong.",
        abstained=True,
        abstain_reason="error",
    )
    with pytest.raises(DeliveryError):
        deliver(_ctx(entry), error, DataTier.TEAM, sender=sender, db=db)
    assert sender.thread_posts == [] and sender.dms == [] and db.statements == []


def test__offline_fake_slack_ordinary_abstention_is_delivered() -> None:
    sender, db = FakeSender(), AuditSim()
    abstain = ResponderOutput(
        request_id="req-1",
        answer_text="I don't have a vetted answer; I've queued this for Maya.",
        abstained=True,
        abstain_reason="no_evidence",
    )
    record = deliver(_ctx(), abstain, DataTier.TEAM, sender=sender, db=db)
    assert record.status == "delivered"
    assert len(sender.thread_posts) == 1


# --- minor: plan_id reaches every audit write --------------------------------


@pytest.mark.parametrize(
    ("entry", "tier"),
    [*_ROUTES, (EntryPoint.WEB, DataTier.RESTRICTED)],
)
def test__offline_fake_slack_plan_id_is_recorded(entry: EntryPoint, tier: DataTier) -> None:
    sender, db = FakeSender(), AuditSim()
    deliver(_ctx(entry), _answer("x"), tier, sender=sender, db=db, plan_id="p-0123456789abcdef")
    assert db.delivery_rows, "the answer must be recorded"
    assert {row["plan_id"] for row in db.delivery_rows} == {"p-0123456789abcdef"}
    assert {event["plan_id"] for event in db.events} == {"p-0123456789abcdef"}


def test__offline_fake_slack_plan_id_defaults_to_none() -> None:
    db = AuditSim()
    deliver(_ctx(), _answer("x"), DataTier.CONFIDENTIAL, sender=FakeSender(), db=db)
    assert {row["plan_id"] for row in db.delivery_rows} == {None}
    assert {event["plan_id"] for event in db.events} == {None}


# --- live (local database only): B1 against the real audit store ------------

LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
DB_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
needs_live_write = pytest.mark.skipif(
    not (LIVE_DSN and DB_WRITE_TESTS),
    reason="needs DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent append-only audit rows: local database only)",
)


def live_request(
    db: object, entry: EntryPoint = EntryPoint.SLACK_MENTION
) -> tuple[RequestContext, str]:
    """A recorded request + plan on the seeded local database.

    Uses the P2-03 seed identities (ADR-021: `workspace-primary`, `m-sam`).
    """
    from services.audit.store import create_request, record_plan
    from tests.api.audit_fakes import make_plan

    ctx = _ctx(entry).model_copy(
        update={
            "request_id": f"test-dlv-{uuid.uuid4().hex[:12]}",
            "trace_id": f"test-trace-{uuid.uuid4().hex[:8]}",
            "workspace_id": "workspace-primary",
            "received_at": datetime.now(UTC),
        }
    )
    create_request(db, ctx)  # type: ignore[arg-type]
    plan_id = record_plan(
        db,  # type: ignore[arg-type]
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        plan=make_plan(),
        plan_id=f"p-{uuid.uuid4().hex[:16]}",
    )
    return ctx, plan_id


def live_rows(db: object, sql: str, params: list[object]) -> list[tuple[object, ...]]:
    with db.tx() as cursor:  # type: ignore[attr-defined]
        cursor.execute(sql, params)
        return [tuple(row) for row in cursor.fetchall()]


_LIVE_ATTEMPTS_SQL = (
    "SELECT payload->>'purpose', payload->>'status', payload->>'tier_delivered', "
    "payload->>'recipient_member_id', plan_id FROM request_events "
    "WHERE request_id = %s AND event_type = 'delivery_attempted' "
    "AND payload ? 'purpose' ORDER BY id"
)


def _live_answered(db: object, ctx: RequestContext, plan_id: str, tier: DataTier) -> None:
    from services.audit.store import record_answer

    record_answer(
        db,  # type: ignore[arg-type]
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        citations=[],
        highest_tier=tier,
        abstained=False,
        reason=None,
        plan_id=plan_id,
    )


@needs_live_write
def test_live_dm_failure_notice_leaves_request_undelivered() -> None:
    from apps.api.core.db import BackendDatabase

    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    try:
        ctx, plan_id = live_request(db)
        _live_answered(db, ctx, plan_id, DataTier.CONFIDENTIAL)
        sender = FakeSender(fail_dm=True)
        answer = ResponderOutput(request_id=ctx.request_id, answer_text="confidential content")
        record = deliver(ctx, answer, DataTier.CONFIDENTIAL, sender=sender, db=db, plan_id=plan_id)
        assert record.status == "failed" and record.notice_posted is True
        assert sender.thread_posts == [("C1", "1700000000.0001", DM_FAILURE_NOTICE)]
        # The real store: plans did not advance, the read API says undelivered.
        plans = live_rows(db, "SELECT status FROM plans WHERE id = %s", [plan_id])
        assert plans == [("answered",)]
        assert request_status(db, ctx.request_id, "m-sam").delivered is False
        rows = live_rows(
            db,
            "SELECT delivery_channel, delivery_status, plan_id FROM delivery_events "
            "WHERE request_id = %s ORDER BY id",
            [ctx.request_id],
        )
        assert rows == [("slack_dm", "pending", plan_id), ("slack_dm", "failed", plan_id)]
        assert live_rows(db, _LIVE_ATTEMPTS_SQL, [ctx.request_id]) == [
            ("dm_failure_notice", "pending", "team", "m-sam", plan_id),
            ("dm_failure_notice", "sent", "team", "m-sam", plan_id),
        ]
    finally:
        db.close()


@needs_live_write
def test_live_delivered_answer_marks_request_delivered() -> None:
    # Positive control: the same store DOES flip to delivered for the answer,
    # so the negative B1 assertions above are not vacuous.
    from apps.api.core.db import BackendDatabase

    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    try:
        ctx, plan_id = live_request(db)
        _live_answered(db, ctx, plan_id, DataTier.RESTRICTED)
        sender = FakeSender()
        answer = ResponderOutput(request_id=ctx.request_id, answer_text="restricted content")
        record = deliver(ctx, answer, DataTier.RESTRICTED, sender=sender, db=db, plan_id=plan_id)
        assert record.status == "delivered" and record.notice_posted is True
        assert live_rows(db, "SELECT status FROM plans WHERE id = %s", [plan_id]) == [
            ("delivered",)
        ]
        assert request_status(db, ctx.request_id, "m-sam").delivered is True
        # Only the DM answer is a delivery row; the thread notice is an event.
        rows = live_rows(
            db,
            "SELECT delivery_channel, delivery_status FROM delivery_events "
            "WHERE request_id = %s ORDER BY id",
            [ctx.request_id],
        )
        assert rows == [("slack_dm", "pending"), ("slack_dm", "delivered")]
        assert [r[:2] for r in live_rows(db, _LIVE_ATTEMPTS_SQL, [ctx.request_id])] == [
            ("private_response_notice", "pending"),
            ("private_response_notice", "sent"),
        ]
    finally:
        db.close()


@needs_live_write
def test_live_web_answer_stays_pending() -> None:
    from apps.api.core.db import BackendDatabase

    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    try:
        ctx, plan_id = live_request(db, EntryPoint.WEB)
        _live_answered(db, ctx, plan_id, DataTier.TEAM)
        answer = ResponderOutput(request_id=ctx.request_id, answer_text="web answer")
        record = deliver(ctx, answer, DataTier.TEAM, db=db, plan_id=plan_id)
        assert record.status == "pending"
        assert live_rows(db, "SELECT status FROM plans WHERE id = %s", [plan_id]) == [("answered",)]
        assert request_status(db, ctx.request_id, "m-sam").delivered is False
    finally:
        db.close()
