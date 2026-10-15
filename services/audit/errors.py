"""Typed audit failures (P3-01).

Callers (P3-04 orchestrator, P4-01/P4-04 routes) fail closed on these: a
required audit write that raises is never treated as success.
"""


class AuditError(Exception):
    """Base for all audit failures."""


class AuditStoreError(AuditError):
    """A required audit write failed or its input was invalid."""


class AuditNotFoundError(AuditStoreError):
    """Referenced request/plan/step/approval row does not exist."""


class AuditTransitionError(AuditStoreError):
    """A status column was asked to move backwards or out of a terminal state."""


class AuditNotAuthorizedError(AuditError):
    """The caller is not the request owner (or not Maya for console reads)."""
