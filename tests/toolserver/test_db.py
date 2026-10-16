"""Offline tests for the toolserver connection helpers (P2-01).

Fake connections prove query/parameter plumbing and read-only handling
without a database. Live behavior is proven by scripts/verify_db_grants.py
and tests/toolserver/test_db_grants.py against a migrated database.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from services.toolserver import db


class FakeCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, params: tuple = ()) -> None:
        self.statements.append((query, params))

    def fetchall(self) -> list[tuple]:
        return list(self.rows)

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.read_only = False
        self.log: list[str] = []

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def execute(self, query: object, params: tuple = ()) -> None:
        text = query if isinstance(query, str) else query.as_string()  # type: ignore[attr-defined]
        self.log.append(text)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.log.append("BEGIN")
        try:
            yield
        except BaseException:
            self.log.append("ROLLBACK")
            raise
        self.log.append("COMMIT")


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConnection()

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[FakeConnection]:
        yield self.conn


def test_fetch_all_passes_parameters() -> None:
    conn = FakeConnection()
    conn.cursor_obj.rows = [(1,), (2,)]
    assert db.fetch_all(conn, "SELECT 1 FROM t WHERE a = %s", ("x",)) == [(1,), (2,)]
    assert conn.cursor_obj.statements == [("SELECT 1 FROM t WHERE a = %s", ("x",))]


def test_fetch_one_returns_none_when_empty() -> None:
    conn = FakeConnection()
    assert db.fetch_one(conn, "SELECT 1 FROM t WHERE a = %s", ("x",)) is None


def test_execute_returns_rowcount() -> None:
    conn = FakeConnection()
    conn.cursor_obj.rowcount = 3
    assert db.execute(conn, "UPDATE t SET a = %s", ("x",)) == 3


def test_read_only_restores_previous_mode() -> None:
    conn = FakeConnection()
    with db.read_only(conn):
        assert conn.read_only is True
    assert conn.read_only is False


def test_no_query_string_is_built_from_inputs() -> None:
    # Helpers accept only bound parameters; a hostile value must travel as a
    # parameter, never interpolated into SQL text.
    conn = FakeConnection()
    hostile = "x'; DROP TABLE t; --"
    db.fetch_all(conn, "SELECT 1 FROM t WHERE a = %s", (hostile,))
    query, params = conn.cursor_obj.statements[0]
    assert query == "SELECT 1 FROM t WHERE a = %s"
    assert params == (hostile,)


def test_create_pool_is_compatible_with_a_transaction_pooler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neon's pooled endpoint rejects the `options` startup parameter and
    cannot keep server-side prepared statements bound to one connection."""
    captured: dict[str, object] = {}

    def fake_pool(dsn: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db, "ConnectionPool", fake_pool)
    db.create_pool("postgresql://fake")
    connect_kwargs = dict(captured.get("kwargs") or {})  # type: ignore[call-overload]
    assert "options" not in captured
    assert "options" not in connect_kwargs
    assert "prepare_threshold" in connect_kwargs
    assert connect_kwargs["prepare_threshold"] is None


def test_create_pool_bounds_the_connection_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database outage must fail a checkout within seconds, not psycopg_pool's
    30 s default, so tool calls cannot park the worker threads /health shares."""
    captured: dict[str, object] = {}

    def fake_pool(dsn: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db, "ConnectionPool", fake_pool)
    db.create_pool("postgresql://fake")
    assert captured.get("timeout") == 5.0
    db.create_pool("postgresql://fake", timeout_s=1.5)
    assert captured.get("timeout") == 1.5


def test_transaction_sets_local_statement_timeout_and_commits() -> None:
    pool = FakePool()
    with db.transaction(pool) as conn:
        conn.execute("SELECT 1")
    assert pool.conn.log == [
        "BEGIN",
        f"SET LOCAL statement_timeout = {db.DEFAULT_STATEMENT_TIMEOUT_MS}",
        "SELECT 1",
        "COMMIT",
    ]


def test_transaction_read_committed_comes_first_and_rolls_back_on_error() -> None:
    pool = FakePool()
    with pytest.raises(RuntimeError), db.transaction(pool, read_committed=True) as conn:
        conn.execute("SELECT 1")
        raise RuntimeError("boom")
    assert pool.conn.log == [
        "BEGIN",
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SET LOCAL statement_timeout = 30000",
        "SELECT 1",
        "ROLLBACK",
    ]


@pytest.mark.parametrize("bad", [0, -5, True, "30s; DROP TABLE t", 1.5])
def test_apply_statement_timeout_accepts_positive_ints_only(bad: object) -> None:
    with pytest.raises(ValueError):
        db.apply_statement_timeout(FakeConnection(), bad)  # type: ignore[arg-type]
