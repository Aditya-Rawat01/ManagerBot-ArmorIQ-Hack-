"""Backend-role PostgreSQL access (P3-01, PRD v2 §8.1, Arch §7.5).

Uses ``DATABASE_URL_BACKEND`` only — the DSN of the login user carrying the
``proxylead_backend`` role (app metadata, never protected content). This
module never reads any other DSN: there is deliberately no parameter, helper,
or fallback for tool-server or migration credentials, so a caller cannot
substitute them by accident. ``services/audit`` writers receive the
:class:`AuditDatabase` protocol, which keeps unit tests offline.

Role defaults (migrations/0005_roles_grants.sql) already set a 10 s statement
timeout for the backend role; the pool repeats it per connection so the bound
holds even where role settings do not apply. Neon connections require
``sslmode=require`` in the DSN (operator procedure in migrations/README.md).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from psycopg_pool import ConnectionPool

STATEMENT_TIMEOUT = "10s"
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5


class BackendDbNotConfiguredError(Exception):
    """Raised when no backend DSN is available. Callers fail closed."""


class AuditCursor(Protocol):
    """The cursor surface ``services/audit`` writers use (real or fake)."""

    rowcount: int

    def execute(self, query: str, params: object = None) -> object: ...
    def fetchone(self) -> object: ...
    def fetchall(self) -> list[object]: ...


class AuditDatabase(Protocol):
    """A transactional source of :class:`AuditCursor` (pool-backed or fake)."""

    @contextmanager
    def tx(self) -> Iterator[AuditCursor]: ...


class BackendDatabase:
    """Lazily-opened ``psycopg`` pool for exactly one DSN: the backend role's.

    Nothing is connected at import or construction time, so importing this
    module and running offline tests never needs production credentials.
    """

    def __init__(self, dsn: str | None) -> None:
        if not dsn:
            raise BackendDbNotConfiguredError(
                "DATABASE_URL_BACKEND is not set; refusing to guess a database"
            )
        self._pool = ConnectionPool(
            dsn,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            open=False,
            kwargs={"options": f"-c statement_timeout={STATEMENT_TIMEOUT}"},
        )
        self._opened = False

    def open(self) -> None:
        if not self._opened:
            self._pool.open()
            self._opened = True

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False

    @contextmanager
    def tx(self) -> Iterator[AuditCursor]:
        """Yield a cursor inside one transaction: commit on success, rollback on error."""
        self.open()
        with self._pool.connection() as connection, connection.cursor() as cursor:
            try:
                yield cursor  # type: ignore[misc]
            except Exception:
                connection.rollback()
                raise
            connection.commit()
