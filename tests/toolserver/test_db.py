"""Offline tests for the toolserver connection helpers (P2-01).

Fake connections prove query/parameter plumbing and read-only handling
without a database. Live behavior is proven by scripts/verify_db_grants.py
and tests/toolserver/test_db_grants.py against a migrated database.
"""

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

    def cursor(self) -> FakeCursor:
        return self.cursor_obj


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
