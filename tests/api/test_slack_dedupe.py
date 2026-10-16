"""Slack event deduplication (P3-05 §11).

Offline: a fake backend with set-semantics UNIQUE enforcement (mirrors the
`slack_events.slack_event_id` UNIQUE constraint), including real threads for
the concurrency case. Live DB concurrency is pending and never claimed.
"""

import threading

import pytest

from apps.api.identity import MemberRecord
from services.audit.errors import AuditStoreError
from services.contracts import AppRole, PersonaKey
from services.slack_socket.events import (
    IncomingSlackEvent,
    SlackRejection,
    mark_processed,
    on_app_mention,
)


class UniqueCursor:
    """Emulates `ON CONFLICT DO NOTHING RETURNING`: first insert wins."""

    def __init__(self, seen: set[str], lock: threading.Lock, fail: bool) -> None:
        self._seen = seen
        self._lock = lock
        self._fail = fail
        self.statements: list[tuple[str, object]] = []
        self.rowcount = 1

    def execute(self, query: str, params: object = None) -> None:
        self.statements.append((query, params))
        if self._fail:
            raise AuditStoreError("fake database down")

    def fetchone(self) -> object:
        return None  # replaced per-statement below

    def fetchall(self) -> list[object]:
        return []


class UniqueDb:
    """Set-backed fake: concurrent duplicate inserts collapse to one winner."""

    def __init__(self, fail: bool = False) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self.fail = fail
        self.inserts = 0

    def tx(self):  # type: ignore[no-untyped-def]
        from contextlib import contextmanager

        db = self

        @contextmanager
        def _tx():  # type: ignore[no-untyped-def]
            cursor = UniqueCursor(db._seen, db._lock, db.fail)

            original_fetchone = cursor.fetchone

            def _fetchone() -> object:
                last_sql, last_params = cursor.statements[-1]
                if "ON CONFLICT" in last_sql and isinstance(last_params, list):
                    event_id = str(last_params[0])
                    with db._lock:
                        if event_id in db._seen:
                            return None
                        db._seen.add(event_id)
                        db.inserts += 1
                        return (1,)
                return original_fetchone()

            cursor.fetchone = _fetchone  # type: ignore[method-assign]
            yield cursor

        return _tx()


def _members() -> dict[PersonaKey, MemberRecord]:
    return {
        PersonaKey.SAM: MemberRecord(
            persona=PersonaKey.SAM,
            member_id="m-sam",
            display_name="Sam",
            app_role=AppRole.SENIOR,
            email="sam@example.com",
            email_ref="PERSONA_SAM_EMAIL",
            slack_user_id="U-sam",
        )
    }


def _deps(db, **overrides):  # type: ignore[no-untyped-def]
    args = {
        "members": _members(),
        "expected_team_id": "T1",
        "allowed_channels": {"C1"},
        "workspace_id": "ws-1",
        "db": db,
    }
    args.update(overrides)
    return args


def _mention(event_id: str = "Ev-dup") -> dict[str, object]:
    return {
        "team_id": "T1",
        "event": {
            "type": "app_mention",
            "user": "U-sam",
            "text": "<@U-bot> atlas budget?",
            "ts": "1700000000.0002",
            "channel": "C1",
            "event_id": event_id,
        },
    }


def test__offline_fake_slack_duplicate_event_id_dropped() -> None:
    db = UniqueDb()
    first = on_app_mention(_mention(), lambda: None, **_deps(db))
    second = on_app_mention(_mention(), lambda: None, **_deps(db))
    assert isinstance(first, IncomingSlackEvent)
    assert isinstance(second, SlackRejection)
    assert second.reason_code == "duplicate"
    assert db.inserts == 1


def test__offline_fake_slack_distinct_ids_both_processed() -> None:
    db = UniqueDb()
    first = on_app_mention(_mention("Ev-a"), lambda: None, **_deps(db))
    second = on_app_mention(_mention("Ev-b"), lambda: None, **_deps(db))
    assert isinstance(first, IncomingSlackEvent)
    assert isinstance(second, IncomingSlackEvent)
    assert first.request_id != second.request_id
    assert db.inserts == 2


def test__offline_fake_slack_concurrent_duplicates_processed_once() -> None:
    db = UniqueDb()
    outcomes: list[object] = []

    def _send() -> None:
        outcomes.append(on_app_mention(_mention(), lambda: None, **_deps(db)))

    threads = [threading.Thread(target=_send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    accepted = [o for o in outcomes if isinstance(o, IncomingSlackEvent)]
    dropped = [o for o in outcomes if isinstance(o, SlackRejection)]
    assert len(accepted) == 1
    assert len(dropped) == 7
    assert all(o.reason_code == "duplicate" for o in dropped)
    assert db.inserts == 1


def test__offline_fake_slack_dedupe_failure_fails_closed() -> None:
    db = UniqueDb(fail=True)
    result = on_app_mention(_mention(), lambda: None, **_deps(db))
    assert isinstance(result, SlackRejection)
    assert result.reason_code == "db_unavailable"


def test__offline_fake_slack_mark_processed_updates_row() -> None:
    db = UniqueDb()
    on_app_mention(_mention(), lambda: None, **_deps(db))
    mark_processed(db, "Ev-dup")
    assert db.inserts == 1
    # A second identical event still drops.
    again = on_app_mention(_mention(), lambda: None, **_deps(db))
    assert isinstance(again, SlackRejection)


def test__offline_fake_slack_audit_row_for_dropped_duplicate() -> None:
    db = UniqueDb()
    on_app_mention(_mention(), lambda: None, **_deps(db))
    # UniqueDb only models slack_events; rejection audit needs a recording db.
    seen: list[tuple[str, object]] = []

    class _RecordingDb(UniqueDb):
        def tx(self):  # type: ignore[no-untyped-def]
            from contextlib import contextmanager

            parent = super().tx()

            @contextmanager
            def _tx():  # type: ignore[no-untyped-def]
                with parent as cursor:
                    original = cursor.execute

                    def _record(query: str, params: object = None) -> None:
                        seen.append((query, params))
                        original(query, params)

                    cursor.execute = _record  # type: ignore[method-assign]
                    yield cursor

            return _tx()

    recording = _RecordingDb()
    recording._seen.add("Ev-dup")
    result = on_app_mention(_mention(), lambda: None, **_deps(recording))
    assert isinstance(result, SlackRejection)
    assert any("slack_event_rejected" in sql for sql, _ in seen)


def test__offline_fake_slack_no_db_means_no_processing() -> None:
    result = on_app_mention(_mention(), lambda: None, **_deps(None))
    assert isinstance(result, SlackRejection)
    assert result.reason_code == "db_unavailable"


@pytest.mark.parametrize("event_id", ["Ev-a", "Ev-b", "Ev-c"])
def test__offline_fake_slack_repeated_distinct_ids(event_id: str) -> None:
    db = UniqueDb()
    result = on_app_mention(_mention(event_id), lambda: None, **_deps(db))
    assert isinstance(result, IncomingSlackEvent)
