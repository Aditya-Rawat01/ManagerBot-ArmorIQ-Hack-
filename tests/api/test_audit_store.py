"""Backend audit store and read API (P3-01).

Offline by default via test-local fakes (`tests/api/audit_fakes.py`, never
production code): no DSN, network, ArmorIQ, or Slack needed. The live tests
at the end run only when `DATABASE_URL_BACKEND` is set privately, and then
prove connectivity plus the backend role's denial on every protected table —
never any secret or protected content. Follow-up coverage (P3-01 review
findings) lives in `test_audit_followups.py`. Sample secrets are
obviously-fake constants.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.api.core.db import BackendDatabase, BackendDbNotConfiguredError
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
    ApprovalState,
    ApprovalStatus,
    AuditEvent,
    AuditEventType,
    DataTier,
    DecisionSource,
    DeliveryChannel,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)
from tests.api.audit_fakes import FAKE_SECRET as _FAKE_SECRET
from tests.api.audit_fakes import (
    NOW,
    ExplodingDatabase,
    FakeDatabase,
    origin_payload,
    protected_name_offenders,
)
from tests.api.audit_fakes import Outcome as _Outcome
from tests.api.audit_fakes import all_sql as _all_sql
from tests.api.audit_fakes import all_text as _all_text
from tests.api.audit_fakes import make_approval as _approval
from tests.api.audit_fakes import make_citation as _citation
from tests.api.audit_fakes import make_ctx as _ctx
from tests.api.audit_fakes import make_decision as _decision
from tests.api.audit_fakes import make_plan as _plan
from tests.api.audit_fakes import maya as _maya
from tests.api.audit_fakes import sam as _sam

# --- create_request ---------------------------------------------------------


def test_create_request_round_trip() -> None:
    db = FakeDatabase(fetchone=[None])
    assert create_request(db, _ctx()) == "req-1"
    assert len(db.transactions) == 1
    assert "INSERT INTO request_events" in _all_sql(db)
    assert "request_received" in _all_text(db)


def test_create_request_replay_is_idempotent() -> None:
    db = FakeDatabase(fetchone=[(origin_payload(),)])
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
    db2 = FakeDatabase(fetchone=[(11, "pending", "dlg-1", "plan-1", 0)])
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
    db = FakeDatabase(fetchone=[(11, "approved", "dlg-1", "plan-1", 0)])
    assert (
        record_approval(
            db,
            request_id="req-1",
            trace_id="trace-1",
            approval=_approval(ApprovalState.APPROVED),
        )
        == 11
    )
    # Lock + lookup only: no UPDATE and no duplicate event.
    assert not any(sql.startswith(("INSERT", "UPDATE")) for sql, _ in db.committed)


def test_approval_backward_transition_rejected() -> None:
    db = FakeDatabase(fetchone=[(11, "approved", "dlg-1", "plan-1", 0)])
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
    db = FakeDatabase(fetchone=[(origin_payload(),), (9,)])
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
            event_type=AuditEventType.PLAN_REJECTED,
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


# --- confirmation lifecycle events (P3-04 ICR, issue #25) -------------------

# Obviously-fake, token-shaped values: never real secrets.
_FAKE_CONFIRMATION_TOKEN = "fake_confirm_9Fk2Lm4Qp7Rt1Vx5Za8Bc3De6Gh0Jk"
_FAKE_URLSAFE_TOKEN = "fAkE-c0nf1rm_T0k3n-9Fk2Lm4Qp7Rt1Vx5Za8Bc3De6Gh"
_ESC_ID = "esc-" + "3f2a9c" + "0" * 26  # f"esc-{uuid.uuid4().hex}" shape
_DIGEST = "ab" * 32  # hashlib.sha256(...).hexdigest() shape


def _confirmation_event(event_type: AuditEventType, payload: dict[str, object]) -> AuditEvent:
    return AuditEvent(
        event_id="ev-conf-1",
        occurred_at=NOW,
        event_type=event_type,
        request_id="req-1",
        trace_id="trace-1",
        payload=payload,
    )


def _created(**overrides: object) -> AuditEvent:
    payload: dict[str, object] = {
        "escalation_id": _ESC_ID,
        "requested_tier": "confidential",
        "plan_digest": _DIGEST,
    }
    payload.update(overrides)
    return _confirmation_event(AuditEventType.ESCALATION_CREATED, payload)


def _resolved(payload: dict[str, object]) -> AuditEvent:
    return _confirmation_event(AuditEventType.CONFIRMATION_RESOLVED, payload)


def _refused(event: AuditEvent) -> None:
    """The write is refused and nothing reaches the database."""
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        append_event(db, event=event)
    assert db.committed == []
    assert "INSERT" not in _all_sql(db)


@pytest.mark.parametrize("tier", ["confidential", "restricted"])
def test_escalation_created_valid_payload_is_recorded(tier: str) -> None:
    db = FakeDatabase()
    append_event(db, event=_created(requested_tier=tier))
    assert "INSERT INTO request_events" in _all_sql(db)
    assert _ESC_ID in _all_text(db) and tier in _all_text(db) and _DIGEST in _all_text(db)


@pytest.mark.parametrize(
    "state", ["accepted", "rejected", "expired", "invalid", "replayed", "mismatch", "unbound"]
)
def test_confirmation_resolved_accepts_known_states(state: str) -> None:
    db = FakeDatabase()
    append_event(db, event=_resolved({"escalation_id": _ESC_ID, "confirmation_state": state}))
    assert "INSERT INTO request_events" in _all_sql(db)


@pytest.mark.parametrize("state", ["invalid", "unbound"])
def test_unidentified_states_need_no_escalation_id(state: str) -> None:
    """No stored confirmation was found, so there is no escalation to name
    (and nothing tempts the caller to fill the ID with the token)."""
    db = FakeDatabase()
    append_event(db, event=_resolved({"confirmation_state": state}))
    assert "INSERT INTO request_events" in _all_sql(db)


@pytest.mark.parametrize("state", ["accepted", "rejected", "expired", "replayed", "mismatch"])
def test_identified_states_require_the_escalation_id(state: str) -> None:
    _refused(_resolved({"confirmation_state": state}))


def test_confirmation_event_rejects_unknown_payload_key() -> None:
    _refused(_created(note="x"))
    _refused(_resolved({"escalation_id": _ESC_ID, "confirmation_state": "accepted", "x": 1}))


def test_confirmation_event_rejects_raw_token_and_writes_nothing() -> None:
    db = FakeDatabase()
    with pytest.raises(AuditStoreError):
        append_event(
            db,
            event=_resolved(
                {
                    "escalation_id": _ESC_ID,
                    "confirmation_state": "accepted",
                    "confirmation_token": _FAKE_CONFIRMATION_TOKEN,
                }
            ),
        )
    assert _FAKE_CONFIRMATION_TOKEN not in _all_text(db)
    assert db.committed == []


@pytest.mark.parametrize("token", [_FAKE_CONFIRMATION_TOKEN, _FAKE_URLSAFE_TOKEN])
def test_a_token_passed_as_an_allowed_field_is_refused(token: str) -> None:
    """The ID and digest shapes are exact, so a token can't ride in an
    allowed key either (a caller bug, not just a hostile key name)."""
    _refused(_created(escalation_id=token))
    _refused(_created(plan_digest=token))
    _refused(_resolved({"escalation_id": token, "confirmation_state": "invalid"}))


def test_confirmation_event_rejects_missing_fields() -> None:
    _refused(_confirmation_event(AuditEventType.ESCALATION_CREATED, {}))
    _refused(_confirmation_event(AuditEventType.ESCALATION_CREATED, {"escalation_id": _ESC_ID}))
    _refused(
        _confirmation_event(
            AuditEventType.ESCALATION_CREATED,
            {"escalation_id": _ESC_ID, "requested_tier": "confidential"},
        )
    )
    _refused(
        _confirmation_event(
            AuditEventType.ESCALATION_CREATED,
            {"escalation_id": _ESC_ID, "plan_digest": _DIGEST},
        )
    )
    _refused(_resolved({"escalation_id": _ESC_ID}))


@pytest.mark.parametrize("state", ["maybe", "", None, ["accepted"], {"s": "accepted"}, 1])
def test_confirmation_event_rejects_unknown_state(state: object) -> None:
    _refused(_resolved({"escalation_id": _ESC_ID, "confirmation_state": state}))


@pytest.mark.parametrize(
    "digest", ["nope!", "AB" * 32, "ab" * 16, "ab" * 33, "", None, ["ab" * 32]]
)
def test_escalation_created_rejects_a_non_sha256_digest(digest: object) -> None:
    _refused(_created(plan_digest=digest))


@pytest.mark.parametrize("tier", ["team", "secret", "", None, ["confidential"], 1])
def test_escalation_created_rejects_a_non_elevated_tier(tier: object) -> None:
    """Only confidential / restricted can be escalated to; `team` never needs it."""
    _refused(_created(requested_tier=tier))


@pytest.mark.parametrize(
    "escalation_id",
    ["has space", "esc-1", "esc-" + "A" * 32, "esc-" + "0" * 31, "esc-" + "0" * 33, "ESC-" + "0" * 32,
     "x-" + "0" * 32, "", None, 7],
)
def test_confirmation_event_rejects_a_non_canonical_escalation_id(escalation_id: object) -> None:
    _refused(_created(escalation_id=escalation_id))
    _refused(_resolved({"escalation_id": escalation_id, "confirmation_state": "accepted"}))


# --- read API ---------------------------------------------------------------


def _origin_payload(member_id: str = "m-sam") -> dict[str, object]:
    return {"trace_id": "trace-1", "payload": origin_payload(member_id)}


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
    db = FakeDatabase(fetchall=[[("req-1", "trace-1", origin["payload"], NOW)]])
    summaries = recent_requests(db, maya)
    assert [summary.request_id for summary in summaries] == ["req-1"]
    db2 = FakeDatabase(
        fetchall=[
            [(NOW, "request_received", None, None, None, {}, None)],
            [(NOW, "authorization_decided", "plan-1", 0, "application_rule", {}, None)],
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
    db4 = FakeDatabase(
        fetchall=[[(9, "req-1", "why?", "open", NOW)], [("req-1", "9", "no_evidence")]]
    )
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
    offenders = [
        f"{path}:{offender}"
        for path in roots
        for offender in protected_name_offenders(path.read_text(encoding="utf-8").splitlines())
    ]
    assert offenders == []


# --- live backend role (skipped without a private DSN) ------------------------

LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
# Everything the backend role must never read (0005 revokes, 0007 claims).
PROTECTED_TABLES = (
    "source_records",
    "knowledge_chunks",
    "decisions",
    "manager_guidance",
    "team_notes",
    "tool_execution_ledger",
    "tool_jti_claims",
    "tasks",
    "egress_sink",
)
needs_live = pytest.mark.skipif(
    not LIVE_DSN, reason="no private DATABASE_URL_BACKEND; fakes cover behavior"
)


@needs_live
def test_live_backend_role_connectivity_and_denial() -> None:
    """Proves connectivity and the backend denial on EVERY protected table.

    Knowledge tables, the execution ledger, jti claims, and the task/egress
    tables (0005/0007) must all be denied; denial surfaces as
    ``AuditStoreError`` caused by ``InsufficientPrivilege``. Never prints the
    DSN.
    """
    from psycopg.errors import InsufficientPrivilege

    db = BackendDatabase(LIVE_DSN)
    try:
        with db.tx() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
        for table in PROTECTED_TABLES:
            with pytest.raises(AuditStoreError) as denied, db.tx() as cursor:
                cursor.execute(f"SELECT 1 FROM public.{table} LIMIT 1")  # noqa: S608 - constant
            assert isinstance(denied.value.__cause__, InsufficientPrivilege), table
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
