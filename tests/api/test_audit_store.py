"""Backend audit store and read API (P3-01).

Offline by default via test-local fakes (defined here, never production
code): no DSN, network, ArmorIQ, or Slack needed. The live class at the end
runs only when `DATABASE_URL_BACKEND` is set privately, and then proves only
connectivity plus the backend role's denial on protected tables — never any
secret or protected content. Sample secrets are obviously-fake constants.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.api.core.db import AuditCursor, BackendDatabase, BackendDbNotConfiguredError
from services.audit import (
    AuditNotAuthorizedError,
    AuditNotFoundError,
    AuditStoreError,
    AuditTransitionError,
)
from services.audit.queries import (
    pending_holds,
    recent_requests,
    request_status,
    timeline,
    unanswered,
)
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
    AppRole,
    ApprovalState,
    ApprovalStatus,
    ArmorIQDecision,
    AuditEvent,
    AuditEventType,
    AuthenticatedUser,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    McpEndpoint,
    PersonaKey,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    ReviewStatus,
    SourceCitation,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
_FAKE_SECRET = "sk-fake-test-secret-0000"


# --- fakes ------------------------------------------------------------------


class FakeCursor:
    """Records statements; replays scripted reads. Never touches a database."""

    def __init__(
        self,
        fetchone: list[object] | None = None,
        fetchall: list[object] | None = None,
        rowcount: int = 1,
    ) -> None:
        self.statements: list[tuple[str, object]] = []
        self._one = list(fetchone or [])
        self._all = list(fetchall or [])
        self.rowcount = rowcount

    def execute(self, query: str, params: object = None) -> None:
        self.statements.append((query, params))

    def fetchone(self) -> object:
        return self._one.pop(0) if self._one else None

    def fetchall(self) -> list[object]:
        if self._all:
            return self._all.pop(0)  # type: ignore[return-value]
        return []


class FakeDatabase:
    """One shared script across cursors; commits statements per transaction."""

    def __init__(
        self,
        fetchone: list[object] | None = None,
        fetchall: list[object] | None = None,
        rowcount: int = 1,
    ) -> None:
        self._one = list(fetchone or [])
        self._all = list(fetchall or [])
        self._rowcount = rowcount
        self.transactions: list[list[tuple[str, object]]] = []
        self.committed: list[tuple[str, object]] = []
        self.rolled_back = 0

    @contextmanager
    def tx(self) -> Iterator[AuditCursor]:
        cursor = FakeCursor(fetchone=self._one, fetchall=self._all, rowcount=self._rowcount)
        # Share the queues so multi-statement flows script in order.
        cursor._one, cursor._all = self._one, self._all
        try:
            yield cursor  # type: ignore[misc]
        except Exception:
            self.rolled_back += 1
            raise
        self.transactions.append(list(cursor.statements))
        self.committed.extend(cursor.statements)


class ExplodingCursor(FakeCursor):
    def execute(self, query: str, params: object = None) -> None:
        super().execute(query, params)
        if len(self.statements) >= 2:
            raise AuditStoreError("fake connection lost")


class ExplodingDatabase(FakeDatabase):
    @contextmanager
    def tx(self) -> Iterator[AuditCursor]:
        cursor = ExplodingCursor()
        try:
            yield cursor  # type: ignore[misc]
        except Exception:
            self.rolled_back += 1
            raise
        self.committed.extend(cursor.statements)


def _all_sql(db: FakeDatabase) -> str:
    return "\n".join(sql for sql, _ in db.committed)


def _all_text(db: FakeDatabase) -> str:
    return _all_sql(db) + "\n" + str([params for _, params in db.committed])


# --- fixtures ---------------------------------------------------------------


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


def _ctx() -> RequestContext:
    return RequestContext(
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester=_sam(),
        entry_point=EntryPoint.SLACK_MENTION,
        received_at=NOW,
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_event_id="E1",
    )


def _plan() -> PlanProposal:
    return PlanProposal(
        summary="budget question",
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={"query": "atlas budget", "limit": 5},
                purpose="search",
            )
        ],
    )


def _decision() -> ArmorIQDecision:
    return ArmorIQDecision(
        outcome=DecisionOutcome.ALLOW,
        source=DecisionSource.ARMORIQ_POLICY_DECISION,
        armoriq_plan_id="aplan-1",
        decided_at=NOW,
    )


def _approval(state: ApprovalState = ApprovalState.PENDING) -> ApprovalStatus:
    return ApprovalStatus(
        request_id="req-1",
        plan_id="plan-1",
        step_index=0,
        tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        state=state,
        delegation_id="dlg-1",
        requested_at=NOW,
        resolved_at=NOW if state is not ApprovalState.PENDING else None,
    )


def _citation() -> SourceCitation:
    return SourceCitation(
        source_id="src-001",
        title="Atlas budget",
        tier=DataTier.TEAM,
        author="Maya",
        created_at=NOW,
        version=1,
        review_status=ReviewStatus.DRAFT,
    )


class _Outcome:
    """Structural stand-in for the gateway StepOutcome (no SDK import)."""

    def __init__(self, decision, approval=None, invocation=None) -> None:
        self.decision = decision
        self.approval = approval
        self.invocation = invocation


# --- create_request ---------------------------------------------------------


def test_create_request_round_trip() -> None:
    db = FakeDatabase(fetchone=[None])
    assert create_request(db, _ctx()) == "req-1"
    assert len(db.transactions) == 1
    assert "INSERT INTO request_events" in _all_sql(db)
    assert "request_received" in _all_text(db)


def test_create_request_replay_is_idempotent() -> None:
    db = FakeDatabase(fetchone=[({"trace_id": "trace-1"},)])
    assert create_request(db, _ctx()) == "req-1"
    assert not any("INSERT" in sql for sql, _ in db.committed)


# --- record_plan ------------------------------------------------------------


def test_record_plan_writes_plan_steps_and_event_atomically() -> None:
    db = FakeDatabase()
    plan_id = record_plan(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan=_plan(),
        armoriq_plan_id="aplan-1",
        matched_topic_rule_id="topic-7",
        chosen_tier=DataTier.TEAM,
        escalation_confirmed=True,
    )
    assert plan_id.startswith("plan-")
    assert len(db.transactions) == 1  # one transaction, no partial records
    sql = _all_sql(db)
    assert "INSERT INTO plans" in sql
    assert "INSERT INTO plan_steps" in sql
    assert "plan_proposed" in sql


def test_record_plan_without_request_fails_closed() -> None:
    db = FakeDatabase(rowcount=0, fetchone=[None])
    with pytest.raises(AuditNotFoundError):
        record_plan(db, request_id="req-9", trace_id="trace-9", plan=_plan())


def test_plan_arguments_stored_redacted() -> None:
    db = FakeDatabase()
    plan = _plan()
    plan.steps[0].arguments["token"] = _FAKE_SECRET
    record_plan(db, request_id="req-1", trace_id="trace-1", plan=plan)
    recorded = str([params for _, params in db.committed])
    assert _FAKE_SECRET not in _all_sql(db)
    assert _FAKE_SECRET not in recorded
    assert "[redacted]" in recorded


# --- record_step_decision ---------------------------------------------------


def test_record_step_decision_moves_step_to_decided() -> None:
    db = FakeDatabase()
    invocation = ToolInvocationResult(
        request_id="req-1",
        plan_id="plan-1",
        step_index=0,
        tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
        jti="jti-9",
        status=ToolExecutionStatus.NOT_REACHED,
        tool_server_reached=False,
    )
    record_step_decision(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan_id="plan-1",
        step_index=0,
        outcome=_Outcome(_decision(), invocation=invocation),
    )
    sql = _all_sql(db)
    assert "INSERT INTO decision_events" in sql
    assert "authorization_decided" in sql
    assert "UPDATE plan_steps" in sql
    assert "jti-9" in str([params for _, params in db.committed])


def test_record_step_decision_on_missing_step_fails() -> None:
    db = FakeDatabase(rowcount=0, fetchone=[None])
    with pytest.raises(AuditNotFoundError):
        record_step_decision(
            db,
            request_id="req-1",
            trace_id="trace-1",
            plan_id="plan-9",
            step_index=0,
            outcome=_Outcome(_decision()),
        )


def test_record_step_decision_rejects_replay() -> None:
    db = FakeDatabase(rowcount=0, fetchone=[("executed",)])
    with pytest.raises(AuditTransitionError):
        record_step_decision(
            db,
            request_id="req-1",
            trace_id="trace-1",
            plan_id="plan-1",
            step_index=0,
            outcome=_Outcome(_decision()),
        )


# --- record_approval --------------------------------------------------------


def test_approval_insert_then_resolve_forward_only() -> None:
    db = FakeDatabase(fetchone=[None, None, (11,)])
    approval_id = record_approval(db, request_id="req-1", trace_id="trace-1", approval=_approval())
    assert approval_id == 11
    assert "hold_created" in _all_text(db)
    db2 = FakeDatabase(fetchone=[(11, "pending")])
    assert (
        record_approval(
            db2,
            request_id="req-1",
            trace_id="trace-1",
            approval=_approval(ApprovalState.APPROVED),
        )
        == 11
    )
    assert "approval_resolved" in _all_text(db2)


def test_approval_replay_returns_same_row_without_duplicate_event() -> None:
    db = FakeDatabase(fetchone=[(11, "approved")])
    assert (
        record_approval(
            db,
            request_id="req-1",
            trace_id="trace-1",
            approval=_approval(ApprovalState.APPROVED),
        )
        == 11
    )
    assert len(db.committed) == 1  # the lookup only; no duplicate event


def test_approval_backward_transition_rejected() -> None:
    db = FakeDatabase(fetchone=[(11, "approved")])
    with pytest.raises(AuditTransitionError):
        record_approval(db, request_id="req-1", trace_id="trace-1", approval=_approval())


# --- record_execution_ref ---------------------------------------------------


def test_execution_ref_needs_ledger_id_for_executed() -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        record_execution_ref(
            db,
            request_id="req-1",
            trace_id="trace-1",
            jti="jti-1",
            ledger_entry_id=None,
            status=ToolExecutionStatus.EXECUTED,
        )
    assert db.committed == []


def test_execution_ref_stores_ids_not_content() -> None:
    db = FakeDatabase()
    record_execution_ref(
        db,
        request_id="req-1",
        trace_id="trace-1",
        plan_id="plan-1",
        step_index=0,
        jti="jti-1",
        ledger_entry_id="ledger-42",
        status=ToolExecutionStatus.EXECUTED,
    )
    text = _all_text(db)
    assert "tool_executed" in text
    assert "ledger-42" in text
    assert "tool_execution_ledger" not in text  # reference only, never the table


# --- record_answer / record_delivery / add_unanswered ------------------------


def test_answer_stores_citation_ids_not_snippets() -> None:
    db = FakeDatabase()
    record_answer(
        db,
        request_id="req-1",
        trace_id="trace-1",
        citations=[_citation()],
        highest_tier=DataTier.TEAM,
        abstained=False,
        reason=None,
    )
    assert "answer_generated" in _all_text(db)
    assert "Atlas budget" not in _all_sql(db)  # titles are content: never stored
    assert "src-001" in str([params for _, params in db.committed])


def test_delivery_round_trip_and_forward_status() -> None:
    db = FakeDatabase(fetchone=[(7,)])
    assert (
        record_delivery(
            db,
            request_id="req-1",
            trace_id="trace-1",
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id="m-sam",
            tier_delivered=DataTier.TEAM,
            status="delivered",
        )
        == 7
    )
    assert "delivery_succeeded" in _all_text(db)


def test_delivery_rejects_unknown_status() -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        record_delivery(
            db,
            request_id="req-1",
            trace_id="trace-1",
            channel=DeliveryChannel.SLACK_DM,
            recipient_member_id="m-sam",
            tier_delivered=DataTier.TEAM,
            status="bogus",  # type: ignore[arg-type]
        )


def test_unanswered_redacts_question_and_bounds_refs() -> None:
    db = FakeDatabase(fetchone=[(9,)])
    question_id = add_unanswered(
        db,
        request_id="req-1",
        trace_id="trace-1",
        workspace_id="ws-1",
        requester_member_id="m-sam",
        question="what did private@example.com decide about layoffs?",
        reason="manager_decision_required",
        safe_refs=["src-1", "src-2"],
    )
    assert question_id == 9
    assert "private@example.com" not in str([params for _, params in db.committed])
    with pytest.raises(AuditStoreError):
        add_unanswered(
            db,
            request_id="req-1",
            trace_id="trace-1",
            workspace_id="ws-1",
            requester_member_id="m-sam",
            question="q",
            reason="no_evidence",
            safe_refs=[f"src-{number}" for number in range(21)],
        )


# --- append_event -----------------------------------------------------------


def test_append_event_routes_by_type_and_redacts() -> None:
    db = FakeDatabase()
    append_event(
        db,
        event=AuditEvent(
            event_id="ev-1",
            occurred_at=NOW,
            event_type=AuditEventType.AUTHORIZATION_DECIDED,
            request_id="req-1",
            trace_id="trace-1",
            decision_source=DecisionSource.APPLICATION_RULE,
            payload={"token": _FAKE_SECRET},
        ),
    )
    assert "INSERT INTO decision_events" in _all_sql(db)
    assert _FAKE_SECRET not in str([params for _, params in db.committed])
    db2 = FakeDatabase()
    append_event(
        db2,
        event=AuditEvent(
            event_id="ev-2",
            occurred_at=NOW,
            event_type=AuditEventType.REQUEST_RECEIVED,
            request_id="req-1",
            trace_id="trace-1",
            payload={},
        ),
    )
    assert "INSERT INTO request_events" in _all_sql(db2)


def test_append_event_rejects_naive_timestamps() -> None:
    # AwareDatetime rejects naive input at construction (contract boundary).
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="ev-3",
            occurred_at=datetime(2026, 10, 15, 12, 0),  # naive: no timezone
            event_type=AuditEventType.REQUEST_RECEIVED,
            request_id="req-1",
            trace_id="trace-1",
            payload={},
        )


# --- read API ---------------------------------------------------------------


def _origin_payload(member_id: str = "m-sam") -> dict[str, object]:
    return {
        "trace_id": "trace-1",
        "payload": {
            "workspace_id": "ws-1",
            "requester_member_id": member_id,
            "entry_point": "slack_mention",
        },
    }


def test_request_status_owner_only() -> None:
    origin = _origin_payload()
    db = FakeDatabase(
        fetchone=[(origin["trace_id"], origin["payload"])],
        fetchall=[[("plan-1",)], [(0,)], [(0,)], [(NOW,)]],
    )
    view = request_status(db, "req-1", "m-sam")
    assert view.request_id == "req-1"
    assert view.plan_ids == ("plan-1",)
    db2 = FakeDatabase(fetchone=[(origin["trace_id"], origin["payload"])])
    with pytest.raises(AuditNotAuthorizedError):
        request_status(db2, "req-1", "m-priya")
    db3 = FakeDatabase(fetchone=[None])
    with pytest.raises(AuditNotFoundError):
        request_status(db3, "req-9", "m-sam")


def test_manager_queries_deny_non_managers() -> None:
    junior = _sam()
    db = FakeDatabase()
    with pytest.raises(AuditNotAuthorizedError):
        recent_requests(db, junior)
    with pytest.raises(AuditNotAuthorizedError):
        timeline(db, "req-1", junior)
    with pytest.raises(AuditNotAuthorizedError):
        pending_holds(db, junior)
    with pytest.raises(AuditNotAuthorizedError):
        unanswered(db, junior)
    assert db.committed == []  # denied before any SQL


def test_recent_requests_timeline_holds_unanswered() -> None:
    maya = _maya()
    origin = _origin_payload()
    db = FakeDatabase(
        fetchone=[(origin["trace_id"], origin["payload"])],
        fetchall=[[("req-1", "trace-1", NOW)]],
    )
    summaries = recent_requests(db, maya)
    assert [summary.request_id for summary in summaries] == ["req-1"]
    db2 = FakeDatabase(
        fetchall=[
            [(NOW, "request_received", None, None, None, {})],
            [(NOW, "authorization_decided", "plan-1", 0, "application_rule", {})],
            [(NOW, "slack_dm", "m-sam", "delivered", "plan-1")],
        ]
    )
    entries = timeline(db2, "req-1", maya)
    assert [entry.kind for entry in entries] == ["request", "decision", "delivery"]
    db3 = FakeDatabase(
        fetchall=[
            [
                (
                    5,
                    "req-1",
                    "plan-1",
                    0,
                    "knowledge_search_confidential",
                    "dlg-1",
                    NOW,
                    "m-sam",
                )
            ]
        ]
    )
    holds = pending_holds(db3, maya)
    assert holds[0].delegation_id == "dlg-1"
    db4 = FakeDatabase(fetchall=[[(9, "req-1", "why?", "open", NOW)], [("req-1", "no_evidence")]])
    items = unanswered(db4, maya)
    assert items[0].reason == "no_evidence"


# --- transaction, safety, UTC, DSN ------------------------------------------


def test_failed_multi_write_commits_nothing() -> None:
    db = ExplodingDatabase()
    with pytest.raises(AuditStoreError):
        record_plan(db, request_id="req-1", trace_id="trace-1", plan=_plan())
    assert db.committed == []
    assert db.rolled_back == 1


def test_user_values_travel_as_params_never_in_sql() -> None:
    db = FakeDatabase(fetchone=[None])
    plan = _plan()
    plan.steps[0].arguments["query"] = _FAKE_SECRET
    plan.steps[0].arguments["token"] = _FAKE_SECRET
    record_plan(db, request_id="req-1", trace_id="trace-1", plan=plan)
    # Ordinary values travel as bound params, never interpolated into SQL text.
    assert _FAKE_SECRET not in _all_sql(db)
    assert "%s" in _all_sql(db)
    # ...while sensitive-keyed values are redacted even inside params.
    recorded = str([params for _, params in db.committed])
    assert "[redacted]" in recorded


def test_naive_datetimes_rejected_at_contract_boundary() -> None:
    # AwareDatetime fields reject naive input at construction, so typed store
    # inputs can never carry naive time. The store's _aware() is belt-and-braces.
    with pytest.raises(ValidationError):
        ApprovalStatus(
            request_id="req-1",
            plan_id="plan-1",
            step_index=0,
            tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
            state=ApprovalState.PENDING,
            delegation_id="dlg-9",
            requested_at=datetime(2026, 10, 15, 12, 0),  # naive: no timezone
            resolved_at=None,
        )
    with pytest.raises(ValidationError):
        AuditEvent(
            event_id="ev-3",
            occurred_at=datetime(2026, 10, 15, 12, 0),  # naive: no timezone
            event_type=AuditEventType.REQUEST_RECEIVED,
            request_id="req-1",
            trace_id="trace-1",
            payload={},
        )


def test_backend_dsn_guard() -> None:
    with pytest.raises(BackendDbNotConfiguredError):
        BackendDatabase("")
    with pytest.raises(BackendDbNotConfiguredError):
        BackendDatabase(None)  # type: ignore[arg-type]


def test_audit_source_names_no_protected_tables_or_foreign_dsns() -> None:
    """Static guard: backend SQL/storage code must not name what it cannot touch."""
    repo_root = Path(__file__).resolve().parents[2]
    roots = [
        repo_root / "apps" / "api" / "core" / "db.py",
        *(repo_root / "services" / "audit").glob("*.py"),
    ]
    banned = (
        "DATABASE_URL_TOOLSERVER",
        "DATABASE_URL_MIGRATE",
        "armoriq_sdk",
        "source_records",
        "knowledge_chunks",
        "team_notes",
        "manager_guidance",
        "tool_execution_ledger",
        "egress_sink",
        "FROM tasks",
        "JOIN tasks",
    )
    offenders = [
        f"{path}:{number}:{line.strip()}"
        for path in roots
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "no-protected-tables" not in line
        for token in banned
        if token in line
    ]
    assert offenders == []


# --- live backend role (skipped without a private DSN) ------------------------

LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
needs_live = pytest.mark.skipif(
    not LIVE_DSN, reason="no private DATABASE_URL_BACKEND; fakes cover behavior"
)


@needs_live
def test_live_backend_role_connectivity_and_denial() -> None:
    """Proves connectivity and the backend denial on protected tables ONLY.

    Full round trips need seeded workspaces/members (P2-03); the backend role
    cannot insert those itself. Never prints the DSN.
    """
    from psycopg.errors import InsufficientPrivilege

    db = BackendDatabase(LIVE_DSN)
    try:
        with db.tx() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
        with pytest.raises(InsufficientPrivilege), db.tx() as cursor:
            cursor.execute("SELECT * FROM team_notes LIMIT 1")
    finally:
        db.close()


# --- live write round trip (opt-in; writes permanent append-only rows) --------

DB_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
needs_live_write = pytest.mark.skipif(
    not (LIVE_DSN and DB_WRITE_TESTS),
    reason="needs DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent append-only audit rows: local database only)",
)


@needs_live_write
def test_live_write_round_trip() -> None:
    """Real Postgres: catches SQL/column and parameter-adaptation errors that
    the offline FakeCursor cannot (e.g. dict payloads, missing columns).
    Needs the seeded workspace and members (P2-03 ingestion) on this database."""
    import uuid

    ctx = _ctx().model_copy(
        update={
            "request_id": f"test-req-{uuid.uuid4().hex[:12]}",
            "received_at": datetime.now(UTC),
            # Seeded by the P2-03 ingestion (ADR-021): run it on this database first.
            "workspace_id": "workspace-primary",
        }
    )
    db = BackendDatabase(LIVE_DSN)
    try:
        assert create_request(db, ctx) == ctx.request_id
        assert create_request(db, ctx) == ctx.request_id  # replay is a no-op
        plan_id = record_plan(db, request_id=ctx.request_id, trace_id=ctx.trace_id, plan=_plan())
        record_step_decision(
            db,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            plan_id=plan_id,
            step_index=0,
            outcome=_Outcome(_decision()),
        )
        view = request_status(db, ctx.request_id, "m-sam")
        assert view.plan_ids == (plan_id,)
        assert view.requester_member_id == "m-sam"
        with pytest.raises(AuditNotAuthorizedError):
            request_status(db, ctx.request_id, "m-priya")
        event_types = [entry.event_type for entry in timeline(db, ctx.request_id, _maya())]
        assert "request_received" in event_types
    finally:
        db.close()
