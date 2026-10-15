"""Toolserver-role PostgreSQL access (P2-01).

Thin helpers over psycopg 3 + psycopg_pool (ADR-028): one pool per DSN,
statement timeout on every connection, read-only transaction support, and
parameterized queries only — helpers take bound parameters, never formatted
SQL. No knowledge of ArmorIQ, plans, or tenants lives here; callers pass
fully-formed queries with server-side tier filters baked in.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg_pool import ConnectionPool

DEFAULT_STATEMENT_TIMEOUT_MS = 30000


def create_pool(
    dsn: str,
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    min_size: int = 1,
    max_size: int = 10,
) -> ConnectionPool:
    """Create one pool for a single role DSN (one pool per process).

    The timeout rides the connection options, so slow queries fail instead of
    hanging a retrieval handler (Arch §7.1: never bypass authorization
    because a query is slow).
    """
    return ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"options": f"-c statement_timeout={statement_timeout_ms}"},
    )


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
