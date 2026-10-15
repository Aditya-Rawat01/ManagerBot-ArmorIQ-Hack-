"""Backend-role PostgreSQL access (P3-01, PRD v2 §8.1, Arch §7.5).

Uses ``DATABASE_URL_BACKEND`` only — the DSN of the login user carrying the
``proxylead_backend`` role (app metadata, never protected content). This
module never reads any other DSN: there is deliberately no parameter, helper,
or fallback for tool-server or migration credentials, so a caller cannot
substitute them by accident. :meth:`BackendDatabase.from_settings` reads only
``Settings.database_url_backend``.

Fail closed on the wrong credentials (P3-01 §9.1): before the pool opens, and
again on every new pooled connection (pool ``configure``), the session user
must be a member of ``proxylead_backend``, must NOT be a member of
``proxylead_toolserver``, must not be a superuser, and must not own (or be a
member of the owner of) the ``public`` schema or any object in it. Otherwise
:class:`BackendRoleError` is raised — the DSN and user name never appear in
the message.

``services/audit`` writers receive the :class:`AuditDatabase` protocol
(defined in :mod:`services.audit.protocols`, re-exported here for import
compatibility), which keeps unit tests offline. Driver and pool failures
inside :meth:`BackendDatabase.tx` surface as
:class:`~services.audit.errors.AuditStoreError`; so does a missing or wrong
DSN (:class:`BackendDbNotConfiguredError` subclasses it).

Statement timeout: the operator sets it on the backend LOGIN user
(migrations/README.md step 4; settings on the NOLOGIN group role are not
inherited), and every transaction repeats it with ``SET LOCAL``. The libpq
``options`` startup parameter is deliberately not used: Neon's pooled
(``-pooler``) endpoint rejects it; server-side prepared statements are
disabled (``prepare_threshold=None``) for the same transaction-pooling
endpoint. JSON payloads (``dict``) are sent as ``jsonb`` (psycopg 3 does not
adapt ``dict`` by default). Neon connections require ``sslmode=require`` in
the DSN.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
from psycopg import Connection
from psycopg.types.json import JsonbDumper
from psycopg_pool import ConnectionPool

from services.audit.errors import AuditError, AuditStoreError
from services.audit.protocols import AuditCursor, AuditDatabase

if TYPE_CHECKING:
    from apps.api.core.config import Settings

STATEMENT_TIMEOUT = "10s"
CONNECT_TIMEOUT_S = 10
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5

BACKEND_GROUP_ROLE = "proxylead_backend"
_FORBIDDEN_GROUP_ROLE = "proxylead_toolserver"

# One row: (member of backend, member of toolserver, superuser, owns schema
# objects). Ownership counts membership in the owner role, so a login that
# inherits the migration owner is rejected too. Read-only catalog query.
_ROLE_CHECK_SQL = (
    "SELECT pg_has_role(current_user, %s, 'MEMBER'), "
    "pg_has_role(current_user, %s, 'MEMBER'), "
    "r.rolsuper, "
    "EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname = 'public' "
    "AND pg_has_role(current_user, n.nspowner, 'MEMBER')) "
    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND pg_has_role(current_user, c.relowner, 'MEMBER')) "
    "FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
)


class BackendDbNotConfiguredError(AuditStoreError):
    """Raised when no usable backend DSN is available. Callers fail closed.

    Subclasses :class:`AuditStoreError` so "every audit failure is an
    ``AuditStoreError``" also covers a missing database.
    """


class BackendRoleError(BackendDbNotConfiguredError):
    """The DSN's session user is not a pure ``proxylead_backend`` login."""


def verify_backend_role(connection: Connection) -> None:
    """Raise :class:`BackendRoleError` unless the session is a backend login.

    Leaves the connection idle (``rollback``), as the pool requires of
    ``configure`` callbacks.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(_ROLE_CHECK_SQL, (BACKEND_GROUP_ROLE, _FORBIDDEN_GROUP_ROLE))
            row = cursor.fetchone()
    finally:
        connection.rollback()
    if row is None:
        raise BackendRoleError("backend DSN session user could not be resolved; refusing it")
    is_backend, is_toolserver, is_superuser, owns_schema = (bool(value) for value in row)
    problems = [
        message
        for failed, message in (
            (not is_backend, f"is not a member of {BACKEND_GROUP_ROLE}"),
            (is_toolserver, f"is a member of {_FORBIDDEN_GROUP_ROLE}"),
            (is_superuser, "is a superuser"),
            (owns_schema, "owns the public schema or objects in it"),
        )
        if failed
    ]
    if problems:
        raise BackendRoleError(
            "DATABASE_URL_BACKEND must name a login that is only a proxylead_backend "
            f"member; its session user {', '.join(problems)}. Refusing to use it."
        )


def _configure(connection: Connection) -> None:
    """Per-connection setup: role check, then send Python dicts as jsonb."""
    verify_backend_role(connection)
    connection.adapters.register_dumper(dict, JsonbDumper)


class BackendDatabase:
    """Lazily-opened ``psycopg`` pool for exactly one DSN: the backend role's.

    Nothing is connected at import or construction time, so importing this
    module and running offline tests never needs production credentials.
    The first :meth:`tx` (or :meth:`open`) verifies the role before the pool
    opens; every pooled connection is verified again as it is created.
    """

    def __init__(self, dsn: str | None) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise BackendDbNotConfiguredError(
                "DATABASE_URL_BACKEND is not set; refusing to guess a database"
            )
        self._dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            open=False,
            configure=_configure,
            kwargs={"prepare_threshold": None},
        )
        self._opened = False
        self._open_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<BackendDatabase opened={self._opened}>"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> BackendDatabase:
        """Build from ``Settings.database_url_backend`` — the only DSN it reads."""
        if settings is None:
            from apps.api.core.config import get_settings

            settings = get_settings()
        secret = settings.database_url_backend
        return cls(secret.get_secret_value() if secret is not None else None)

    def open(self) -> None:
        with self._open_lock:
            if self._opened:
                return
            # Probe once synchronously so a wrong DSN fails immediately with
            # BackendRoleError instead of the pool retrying in the background.
            with psycopg.connect(
                self._dsn, connect_timeout=CONNECT_TIMEOUT_S, prepare_threshold=None
            ) as probe:
                verify_backend_role(probe)
            self._pool.open()
            self._opened = True

    def close(self) -> None:
        with self._open_lock:
            if self._opened:
                self._pool.close()
                self._opened = False

    @contextmanager
    def tx(self) -> Iterator[AuditCursor]:
        """Yield a cursor inside one transaction: commit on success, rollback on error.

        Driver/pool failures are re-raised as ``AuditStoreError`` naming only
        the exception class (never the DSN or statement parameters).
        """
        try:
            self.open()
            with self._pool.connection() as connection, connection.cursor() as cursor:
                try:
                    # Bound every statement even where the role setting is
                    # missing; SET LOCAL also works through Neon's pooled endpoint.
                    cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                    yield cursor  # type: ignore[misc]
                except Exception:
                    connection.rollback()
                    raise
                connection.commit()
        except AuditError:
            raise
        except psycopg.Error as exc:
            # psycopg_pool's PoolTimeout / PoolClosed / TooManyRequests are
            # psycopg.OperationalError subclasses, so they land here too.
            raise AuditStoreError(f"backend database error ({type(exc).__name__})") from exc


__all__ = [
    "BACKEND_GROUP_ROLE",
    "STATEMENT_TIMEOUT",
    "AuditCursor",
    "AuditDatabase",
    "BackendDatabase",
    "BackendDbNotConfiguredError",
    "BackendRoleError",
    "verify_backend_role",
]
