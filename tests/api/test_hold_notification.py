"""HOLD notification to Maya (P3-06, R-40/R-44, D-22 pending).

Offline: `__offline_fake_slack` sender, fake audit store, fake member table.
Every test asserts on the actual DM text against the metadata allowlist —
and on what is absent.
"""

from datetime import UTC, datetime

import pytest

from apps.api.delivery import DeliveryError, notify_hold
from apps.api.identity import MemberRecord
from services.audit.queries import request_status
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
from tests.api.conftest import make_settings
from tests.api.test_delivery_channel import AuditSim

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
# Test-only console URL: proves the link comes from settings (never a
# hardcoded default in delivery.py).
CONSOLE = "https://console.example.test/armoriq"


@pytest.fixture(autouse=True)
def _console_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic settings: never a developer's environment or `.env` file."""
    monkeypatch.setattr(
        "apps.api.delivery.get_settings",
        lambda: make_settings(armoriq_console_url=CONSOLE),
        raising=False,
    )


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
    sender, db = FakeSender(), AuditSim()
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
    sender, db = FakeSender(), AuditSim()
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert "sam" in text  # requester persona
    assert "knowledge_search_confidential" in text  # tool name
    assert "req-7" in text  # request ID
    assert CONSOLE in text  # console link from settings


def test__offline_fake_slack_hold_text_has_no_protected_content() -> None:
    sender, db = FakeSender(), AuditSim()
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
    sender, db = FakeSender(), AuditSim()
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert "Approve" not in text and "Reject" not in text and "button" not in text
    assert text.count("http") == 1  # exactly the configured console URL, no deep link


def test__offline_fake_slack_hold_links_base_only_pending_d22() -> None:
    sender, db = FakeSender(), AuditSim()
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert "/approve" not in text and "/delegat" not in text and "?" not in text


# --- failure handling -------------------------------------------------------


def test__offline_fake_slack_missing_maya_mapping_fails_safely() -> None:
    sender, db = FakeSender(), AuditSim()
    record = notify_hold(
        _ctx(), _approval(), sender=sender, db=db, members=_members(maya_slack=None)
    )
    assert record.status == "failed"
    assert record.error_class == "no_maya_mapping"
    assert sender.dms == [] and sender.thread_posts == []


def test__offline_fake_slack_no_members_table_fails_safely() -> None:
    sender, db = FakeSender(), AuditSim()
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=None)
    assert record.status == "failed"
    assert sender.dms == []


def test__offline_fake_slack_hold_dm_failure_recorded() -> None:
    sender, db = FakeSender(fail_dm=True), AuditSim()
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert record.status == "failed"
    assert record.error_class is not None
    assert sender.thread_posts == []  # failure stays private, never broadcast


def test__offline_fake_slack_hold_no_store_refuses() -> None:
    with pytest.raises(DeliveryError):
        notify_hold(_ctx(), _approval(), sender=FakeSender(), db=None, members=_members())


# --- I5: only this request's pending HOLD notifies Maya ----------------------


def test__offline_fake_slack_hold_for_another_request_refuses() -> None:
    sender, db = FakeSender(), AuditSim()
    foreign = _approval().model_copy(update={"request_id": "req-OTHER"})
    with pytest.raises(DeliveryError):
        notify_hold(_ctx(), foreign, sender=sender, db=db, members=_members())
    assert sender.dms == [] and db.statements == []


@pytest.mark.parametrize("state", [s for s in ApprovalState if s is not ApprovalState.PENDING])
def test__offline_fake_slack_hold_not_pending_refuses(state: ApprovalState) -> None:
    sender, db = FakeSender(), AuditSim()
    resolved = _approval().model_copy(update={"state": state, "resolved_at": NOW})
    with pytest.raises(DeliveryError):
        notify_hold(_ctx(), resolved, sender=sender, db=db, members=_members())
    assert sender.dms == [] and db.statements == []


def test__offline_fake_slack_hold_rejected_foreign_approval_refuses() -> None:
    # The review's repro: a rejected approval from another request.
    sender, db = FakeSender(), AuditSim()
    stale = _approval().model_copy(
        update={"request_id": "req-OTHER", "state": ApprovalState.REJECTED, "resolved_at": NOW}
    )
    with pytest.raises(DeliveryError):
        notify_hold(_ctx(), stale, sender=sender, db=db, members=_members())
    assert sender.dms == []


def test__offline_fake_slack_hold_records_team_tier_not_held_tool_tier() -> None:
    sender, db = FakeSender(), AuditSim()
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    # The held tool is confidential, but the DM carries metadata only: the
    # audit evidence must not claim confidential content went to Maya.
    assert record.tier_delivered is DataTier.TEAM
    assert {a["tier_delivered"] for a in db.attempts("hold_notice")} == {"team"}
    assert "confidential" not in str(db.attempts())


# --- B1: the HOLD DM never marks the request delivered ----------------------


def test__offline_fake_slack_hold_dm_never_marks_request_delivered() -> None:
    sender, db = FakeSender(), AuditSim()
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert record.status == "delivered"  # Maya got the DM ...
    assert len(sender.dms) == 1
    # ... but the requester's request is still on HOLD, not delivered.
    assert db.delivery_rows == []
    assert not any("INSERT INTO delivery_events" in sql for sql, _ in db.statements)
    assert db.plans_delivered_updates() == []
    assert db.plan_status != "delivered"
    assert request_status(db, "req-7", "m-sam").delivered is False
    (event,) = [a for a in db.attempts("hold_notice") if a["status"] == "sent"]
    assert event["channel"] == "slack_dm"
    assert event["recipient_member_id"] == "m-maya"
    (_, text) = sender.dms[0]
    assert text not in db.params_text()  # metadata only, never the DM text


def test__offline_fake_slack_hold_dm_failure_never_marks_request_delivered() -> None:
    sender, db = FakeSender(fail_dm=True), AuditSim()
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert record.status == "failed"
    assert db.delivery_rows == [] and db.plans_delivered_updates() == []
    failed = [a for a in db.attempts("hold_notice") if a["status"] == "failed"]
    assert [a["error_class"] for a in failed] == ["RuntimeError"]


# --- I2: audit before send ---------------------------------------------------


def test__offline_fake_slack_hold_audit_down_sends_nothing() -> None:
    sender, db = FakeSender(), AuditSim(fail_all=True)
    record = notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert record.status == "failed"
    assert record.error_class == "audit_failed"
    assert sender.dms == []


def test__offline_fake_slack_hold_pending_recorded_before_dm() -> None:
    db = AuditSim()
    seen: list[list[object]] = []

    class CheckingSender(FakeSender):
        def send_dm(self, slack_user_id: str, text: str) -> None:
            seen.append([a["status"] for a in db.attempts("hold_notice")])
            super().send_dm(slack_user_id, text)

    sender = CheckingSender()
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert seen == [["pending"]]
    assert [a["status"] for a in db.attempts("hold_notice")] == ["pending", "sent"]


def test__offline_fake_slack_hold_audit_fails_after_dm_raises_distinct_error() -> None:
    from apps.api.delivery import DeliveryAuditAfterSendError

    sender, db = FakeSender(), AuditSim(fail_tx=(1,))  # pending ok, sent fails
    with pytest.raises(DeliveryAuditAfterSendError) as raised:
        notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    assert raised.value.record.status == "delivered"
    assert raised.value.record.error_class == "audit_failed"
    assert len(sender.dms) == 1  # sent once; P3-04 must not re-send


# --- I4: console link from settings, validated, never hardcoded -------------

_HOLD_HEAD = (
    "ProxyLead needs your review.\n"
    "Request req-7 from sam needs a decision on knowledge_search_confidential.\n"
)


def test__offline_fake_slack_hold_text_exact_with_settings_link() -> None:
    sender, db = FakeSender(), AuditSim()
    settings = make_settings(armoriq_console_url="https://console.example.test/x")
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members(), settings=settings)
    assert sender.dms == [
        (
            "U-maya",
            _HOLD_HEAD + "Review pending approvals in the ArmorIQ console: "
            "https://console.example.test/x",
        )
    ]


def test__offline_fake_slack_hold_link_defaults_to_get_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "apps.api.delivery.get_settings",
        lambda: make_settings(armoriq_console_url="https://from-settings.example.test"),
    )
    sender, db = FakeSender(), AuditSim()
    notify_hold(_ctx(), _approval(), sender=sender, db=db, members=_members())
    (_, text) = sender.dms[0]
    assert text.endswith("console: https://from-settings.example.test")
    assert "platform.armoriq.ai" not in text  # no hardcoded fallback


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "http://console.example.test",  # not https
        "https://console.example.test/?q=1",  # query
        "https://console.example.test/?",  # empty query
        "https://console.example.test/#frag",  # fragment
        "https://user:pw@console.example.test",  # credentials
        "https://",  # no host
        "https:///path",  # no host
        "javascript:alert(1)",
        "ftp://console.example.test",
        "console.example.test",  # no scheme
        "https://console.example.test/<!channel>",  # Slack control sequence
        "https://console.example.test|click me",  # Slack link label
        "https://console.example.test/a b",  # whitespace
        "https://console.example.test:99999",  # malformed port
    ],
)
def test__offline_fake_slack_hold_invalid_console_url_omits_link(bad_url: str) -> None:
    sender, db = FakeSender(), AuditSim()
    settings = make_settings(armoriq_console_url=bad_url)
    record = notify_hold(
        _ctx(), _approval(), sender=sender, db=db, members=_members(), settings=settings
    )
    assert record.status == "delivered"  # the HOLD is still announced ...
    # ... with neutral wording and no link at all (never a hardcoded URL).
    assert sender.dms == [
        ("U-maya", _HOLD_HEAD + "Review pending approvals in the ArmorIQ console.")
    ]
