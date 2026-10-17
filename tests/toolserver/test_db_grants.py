"""Live grant tests: backend denial on protected content (P2-01, G0 evidence).

Needs a migrated database plus the operator-created login users (see
migrations/README.md). Skipped — never failed — when DSNs are absent, so CI
(without secrets) stays green. DSNs are captured at collection time because
the conftest scrubs the environment during test runs; they are never logged.
"""

import os

import pytest
from psycopg import errors

from scripts import verify_db_grants as verify_script
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
    conn.rollback()  # the denial aborted the transaction; start a fresh one
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(
            conn,
            "DELETE FROM public.tool_execution_ledger WHERE id = -1",
        )


@NEEDS_DB
def test_backend_cannot_read_or_write_attempts(backend_conn) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(backend_conn, "SELECT 1 FROM public.tool_call_attempts LIMIT 1")
    backend_conn.rollback()
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(
            backend_conn,
            "INSERT INTO public.tool_call_attempts (credential_id, mcp_endpoint, tool_name, "
            "outcome) VALUES ('grant_check', 'memory-mcp', 'grant_check', 'invalid_context') "
            "RETURNING id",
        )


@NEEDS_DB
def test_toolserver_cannot_rewrite_attempts(toolserver_conn) -> None:
    db.fetch_all(toolserver_conn, "SELECT 1 FROM public.tool_call_attempts LIMIT 1")
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(
            toolserver_conn,
            "UPDATE public.tool_call_attempts SET outcome = outcome WHERE id = -1",
        )
    toolserver_conn.rollback()
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(toolserver_conn, "DELETE FROM public.tool_call_attempts WHERE id = -1")


@NEEDS_DB
def test_backend_cannot_read_protected_sequences(backend_conn) -> None:
    # IDENTITY inserts need no sequence grant; reading last_value would leak row counts.
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(backend_conn, "SELECT last_value FROM public.team_notes_id_seq")


APPROVED_VIEWS = (
    "approved_company_data_team",
    "approved_company_data_confidential",
    "approved_company_data_restricted",
)


@NEEDS_DB
@pytest.mark.parametrize("role", ["backend", "toolserver"])
def test_no_runtime_role_reads_or_writes_company_projects(request, role: str) -> None:
    """0011 (M1): the base table holds every tier's raw columns; views read it
    with their owner's privileges, so no runtime role is granted the table."""
    conn = request.getfixturevalue(f"{role}_conn")
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(conn, "SELECT 1 FROM public.company_projects LIMIT 1")
    conn.rollback()
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(conn, "UPDATE public.company_projects SET summary = summary WHERE id = -1")
    conn.rollback()


@NEEDS_DB
@pytest.mark.parametrize("view", APPROVED_VIEWS)
def test_approved_views_toolserver_only(backend_conn, toolserver_conn, view: str) -> None:
    query = f"SELECT 1 FROM public.{view} LIMIT 1"  # noqa: S608 -- view from module allowlist
    db.fetch_all(toolserver_conn, query)
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(backend_conn, query)


@NEEDS_DB
@pytest.mark.parametrize("table", PROTECTED_TABLES)
def test_toolserver_can_select_knowledge_tables(toolserver_conn, table: str) -> None:
    query = f"SELECT 1 FROM public.{table} LIMIT 1"  # noqa: S608 -- table from module allowlist
    db.fetch_all(toolserver_conn, query)


WORKSPACE_TABLES = ("tasks", "egress_sink")


@NEEDS_DB
@pytest.mark.parametrize("table", WORKSPACE_TABLES)
def test_backend_cannot_read_or_write_workspace_tables(backend_conn, table: str) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        db.fetch_all(backend_conn, f"SELECT 1 FROM public.{table} LIMIT 1")  # noqa: S608
    backend_conn.rollback()
    assert db.fetch_one(
        backend_conn, "SELECT has_table_privilege(%s, 'INSERT')", (f"public.{table}",)
    ) == (False,)


@NEEDS_DB
@pytest.mark.parametrize("table", WORKSPACE_TABLES)
def test_toolserver_workspace_tables_are_insert_only(toolserver_conn, table: str) -> None:
    """0012: SELECT + INSERT (the P2-10 handlers' only statements), never
    UPDATE or DELETE."""
    db.fetch_all(toolserver_conn, f"SELECT 1 FROM public.{table} LIMIT 1")  # noqa: S608
    assert db.fetch_one(
        toolserver_conn, "SELECT has_table_privilege(%s, 'INSERT')", (f"public.{table}",)
    ) == (True,)
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(
            toolserver_conn,
            f"UPDATE public.{table} SET workspace_id = workspace_id WHERE id = -1",  # noqa: S608
        )
    toolserver_conn.rollback()
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(toolserver_conn, f"DELETE FROM public.{table} WHERE id = -1")  # noqa: S608


@NEEDS_DB
def test_toolserver_never_deletes_notes(toolserver_conn) -> None:
    """0013 (P2-09 review M2): history is never deleted by the runtime role."""
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(toolserver_conn, "DELETE FROM public.team_notes WHERE id = -1")


@NEEDS_DB
@pytest.mark.parametrize("column", verify_script.TEAM_NOTES_IMMUTABLE_COLUMNS)
def test_toolserver_cannot_rewrite_note_content(toolserver_conn, column: str) -> None:
    """0013 (M2): content, author, workspace, tier, version and hashes are
    immutable for the toolserver."""
    with pytest.raises(errors.InsufficientPrivilege):
        db.execute(
            toolserver_conn,
            f"UPDATE public.team_notes SET {column} = {column} WHERE id = -1",  # noqa: S608
        )


@NEEDS_DB
def test_toolserver_updates_only_note_lifecycle_columns(toolserver_conn) -> None:
    """0013 (M2): exactly the columns confirm/supersede write stay updatable,
    and `FOR UPDATE` keeps working with the column-level UPDATE grant."""
    columns = verify_script.TEAM_NOTES_LIFECYCLE_COLUMNS
    assignments = ", ".join(f"{column} = {column}" for column in columns)
    query = f"UPDATE public.team_notes SET {assignments} WHERE id = -1"  # noqa: S608
    assert db.execute(toolserver_conn, query) == 0
    lock = "SELECT id FROM public.team_notes WHERE id = -1 FOR UPDATE"
    assert db.fetch_all(toolserver_conn, lock) == []
    toolserver_conn.rollback()
