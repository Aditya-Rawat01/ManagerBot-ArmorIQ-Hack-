"""P3-01 follow-ups: review findings on the merged audit store (PR #60).

Offline by default (fakes in `tests/api/audit_fakes.py`); names added by the
follow-up are imported inside each test so every test fails on its own on
the pre-follow-up code. The live tests at the end need a LOCAL migrated and
seeded database: `DATABASE_URL_BACKEND` (+ `DATABASE_URL_TOOLSERVER` for the
role guard) and, for writes, `PROXYLEAD_DB_WRITE_TESTS=1`. DSNs are captured
at import (the API conftest scrubs settings variables) and never printed.
"""

import ast
import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg_pool
import pytest
from psycopg import errors as pg_errors

from apps.api.core.db import BackendDatabase, BackendDbNotConfiguredError
from services.audit import (
    AuditNotFoundError,
    AuditStoreError,
    AuditTransitionError,
)
from services.audit.queries import pending_holds, recent_requests, timeline, unanswered
from services.audit.store import (
    add_unanswered,
    append_event,
    create_request,
    record_answer,
    record_approval,
    record_delivery,
    record_execution_ref,
    record_plan,
    record_step_decision,
)
from services.contracts import (
    ApprovalState,
    ApprovalStatus,
    AuditEvent,
    AuditEventType,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    McpEndpoint,
    PlannedToolCall,
    PlanProposal,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)
from tests.api.audit_fakes import (
    NOW,
    FakeDatabase,
    Outcome,
    all_sql,
    make_approval,
    make_citation,
    make_ctx,
    make_decision,
    make_plan,
    maya,
    origin_payload,
    statements_with,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PLAN_ID = "p-0123456789abcdef"


def _params_of(db: FakeDatabase, fragment: str) -> list[object]:
    matches = statements_with(db, fragment)
    assert matches, f"no statement containing {fragment!r}"
    params = matches[0][1]
    assert isinstance(params, list)
    return params


def _payloads(db: FakeDatabase, fragment: str) -> list[dict[str, object]]:
    return [
        value
        for _, params in statements_with(db, fragment)
        for value in (params or [])  # type: ignore[union-attr]
        if isinstance(value, dict)
    ]


# --- 1. caller-supplied plan_id (gateway PlanHandle.plan_id) --------------------


def test_record_plan_accepts_gateway_plan_id() -> None:
    db = FakeDatabase(fetchone=[None])
    plan_id = record_plan(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan=make_plan(),
        plan_id=GATEWAY_PLAN_ID,
    )
    assert plan_id == GATEWAY_PLAN_ID
    assert _params_of(db, "INSERT INTO plans")[0] == GATEWAY_PLAN_ID
    assert _params_of(db, "INSERT INTO plan_steps")[0] == GATEWAY_PLAN_ID
    assert GATEWAY_PLAN_ID in _params_of(db, "plan_proposed")


@pytest.mark.parametrize(
    "bad", ["", " p-1", "p 1", "p-1\n", "x" * 129, "p-1;DROP", "../plan", "-leading"]
)
def test_record_plan_rejects_malformed_plan_id(bad: str) -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        record_plan(db, request_id="req-1", trace_id="trace-1", plan=make_plan(), plan_id=bad)
    assert db.attempted == []  # validated before any SQL


def test_record_plan_rejects_already_recorded_plan_id() -> None:
    db = FakeDatabase(fetchone=[("req-other",)])
    with pytest.raises(AuditTransitionError):
        record_plan(
            db, request_id="req-1", trace_id="trace-1", plan=make_plan(), plan_id=GATEWAY_PLAN_ID
        )
    assert not any(sql.startswith("INSERT") for sql, _ in db.attempted)


# --- 2. every failure is an AuditStoreError (no DSN / params in messages) -------

_SECRET_PARAM = "hunter2-secret-param"


def _driver_error() -> Exception:
    return pg_errors.UniqueViolation(f"duplicate key value (id)=({_SECRET_PARAM})")


_OPERATIONS = {
    "create_request": lambda db: create_request(db, make_ctx()),
    "record_plan": lambda db: record_plan(
        db, request_id="req-1", trace_id="trace-1", plan=make_plan()
    ),
    "record_step_decision": lambda db: record_step_decision(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan_id="plan-1",
        step_index=0,
        outcome=Outcome(make_decision()),
    ),
    "record_approval": lambda db: record_approval(
        db, request_id="req-1", trace_id="trace-1", approval=make_approval()
    ),
    "record_execution_ref": lambda db: record_execution_ref(
        db,
        request_id="req-1",
        trace_id="trace-1",
        jti="jti-1",
        ledger_entry_id=None,
        status=ToolExecutionStatus.ERROR,
    ),
    "record_answer": lambda db: record_answer(
        db,
        request_id="req-1",
        trace_id="trace-1",
        citations=[],
        highest_tier=DataTier.TEAM,
        abstained=False,
        reason=None,
    ),
    "record_delivery": lambda db: record_delivery(
        db,
        request_id="req-1",
        trace_id="trace-1",
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id="m-sam",
        tier_delivered=DataTier.TEAM,
        status="delivered",
    ),
    "add_unanswered": lambda db: add_unanswered(
        db,
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester_member_id="m-sam",
        question="q",
        reason="no_evidence",
    ),
    "append_event": lambda db: append_event(
        db,
        event=AuditEvent(
            event_id="ev-1",
            occurred_at=NOW,
            event_type=AuditEventType.PLAN_REJECTED,
            request_id="req-1",
            trace_id="trace-1",
        ),
    ),
    "recent_requests": lambda db: recent_requests(db, maya()),
    "timeline": lambda db: timeline(db, "req-1", maya()),
    "pending_holds": lambda db: pending_holds(db, maya()),
    "unanswered": lambda db: unanswered(db, maya()),
}


@pytest.mark.parametrize("operation", sorted(_OPERATIONS))
def test_driver_errors_surface_as_audit_store_error(operation: str) -> None:
    db = FakeDatabase(fail_with=_driver_error())
    with pytest.raises(AuditStoreError) as raised:
        _OPERATIONS[operation](db)
    assert isinstance(raised.value.__cause__, pg_errors.UniqueViolation)
    assert _SECRET_PARAM not in str(raised.value)
    assert db.committed == []


def test_typed_audit_errors_pass_through_unchanged() -> None:
    db = FakeDatabase(rowcount=0, fetchone=[None])
    with pytest.raises(AuditNotFoundError):
        record_plan(db, request_id="req-9", trace_id="trace-9", plan=make_plan())


class _FailingPool:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def connection(self) -> object:  # mimics psycopg_pool's context manager
        raise self._exc

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: psycopg_pool.PoolTimeout("couldn't get a connection after 30 sec"),
        lambda: psycopg_pool.PoolClosed("the pool is closed"),
        lambda: pg_errors.OperationalError(
            "connection to postgresql://backend:hunter2@db.internal/proxylead failed"
        ),
    ],
)
def test_backend_database_wraps_pool_and_driver_errors(exc_factory) -> None:
    db = BackendDatabase("postgresql://backend:hunter2@db.invalid/proxylead")
    db.open = lambda: None  # type: ignore[method-assign] - skip the network probe
    db._pool = _FailingPool(exc_factory())  # type: ignore[assignment]
    with pytest.raises(AuditStoreError) as raised, db.tx():
        pass
    assert "hunter2" not in str(raised.value)
    assert "db.invalid" not in str(raised.value)
    assert "hunter2" not in repr(db)


def test_backend_not_configured_is_an_audit_store_error() -> None:
    assert issubclass(BackendDbNotConfiguredError, AuditStoreError)
    with pytest.raises(AuditStoreError):
        BackendDatabase("   ")


# --- 3. DSN guard: only DATABASE_URL_BACKEND, only a pure backend login ----------


def test_from_settings_reads_only_the_backend_dsn() -> None:
    from tests.api.conftest import make_settings

    with pytest.raises(BackendDbNotConfiguredError):
        BackendDatabase.from_settings(make_settings())
    dsn = "postgresql://backend-login@db.invalid/proxylead"
    db = BackendDatabase.from_settings(make_settings(database_url_backend=dsn))
    assert db._pool.conninfo == dsn
    assert db._opened is False  # nothing connected at construction


class _RoleCursor:
    def __init__(self, row: object) -> None:
        self._row = row
        self.executed: list[tuple[str, object]] = []

    def __enter__(self) -> "_RoleCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> object:
        return self._row


class _RoleConnection:
    def __init__(self, row: object) -> None:
        self.cursor_obj = _RoleCursor(row)
        self.rolled_back = 0

    def cursor(self) -> _RoleCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        self.rolled_back += 1


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ((False, False, False, False), "is not a member of proxylead_backend"),
        ((True, True, False, False), "is a member of proxylead_toolserver"),
        ((True, False, True, False), "is a superuser"),
        ((True, False, False, True), "owns the public schema"),
        ((False, True, True, True), "is a superuser"),
        (None, "could not be resolved"),
    ],
)
def test_role_check_refuses_non_backend_logins(row: object, problem: str) -> None:
    from apps.api.core.db import BackendRoleError, verify_backend_role

    connection = _RoleConnection(row)
    with pytest.raises(BackendRoleError) as raised:
        verify_backend_role(connection)  # type: ignore[arg-type]
    assert problem in str(raised.value)
    assert connection.rolled_back == 1  # left idle for the pool
    assert isinstance(raised.value, AuditStoreError)


def test_role_check_accepts_backend_login() -> None:
    from apps.api.core.db import verify_backend_role

    connection = _RoleConnection((True, False, False, False))
    verify_backend_role(connection)  # type: ignore[arg-type]
    sql, params = connection.cursor_obj.executed[0]
    assert params == ("proxylead_backend", "proxylead_toolserver")
    assert "rolsuper" in sql and "relowner" in sql and "nspowner" in sql
    assert connection.rolled_back == 1


def test_pool_runs_role_check_and_disables_prepared_statements() -> None:
    from apps.api.core import db as db_module

    db = BackendDatabase("postgresql://backend-login@db.invalid/proxylead")
    assert db._pool.kwargs.get("prepare_threshold", "unset") is None
    assert db._pool._configure is db_module._configure

    calls: list[str] = []
    original = db_module.verify_backend_role
    db_module.verify_backend_role = lambda connection: calls.append("checked")  # type: ignore[assignment]
    try:

        class _Adapters:
            def register_dumper(self, *args: object) -> None:
                calls.append("dumper")

        class _Connection:
            adapters = _Adapters()

        db_module._configure(_Connection())  # type: ignore[arg-type]
    finally:
        db_module.verify_backend_role = original
    assert calls == ["checked", "dumper"]


# --- 4. redaction gaps: see tests/api/test_audit_redaction.py --------------------

# --- 5. append_event keeps the typed writers' evidence rules -------------------


@pytest.mark.parametrize(
    "event",
    [
        AuditEvent(
            event_id="ev-x",
            occurred_at=NOW,
            event_type=AuditEventType.TOOL_EXECUTED,
            request_id="req-1",
            trace_id="trace-1",
            jti="jti-1",
            payload={"execution_status": "executed"},  # no ledger_entry_id
        ),
        AuditEvent(
            event_id="ev-x",
            occurred_at=NOW,
            event_type=AuditEventType.TOOL_EXECUTED,
            request_id="req-1",
            trace_id="trace-1",
            payload={"ledger_entry_id": "ledger-1"},  # no jti
        ),
        AuditEvent(
            event_id="ev-x",
            occurred_at=NOW,
            event_type=AuditEventType.TOOL_EXECUTED,
            request_id="req-1",
            trace_id="trace-1",
            jti="jti-1",
            payload={"ledger_entry_id": "ledger-1", "execution_status": "not_reached"},
        ),
        AuditEvent(
            event_id="ev-x",
            occurred_at=NOW,
            event_type=AuditEventType.TOOL_REJECTED,
            request_id="req-1",
            trace_id="trace-1",
        ),
        AuditEvent(
            event_id="ev-x",
            occurred_at=NOW,
            event_type=AuditEventType.AUTHORIZATION_DECIDED,
            request_id="req-1",
            trace_id="trace-1",
            payload={"outcome": "allow"},  # no decision_source
        ),
        AuditEvent(
            event_id="ev-x",
            occurred_at=NOW,
            event_type=AuditEventType.REQUEST_RECEIVED,
            request_id="req-1",
            trace_id="trace-1",
            payload={"requester_member_id": "m-maya", "workspace_id": "ws-1"},
        ),
    ],
    ids=[
        "executed-without-ledger",
        "executed-without-jti",
        "executed-claiming-not-reached",
        "rejected-without-jti",
        "decision-without-source",
        "forged-request-identity",
    ],
)
def test_append_event_enforces_typed_writer_evidence_rules(event: AuditEvent) -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        append_event(db, event=event)
    assert db.attempted == []


def test_append_event_accepts_evidenced_tool_execution_and_off_plan_decision() -> None:
    db = FakeDatabase()
    append_event(
        db,
        event=AuditEvent(
            event_id="ev-1",
            occurred_at=NOW,
            event_type=AuditEventType.TOOL_EXECUTED,
            request_id="req-1",
            trace_id="trace-1",
            jti="jti-1",
            payload={"ledger_entry_id": "ledger-1"},
        ),
    )
    append_event(
        db,
        event=AuditEvent(
            event_id="ev-2",
            occurred_at=NOW,
            event_type=AuditEventType.AUTHORIZATION_DECIDED,
            request_id="req-1",
            trace_id="trace-1",
            decision_source=DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
            payload={"tool_name": "send_external_message", "outcome": "block"},
        ),
    )
    assert len(statements_with(db, "INSERT INTO decision_events")) == 2


# --- 6. PRD §10.1 field groups persisted (and read back below / live) -----------


def test_create_request_records_slack_thread_ts() -> None:
    db = FakeDatabase(fetchone=[None])
    create_request(db, make_ctx().model_copy(update={"slack_thread_ts": "1700000000.000100"}))
    [payload] = _payloads(db, "INSERT INTO request_events")
    assert payload["slack_thread_ts"] == "1700000000.000100"


def _topic_plan() -> PlanProposal:
    return make_plan().model_copy(
        update={"matched_topic_rule_id": "topic-7", "escalation_user_confirmed": True}
    )


def test_record_plan_honours_plan_topic_rule_and_escalation() -> None:
    db = FakeDatabase()
    record_plan(db, request_id="req-1", trace_id="trace-1", plan=_topic_plan())
    [payload] = _payloads(db, "plan_proposed")
    assert payload["matched_topic_rule_id"] == "topic-7"
    assert payload["escalation_confirmed"] is True
    assert payload["tools"] == ["knowledge_search_team"]


def test_record_plan_explicit_args_win_and_token_expiry_recorded() -> None:
    db = FakeDatabase()
    expires = NOW + timedelta(minutes=20)
    record_plan(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan=_topic_plan(),
        matched_topic_rule_id="topic-9",
        escalation_confirmed=False,
        chosen_tier=DataTier.CONFIDENTIAL,
        armoriq_plan_id="aplan-1",
        token_expires_at=expires,
    )
    [payload] = _payloads(db, "plan_proposed")
    assert payload["matched_topic_rule_id"] == "topic-9"
    assert payload["escalation_confirmed"] is False
    assert payload["chosen_tier"] == "confidential"
    assert payload["armoriq_plan_id"] == "aplan-1"
    assert payload["intent_expires_at"] == expires.isoformat()


def test_execution_ref_records_reached_validation_and_result_ids() -> None:
    db = FakeDatabase()
    record_execution_ref(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan_id="plan-1",
        step_index=0,
        jti="jti-1",
        ledger_entry_id=None,
        status=ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
        tool_server_reached=True,
        validation_outcome="rejected:schema",
        result_id="result-7",
        error_class="SchemaError",
    )
    [payload] = _payloads(db, "INSERT INTO decision_events")
    assert payload["tool_server_reached"] is True
    assert payload["validation_outcome"] == "rejected:schema"
    assert payload["result_id"] == "result-7"
    assert payload["execution_status"] == "rejected_by_tool_server"


@pytest.mark.parametrize(
    ("status", "reached", "ledger"),
    [
        (ToolExecutionStatus.EXECUTED, False, "ledger-1"),
        (ToolExecutionStatus.NOT_REACHED, True, None),
        (ToolExecutionStatus.NOT_REACHED, None, "ledger-1"),
    ],
)
def test_execution_ref_rejects_inconsistent_reach(
    status: ToolExecutionStatus, reached: bool | None, ledger: str | None
) -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        record_execution_ref(
            db,
            request_id="req-1",
            trace_id="trace-1",
            jti="jti-1",
            ledger_entry_id=ledger,
            status=status,
            tool_server_reached=reached,
        )
    assert db.attempted == []


def test_step_decision_records_decision_delegation_id() -> None:
    db = FakeDatabase()
    record_step_decision(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan_id="plan-1",
        step_index=0,
        outcome=Outcome(make_decision(delegation_id="dlg-7")),
    )
    params = _params_of(db, "INSERT INTO decision_events")
    assert params[6] == "dlg-7"  # delegation_id column
    [payload] = _payloads(db, "INSERT INTO decision_events")
    assert payload["delegation_id"] == "dlg-7"


def test_step_decision_rejects_conflicting_delegation_ids() -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        record_step_decision(
            db,
            request_id="req-1",
            trace_id="trace-1",
            plan_id="plan-1",
            step_index=0,
            outcome=Outcome(
                make_decision(delegation_id="dlg-7"),
                approval=make_approval(ApprovalState.APPROVED, delegation_id="dlg-8"),
            ),
        )
    assert db.attempted == []


@pytest.mark.parametrize(
    ("source", "is_policy"),
    [
        ("armoriq_issuance", True),
        ("armoriq_policy_decision", True),
        ("armoriq_proxy_decision", True),
        ("armoriq_sdk_plan_check", False),
        ("tool_server_rule", False),
        ("application_rule", False),
        ("unknown", False),
    ],
)
def test_timeline_shows_tool_and_true_armoriq_label(source: str, is_policy: bool) -> None:
    db = FakeDatabase(
        fetchall=[
            [(NOW, "hold_created", "plan-1", 0, None, {"state": "pending"}, "read_source_team")],
            [
                (NOW, "authorization_decided", "plan-1", 0, source, {}, "knowledge_search_team"),
                (NOW, "authorization_decided", None, None, source, {"tool_name": "x_tool"}, None),
            ],
            [],
        ]
    )
    entries = timeline(db, "req-1", maya())
    request_entry, planned, off_plan = entries
    assert request_entry.tool_name == "read_source_team"
    assert request_entry.is_armoriq_policy is False  # only decisions can be
    assert planned.tool_name == "knowledge_search_team"
    assert off_plan.tool_name == "x_tool"
    assert planned.is_armoriq_policy is is_policy
    assert "JOIN plan_steps" in all_sql(db)


# --- 7. approval rows learn their delegation_id, never steal another's ---------


def test_approval_resolution_fills_missing_delegation_id() -> None:
    # No row for dlg-1 yet; the pending row was created before the delegation.
    db = FakeDatabase(fetchone=[None, (11, "pending", None, "plan-1", 0)])
    approval_id = record_approval(
        db,
        request_id="req-1",
        trace_id="trace-1",
        approval=make_approval(ApprovalState.APPROVED, delegation_id="dlg-1"),
    )
    assert approval_id == 11
    [(sql, params)] = statements_with(db, "UPDATE approval_requests")
    assert "delegation_id = COALESCE(delegation_id, %s)" in sql
    assert "dlg-1" in params  # type: ignore[operator]


def test_pending_hold_learns_its_delegation_id() -> None:
    db = FakeDatabase(fetchone=[None, (11, "pending", None, "plan-1", 0)])
    record_approval(db, request_id="req-1", trace_id="trace-1", approval=make_approval())
    [(sql, params)] = statements_with(db, "UPDATE approval_requests")
    assert "COALESCE(delegation_id, %s)" in sql
    assert params == ["dlg-1", 11]
    assert not statements_with(db, "INSERT INTO request_events")  # same state: no event


def test_approval_fallback_never_takes_another_delegations_row() -> None:
    db = FakeDatabase(fetchone=[None, (11, "pending", "dlg-1", "plan-1", 0)])
    with pytest.raises(AuditTransitionError):
        record_approval(
            db,
            request_id="req-1",
            trace_id="trace-1",
            approval=make_approval(delegation_id="dlg-2"),
        )
    assert not any(sql.startswith(("INSERT", "UPDATE")) for sql, _ in db.attempted)


def test_approval_delegation_bound_to_its_plan_step() -> None:
    db = FakeDatabase(fetchone=[(11, "pending", "dlg-1", "plan-other", 3)])
    with pytest.raises(AuditTransitionError):
        record_approval(db, request_id="req-1", trace_id="trace-1", approval=make_approval())


def test_approval_rejects_plan_of_another_request() -> None:
    # Lookups find nothing and INSERT ... SELECT finds no plan of this request.
    db = FakeDatabase(fetchone=[None, None, None])
    with pytest.raises(AuditNotFoundError):
        record_approval(db, request_id="req-1", trace_id="trace-1", approval=make_approval())
    [insert] = [sql for sql, _ in db.attempted if sql.startswith("INSERT INTO approval_requests")]
    assert "FROM plans WHERE id = %s AND request_id = %s" in insert
    assert db.committed == []


# --- 8. a failed delivery never marks the plan delivered -----------------------


@pytest.mark.parametrize(("status", "updates_plan"), [("failed", False), ("delivered", True)])
def test_failed_delivery_leaves_plan_status(status: str, updates_plan: bool) -> None:
    db = FakeDatabase(fetchone=[(7,)])
    record_delivery(
        db,
        request_id="req-1",
        trace_id="trace-1",
        channel=DeliveryChannel.SLACK_DM,
        recipient_member_id="m-sam",
        tier_delivered=DataTier.CONFIDENTIAL,
        status=status,  # type: ignore[arg-type]
        error_class="SlackApiError" if status == "failed" else None,
    )
    assert bool(statements_with(db, "UPDATE plans")) is updates_plan


# --- 9. minor findings -----------------------------------------------------------


def test_create_request_is_serialized_per_request() -> None:
    db = FakeDatabase(fetchone=[None])
    create_request(db, make_ctx())
    first_sql, first_params = db.committed[0]
    assert "pg_advisory_xact_lock" in first_sql
    assert first_params[1] == "req-1"  # type: ignore[index]


def test_create_request_replay_must_match_requester() -> None:
    db = FakeDatabase(fetchone=[(origin_payload(member_id="m-priya"),)])
    with pytest.raises(AuditStoreError):
        create_request(db, make_ctx())
    assert not any(sql.startswith("INSERT") for sql, _ in db.attempted)


def test_record_approval_is_serialized_per_plan_step() -> None:
    db = FakeDatabase(fetchone=[None, None, (11,)])
    record_approval(db, request_id="req-1", trace_id="trace-1", approval=make_approval())
    first_sql, first_params = db.committed[0]
    assert "pg_advisory_xact_lock" in first_sql
    assert first_params[1] == "plan-1:0"  # type: ignore[index]


def test_add_unanswered_validates_reason() -> None:
    db = FakeDatabase(fetchone=[(origin_payload(),), (9,)])
    with pytest.raises(AuditStoreError):
        add_unanswered(
            db,
            request_id="req-1",
            trace_id="trace-1",
            workspace_id="ws-1",
            requester_member_id="m-sam",
            question="why?",
            reason="because",  # type: ignore[arg-type]
        )
    assert db.attempted == []


@pytest.mark.parametrize(
    ("workspace_id", "requester_member_id"), [("ws-1", "m-priya"), ("ws-2", "m-sam")]
)
def test_add_unanswered_identity_comes_from_trusted_request(
    workspace_id: str, requester_member_id: str
) -> None:
    db = FakeDatabase(fetchone=[(origin_payload(),), (9,)])
    with pytest.raises(AuditStoreError):
        add_unanswered(
            db,
            request_id="req-1",
            trace_id="trace-1",
            workspace_id=workspace_id,
            requester_member_id=requester_member_id,
            question="why?",
            reason="no_evidence",
        )
    assert not any(sql.startswith("INSERT") for sql, _ in db.attempted)


def test_add_unanswered_without_request_fails_closed() -> None:
    db = FakeDatabase(fetchone=[None])
    with pytest.raises(AuditNotFoundError):
        add_unanswered(
            db,
            request_id="req-9",
            trace_id="trace-9",
            workspace_id="ws-1",
            requester_member_id="m-sam",
            question="why?",
            reason="no_evidence",
        )


def test_add_unanswered_does_not_duplicate_record_answer_abstention() -> None:
    prior = {"citations": [{"source_id": "src-1", "tier": "team"}], "reason": "no_evidence"}
    db = FakeDatabase(fetchone=[(origin_payload(),), (9,), (prior,)])
    question_id = add_unanswered(
        db,
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester_member_id="m-sam",
        question="why?",
        reason="no_evidence",
        safe_refs=["src-1"],
    )
    assert question_id == 9
    assert statements_with(db, "INSERT INTO unanswered_questions")
    assert not statements_with(db, "INSERT INTO request_events")
    # Only record_answer's abstention counts, never another question's companion.
    [(lookup, _)] = statements_with(db, "event_type = 'answer_abstained'")
    assert "unanswered_question_id' IS NULL" in lookup


@pytest.mark.parametrize(
    ("reason", "refs"), [("conflict", []), ("no_evidence", ["src-2"])], ids=["reason", "refs"]
)
def test_add_unanswered_keeps_new_reason_or_refs(reason: str, refs: list[str]) -> None:
    prior = {"citations": [{"source_id": "src-1", "tier": "team"}], "reason": "no_evidence"}
    db = FakeDatabase(fetchone=[(origin_payload(),), (9,), (prior,)])
    add_unanswered(
        db,
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester_member_id="m-sam",
        question="why?",
        reason=reason,  # type: ignore[arg-type]
        safe_refs=refs,
    )
    [payload] = _payloads(db, "INSERT INTO request_events")
    assert payload == {"unanswered_question_id": 9, "reason": reason, "safe_refs": refs}


@pytest.mark.parametrize(
    "refs", [["Atlas budget was cut by 20% after the review"], ["x" * 201], "src-1", [""]]
)
def test_add_unanswered_rejects_snippet_refs(refs: object) -> None:
    db = FakeDatabase(fetchone=[(origin_payload(),), (9,)])
    with pytest.raises(AuditStoreError):
        add_unanswered(
            db,
            request_id="req-1",
            trace_id="trace-1",
            workspace_id="ws-1",
            requester_member_id="m-sam",
            question="why?",
            reason="no_evidence",
            safe_refs=refs,  # type: ignore[arg-type]
        )


def test_unanswered_reason_keyed_by_question_id() -> None:
    db = FakeDatabase(
        fetchall=[
            [(9, "req-1", "first?", "open", NOW), (10, "req-1", "second?", "open", NOW)],
            # Companion events name their question; the later request-level
            # abstention (record_answer) must not overwrite them.
            [
                ("req-1", "9", "conflict"),
                ("req-1", "10", "source_unavailable"),
                ("req-1", None, "superseded"),
            ],
        ]
    )
    items = {item.question_id: item.reason for item in unanswered(db, maya())}
    assert items == {9: "conflict", 10: "source_unavailable"}
    assert "unanswered_question_id" in all_sql(db)


def test_unanswered_falls_back_to_request_abstention_reason() -> None:
    db = FakeDatabase(
        fetchall=[[(9, "req-1", "why?", "open", NOW)], [("req-1", None, "no_evidence")]]
    )
    [item] = unanswered(db, maya())
    assert item.reason == "no_evidence"


def test_recent_requests_one_query_and_tolerates_missing_origin() -> None:
    db = FakeDatabase(
        fetchall=[
            [
                ("req-1", "trace-1", origin_payload(), NOW),
                ("req-2", "trace-2", None, NOW),  # no request_received event
                ("req-3", "trace-3", "corrupt", NOW),
            ]
        ]
    )
    summaries = recent_requests(db, maya())
    assert [s.request_id for s in summaries] == ["req-1", "req-2", "req-3"]
    assert summaries[0].requester_member_id == "m-sam"
    assert summaries[1].requester_member_id == ""
    assert summaries[2].workspace_id == ""
    assert len(db.committed) == 1  # no per-request round trips (N+1)


@pytest.mark.parametrize(
    ("status", "label"),
    [
        (ToolExecutionStatus.REJECTED_BY_TOOL_SERVER, "rejected"),
        (ToolExecutionStatus.NOT_REACHED, "not_reached"),
        (ToolExecutionStatus.ERROR, "error"),
        (ToolExecutionStatus.UNKNOWN, "unknown"),
    ],
)
def test_execution_ref_step_labels_are_distinct(status: ToolExecutionStatus, label: str) -> None:
    db = FakeDatabase()
    record_execution_ref(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan_id="plan-1",
        step_index=0,
        jti="jti-1",
        ledger_entry_id=None,
        status=status,
    )
    params = _params_of(db, "UPDATE plan_steps")
    assert params[0] == label
    [payload] = _payloads(db, "INSERT INTO decision_events")
    assert payload["execution_status"] == status.value
    assert "tool_executed" not in str(_params_of(db, "INSERT INTO decision_events"))


def _imports_apps(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == "apps" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "apps":
            return True
    return False


def test_audit_services_never_import_apps() -> None:
    offenders = [
        path.name for path in (REPO_ROOT / "services" / "audit").glob("*.py") if _imports_apps(path)
    ]
    assert offenders == []


def test_db_protocols_moved_with_import_compat() -> None:
    from apps.api.core import db as db_module
    from services.audit import protocols

    assert db_module.AuditCursor is protocols.AuditCursor
    assert db_module.AuditDatabase is protocols.AuditDatabase


@pytest.mark.parametrize(
    "line",
    [
        'cursor.execute("SELECT * FROM decisions WHERE id = %s")',
        '"SELECT 1 FROM public.tasks"',
        "JOIN tasks t ON t.id = x",
        'conn.execute("SELECT 1 FROM tool_jti_claims")',
        "dsn = settings.database_url_toolserver",
        "from services.toolserver import db",
        'os.environ["DATABASE_URL_MIGRATE"]',
    ],
)
def test_static_guard_catches_planted_violations(line: str) -> None:
    from tests.api.audit_fakes import protected_name_offenders

    assert protected_name_offenders([line])
    # Split across adjacent string literals is still caught.
    assert protected_name_offenders(['"SELECT 1 FROM "', '"decisions"'])
    # Prose and the allowed event tables are not.
    assert not protected_name_offenders(
        ["# authorization decisions and tasks", '"FROM decision_events"']
    )


# --- 10. events cannot be deleted through their plan (migration 0009) ----------


def test_migration_0009_restricts_event_fks() -> None:
    [migration] = sorted((REPO_ROOT / "migrations").glob("0009_*.sql"))
    sql = migration.read_text(encoding="utf-8")
    for table in ("request_events", "decision_events", "delivery_events"):
        assert f"'{table}'" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "confdeltype" in sql  # real constraint names looked up, not guessed


# --- live (local database only) --------------------------------------------------

LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")
OWNER_DSN = os.environ.get("DATABASE_URL_MIGRATE")
DB_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
needs_role_dsns = pytest.mark.skipif(
    not (LIVE_DSN and TOOLSERVER_DSN),
    reason="needs DATABASE_URL_BACKEND and DATABASE_URL_TOOLSERVER (local database)",
)
needs_live_write = pytest.mark.skipif(
    not (LIVE_DSN and DB_WRITE_TESTS),
    reason="needs DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent append-only audit rows: local database only)",
)


@needs_role_dsns
def test_live_backend_database_refuses_non_backend_dsns() -> None:
    from apps.api.core.db import BackendRoleError

    for dsn in (TOOLSERVER_DSN, OWNER_DSN):
        if not dsn:
            continue
        db = BackendDatabase(dsn)
        try:
            with pytest.raises(BackendRoleError) as refused, db.tx():
                pass
            assert dsn not in str(refused.value)
        finally:
            db.close()
    good = BackendDatabase(LIVE_DSN)
    try:
        with good.tx() as cursor:
            cursor.execute("SELECT 1")
    finally:
        good.close()


def _live_ctx():
    return make_ctx().model_copy(
        update={
            "request_id": f"test-req-{uuid.uuid4().hex[:12]}",
            "trace_id": f"test-trace-{uuid.uuid4().hex[:8]}",
            "received_at": datetime.now(UTC),
            # Seeded by the P2-03 ingestion (ADR-021): run it on this database first.
            "workspace_id": "workspace-primary",
            "slack_thread_ts": "1700000000.000100",
        }
    )


def _live_plan() -> PlanProposal:
    return PlanProposal(
        summary="live follow-up plan",
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={"query": "release checklist", "limit": 3},
                purpose="search",
            ),
            PlannedToolCall(
                step_index=1,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
                arguments={"query": "vendor renewal"},
                purpose="escalated search",
            ),
        ],
        matched_topic_rule_id="topic-live",
        escalation_user_confirmed=True,
    )


def _scalar(db: BackendDatabase, sql: str, params: list[object]) -> object:
    with db.tx() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return row[0] if row else None  # type: ignore[index]


def _by_type(entries: list, event_type: str, kind: str = "request") -> list:
    return [entry for entry in entries if entry.event_type == event_type and entry.kind == kind]


@needs_live_write
def test_live_full_round_trip_reads_back_every_field_group() -> None:
    """Writes one complete request through every writer, then reads each
    PRD §10.1 field group back through the read API (or the backend's own
    tables). Proves the gateway plan_id correlation end to end."""
    ctx = _live_ctx()
    gateway_plan_id = f"p-{uuid.uuid4().hex[:16]}"
    delegation_id = f"dlg-{uuid.uuid4().hex[:12]}"
    db = BackendDatabase(LIVE_DSN)
    try:
        # Request identity + Slack refs; replay is a no-op, a forged replay fails.
        assert create_request(db, ctx) == ctx.request_id
        assert create_request(db, ctx) == ctx.request_id
        with pytest.raises(AuditStoreError):
            create_request(db, ctx.model_copy(update={"requester": maya()}))

        # Plan under the gateway's ID; topic rule / escalation from the plan.
        expires = datetime.now(UTC) + timedelta(minutes=20)
        assert (
            record_plan(
                db,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                plan=_live_plan(),
                plan_id=gateway_plan_id,
                armoriq_plan_id="aplan-live",
                chosen_tier=DataTier.CONFIDENTIAL,
                token_expires_at=expires,
            )
            == gateway_plan_id
        )

        # Step 0: ALLOW + ledger-backed execution reference.
        jti = uuid.uuid4().hex
        record_step_decision(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=gateway_plan_id,
            step_index=0,
            outcome=Outcome(
                make_decision(),
                invocation=ToolInvocationResult(
                    request_id=ctx.request_id,
                    plan_id=gateway_plan_id,
                    step_index=0,
                    tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                    jti=jti,
                    status=ToolExecutionStatus.EXECUTED,
                    tool_server_reached=True,
                    ledger_entry_id="ledger-live-1",
                ),
            ),
        )
        record_execution_ref(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=gateway_plan_id,
            step_index=0,
            jti=jti,
            ledger_entry_id="ledger-live-1",
            status=ToolExecutionStatus.EXECUTED,
            validation_outcome="accepted",
            result_id="result-live-1",
        )

        # Step 1: HOLD before the delegation exists, then the delegation, then approval.
        hold = ApprovalStatus(
            request_id=ctx.request_id,
            plan_id=gateway_plan_id,  # the gateway's ID: no FK violation
            step_index=1,
            tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
            state=ApprovalState.PENDING,
            delegation_id=None,
            requested_at=datetime.now(UTC),
        )
        approval_id = record_approval(
            db, request_id=ctx.request_id, trace_id=ctx.trace_id, approval=hold
        )
        with_delegation = hold.model_copy(update={"delegation_id": delegation_id})
        assert (
            record_approval(
                db, request_id=ctx.request_id, trace_id=ctx.trace_id, approval=with_delegation
            )
            == approval_id
        )
        [pending] = [h for h in pending_holds(db, maya()) if h.plan_id == gateway_plan_id]
        assert pending.delegation_id == delegation_id
        assert pending.request_id == ctx.request_id
        approved = with_delegation.model_copy(
            update={"state": ApprovalState.APPROVED, "resolved_at": datetime.now(UTC)}
        )
        assert (
            record_approval(db, request_id=ctx.request_id, trace_id=ctx.trace_id, approval=approved)
            == approval_id
        )
        assert not [h for h in pending_holds(db, maya()) if h.plan_id == gateway_plan_id]
        record_step_decision(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=gateway_plan_id,
            step_index=1,
            outcome=Outcome(make_decision(delegation_id=delegation_id), approval=approved),
        )
        record_execution_ref(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=gateway_plan_id,
            step_index=1,
            jti=uuid.uuid4().hex,
            ledger_entry_id=None,
            status=ToolExecutionStatus.UNKNOWN,
            tool_server_reached=True,
        )
        step_status = "SELECT status FROM plan_steps WHERE plan_id = %s AND step_index = %s"
        assert _scalar(db, step_status, [gateway_plan_id, 0]) == "executed"
        assert _scalar(db, step_status, [gateway_plan_id, 1]) == "unknown"

        # Off-plan attempt (no plan step) through append_event, truthfully labeled.
        append_event(
            db,
            event=AuditEvent(
                event_id=f"ev-{uuid.uuid4().hex[:8]}",
                occurred_at=datetime.now(UTC),
                event_type=AuditEventType.AUTHORIZATION_DECIDED,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                plan_id=gateway_plan_id,
                decision_source=DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
                payload={
                    "tool_name": ToolName.SEND_EXTERNAL_MESSAGE.value,
                    "outcome": DecisionOutcome.BLOCK.value,
                },
            ),
        )

        # Abstained answer + queue entry: exactly one answer_abstained event.
        record_answer(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            citations=[make_citation()],
            highest_tier=DataTier.CONFIDENTIAL,
            abstained=True,
            reason="no_evidence",
            plan_id=gateway_plan_id,
        )
        question_id = add_unanswered(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            workspace_id="workspace-primary",
            requester_member_id="m-sam",
            question="what did ops@example.com decide? key xoxb-1111-2222-abcdefabcdef",
            reason="no_evidence",
            safe_refs=["src-001"],
        )
        [item] = [i for i in unanswered(db, maya()) if i.question_id == question_id]
        assert item.reason == "no_evidence"
        assert "ops@example.com" not in item.question and "xoxb-" not in item.question
        plan_status = "SELECT status FROM plans WHERE id = %s"
        assert _scalar(db, plan_status, [gateway_plan_id]) == "answered"

        # A failed DM leaves the plan answered; a success marks it delivered.
        record_delivery(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id="m-sam",
            tier_delivered=DataTier.CONFIDENTIAL,
            status="failed",
            error_class="SlackApiError",
            plan_id=gateway_plan_id,
        )
        assert _scalar(db, plan_status, [gateway_plan_id]) == "answered"
        record_delivery(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id="m-sam",
            tier_delivered=DataTier.CONFIDENTIAL,
            status="delivered",
            plan_id=gateway_plan_id,
        )
        assert _scalar(db, plan_status, [gateway_plan_id]) == "delivered"

        # --- read back ---------------------------------------------------------
        entries = timeline(db, ctx.request_id, maya())
        [received] = _by_type(entries, "request_received")
        assert received.payload["requester_member_id"] == "m-sam"
        assert received.payload["workspace_id"] == "workspace-primary"
        assert received.payload["entry_point"] == "slack_mention"
        assert received.payload["slack_event_id"] == "E1"
        assert received.payload["slack_thread_ts"] == "1700000000.000100"

        [proposed] = _by_type(entries, "plan_proposed")
        assert proposed.plan_id == gateway_plan_id
        assert proposed.payload["matched_topic_rule_id"] == "topic-live"
        assert proposed.payload["escalation_confirmed"] is True
        assert proposed.payload["chosen_tier"] == "confidential"
        assert proposed.payload["armoriq_plan_id"] == "aplan-live"
        assert proposed.payload["intent_expires_at"] == expires.isoformat()
        assert proposed.payload["tools"] == [
            "knowledge_search_team",
            "knowledge_search_confidential",
        ]

        decisions = _by_type(entries, "authorization_decided", "decision")
        by_tool = {entry.tool_name: entry for entry in decisions}
        assert by_tool["knowledge_search_team"].is_armoriq_policy is True
        assert by_tool["knowledge_search_team"].step_index == 0
        confidential = by_tool["knowledge_search_confidential"]
        assert confidential.payload["delegation_id"] == delegation_id
        assert confidential.payload["approval_state"] == "approved"
        off_plan = by_tool["send_external_message"]
        assert off_plan.decision_source == "armoriq_sdk_plan_check"
        assert off_plan.is_armoriq_policy is False

        [executed] = _by_type(entries, "tool_executed", "decision")
        assert executed.tool_name == "knowledge_search_team"
        assert executed.payload["ledger_entry_id"] == "ledger-live-1"
        assert executed.payload["tool_server_reached"] is True
        assert executed.payload["validation_outcome"] == "accepted"
        assert executed.payload["result_id"] == "result-live-1"
        [unverified] = _by_type(entries, "tool_rejected", "decision")
        assert unverified.payload["execution_status"] == "unknown"
        assert unverified.tool_name == "knowledge_search_confidential"

        [hold_created] = _by_type(entries, "hold_created")
        assert hold_created.tool_name == "knowledge_search_confidential"
        [resolved] = _by_type(entries, "approval_resolved")
        assert resolved.payload["delegation_id"] == delegation_id
        assert resolved.payload["state"] == "approved"

        [abstained] = _by_type(entries, "answer_abstained")  # no duplicate
        assert abstained.payload["citations"] == [{"source_id": "src-001", "tier": "team"}]
        assert abstained.payload["highest_tier_returned"] == "confidential"

        [failed] = _by_type(entries, "delivery_failed")
        assert failed.payload["error_class"] == "SlackApiError"
        assert failed.payload["recipient_member_id"] == "m-sam"
        [succeeded] = _by_type(entries, "delivery_succeeded")
        assert succeeded.payload["tier_delivered"] == "confidential"
        assert {e.event_type for e in entries if e.kind == "delivery"} == {
            "delivery_failed",
            "delivery_delivered",
        }

        summaries = {s.request_id: s for s in recent_requests(db, maya(), limit=200)}
        assert summaries[ctx.request_id].requester_member_id == "m-sam"
        assert "@example.com" not in json.dumps([e.payload for e in entries])
    finally:
        db.close()


@needs_live_write
def test_live_plan_delete_cannot_cascade_into_events() -> None:
    ctx = _live_ctx()
    db = BackendDatabase(LIVE_DSN)
    try:
        create_request(db, ctx)
        plan_id = record_plan(
            db, request_id=ctx.request_id, trace_id=ctx.trace_id, plan=make_plan()
        )
        with pytest.raises(AuditStoreError) as refused, db.tx() as cursor:
            cursor.execute("DELETE FROM plans WHERE id = %s", [plan_id])
        # 23503 on PostgreSQL 16, 23001 (restrict_violation) on PostgreSQL 18 / Neon.
        assert isinstance(
            refused.value.__cause__, (pg_errors.ForeignKeyViolation, pg_errors.RestrictViolation)
        )
        count = "SELECT COUNT(*) FROM request_events WHERE plan_id = %s"
        assert _scalar(db, count, [plan_id]) == 1
    finally:
        db.close()


def _race(workers: int, action) -> list[BaseException]:
    barrier = threading.Barrier(workers)
    failures: list[BaseException] = []

    def run() -> None:
        barrier.wait()
        try:
            action()
        except BaseException as exc:  # collected and asserted by the caller
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return failures


@needs_live_write
def test_live_concurrent_create_request_retries_write_one_event() -> None:
    ctx = _live_ctx()
    db = BackendDatabase(LIVE_DSN)
    try:
        db.open()
        assert _race(5, lambda: create_request(db, ctx)) == []
        count = (
            "SELECT COUNT(*) FROM request_events "
            "WHERE request_id = %s AND event_type = 'request_received'"
        )
        assert _scalar(db, count, [ctx.request_id]) == 1
    finally:
        db.close()


@needs_live_write
def test_live_concurrent_holds_create_one_pending_row() -> None:
    ctx = _live_ctx()
    db = BackendDatabase(LIVE_DSN)
    try:
        create_request(db, ctx)
        plan_id = record_plan(
            db, request_id=ctx.request_id, trace_id=ctx.trace_id, plan=make_plan()
        )
        hold = ApprovalStatus(
            request_id=ctx.request_id,
            plan_id=plan_id,
            step_index=0,
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
            state=ApprovalState.PENDING,
            delegation_id=None,
            requested_at=datetime.now(UTC),
        )
        failures = _race(
            5,
            lambda: record_approval(
                db, request_id=ctx.request_id, trace_id=ctx.trace_id, approval=hold
            ),
        )
        assert failures == []
        count = "SELECT COUNT(*) FROM approval_requests WHERE plan_id = %s AND state = 'pending'"
        assert _scalar(db, count, [plan_id]) == 1
    finally:
        db.close()


@needs_live_write
def test_live_unanswered_questions_keep_their_own_reasons() -> None:
    ctx = _live_ctx()
    db = BackendDatabase(LIVE_DSN)
    try:
        create_request(db, ctx)
        ids = {
            reason: add_unanswered(
                db,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                workspace_id="workspace-primary",
                requester_member_id="m-sam",
                question=f"question for {reason}",
                reason=reason,
            )
            for reason in ("conflict", "source_unavailable")
        }
        # A later request-level abstention must not overwrite either reason.
        record_answer(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            citations=[],
            highest_tier=DataTier.TEAM,
            abstained=True,
            reason="superseded",
        )
        reasons = {item.question_id: item.reason for item in unanswered(db, maya())}
        assert reasons[ids["conflict"]] == "conflict"
        assert reasons[ids["source_unavailable"]] == "source_unavailable"
        with pytest.raises(AuditStoreError):
            add_unanswered(
                db,
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                workspace_id="workspace-primary",
                requester_member_id="m-priya",  # not the trusted requester
                question="spoofed",
                reason="conflict",
            )
    finally:
        db.close()
