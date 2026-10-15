"""Typed audit failures (P3-01).

Callers (P3-04 orchestrator, P4-01/P4-04 routes) fail closed on these: a
required audit write that raises is never treated as success.

Every public store/query function is wrapped by :func:`fail_closed`, so a
driver, pool, or unexpected error surfaces as :class:`AuditStoreError`
(``raise ... from exc`` keeps the cause for debugging). Messages carry only
the operation name and the exception class — never a DSN, SQL parameters, or
payload values. Typed audit errors (:class:`AuditNotFoundError`,
:class:`AuditTransitionError`, :class:`AuditNotAuthorizedError`) pass through
unchanged.
"""

import functools
from collections.abc import Callable


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


def fail_closed[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Re-raise every non-audit exception as :class:`AuditStoreError`.

    Keeps the module contract "an audit call either succeeds or raises an
    ``AuditError``" even for ``psycopg``/pool errors or adapter failures, so
    P3-04 can catch one base class and abort the request.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except AuditError:
            raise
        except Exception as exc:
            raise AuditStoreError(f"audit {func.__name__} failed ({type(exc).__name__})") from exc

    return wrapper
