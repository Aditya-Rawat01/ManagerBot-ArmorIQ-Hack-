"""Topic-map tier selection (P3-01 backend reads, P3-02 matching).

Offline: test-local fake database, no DSN or network. The live test at the
end needs a LOCAL migrated and seeded database (`DATABASE_URL_BACKEND` +
`PROXYLEAD_DB_WRITE_TESTS=1`). Regression tests for the PR #69 review
findings live in `tests/api/test_planner_followups.py`.
"""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from apps.api.topic_map import (
    TopicMatch,
    list_rules,
    match,
    normalize_topic,
    upsert_rule,
)
from services.audit.errors import AuditNotAuthorizedError, AuditStoreError
from services.contracts import (
    AppRole,
    AuthenticatedUser,
    DataTier,
    PersonaKey,
)


class FakeCursor:
    def __init__(self, rows=None, one=None) -> None:
        self.statements: list[tuple[str, object]] = []
        self._rows = list(rows or [])
        self._one = list(one or [])

    def execute(self, query: str, params: object = None) -> None:
        self.statements.append((query, params))

    def fetchone(self) -> object:
        return self._one.pop(0) if self._one else None

    def fetchall(self) -> list[object]:
        return self._rows


class FakeDb:
    """Test-local stand-in for the backend connection (no database)."""

    def __init__(self, rows=None, one=None, fail: bool = False) -> None:
        self._rows = rows
        self._one = one
        self.fail = fail
        self.cursors: list[FakeCursor] = []

    def __enter_tx(self) -> FakeCursor:
        if self.fail:
            raise ConnectionError("fake database down")
        cursor = FakeCursor(rows=self._rows, one=self._one)
        self.cursors.append(cursor)
        return cursor

    @contextmanager
    def tx(self) -> Iterator[FakeCursor]:
        yield self.__enter_tx()


def _maya() -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id="m-maya",
        persona=PersonaKey.MAYA,
        display_name="Maya",
        app_role=AppRole.MANAGER,
        masked_email="m***@example.com",
    )


def _sam() -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id="m-sam",
        persona=PersonaKey.SAM,
        display_name="Sam",
        app_role=AppRole.SENIOR,
        masked_email="s***@example.com",
    )


def _rules_db() -> FakeDb:
    # Shape for list_rules (id, workspace_id, topic, max_tier).
    return FakeDb(
        rows=[
            ("rule-1", "ws-1", "atlas budget", "confidential"),
            ("rule-2", "ws-1", "atlas budget review", "restricted"),
            ("rule-3", "ws-1", "hiring", "team"),
        ]
    )


def _match_rules_db() -> FakeDb:
    # Shape for match (id, topic, max_tier).
    return FakeDb(
        rows=[
            ("rule-1", "atlas budget", "confidential"),
            ("rule-2", "atlas budget review", "restricted"),
            ("rule-3", "hiring", "team"),
        ]
    )


# --- normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Atlas Budget! ", "atlas budget"),
        ("HIRING\tplans\n2026", "hiring plans 2026"),
        ("Q3-budget (review)", "q3 budget review"),
        ("", ""),
    ],
)
def test_normalize_topic(raw: str, expected: str) -> None:
    assert normalize_topic(raw) == expected


# --- matching ---------------------------------------------------------------


def test_match_substring_and_most_specific_wins() -> None:
    matched = match(_match_rules_db(), "ws-1", "What was decided in the atlas budget review?")
    assert isinstance(matched, TopicMatch)
    assert matched.rule_id == "rule-2"  # longest covering topic wins
    assert matched.max_tier is DataTier.RESTRICTED


def test_match_token_coverage_without_exact_substring() -> None:
    matched = match(_match_rules_db(), "ws-1", "hiring pipeline update")
    assert matched is not None
    assert matched.rule_id == "rule-3"


def test_no_match_returns_none() -> None:
    assert match(_match_rules_db(), "ws-1", "what is the lunch menu?") is None


def test_match_short_tokens_still_match_as_whole_tokens() -> None:
    db = FakeDb(rows=[("rule-q4", "q4 pricing", "confidential")])
    found = match(db, "ws-1", "Where is the Q4-pricing deck?")
    assert found is not None and found.rule_id == "rule-q4"


def test_match_database_error_is_team_only() -> None:
    assert match(FakeDb(fail=True), "ws-1", "atlas budget review") is None


def test_match_scopes_by_workspace() -> None:
    db = _match_rules_db()
    match(db, "ws-9", "atlas budget")
    _, params = db.cursors[0].statements[0]
    assert params == ["ws-9"]


def test_match_skips_corrupt_rows() -> None:
    db = FakeDb(
        rows=[
            ("rule-x", "atlas budget", "topsecret"),
            ("rule-1", "hiring", "team"),
        ]
    )
    matched = match(db, "ws-1", "hiring sync")
    assert matched is not None and matched.rule_id == "rule-1"


def test_match_uses_parameterized_query() -> None:
    db = _match_rules_db()
    match(db, "ws-1", "Robert'); DROP TABLE topic_access_rules;--")
    sql, _ = db.cursors[0].statements[0]
    assert "%s" in sql
    assert "DROP TABLE" not in sql


# --- rules never grant access by themselves ----------------------------------


def test_match_result_is_scope_advice_not_authorization() -> None:
    matched = match(_match_rules_db(), "ws-1", "atlas budget review")
    assert matched is not None
    # A match carries no decision, source, or execution proof — the planner,
    # ArmorIQ, and the tool server still decide independently.
    assert not hasattr(matched, "decision")
    assert not hasattr(matched, "allowed")


# --- list / upsert (Maya-only) ----------------------------------------------


def test_list_rules_maya_only() -> None:
    db = _rules_db()
    rules = list_rules(db, "ws-1", _maya())
    assert [rule.id for rule in rules] == ["rule-1", "rule-2", "rule-3"]
    assert rules[0].max_tier is DataTier.CONFIDENTIAL
    with pytest.raises(AuditNotAuthorizedError):
        list_rules(FakeDb(), "ws-1", _sam())


def test_upsert_rule_maya_only_and_normalizes() -> None:
    db = FakeDb(one=[("rule-9",)])
    rule_id = upsert_rule(
        db,
        actor=_maya(),
        workspace_id="ws-1",
        topic="  Atlas Budget! ",
        max_tier=DataTier.CONFIDENTIAL,
    )
    assert rule_id == "rule-9"
    sql, params = db.cursors[0].statements[0]
    assert "ON CONFLICT (workspace_id, topic)" in sql
    assert params == ["ws-1", "atlas budget", "confidential", "m-maya"]
    with pytest.raises(AuditNotAuthorizedError):
        upsert_rule(
            FakeDb(),
            actor=_sam(),
            workspace_id="ws-1",
            topic="hiring",
            max_tier=DataTier.TEAM,
        )


def test_upsert_rule_rejects_empty_or_overlong_topic() -> None:
    with pytest.raises(AuditStoreError):
        upsert_rule(
            FakeDb(),
            actor=_maya(),
            workspace_id="ws-1",
            topic="   ",
            max_tier=DataTier.TEAM,
        )
    with pytest.raises(AuditStoreError):
        upsert_rule(
            FakeDb(),
            actor=_maya(),
            workspace_id="ws-1",
            topic="x" * 201,
            max_tier=DataTier.TEAM,
        )


def test_list_rules_rejects_corrupt_tier() -> None:
    db = FakeDb(rows=[("rule-x", "ws-1", "hiring", "topsecret")])
    with pytest.raises(AuditStoreError):
        list_rules(db, "ws-1", _maya())


# --- live (local database only) ----------------------------------------------

# Captured at import: the API conftest scrubs settings variables per test.
LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
DB_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
needs_live_write = pytest.mark.skipif(
    not (LIVE_DSN and DB_WRITE_TESTS),
    reason="needs DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1 (local database only)",
)


@needs_live_write
def test_live_topic_rules_round_trip_under_backend_role() -> None:
    """upsert / list / match through the backend login against a migrated,
    seeded LOCAL database (workspace from the P2-03 seed). Cleans up its row."""
    from apps.api.core.db import BackendDatabase

    workspace = "workspace-primary"
    marker = f"zq{uuid.uuid4().hex[:10]}"
    topic = f"Livetopic {marker}"
    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    rule_id: str | None = None
    try:
        rule_id = upsert_rule(
            db, actor=_maya(), workspace_id=workspace, topic=topic, max_tier=DataTier.CONFIDENTIAL
        )
        again = upsert_rule(
            db, actor=_maya(), workspace_id=workspace, topic=topic, max_tier=DataTier.RESTRICTED
        )
        assert again == rule_id  # same (workspace, normalized topic) row
        listed = {rule.id: rule for rule in list_rules(db, workspace, _maya())}
        assert listed[rule_id].topic == f"livetopic {marker}"
        assert listed[rule_id].max_tier is DataTier.RESTRICTED
        with db.tx() as cursor:
            cursor.execute(
                "SELECT created_by_member_id FROM topic_access_rules WHERE id = %s", [rule_id]
            )
            assert cursor.fetchone() == ("m-maya",)
        found = match(db, workspace, f"what about the LIVETOPIC {marker} numbers?")
        assert found is not None and found.rule_id == rule_id
        assert match(db, workspace, f"livetopic {marker[:-1]}") is None  # whole tokens only
        with pytest.raises(AuditStoreError):
            upsert_rule(
                db, actor=_maya(), workspace_id=workspace, topic="ai", max_tier=DataTier.TEAM
            )
        with pytest.raises(AuditNotAuthorizedError):
            list_rules(db, workspace, _sam())
    finally:
        if rule_id is not None:
            with db.tx() as cursor:
                cursor.execute("DELETE FROM topic_access_rules WHERE id = %s", [rule_id])
        db.close()
