"""Hash-chain tests (P2-07). Pure chain-logic tests run anywhere; database
tests need DATABASE_URL_TOOLSERVER (live database) and skip otherwise, so CI
(without secrets) stays green. DSNs are captured at collection time because
the conftest scrubs the environment during test runs; they are never logged.
"""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from services.contracts import ToolExecutionStatus
from services.toolserver.ledger import (
    GENESIS_PREV_HASH,
    Ledger,
    LedgerEntry,
    VerificationReport,
    arguments_sha256,
    compute_entry_hash,
    verify_rows,
)

TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")
# The ledger and the claims table are append-only (no DELETE for any runtime
# role), so these tests write PERMANENT rows. Run them only against a local
# or throwaway database, never the shared Neon evidence ledger.
WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"

NEEDS_DB = pytest.mark.skipif(
    not (TOOLSERVER_DSN and WRITE_TESTS),
    reason="needs DATABASE_URL_TOOLSERVER and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent ledger rows: local database only)",
)


def uid(prefix: str) -> str:
    """Unique per run, so re-runs never collide with earlier permanent rows."""
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


BASE_FIELDS = {
    "request_id": "req-1",
    "trace_id": "trace-1",
    "plan_id": "plan-1",
    "armoriq_plan_id": None,
    "step_index": 0,
    "tool_name": "knowledge_search_team",
    "jti": "jti-1",
    "requester_member_id": "m-sam",
    "workspace_id": "workspace-primary",
    "validation_outcome": "accepted",
    "execution_status": "executed",
    "result_id": None,
    "error_class": None,
    "arguments_sha256": arguments_sha256({"query": "decisions"}),
}


T0 = datetime(2026, 10, 16, 9, 30, 0, 123456, tzinfo=UTC)


def make_rows(count: int) -> list[dict]:
    rows = []
    prev = GENESIS_PREV_HASH
    for index in range(count):
        fields = dict(BASE_FIELDS, jti=f"jti-{index}", created_at=T0 + timedelta(seconds=index))
        entry_hash = compute_entry_hash(fields, prev)
        rows.append({"id": index + 1, **fields, "prev_hash": prev, "entry_hash": entry_hash})
        prev = entry_hash
    return rows


def make_entry(jti: str, **overrides: object) -> LedgerEntry:
    values: dict[str, object] = {
        "request_id": uid("req"),
        "trace_id": "trace-1",
        "tool_name": "knowledge_search_team",
        "jti": jti,
        "execution_status": ToolExecutionStatus.EXECUTED,
        "plan_id": "plan-1",
        "step_index": 0,
        "requester_member_id": "m-sam",
        "workspace_id": "workspace-primary",
        "arguments": {"query": "decisions"},
    }
    values.update(overrides)
    return LedgerEntry(**values)  # type: ignore[arg-type]


@pytest.fixture
def ledger() -> Ledger:
    assert TOOLSERVER_DSN is not None
    handle = Ledger(TOOLSERVER_DSN)
    yield handle
    handle.close()


# --- pure chain logic (no database) -----------------------------------------


def test_chain_valid() -> None:
    report = verify_rows(make_rows(3))
    assert report == VerificationReport(ok=True, checked=3, broken_at=None, detail="chain valid")


def test_detects_modification() -> None:
    rows = make_rows(3)
    rows[1]["tool_name"] = "knowledge_search_restricted"
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 2


def test_detects_reordering() -> None:
    rows = make_rows(3)
    rows[1], rows[2] = rows[2], rows[1]
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 3


def test_detects_interior_deletion() -> None:
    rows = make_rows(3)
    del rows[1]
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 3


def test_tail_truncation_is_undetectable() -> None:
    """Documents the limitation: removed newest rows leave a valid prefix."""
    assert verify_rows(make_rows(3)[:2]).ok


def test_detects_created_at_modification() -> None:
    """The execution timestamp is hashed: moving it by 1 µs breaks the row."""
    rows = make_rows(3)
    rows[1]["created_at"] = rows[1]["created_at"] + timedelta(microseconds=1)
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 2


def test_created_at_is_normalized_to_utc() -> None:
    """The same instant in another session time zone verifies identically."""
    rows = make_rows(2)
    ist = timezone(timedelta(hours=5, minutes=30))
    for row in rows:
        row["created_at"] = row["created_at"].astimezone(ist)
    assert verify_rows(rows).ok


def test_naive_created_at_fails_verification_instead_of_crashing() -> None:
    rows = make_rows(2)
    rows[0]["created_at"] = rows[0]["created_at"].replace(tzinfo=None)
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 1


# --- live database ----------------------------------------------------------


@NEEDS_DB
def test_append_and_verify_chain(ledger: Ledger) -> None:
    first = ledger.append(make_entry(uid("chain")))
    second = ledger.append(make_entry(uid("chain")))
    assert second.value > first.value
    report = ledger.verify_chain()
    assert report.ok and report.checked >= 2


@NEEDS_DB
def test_created_at_round_trips_into_the_hash(ledger: Ledger) -> None:
    """The stored created_at is exactly the hashed one (µs precision, tz-aware)."""
    jti = uid("created")
    ledger.append(make_entry(jti))
    stored = ledger.get_by_jti(jti)
    assert stored is not None
    assert stored["created_at"].tzinfo is not None
    assert compute_entry_hash(stored, stored["prev_hash"]) == stored["entry_hash"]
    edited = dict(stored, created_at=stored["created_at"] - timedelta(seconds=1))
    assert compute_entry_hash(edited, stored["prev_hash"]) != stored["entry_hash"]


@NEEDS_DB
def test_concurrent_appends_single_chain(ledger: Ledger) -> None:
    ids: list[int] = []
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            entry_id = ledger.append(make_entry(uid(f"concurrent-{index}")))
            ids.append(entry_id.value)
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(ids)) == 20
    assert ledger.verify_chain().ok


@NEEDS_DB
def test_concurrent_appends_single_chain_under_repeatable_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pool whose connections default to REPEATABLE READ must not fork the
    chain: append forces READ COMMITTED as its first statement. (Under
    REPEATABLE READ the snapshot predates the lock wait, so the tip read
    after the lock would be stale.) The pool is swapped in through
    `db.create_pool`, so this also runs against the pre-fix writer."""
    from psycopg import IsolationLevel
    from psycopg_pool import ConnectionPool

    from services.toolserver import db

    def repeatable_read(conn) -> None:
        conn.isolation_level = IsolationLevel.REPEATABLE_READ

    pools: list[ConnectionPool] = []

    def rr_pool(dsn: str, **kwargs: object) -> ConnectionPool:
        pool = ConnectionPool(dsn, min_size=1, max_size=10, configure=repeatable_read)
        pools.append(pool)
        return pool

    monkeypatch.setattr(db, "create_pool", rr_pool)
    assert TOOLSERVER_DSN is not None
    handle = Ledger(TOOLSERVER_DSN)
    errors: list[Exception] = []
    barrier = threading.Barrier(20)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            handle.append(make_entry(uid(f"rr-{index}")))
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert handle.verify_chain().ok
    finally:
        handle.close()
        for pool in pools:
            pool.close()
