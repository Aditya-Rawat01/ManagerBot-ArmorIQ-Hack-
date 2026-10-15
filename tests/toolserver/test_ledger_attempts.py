"""Rejected-attempt evidence (P2-07 follow-up; task §2, §9 step 4).

Every call that passes the credential check gets one `tool_call_attempts`
row with its verification outcome. Offline tests drive `verify_and_record`
with an in-memory ledger double; database tests need DATABASE_URL_TOOLSERVER
plus PROXYLEAD_DB_WRITE_TESTS=1 (they write permanent rows: local database
only). DSNs are captured at collection time and never logged.
"""

import dataclasses
import os
import time
import uuid

import pytest
from psycopg import errors

from services.authorization.ctx_envelope import CtxEnvelope, sign_ctx
from services.contracts import ToolExecutionStatus
from services.toolserver import db
from services.toolserver import ledger as ledger_module
from services.toolserver.ledger import Ledger, LedgerEntry, LedgerEntryId
from services.toolserver.security import (
    CONTEXT_EXPIRED,
    INVALID_CONTEXT,
    REPLAY,
    CtxRejection,
)

TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")
WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"

NEEDS_DB = pytest.mark.skipif(
    not (TOOLSERVER_DSN and WRITE_TESTS),
    reason="needs DATABASE_URL_TOOLSERVER and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent attempt/ledger rows: local database only)",
)

SECRET = "test-ctx-secret-not-a-real-secret"
ENDPOINT = "memory-mcp"
TOOL = "knowledge_search_team"
CREDENTIAL = "toolserver_bearer_memory"
ARGUMENT_MARKER = "super-secret-query-fragment-attempts"


def uid(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


def signed(jti: str, *, now: float, ttl: float = 300.0, secret: str = SECRET) -> str:
    return sign_ctx(
        CtxEnvelope(
            request_id="req-attempt",
            trace_id="trace-1",
            workspace_id="workspace-primary",
            requester_member_id="m-sam",
            armoriq_member_email_ref="PERSONA_SAM_EMAIL",
            app_role="senior",
            plan_id="plan-1",
            step_index=0,
            tool=TOOL,
            aud=ENDPOINT,
            iat=now,
            exp=now + ttl,
            jti=jti,
        ),
        secret,
    )


class FakeAttemptLedger:
    """In-memory `AttemptLedger`: claims like InMemoryReplayGuard, keeps
    attempts, and maps jtis to ledger entries appended by the test."""

    def __init__(self, *, fail_record: bool = False) -> None:
        self.claimed: set[str] = set()
        self.entries: dict[str, LedgerEntryId] = {}
        self.attempts: list[ledger_module.ToolCallAttempt] = []
        self.fail_record = fail_record

    def seen_jti(self, jti: str) -> bool:
        if jti in self.claimed:
            return True
        self.claimed.add(jti)
        return False

    def record_attempt(self, attempt: ledger_module.ToolCallAttempt) -> int:
        ledger_module.validate_attempt(attempt)
        if self.fail_record:
            raise RuntimeError("database unavailable")
        self.attempts.append(attempt)
        return len(self.attempts)

    def find_entry_id(self, jti: str) -> LedgerEntryId | None:
        return self.entries.get(jti)


def call(ledger: object, token: str | None, **extra: object):
    arguments: dict[str, object] = {"query": ARGUMENT_MARKER, "limit": 5, **extra}
    if token is not None:
        arguments["_ctx"] = token
    return ledger_module.verify_and_record(
        ledger,  # type: ignore[arg-type]
        ENDPOINT,
        TOOL,
        arguments,
        credential_id=CREDENTIAL,
        ctx_secret=SECRET,
    )


def assert_no_secrets(attempt: ledger_module.ToolCallAttempt, token: str | None) -> None:
    stored = " ".join(str(value) for value in dataclasses.asdict(attempt).values())
    assert ARGUMENT_MARKER not in stored
    if token:
        assert token not in stored
        assert token.split(".")[0] not in stored


# --- offline ----------------------------------------------------------------


def test_accepted_attempt_records_verified_identity() -> None:
    fake = FakeAttemptLedger()
    token = signed("jti-ok", now=time.time())
    verified = call(fake, token)
    assert verified.ctx.jti == "jti-ok"
    (attempt,) = fake.attempts
    assert attempt.outcome == "accepted"
    assert (attempt.jti, attempt.request_id, attempt.workspace_id) == (
        "jti-ok",
        "req-attempt",
        "workspace-primary",
    )
    assert attempt.requester_member_id == "m-sam"
    assert (attempt.mcp_endpoint, attempt.tool_name, attempt.credential_id) == (
        ENDPOINT,
        TOOL,
        CREDENTIAL,
    )
    assert_no_secrets(attempt, token)


@pytest.mark.parametrize(
    ("make_token", "code"),
    [
        (lambda now: None, INVALID_CONTEXT),
        (lambda now: signed("jti-bad", now=now, secret="wrong-secret"), INVALID_CONTEXT),
        (lambda now: signed("jti-old", now=now - 3600, ttl=60), CONTEXT_EXPIRED),
    ],
)
def test_rejected_attempt_is_recorded_without_unverified_identity(make_token, code: str) -> None:
    fake = FakeAttemptLedger()
    token = make_token(time.time())
    with pytest.raises(CtxRejection) as exc_info:
        call(fake, token)
    assert exc_info.value.code == code
    (attempt,) = fake.attempts
    assert attempt.outcome == code
    assert attempt.jti is None and attempt.request_id is None
    assert attempt.workspace_id is None and attempt.requester_member_id is None
    assert_no_secrets(attempt, token)


def test_replay_is_recorded_with_reference_to_original_entry() -> None:
    fake = FakeAttemptLedger()
    token = signed("jti-replayed", now=time.time())
    call(fake, token)
    fake.entries["jti-replayed"] = LedgerEntryId(99)  # the first call's ledger row
    with pytest.raises(CtxRejection) as exc_info:
        call(fake, token)
    assert exc_info.value.code == REPLAY
    accepted, replay = fake.attempts
    assert accepted.outcome == "accepted"
    assert replay.outcome == REPLAY
    assert replay.jti == "jti-replayed"
    assert replay.original_entry_id == LedgerEntryId(99)
    assert replay.request_id is None, "only the verified jti is captured for replays"
    assert_no_secrets(replay, token)


def test_recording_failure_never_turns_a_rejection_into_an_execution() -> None:
    fake = FakeAttemptLedger(fail_record=True)
    with pytest.raises(CtxRejection) as exc_info:
        call(fake, signed("jti-x", now=time.time(), secret="wrong-secret"))
    assert exc_info.value.code == INVALID_CONTEXT


def test_accepted_call_fails_closed_when_attempt_cannot_be_recorded() -> None:
    fake = FakeAttemptLedger(fail_record=True)
    with pytest.raises(CtxRejection) as exc_info:
        call(fake, signed("jti-y", now=time.time()))
    assert exc_info.value.code == ledger_module.INTERNAL
    assert "jti-y" in fake.claimed, "the jti stays claimed: no retry with the same _ctx"


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": "allowed_anyway"},
        {"outcome": "invalid_context", "jti": "unverified-jti"},
        {"outcome": "context_expired", "requester_member_id": "m-maya"},
        {"outcome": "accepted", "jti": None},
        {"outcome": "accepted", "jti": "j", "original_entry_id": LedgerEntryId(1)},
        {"outcome": "replay", "jti": "j", "mcp_endpoint": "evil-mcp"},
        {"outcome": "replay", "jti": "j", "tool_name": "knowledge search; DROP"},
        {"outcome": "replay", "jti": "j", "credential_id": "Bearer abc def"},
        # identifier-shaped, but not a credential NAME (could be a token value)
        {"outcome": "replay", "jti": "j", "credential_id": "not_a_known_credential_name"},
    ],
)
def test_validate_attempt_rejects_inconsistent_rows(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "credential_id": CREDENTIAL,
        "mcp_endpoint": ENDPOINT,
        "tool_name": TOOL,
        "outcome": "replay",
        "jti": "j",
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        ledger_module.validate_attempt(ledger_module.ToolCallAttempt(**values))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"outcome": "replay", "jti": "j", "original_entry_id": LedgerEntryId(1)},
        {"outcome": "accepted", "jti": "j", "request_id": "r", "workspace_id": "w"},
        {"outcome": "invalid_context"},
        {"outcome": "context_expired", "credential_id": "toolserver_bearer_workspace"},
    ],
)
def test_validate_attempt_accepts_consistent_rows(values: dict[str, object]) -> None:
    """Positive control for the rejection cases above."""
    base: dict[str, object] = {
        "credential_id": CREDENTIAL,
        "mcp_endpoint": ENDPOINT,
        "tool_name": TOOL,
    }
    ledger_module.validate_attempt(ledger_module.ToolCallAttempt(**{**base, **values}))  # type: ignore[arg-type]


def test_credential_ids_name_real_bearer_settings() -> None:
    """Attempt rows name a configured bearer setting, never a token value."""
    from services.toolserver.config import Settings

    secret_fields = {
        name for name in Settings.model_fields if name.startswith("toolserver_bearer_")
    }
    assert secret_fields == ledger_module.SERVICE_CREDENTIAL_IDS


# --- live database ----------------------------------------------------------


@pytest.fixture
def pool():
    """One pool per process: the ledger gets it injected, raw SQL shares it."""
    assert TOOLSERVER_DSN is not None
    shared = db.create_pool(TOOLSERVER_DSN)
    yield shared
    shared.close()


@pytest.fixture
def ledger(pool) -> Ledger:
    return Ledger(pool=pool)


def attempts_for(pool, jti: str) -> list[dict]:
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, jti, request_id, original_entry_id, credential_id, "
            "mcp_endpoint, tool_name FROM public.tool_call_attempts WHERE jti = %s ORDER BY id",
            (jti,),
        )
        names = [d.name for d in cur.description or []]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


@NEEDS_DB
def test_replay_leaves_durable_evidence(ledger: Ledger, pool) -> None:
    jti = uid("attempt")
    token = signed(jti, now=time.time())
    verified = call(ledger, token)
    entry_id = ledger.append(
        LedgerEntry(
            request_id=verified.ctx.request_id,
            trace_id=verified.ctx.trace_id,
            tool_name=TOOL,
            jti=verified.ctx.jti,
            execution_status=ToolExecutionStatus.EXECUTED,
            arguments=verified.business_arguments,
        )
    )
    with pytest.raises(CtxRejection) as exc_info:
        call(ledger, token)
    assert exc_info.value.code == REPLAY
    rows = attempts_for(pool, jti)
    assert [row["outcome"] for row in rows] == ["accepted", "replay"]
    assert rows[1]["original_entry_id"] == entry_id.value
    assert rows[0]["request_id"] == "req-attempt" and rows[1]["request_id"] is None


@NEEDS_DB
def test_rejected_attempt_row_has_no_identity(ledger: Ledger) -> None:
    attempt_id = ledger.record_attempt(
        ledger_module.ToolCallAttempt(
            credential_id=CREDENTIAL,
            mcp_endpoint=ENDPOINT,
            tool_name=TOOL,
            outcome=INVALID_CONTEXT,
        )
    )
    assert attempt_id > 0


@NEEDS_DB
@pytest.mark.parametrize(
    "values",
    [
        # outcome outside the fixed set
        ("bogus", None, None),
        # identity on an unverified outcome
        ("invalid_context", "jti-unverified", None),
        # replay reference on a non-replay
        ("accepted", "jti-z", -1),
    ],
)
def test_attempt_checks_hold_in_the_database(pool, values: tuple) -> None:
    """The schema enforces the same rules even if the writer were bypassed."""
    outcome, jti, original = values
    with (
        pytest.raises((errors.CheckViolation, errors.ForeignKeyViolation)),
        db.transaction(pool) as conn,
    ):
        conn.execute(
            "INSERT INTO public.tool_call_attempts (credential_id, mcp_endpoint, "
            "tool_name, outcome, jti, original_entry_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (CREDENTIAL, ENDPOINT, TOOL, outcome, jti, original),
        )


@NEEDS_DB
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("error_class", "Traceback: protected text"),
        ("execution_status", "not_reached"),
        ("validation_outcome", "anything goes"),
        ("arguments_sha256", "Z" * 64),
    ],
)
def test_ledger_row_checks_hold_in_the_database(pool, column: str, value: str) -> None:
    """0008 CHECKs (NOT VALID: new rows only) mirror the writer's validation."""
    row = {
        "request_id": "req-check",
        "trace_id": "trace-1",
        "tool_name": TOOL,
        "jti": uid("check"),
        "validation_outcome": "accepted",
        "execution_status": "executed",
        "error_class": None,
        "arguments_sha256": "0" * 64,
        "prev_hash": "0" * 64,
        "entry_hash": "0" * 64,
    }
    row[column] = value
    with pytest.raises(errors.CheckViolation), db.transaction(pool) as conn:
        conn.execute(
            f"INSERT INTO public.tool_execution_ledger ({', '.join(row)}) "  # noqa: S608 -- fixed test columns
            f"VALUES ({', '.join(['%s'] * len(row))})",
            tuple(row.values()),
        )


@NEEDS_DB
def test_ledger_id_is_generated_always(pool) -> None:
    with pytest.raises(errors.GeneratedAlways), db.transaction(pool) as conn:
        conn.execute(
            "INSERT INTO public.tool_execution_ledger (id, request_id, trace_id, tool_name, "
            "jti, execution_status, arguments_sha256, prev_hash, entry_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (-1, "req", "trace", TOOL, uid("id"), "executed", "0" * 64, "0" * 64, "0" * 64),
        )
