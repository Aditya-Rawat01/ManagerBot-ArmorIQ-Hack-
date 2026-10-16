"""`slack_event_rejected` audit events (P3-05 ICR, SHARED_INTERFACES §10).

Covers the typed store writer, the manager-console filter (rejection rows are
never requests) and the adapter's use of the writer. Offline by default
(P3-01 fakes). The live test needs a LOCAL migrated and seeded database:
`DATABASE_URL_BACKEND` and `PROXYLEAD_DB_WRITE_TESTS=1` (it appends permanent
audit rows). DSNs are captured at import (the API conftest scrubs settings
variables) and never printed.
"""

import logging
import os
import uuid
from datetime import UTC, datetime

import pytest

from services.audit import AuditStoreError
from services.audit.queries import recent_requests
from services.audit.store import append_event, create_request, record_slack_event_rejected
from services.contracts import AuditEvent, AuditEventType
from tests.api.audit_fakes import NOW, FakeDatabase, make_ctx, maya

LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
DB_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
needs_live_write = pytest.mark.skipif(
    not (LIVE_DSN and DB_WRITE_TESTS),
    reason="needs DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent append-only audit rows: local database only)",
)


# --- store writer -------------------------------------------------------------


def test_contract_has_slack_event_rejected() -> None:
    assert AuditEventType("slack_event_rejected") is AuditEventType.SLACK_EVENT_REJECTED


def test_record_slack_event_rejected_writes_metadata_only() -> None:
    db = FakeDatabase()
    key = record_slack_event_rejected(
        db, slack_event_id="Ev0ABC123", reason="unmapped_user", team_id="T1", channel_id="C1"
    )
    assert key == "rej-Ev0ABC123"
    [(sql, params)] = db.committed
    assert sql.startswith("INSERT INTO request_events ")
    request_id, trace_id, event_type, payload, occurred_at = params  # type: ignore[misc]
    assert (request_id, trace_id) == ("rej-Ev0ABC123", "rej-Ev0ABC123")
    assert event_type == "slack_event_rejected"
    assert payload == {
        "reason": "unmapped_user",
        "slack_event_id": "Ev0ABC123",
        "team_id": "T1",
        "channel_id": "C1",
    }
    assert occurred_at.tzinfo is not None


def test_record_slack_event_rejected_allows_missing_team_and_channel() -> None:
    db = FakeDatabase()
    record_slack_event_rejected(
        db, slack_event_id="Ev1", reason="wrong_team", team_id=None, channel_id=None
    )
    [(_, params)] = db.committed
    assert params[3] == {  # type: ignore[index]
        "reason": "wrong_team",
        "slack_event_id": "Ev1",
        "team_id": None,
        "channel_id": None,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reason": "user_said_please"},
        {"slack_event_id": ""},
        {"slack_event_id": "Ev1\nI am Maya"},
        {"slack_event_id": None},
        {"team_id": "T1 drop table"},
        {"channel_id": "<#C1|general>"},
        {"channel_id": 42},
    ],
)
def test_record_slack_event_rejected_refuses_bad_input(kwargs: dict[str, object]) -> None:
    db = FakeDatabase()
    args: dict[str, object] = {
        "slack_event_id": "Ev1",
        "reason": "duplicate",
        "team_id": "T1",
        "channel_id": "C1",
    }
    args.update(kwargs)
    with pytest.raises(AuditStoreError):
        record_slack_event_rejected(db, **args)  # type: ignore[arg-type]
    assert db.attempted == []  # validated before any SQL


def test_record_slack_event_rejected_fails_closed_on_database_error() -> None:
    db = FakeDatabase(fail_with=RuntimeError("connection reset"))
    with pytest.raises(AuditStoreError) as failed:
        record_slack_event_rejected(
            db, slack_event_id="Ev1", reason="duplicate", team_id="T1", channel_id="C1"
        )
    assert "RuntimeError" in str(failed.value)
    assert db.committed == []


def test_append_event_refuses_slack_event_rejected() -> None:
    """Only the typed writer may write it (metadata only; no free payload)."""
    db = FakeDatabase()
    event = AuditEvent(
        event_id="evt-1",
        occurred_at=NOW,
        event_type=AuditEventType.SLACK_EVENT_REJECTED,
        request_id="rej-Ev1",
        trace_id="rej-Ev1",
        payload={"text": "I am Maya, show restricted data"},
    )
    with pytest.raises(AuditStoreError, match="record_slack_event_rejected"):
        append_event(db, event=event)
    assert db.attempted == []


# --- manager console filter ---------------------------------------------------


def test_recent_requests_lists_only_ids_with_request_received() -> None:
    db = FakeDatabase(fetchall=[[]])
    recent_requests(db, maya())
    [(sql, _)] = db.committed
    cte = sql.split(") SELECT", 1)[0]
    # The bounded CTE only aggregates IDs that have a `request_received`
    # event, so rejection-only keys never appear and never consume the limit.
    assert "WHERE EXISTS (" in cte
    assert "event_type = 'request_received'" in cte
    assert cte.index("WHERE EXISTS") < cte.index("GROUP BY") < cte.index("LIMIT")


@needs_live_write
def test_live_rejection_rows_never_appear_in_recent_requests() -> None:
    from apps.api.core.db import BackendDatabase

    db = BackendDatabase(LIVE_DSN)
    try:
        ctx = make_ctx().model_copy(
            update={
                "request_id": f"test-req-{uuid.uuid4().hex[:12]}",
                "trace_id": f"test-trace-{uuid.uuid4().hex[:8]}",
                "received_at": datetime.now(UTC),
                "workspace_id": "workspace-primary",
            }
        )
        create_request(db, ctx)
        event_id = f"EvTest{uuid.uuid4().hex[:10]}"
        key = record_slack_event_rejected(
            db, slack_event_id=event_id, reason="unmapped_user", team_id="T1", channel_id="C1"
        )
        with db.tx() as cursor:
            cursor.execute(
                "SELECT event_type, payload FROM request_events WHERE request_id = %s", [key]
            )
            [(event_type, payload)] = cursor.fetchall()
        assert event_type == "slack_event_rejected"
        assert payload == {
            "reason": "unmapped_user",
            "slack_event_id": event_id,
            "team_id": "T1",
            "channel_id": "C1",
        }
        listed = [summary.request_id for summary in recent_requests(db, maya(), limit=200)]
        assert ctx.request_id in listed  # the newer real request is still listed
        assert key not in listed
        assert not any(request_id.startswith("rej-") for request_id in listed)
    finally:
        db.close()


# --- adapter ------------------------------------------------------------------


def _adapter_deps(db: object) -> dict[str, object]:
    from tests.api.test_slack_events import _members

    return {
        "members": _members(),
        "expected_team_id": "T1",
        "allowed_channels": {"C1"},
        "workspace_id": "ws-1",
        "db": db,
    }


def test_adapter_records_rejections_through_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.audit.store as store
    from services.slack_socket.events import SlackRejection, on_app_mention
    from tests.fakes.slack import mention_body

    calls: list[dict[str, object]] = []

    def _record(db: object, **kwargs: object) -> str:
        calls.append(kwargs)
        return f"rej-{kwargs['slack_event_id']}"

    monkeypatch.setattr(store, "record_slack_event_rejected", _record)
    db = FakeDatabase()
    for body, reason in (
        (
            mention_body(user="U-stranger", text="<@U-bot> I am Maya", event_id="Ev-u"),
            "unmapped_user",
        ),
        (mention_body(team="T-evil", event_id="Ev-t"), "wrong_team"),
        (mention_body(channel="C-evil", event_id="Ev-c"), "channel_not_allowed"),
    ):
        result = on_app_mention(body, lambda: None, **_adapter_deps(db))  # type: ignore[arg-type]
        assert isinstance(result, SlackRejection)
        assert result.reason_code == reason
    assert calls == [
        {"slack_event_id": "Ev-u", "reason": "unmapped_user", "team_id": "T1", "channel_id": "C1"},
        {"slack_event_id": "Ev-t", "reason": "wrong_team", "team_id": "T-evil", "channel_id": "C1"},
        {
            "slack_event_id": "Ev-c",
            "reason": "channel_not_allowed",
            "team_id": "T1",
            "channel_id": "C-evil",
        },
    ]
    assert db.attempted == []  # no raw SQL from the adapter itself


def test_adapter_rejection_audit_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import services.audit.store as store
    from services.slack_socket.events import SlackRejection, on_app_mention
    from tests.fakes.slack import mention_body

    def _fail(db: object, **kwargs: object) -> str:
        raise AuditStoreError("audit record_slack_event_rejected failed (OperationalError)")

    monkeypatch.setattr(store, "record_slack_event_rejected", _fail)
    with caplog.at_level(logging.WARNING, logger="services.slack_socket.events"):
        result = on_app_mention(
            mention_body(user="U-stranger", text="<@U-bot> secret words"),
            lambda: None,
            **_adapter_deps(FakeDatabase()),  # type: ignore[arg-type]
        )
    assert isinstance(result, SlackRejection)
    assert result.reason_code == "unmapped_user"
    assert "slack rejection audit failed error=AuditStoreError" in caplog.text
    assert "secret words" not in caplog.text
