"""Database surface the audit store needs (P3-01).

``services/audit`` depends only on these structural protocols, never on
``apps`` (services must not import the API package). The pool-backed
implementation is ``apps.api.core.db.BackendDatabase`` (``DATABASE_URL_BACKEND``
only); offline tests pass fakes. ``apps.api.core.db`` re-exports both names for
backward compatibility.
"""

from contextlib import AbstractContextManager
from typing import Protocol


class AuditCursor(Protocol):
    """The cursor surface ``services/audit`` writers use (real or fake)."""

    rowcount: int

    def execute(self, query: str, params: object = None) -> object: ...
    def fetchone(self) -> object: ...
    def fetchall(self) -> list[object]: ...


class AuditDatabase(Protocol):
    """A transactional source of :class:`AuditCursor` (pool-backed or fake).

    ``tx()`` yields a cursor inside ONE transaction: commit on success,
    rollback on any exception.
    """

    def tx(self) -> AbstractContextManager[AuditCursor]: ...


__all__ = ["AuditCursor", "AuditDatabase"]
