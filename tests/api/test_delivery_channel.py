"""Delivery routing by entry point and tier (P3-06 §11).

Offline: `__offline_fake_slack` sender and fake audit store (no Slack, no
Neon). Leakage tests assert on the actual payloads handed to the sender —
absence of protected text, not just return values.
"""

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


class FakeCursor:
    def __init__(self, one=None) -> None:
        self.statements: list[tuple[str, object]] = []
        self._one = list(one or [])
        self.rowcount = 1

    def execute(self, query: str, params: object = None) -> None:
        self.statements.append((query, params))

    def fetchone(self) -> object:
        return self._one.pop(0) if self._one else None

    def fetchall(self) -> list[object]:
        return []


class FakeDb:
    """Test-local audit store double (records SQL, no database)."""

    def __init__(self, one=None, fail: bool = False) -> None:
        self._one = one
        self.fail = fail
        self.cursors: list[FakeCursor] = []

    @contextmanager
    def tx(self):  # type: ignore[no-untyped-def]
        cursor = FakeCursor(one=self._one)
        self.cursors.append(cursor)
        if self.fail:
            original = cursor.execute

            def _boom(query: str, params: object = None) -> None:
                original(query, params)
                raise AuditStoreError("fake audit write failed")

            cursor.execute = _boom  # type: ignore[method-assign]
        yield cursor


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
) -> RequestContext:
    refs: dict[str, object] = (
        {}
        if entry is EntryPoint.WEB
        else {
            "slack_team_id": "T1",
            "slack_channel_id": "C1",
            "slack_event_id": "E1",
            "slack_thread_ts": "1700000000.0001",
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
    sender, db = FakeSender(), FakeDb(one=[(7,)])
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
    sender, db = FakeSender(), FakeDb(one=[(7,), (8,)])
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
    sender, db = FakeSender(), FakeDb(one=[(7,)])
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
    sender, db = FakeSender(), FakeDb(one=[(7,)])
    record = deliver(
        _ctx(EntryPoint.WEB),
        _answer("private content"),
        tier,
        sender=None,
        db=db,
    )
    assert record.channel is DeliveryChannel.WEB_PRIVATE
    assert record.status == "delivered"
    assert sender.dms == [] and sender.thread_posts == []


# --- leakage proofs ---------------------------------------------------------


def test__offline_fake_slack_notice_is_fixed_constant() -> None:
    assert GENERIC_THREAD_NOTICE == "I sent you a private response."


def test__offline_fake_slack_thread_notice_carries_no_content() -> None:
    sender, db = FakeSender(), FakeDb(one=[(7,), (8,)])
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
    sender, db = FakeSender(), FakeDb(one=[(7,), (8,)])
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
    sender, db = FakeSender(), FakeDb(one=[(7,)])
    record = deliver(
        _ctx(),
        _answer("team answer [src-1]", citations=["src-1"]),
        DataTier.TEAM,
        sender=sender,
        db=db,
    )
    assert record.channel is DeliveryChannel.SLACK_THREAD


# --- failure handling -------------------------------------------------------


def test__offline_fake_slack_failed_dm_never_falls_back_to_thread() -> None:
    sender, db = FakeSender(fail_dm=True), FakeDb(one=[(7,), (8,)])
    secret = "confidential content must not leak"
    record = deliver(_ctx(), _answer(secret), DataTier.CONFIDENTIAL, sender=sender, db=db)
    assert record.status == "failed"
    assert record.error_class is not None
    for _, _, posted in sender.thread_posts:
        assert secret not in posted
        assert posted in (GENERIC_THREAD_NOTICE, DM_FAILURE_NOTICE)


def test__offline_fake_slack_failed_audit_not_reported_delivered() -> None:
    sender, db = FakeSender(), FakeDb(fail=True)
    record = deliver(_ctx(), _answer("team update"), DataTier.TEAM, sender=sender, db=db)
    assert record.status == "failed"
    assert record.error_class == "audit_failed"


def test__offline_fake_slack_missing_mapping_fails_safely() -> None:
    sender, db = FakeSender(), FakeDb(one=[(7,)])
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
        deliver(_ctx(), other, DataTier.TEAM, sender=FakeSender(), db=FakeDb())


def test__offline_fake_slack_no_store_refuses() -> None:
    with pytest.raises(DeliveryError):
        deliver(_ctx(), _answer("x"), DataTier.TEAM, sender=FakeSender(), db=None)


def test__offline_fake_slack_recipient_is_mapped_identity() -> None:
    sender, db = FakeSender(), FakeDb(one=[(7,), (8,)])
    deliver(_ctx(), _answer("x"), DataTier.RESTRICTED, sender=sender, db=db)
    # No recipient input exists anywhere: destination is the trusted mapping.
    assert [user for user, _ in sender.dms] == ["U-sam"]
