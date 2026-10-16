"""Toolserver-role PostgreSQL access (P2-01).

Thin helpers over psycopg 3 + psycopg_pool (ADR-028): one pool per process
(create it once at startup and share it — the ledger accepts an injected pool),
a statement timeout on every transaction, read-only transaction support, and
parameterized queries only — helpers take bound parameters, never formatted
SQL. No knowledge of ArmorIQ, plans, or tenants lives here; callers pass
fully-formed queries with server-side tier filters baked in.

Statement timeout: the operator sets it on the toolserver LOGIN user
(migrations/README.md step 4; settings on the NOLOGIN group role are not
inherited), and :func:`transaction` repeats it per transaction with
``SET LOCAL``. The libpq ``options`` startup parameter is deliberately not
used: Neon's pooled (``-pooler``) endpoint rejects it. Callers that check out
a pooled connection directly call :func:`apply_statement_timeout` at the start
of each transaction (``SET LOCAL`` ends with the transaction). Automatic
server-side prepared statements are off for the same pooler.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import sql
from psycopg_pool import ConnectionPool

DEFAULT_STATEMENT_TIMEOUT_MS = 30000
# Seconds a caller waits for a pooled connection before failing (psycopg_pool
# default: 30). Short, so a database outage fails tool calls fast instead of
# parking worker threads that the rest of the app (e.g. /health) shares.
DEFAULT_POOL_TIMEOUT_S = 5.0


def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    timeout_s: float = DEFAULT_POOL_TIMEOUT_S,
) -> ConnectionPool:
    """Create the pool for a single role DSN (one pool per process).

    No startup ``options``: the timeout is applied per transaction (see the
    module docstring), so slow queries fail instead of hanging a retrieval
    handler (Arch §7.1: never bypass authorization because a query is slow).
    ``prepare_threshold=None`` disables psycopg's automatic server-side
    prepared statements, which a transaction-mode pooler (Neon ``-pooler``)
    cannot keep bound to one server connection. ``timeout_s`` bounds the wait
    for a connection (``PoolTimeout`` after it).
    """
    return ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"prepare_threshold": None},
        timeout=timeout_s,
        open=True,
    )


def apply_statement_timeout(
    conn: Any, statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
) -> None:
    """``SET LOCAL statement_timeout`` for the current transaction only.

    ``SET`` takes no bind parameters, so the value is validated as a positive
    int and composed as a literal (never formatted from caller text). Works
    through Neon's pooled endpoint, unlike session-level ``SET``.
    """
    if (
        isinstance(statement_timeout_ms, bool)
        or not isinstance(statement_timeout_ms, int)
        or statement_timeout_ms <= 0
    ):
        raise ValueError("statement_timeout_ms must be a positive int")
    conn.execute(
        sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(statement_timeout_ms))
    )


@contextmanager
def transaction(
    pool: Any,
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    read_committed: bool = False,
) -> Iterator[Any]:
    """Check out a connection and run the block in one transaction.

    Commits on success, rolls back on error (also on autocommit connections:
    ``conn.transaction()`` issues an explicit ``BEGIN`` there). With
    ``read_committed`` the isolation level is forced as the transaction's
    first statement, whatever the connection's default is.
    """
    with pool.connection() as conn, conn.transaction():
        if read_committed:
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        apply_statement_timeout(conn, statement_timeout_ms)
        yield conn


def fetch_all(conn: Any, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Run a parameterized SELECT; return all rows (callers apply limits)."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def fetch_one(conn: Any, query: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    """Run a parameterized SELECT; return one row or None."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return tuple(row) if row is not None else None


def execute(conn: Any, query: str, params: tuple[Any, ...] = ()) -> int:
    """Run a parameterized INSERT/UPDATE/DELETE; return the row count."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.rowcount


@contextmanager
def read_only(conn: Any) -> Iterator[None]:
    """Run the block in a read-only transaction; restore the previous mode."""
    previous = conn.read_only
    conn.read_only = True
    try:
        yield
    finally:
        conn.read_only = previous
