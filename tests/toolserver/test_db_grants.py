"""Live grant tests: backend denial on protected content (P2-01, G0 evidence).

Needs a migrated database plus the operator-created login users (see
migrations/README.md). Skipped — never failed — when DSNs are absent, so CI
(without secrets) stays green. DSNs are captured at collection time because
the conftest scrubs the environment during test runs; they are never logged.
"""

import os

import pytest
from psycopg import errors

from services.toolserver import db

BACKEND_DSN = os.environ.get("DATABASE_URL_BACKEND")
TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")

NEEDS_DB = pytest.mark.skipif(
    not (BACKEND_DSN and TOOLSERVER_DSN),
    reason="needs DATABASE_URL_BACKEND and DATABASE_URL_TOOLSERVER (live database)",
)

PROTECTED_TABLES = (
    "source_records",
    "knowledge_chunks",
    "decisions",
    "manager_guidance",
    "team_notes",
)
BACKEND_ALLOWED_TABLES = (
    "workspaces",
    "members",
    "slack_events",
    "topic_access_rules",
    "plans",
    "plan_steps",
    "approval_requests",
    "request_events",
    "decision_events",
    "delivery_events",
    "unanswered_questions",
    "draft_review_queue",
)


@pytest.fixture
def backend_conn():
    assert BACKEND_DSN is not None
    pool = db.create_pool(BACKEND_DSN)
    try:
        with pool.connection() as conn:
            yield conn
    finally:
        pool.close()


@pytest.fixture
def toolserver_conn():
    assert TOOLSERVER_DSN is not None
    pool = db.create_pool(TOOLSERVER_DSN)
    try:
        with pool.connection() as conn:
            yield conn
    finally:
        pool.close()


@NEEDS_DB
@pytest.mark.parametrize("table", PROTECTED_TABLES)
def test_backend_cannot_select_protected_tables(backend_conn, table: str) -> None:
    query = f"SELECT 1 FROM public.{table} LIMIT 1"  # noqa: S608 -- table from module allowlist
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(backend_conn, query)


@NEEDS_DB
@pytest.mark.parametrize("table", BACKEND_ALLOWED_TABLES)
def test_backend_can_select_allowed_tables(backend_conn, table: str) -> None:
    query = f"SELECT 1 FROM public.{table} LIMIT 1"  # noqa: S608 -- table from module allowlist
    db.fetch_all(backend_conn, query)


@NEEDS_DB
def test_backend_cannot_select_ledger(backend_conn) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(backend_conn, "SELECT 1 FROM public.tool_execution_ledger LIMIT 1")


@NEEDS_DB
@pytest.mark.parametrize("role", ["backend", "toolserver"])
def test_runtime_roles_cannot_rewrite_ledger(request, role: str) -> None:
    conn = request.getfixturevalue(f"{role}_conn")
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(
            conn,
            "UPDATE public.tool_execution_ledger SET execution_status = 'error' WHERE id = -1",
        )
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(
            conn,
            "DELETE FROM public.tool_execution_ledger WHERE id = -1",
        )


@NEEDS_DB
@pytest.mark.parametrize("table", PROTECTED_TABLES)
def test_toolserver_can_select_knowledge_tables(toolserver_conn, table: str) -> None:
    query = f"SELECT 1 FROM public.{table} LIMIT 1"  # noqa: S608 -- table from module allowlist
    db.fetch_all(toolserver_conn, query)
