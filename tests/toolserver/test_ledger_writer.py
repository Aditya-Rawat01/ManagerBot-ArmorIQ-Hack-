"""Offline tests for the ledger writer (P2-07 follow-up).

A fake pool/connection records every statement, so the transaction shape of
`Ledger.append` is checked without a database: forced READ COMMITTED, the
fixed advisory-lock key, `created_at` read under the lock and hashed, the
per-transaction statement timeout, the jti-constraint check, input
validation, and pool injection. Live behavior is covered by
test_ledger_chain.py / test_idempotency.py against a local database.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import errors

from services.contracts import ToolExecutionStatus
from services.toolserver import db
from services.toolserver import ledger as ledger_module
from services.toolserver.ledger import DuplicateJti, Ledger, LedgerEntry, compute_entry_hash

NOW = datetime(2026, 10, 16, 15, 0, 0, 654321, tzinfo=timezone(timedelta(hours=5, minutes=30)))
TIP = "a" * 64
ORIGINAL_ID = 7
NEW_ID = 41


class FakeUniqueViolation(errors.UniqueViolation):
    """A UniqueViolation whose `diag` names the violated constraint."""

    def __init__(self, constraint_name: str) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self._constraint_name = constraint_name

    @property
    def diag(self) -> Any:  # type: ignore[override]
        return SimpleNamespace(constraint_name=self._constraint_name)


def _text(query: object) -> str:
    if isinstance(query, str):
        return query
    return query.as_string()  # type: ignore[attr-defined]  # psycopg.sql.Composed


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn
        self._row: tuple | None = None
        self.description = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: object, params: tuple = ()) -> "FakeCursor":
        text = " ".join(_text(query).split())
        self._conn.log.append((text, params))
        self._row = self._conn.respond(text, params)
        return self

    def fetchone(self) -> tuple | None:
        return self._row


class FakeConnection:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.insert_error: Exception | None = None
        self.inserted: list[tuple] = []

    def respond(self, text: str, params: tuple) -> tuple | None:
        if text.startswith("INSERT INTO public.tool_execution_ledger"):
            if self.insert_error is not None:
                raise self.insert_error
            self.inserted.append(params)
            return (NEW_ID,)
        if text.startswith("INSERT INTO public.tool_call_attempts"):
            self.inserted.append(params)
            return (5,)
        if "clock_timestamp()" in text:
            return (NOW,)
        if text.startswith("SELECT entry_hash FROM public.tool_execution_ledger"):
            return (TIP,)
        if text.startswith("SELECT id FROM public.tool_execution_ledger WHERE jti"):
            return (ORIGINAL_ID,)
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, query: object, params: tuple = ()) -> FakeCursor:
        return self.cursor().execute(query, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.log.append(("BEGIN", None))
        try:
            yield
        except BaseException:
            self.log.append(("ROLLBACK", None))
            raise
        self.log.append(("COMMIT", None))

    def rollback(self) -> None:
        self.log.append(("ROLLBACK", None))


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConnection()
        self.closed = False

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[FakeConnection]:
        yield self.conn

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> FakePool:
    fake = FakePool()
    # The DSN path goes through db.create_pool, so these tests also run
    # against code that predates pool injection.
    monkeypatch.setattr(db, "create_pool", lambda *args, **kwargs: fake)
    return fake


def make_entry(**overrides: object) -> LedgerEntry:
    values: dict[str, object] = {
        "request_id": "req-1",
        "trace_id": "trace-1",
        "tool_name": "knowledge_search_team",
        "jti": "jti-1",
        "execution_status": ToolExecutionStatus.EXECUTED,
        "plan_id": "plan-1",
        "step_index": 0,
        "requester_member_id": "m-sam",
        "workspace_id": "workspace-primary",
        "arguments": {"query": "decisions"},
    }
    values.update(overrides)
    return LedgerEntry(**values)  # type: ignore[arg-type]


def statements(conn: FakeConnection) -> list[str]:
    return [text for text, _ in conn.log]


def index_of(conn: FakeConnection, prefix: str) -> int:
    return next(i for i, text in enumerate(statements(conn)) if text.startswith(prefix))


# --- finding 1: created_at is read under the lock and hashed ----------------


def test_append_inserts_and_hashes_created_at(pool: FakePool) -> None:
    assert Ledger("postgresql://fake").append(make_entry()).value == NEW_ID
    conn = pool.conn
    lock = index_of(conn, "SELECT pg_advisory_xact_lock")
    clock = index_of(conn, "SELECT clock_timestamp()")
    insert = index_of(conn, "INSERT INTO public.tool_execution_ledger")
    assert lock < clock < insert, "created_at must be taken after the lock"
    insert_sql, params = conn.log[insert]
    columns = insert_sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
    row = dict(zip(columns, params, strict=True))
    assert row["created_at"] == NOW
    assert row["prev_hash"] == TIP
    assert row["entry_hash"] == compute_entry_hash(row, TIP)
    assert row["entry_hash"] != compute_entry_hash(dict(row, created_at=None), TIP)


def test_created_at_hash_is_utc_iso_with_microseconds() -> None:
    utc = NOW.astimezone(UTC)
    assert ledger_module.canonical_timestamp(NOW) == "2026-10-16T09:30:00.654321+00:00"
    assert compute_entry_hash({"created_at": NOW}, TIP) == compute_entry_hash(
        {"created_at": utc}, TIP
    )
    with pytest.raises(ValueError):
        ledger_module.canonical_timestamp(NOW.replace(tzinfo=None))


# --- finding 3: only the jti constraint is a duplicate jti -----------------


def test_primary_key_clash_is_not_reported_as_duplicate_jti(pool: FakePool) -> None:
    pool.conn.insert_error = FakeUniqueViolation("tool_execution_ledger_pkey")
    with pytest.raises(errors.UniqueViolation) as exc_info:
        Ledger("postgresql://fake").append(make_entry())
    assert not isinstance(exc_info.value, DuplicateJti)


def test_jti_constraint_clash_is_duplicate_jti(pool: FakePool) -> None:
    pool.conn.insert_error = FakeUniqueViolation("tool_execution_ledger_jti_key")
    with pytest.raises(DuplicateJti) as exc_info:
        Ledger("postgresql://fake").append(make_entry())
    assert exc_info.value.existing_id.value == ORIGINAL_ID


# --- finding 4: READ COMMITTED forced; fixed documented lock key ------------


def test_append_forces_read_committed_as_first_statement(pool: FakePool) -> None:
    Ledger("postgresql://fake").append(make_entry())
    log = statements(pool.conn)
    begin = log.index("BEGIN")
    assert log[begin + 1] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert begin + 1 < index_of(pool.conn, "SELECT pg_advisory_xact_lock")


def test_append_lock_key_is_a_fixed_64bit_constant(pool: FakePool) -> None:
    Ledger("postgresql://fake").append(make_entry())
    lock_sql, params = pool.conn.log[index_of(pool.conn, "SELECT pg_advisory_xact_lock")]
    assert "hashtext" not in lock_sql
    assert lock_sql == "SELECT pg_advisory_xact_lock(%s)"
    assert params == (ledger_module.APPEND_LOCK_KEY,)
    assert int.from_bytes(b"pxledger", "big", signed=True) == ledger_module.APPEND_LOCK_KEY
    assert -(2**63) <= ledger_module.APPEND_LOCK_KEY < 2**63


# --- finding 5: no free text, no meaningless rows ---------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"error_class": "Traceback: SELECT secret FROM team_notes"},
        {"error_class": "has space"},
        {"error_class": "x" * 129},
        {"validation_outcome": "accepted, but here is protected content"},
        {"validation_outcome": "replay"},
        {"execution_status": ToolExecutionStatus.NOT_REACHED},
        {
            "validation_outcome": "invalid_arguments",
            "execution_status": ToolExecutionStatus.EXECUTED,
        },
    ],
)
def test_append_rejects_invalid_fields_before_touching_the_database(
    pool: FakePool, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        Ledger("postgresql://fake").append(make_entry(**overrides))
    assert pool.conn.log == []


def test_append_accepts_dotted_error_class_and_post_verify_rejection(pool: FakePool) -> None:
    handle = Ledger("postgresql://fake")
    handle.append(make_entry(error_class="psycopg.errors.QueryCanceled"))
    handle.append(
        make_entry(
            jti="jti-2",
            validation_outcome="invalid_arguments",
            execution_status=ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
        )
    )
    assert len(pool.conn.inserted) == 2


# --- finding 7: pool injection ----------------------------------------------


def test_injected_pool_is_used_and_not_closed() -> None:
    shared = FakePool()
    handle = Ledger(pool=shared)  # type: ignore[arg-type]
    handle.append(make_entry())
    assert shared.conn.inserted
    handle.close()
    assert shared.closed is False, "the process owns an injected pool"


def test_dsn_pool_is_owned_and_closed(pool: FakePool) -> None:
    Ledger("postgresql://fake").close()
    assert pool.closed is True


def test_ledger_needs_exactly_one_of_dsn_or_pool() -> None:
    with pytest.raises(ValueError):
        Ledger()
    with pytest.raises(ValueError):
        Ledger("postgresql://fake", pool=FakePool())  # type: ignore[arg-type]


# --- finding 8: every ledger transaction carries a statement timeout --------


def test_every_ledger_transaction_sets_local_statement_timeout(pool: FakePool) -> None:
    handle = Ledger("postgresql://fake")
    handle.append(make_entry())
    handle.seen_jti("jti-9")
    handle.find_entry_id("jti-1")
    log = statements(pool.conn)
    begins = [i for i, text in enumerate(log) if text == "BEGIN"]
    assert len(begins) == 3
    timeout = f"SET LOCAL statement_timeout = {db.DEFAULT_STATEMENT_TIMEOUT_MS}"
    for begin in begins:
        window = log[begin : begin + 3]
        assert timeout in window, window
