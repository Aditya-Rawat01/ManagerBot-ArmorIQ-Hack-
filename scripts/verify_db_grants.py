"""Prove the backend role cannot read protected content (P2-01, G0 evidence).

Usage (operator only, repository root):
    DATABASE_URL_BACKEND='<backend DSN>' DATABASE_URL_TOOLSERVER='<toolserver DSN>' \\
        python scripts/verify_db_grants.py

Connects as each runtime login user and checks the 0005/0007/0008/0009 grant
matrix: backend SELECT on every protected table/view must fail with permission
denied; allowed tables must work; neither runtime role may UPDATE or DELETE
the ledger; the toolserver may not DELETE jti claims nor UPDATE or DELETE
attempts; the backend cannot SELECT the ledger, jti claims, or attempts, nor
INSERT attempts; the toolserver role must reach the knowledge tables and
SELECT attempts; append-only audit events must not be deletable through
their plan (0009: the event tables' plan FKs are ON DELETE RESTRICT, checked
in the catalog and, when a seeded member exists, by a backend DELETE inside
a rolled-back transaction). Each check runs in its own transaction with the
toolserver db helper's ``SET LOCAL statement_timeout``. Prints table names
and results only — DSNs are never printed or logged.

Exit 0 when every check matches, 1 on any mismatch, 2 on configuration or
connection errors.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
KNOWLEDGE_TABLES = PROTECTED_TABLES
EVENT_TABLES = ("request_events", "decision_events", "delivery_events")
# P2-08 approved views (D-11): synthetic company data, toolserver reads only.
COMPANY_PROJECTS_TABLE = "company_projects"
APPROVED_VIEWS = (
    "approved_company_data_team",
    "approved_company_data_confidential",
    "approved_company_data_restricted",
)


@dataclass
class Report:
    passed: int = 0
    failed: int = 0
    lines: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passed += 1
        self.lines.append(f"PASS {message}")
        print(f"PASS {message}")

    def bad(self, message: str) -> None:
        self.failed += 1
        self.lines.append(f"FAIL {message}")
        print(f"FAIL {message}")

    def skip(self, message: str) -> None:
        self.lines.append(f"SKIP {message}")
        print(f"SKIP {message}")


def _denied(exc: BaseException) -> bool:
    from psycopg import errors

    return isinstance(exc, errors.InsufficientPrivilege)


def _expect_denied(report: Report, conn: object, label: str, sql: str) -> None:
    from services.toolserver import db

    try:
        db.apply_statement_timeout(conn)
        db.fetch_all(conn, sql)
    except Exception as exc:
        if _denied(exc):
            report.ok(f"DENY {label} (permission denied)")
        else:
            report.bad(f"DENY {label} (unexpected {type(exc).__name__})")
    else:
        report.bad(f"DENY {label} (query succeeded — grant is too broad!)")
    finally:
        # One transaction per check: an expected denial aborts the transaction
        # (later checks would fail with InFailedSqlTransaction), and a
        # too-broad grant must never persist a change.
        conn.rollback()


def _expect_allowed(report: Report, conn: object, label: str, sql: str) -> None:
    from services.toolserver import db

    try:
        db.apply_statement_timeout(conn)
        db.fetch_all(conn, sql)
    except Exception as exc:
        report.bad(f"ALLOW {label} (unexpected {type(exc).__name__})")
    else:
        report.ok(f"ALLOW {label}")
    finally:
        conn.rollback()


def _check_event_fks_restrict(report: Report, conn: object) -> None:
    """Catalog check (no data needed): event-table plan FKs never cascade."""
    from services.toolserver import db

    try:
        rows = db.fetch_all(
            conn,
            "SELECT t.relname, c.confdeltype::text FROM pg_catalog.pg_constraint c "
            "JOIN pg_catalog.pg_class t ON t.oid = c.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' AND c.contype = 'f' "
            "AND c.confrelid = 'public.plans'::regclass AND t.relname = ANY(%s)",
            (list(EVENT_TABLES),),
        )
    except Exception as exc:
        report.bad(f"FK {', '.join(EVENT_TABLES)} -> plans (unexpected {type(exc).__name__})")
        return
    finally:
        conn.rollback()
    for table in EVENT_TABLES:
        actions = sorted(action for name, action in rows if name == table)
        if actions == ["r"]:
            report.ok(f"FK {table}.plan_id -> plans is ON DELETE RESTRICT")
        else:
            report.bad(f"FK {table}.plan_id -> plans delete actions {actions} (expected ['r'])")


def _check_plan_delete_keeps_events(report: Report, conn: object) -> None:
    """As backend: a plan with an audit event cannot be deleted (rolled back)."""
    from psycopg import errors

    from services.toolserver import db

    label = "backend DELETE plan with audit events"
    try:
        member = db.fetch_one(conn, "SELECT workspace_id, id FROM public.members LIMIT 1")
        if member is None:
            report.skip(f"{label} (needs a seeded workspace + member; catalog check covers it)")
            return
        suffix = uuid.uuid4().hex[:12]
        plan_id = f"verify-plan-{suffix}"
        request_id = f"verify-req-{suffix}"
        db.execute(
            conn,
            "INSERT INTO public.plans (id, request_id, trace_id, workspace_id, "
            "requester_member_id, entry_point, status) "
            "VALUES (%s, %s, %s, %s, %s, 'web', 'planned')",
            (plan_id, request_id, request_id, member[0], member[1]),
        )
        db.execute(
            conn,
            "INSERT INTO public.request_events (request_id, trace_id, plan_id, event_type) "
            "VALUES (%s, %s, %s, 'plan_proposed')",
            (request_id, request_id, plan_id),
        )
        db.execute(conn, "DELETE FROM public.plans WHERE id = %s", (plan_id,))
    except (errors.ForeignKeyViolation, errors.RestrictViolation):
        # PostgreSQL 16 reports an ON DELETE RESTRICT refusal as 23503
        # (foreign_key_violation); PostgreSQL 18 (Neon) as 23001 (restrict_violation).
        report.ok(f"DENY {label} (restrict/foreign key violation; events kept)")
    except Exception as exc:
        report.bad(f"DENY {label} (unexpected {type(exc).__name__})")
    else:
        report.bad(f"DENY {label} (delete succeeded — events would cascade!)")
    finally:
        # Never persist the probe rows, whatever happened.
        conn.rollback()


def verify(backend_dsn: str, toolserver_dsn: str) -> Report:
    from services.toolserver import db

    report = Report()
    try:
        backend = db.create_pool(backend_dsn)
        toolserver = db.create_pool(toolserver_dsn)
    except Exception as exc:
        report.bad(f"connect ({type(exc).__name__})")
        return report
    try:
        with backend.connection() as conn, toolserver.connection() as ts_conn:
            backend_user = db.fetch_one(conn, "SELECT current_user")
            toolserver_user = db.fetch_one(ts_conn, "SELECT current_user")
            print(f"backend session user: {backend_user[0]}")
            print(f"toolserver session user: {toolserver_user[0]}")
            for table in PROTECTED_TABLES:
                _expect_denied(
                    report,
                    conn,
                    f"backend SELECT {table}",
                    f"SELECT 1 FROM public.{table} LIMIT 1",  # noqa: S608 -- table from module allowlist, never user input,
                )
            for table in BACKEND_ALLOWED_TABLES:
                _expect_allowed(
                    report,
                    conn,
                    f"backend SELECT {table}",
                    f"SELECT 1 FROM public.{table} LIMIT 1",  # noqa: S608 -- table from module allowlist, never user input,
                )
            _expect_denied(
                report,
                conn,
                "backend SELECT tool_execution_ledger",
                "SELECT 1 FROM public.tool_execution_ledger LIMIT 1",
            )
            _expect_denied(
                report,
                conn,
                "backend SELECT tool_jti_claims",
                "SELECT 1 FROM public.tool_jti_claims LIMIT 1",
            )
            _expect_denied(
                report,
                conn,
                "backend SELECT tool_call_attempts",
                "SELECT 1 FROM public.tool_call_attempts LIMIT 1",
            )
            _expect_denied(
                report,
                conn,
                "backend INSERT tool_call_attempts",
                "INSERT INTO public.tool_call_attempts (credential_id, mcp_endpoint, tool_name, "
                "outcome) VALUES ('grant_check', 'memory-mcp', 'grant_check', 'invalid_context') "
                "RETURNING id",
            )
            _expect_denied(
                report,
                conn,
                "backend UPDATE tool_execution_ledger",
                "UPDATE public.tool_execution_ledger SET execution_status = 'error' WHERE id = -1",
            )
            _expect_denied(
                report,
                conn,
                "backend DELETE tool_execution_ledger",
                "DELETE FROM public.tool_execution_ledger WHERE id = -1",
            )
            _expect_denied(
                report,
                conn,
                "backend SELECT team_notes_id_seq (row-count leak)",
                "SELECT last_value FROM public.team_notes_id_seq",
            )
            _expect_denied(
                report,
                conn,
                "backend SELECT company_projects",
                "SELECT 1 FROM public.company_projects LIMIT 1",
            )
            for view in APPROVED_VIEWS:
                _expect_denied(
                    report,
                    conn,
                    f"backend SELECT {view}",
                    f"SELECT 1 FROM public.{view} LIMIT 1",  # noqa: S608 -- view from module allowlist, never user input,
                )
            for table in EVENT_TABLES:
                _expect_denied(
                    report,
                    conn,
                    f"backend UPDATE {table}",
                    f"UPDATE public.{table} SET id = id WHERE id = -1",  # noqa: S608 -- table from module allowlist, never user input,
                )
                _expect_denied(
                    report,
                    conn,
                    f"backend DELETE {table}",
                    f"DELETE FROM public.{table} WHERE id = -1",  # noqa: S608 -- table from module allowlist, never user input,
                )
            _check_event_fks_restrict(report, conn)
            _check_plan_delete_keeps_events(report, conn)
            for table in KNOWLEDGE_TABLES:
                _expect_allowed(
                    report,
                    ts_conn,
                    f"toolserver SELECT {table}",
                    f"SELECT 1 FROM public.{table} LIMIT 1",  # noqa: S608 -- table from module allowlist, never user input,
                )
            _expect_denied(
                report,
                ts_conn,
                "toolserver UPDATE tool_execution_ledger",
                "UPDATE public.tool_execution_ledger SET execution_status = 'error' WHERE id = -1",
            )
            _expect_denied(
                report,
                ts_conn,
                "toolserver DELETE tool_execution_ledger",
                "DELETE FROM public.tool_execution_ledger WHERE id = -1",
            )
            _expect_denied(
                report,
                ts_conn,
                "toolserver DELETE tool_jti_claims",
                "DELETE FROM public.tool_jti_claims WHERE jti = ''",
            )
            _expect_allowed(
                report,
                ts_conn,
                "toolserver SELECT tool_call_attempts",
                "SELECT 1 FROM public.tool_call_attempts LIMIT 1",
            )
            _expect_denied(
                report,
                ts_conn,
                "toolserver UPDATE tool_call_attempts",
                "UPDATE public.tool_call_attempts SET outcome = outcome WHERE id = -1",
            )
            _expect_denied(
                report,
                ts_conn,
                "toolserver DELETE tool_call_attempts",
                "DELETE FROM public.tool_call_attempts WHERE id = -1",
            )
            _expect_allowed(
                report,
                ts_conn,
                "toolserver SELECT company_projects",
                "SELECT 1 FROM public.company_projects LIMIT 1",
            )
            for view in APPROVED_VIEWS:
                _expect_allowed(
                    report,
                    ts_conn,
                    f"toolserver SELECT {view}",
                    f"SELECT 1 FROM public.{view} LIMIT 1",  # noqa: S608 -- view from module allowlist, never user input,
                )
            _expect_denied(
                report,
                ts_conn,
                "toolserver UPDATE company_projects",
                "UPDATE public.company_projects SET summary = summary WHERE project_key = ''",
            )
            _expect_denied(
                report,
                ts_conn,
                "toolserver DELETE company_projects",
                "DELETE FROM public.company_projects WHERE project_key = ''",
            )
    except Exception as exc:
        report.bad(f"verify ({type(exc).__name__})")
    finally:
        backend.close()
        toolserver.close()
    print(f"verify_db_grants: {report.passed} passed, {report.failed} failed.")
    return report


def main() -> int:
    backend_dsn = os.environ.get("DATABASE_URL_BACKEND")
    toolserver_dsn = os.environ.get("DATABASE_URL_TOOLSERVER")
    missing = [
        name
        for name, value in (
            ("DATABASE_URL_BACKEND", backend_dsn),
            ("DATABASE_URL_TOOLSERVER", toolserver_dsn),
        )
        if not value
    ]
    if missing:
        print(
            f"verify_db_grants: missing {', '.join(missing)} (operator only).",
            file=sys.stderr,
        )
        return 2
    try:
        report = verify(backend_dsn or "", toolserver_dsn or "")
    except Exception as exc:
        print(f"verify_db_grants: error ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
