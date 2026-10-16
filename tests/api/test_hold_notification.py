"""HOLD notification to Maya (P3-06, R-40/R-44, D-22 pending).

Offline: `__offline_fake_slack` sender, fake audit store, fake member table.
Every test asserts on the actual DM text against the metadata allowlist —
and on what is absent.
"""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from apps.api.delivery import DeliveryError, notify_hold
from apps.api.identity import MemberRecord
from services.contracts import (
    AppRole,
    ApprovalState,
    ApprovalStatus,
    AuthenticatedUser,
    DataTier,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    RequestContext,
    ToolName,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CONSOLE = "https://platform.armoriq.ai"


class FakeSender:
    """Offline fake Slack transport (`__offline_fake_slack`)."""

    def __init__(self, fail_dm: bool = False) -> None:
        self.dms: list[tuple[str, str]] = []
        self.thread_posts: list[tuple[str, str | None, str]] = []
        self.fail_dm = fail_dm

    def post_in_thread(self, channel_id: str, thread_ts: str | None, text: str) -> None:
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
    def __init__(self, one=None) -> None:
        self._one = one

    @contextmanager
    def tx(self):  # type: ignore[no-untyped-def]
        yield FakeCursor(one=self._one)


def _members(maya_slack: str | None = "U-maya") -> dict[PersonaKey, MemberRecord]:
    return {
        PersonaKey.MAYA: MemberRecord(
            persona=PersonaKey.MAYA,
            member_id="m-maya",
            display_name="Maya",
            app_role=AppRole.MANAGER,
            email="maya@example.com",
            email_ref="PERSONA_MAYA_EMAIL",
            slack_user_id=maya_slack,
        ),
        PersonaKey.SAM: MemberRecord(
            persona=PersonaKey.SAM,
            member_id="m-sam",
            display_name="Sam",
            app_role=AppRole.SENIOR,
            email="sam@example.com",
            email_ref="PERSONA_SAM_EMAIL",
            slack_user_id="U-sam",
        ),
    }


def _ctx() -> RequestContext:
    return RequestContext(
        request_id="req-7",
        trace_id="trace-7",
        workspace_id="ws-1",
        requester=AuthenticatedUser(
            member_id="m-sam",
            persona=PersonaKey.SAM,
            display_name="Sam",
            app_role=AppRole.SENIOR,
            masked_email="s***@example.com",
            slack_user_id="U-sam",
        ),
        entry_point=EntryPoint.SLACK_MENTION,
        received_at=NOW,
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_event_id="E1",
    )


def _approval() -> ApprovalStatus:
    return ApprovalStatus(
        request_id="req-7",
        plan_id="plan-7",
        step_index=0,
        tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        state=ApprovalState.PENDING,
        delegation_id="dlg-7",
        requested_at=NOW,
        resolved_at=None,
    )


# --- recipient: Maya only ---------------------------------------------------


def test__offline_fake_slack_hold_dm_goes_to_maya_only() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert record.status == "delivered"
    assert record.channel is DeliveryChannel.SLACK_DM
    assert record.recipient_member_id == "m-maya"
    assert len(sender.dms) == 1
    user, _ = sender.dms[0]
    assert user == "U-maya"  # never the requester's U-sam
    assert sender.thread_posts == []


# --- metadata allowlist -----------------------------------------------------


def test__offline_fake_slack_hold_text_is_allowlisted() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert "sam" in text  # requester persona
    assert "knowledge_search_confidential" in text  # tool name
    assert "req-7" in text  # request ID
    assert CONSOLE in text  # console base URL


def test__offline_fake_slack_hold_text_has_no_protected_content() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    for leaked in (
        "atlas budget",  # query text would appear here if interpolated
        "src-",  # source references
        "[citat",  # citations
        "answer text",
        "@example.com",  # raw emails
        "ak_test",  # tokens
        "Bearer",
        "dlg-7",  # delegation ID is NOT on the DM allowlist
    ):
        assert leaked not in text


def test__offline_fake_slack_hold_has_no_approval_affordance() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert "Approve" not in text and "Reject" not in text and "button" not in text
    assert text.count("http") == 1  # exactly the console base URL, no deep link


def test__offline_fake_slack_hold_links_base_only_pending_d22() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert "/approve" not in text and "/delegat" not in text and "?" not in text


# --- failure handling -------------------------------------------------------


def test__offline_fake_slack_missing_maya_mapping_fails_safely() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    record = notify_hold(
        _ctx(), _approval(), sender=sender, db=db, members=_members(maya_slack=None)
    )
    assert record.status == "failed"
    assert record.error_class == "no_maya_mapping"
    assert sender.dms == [] and sender.thread_posts == []


def test__offline_fake_slack_no_members_table_fails_safely() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=None)
    assert record.status == "failed"
    assert sender.dms == []


def test__offline_fake_slack_hold_dm_failure_recorded() -> None:
    sender, db = FakeSender(fail_dm=True), FakeDb(one=[(3,)])
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert record.status == "failed"
    assert record.error_class is not None
    assert sender.thread_posts == []  # failure stays private, never broadcast


def test__offline_fake_slack_hold_no_store_refuses() -> None:
    with pytest.raises(DeliveryError):
        notify_hold(_ctx(), _approval(), sender=FakeSender(), db=None, members=_members())


def test__offline_fake_slack_hold_records_confidential_tier_metadata() -> None:
    sender, db = FakeSender(), FakeDb(one=[(3,)])
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    # Labels the held action's tier (metadata about that tier, not its content).
    assert record.tier_delivered is DataTier.CONFIDENTIAL
