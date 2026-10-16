"""Offline migration lint (P2-01): numbering, grants, enum parity.

Runs without any database: it parses the committed SQL text plus the
repository's contract enums. Live behavior is proven separately by
scripts/verify_db_grants.py and tests/toolserver/test_db_grants.py against a
migrated database.
"""

import re
from pathlib import Path

import pytest

from scripts import migrate as migrate_script
from scripts import verify_db_grants as verify_script
from services.contracts.enums import (
    ApprovalState,
    DataTier,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    ReviewStatus,
    ToolExecutionStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

PROTECTED_TABLES = (
    "source_records",
    "knowledge_chunks",
    "decisions",
    "manager_guidance",
    "team_notes",
)
APPEND_ONLY_TABLES = (
    "tool_execution_ledger",
    "tool_call_attempts",
    "request_events",
    "decision_events",
    "delivery_events",
)


def _sql_text() -> str:
    ordered = migrate_script.discover(MIGRATIONS_DIR)
    return "\n".join(path.read_text(encoding="utf-8") for _, path in ordered)


def test_migrations_are_numbered_ordered_and_unique() -> None:
    ordered = migrate_script.discover(MIGRATIONS_DIR)
    numbers = [number for number, _ in ordered]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    assert numbers[0] >= 1
    assert len(ordered) >= 6


def test_protected_tables_are_never_granted_to_backend() -> None:
    grants_to_backend = [
        line for line in _sql_text().splitlines() if "GRANT" in line and "proxylead_backend" in line
    ]
    assert grants_to_backend, "expected backend grants to exist"
    for line in grants_to_backend:
        for table in PROTECTED_TABLES:
            assert table not in line, f"backend grant on protected table: {line}"


def test_ledger_and_event_tables_are_append_only() -> None:
    text = _sql_text()
    for table in APPEND_ONLY_TABLES:
        for role in ("proxylead_backend", "proxylead_toolserver"):
            assert f"REVOKE UPDATE, DELETE ON public.{table} FROM {role}" in text, (
                f"missing append-only revoke: {table} / {role}"
            )


def test_backend_has_no_ledger_or_egress_access() -> None:
    text = _sql_text()
    assert "REVOKE ALL ON public.tool_execution_ledger FROM proxylead_backend" in text
    assert "REVOKE ALL ON public.tool_jti_claims FROM proxylead_backend" in text
    assert "REVOKE ALL ON public.tool_call_attempts FROM proxylead_backend" in text
    assert "REVOKE ALL ON public.tool_call_attempts FROM PUBLIC" in text
    assert "REVOKE ALL ON public.tasks FROM proxylead_backend" in text
    assert "REVOKE ALL ON public.egress_sink FROM proxylead_backend" in text


def test_egress_sink_correlation_columns() -> None:
    """0012 (P2-10 §9.1): sink rows carry the verified jti and requester; one
    row per jti; the sink stays append-only and invisible to the backend."""
    path = MIGRATIONS_DIR / "0012_egress_sink_correlation.sql"
    text = path.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS jti TEXT" in text
    assert "ADD COLUMN IF NOT EXISTS requester_member_id TEXT" in text
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS egress_sink_jti_uidx\n"
        "    ON public.egress_sink (jti) WHERE jti IS NOT NULL" in text
    )
    assert "GRANT SELECT, INSERT ON public.egress_sink TO proxylead_toolserver" in text
    assert "REVOKE UPDATE, DELETE ON public.egress_sink FROM proxylead_toolserver" in text
    assert "REVOKE ALL ON public.egress_sink FROM proxylead_backend" in text


def test_workspace_tables_least_privilege() -> None:
    """0012: the toolserver never rewrites follow-up tasks or sink rows, and
    the grant check covers both tables."""
    text = _sql_text()
    for table in ("tasks", "egress_sink"):
        assert f"REVOKE UPDATE, DELETE ON public.{table} FROM proxylead_toolserver" in text
        assert f"GRANT SELECT, INSERT ON public.{table} TO proxylead_toolserver" in text
    last_tasks_grant = text.rindex("ON public.tasks TO proxylead_toolserver")
    assert text[last_tasks_grant - 30 : last_tasks_grant].endswith("GRANT SELECT, INSERT ")
    assert text.rindex("REVOKE UPDATE, DELETE ON public.tasks FROM proxylead_toolserver") > (
        text.rindex("GRANT SELECT, INSERT, UPDATE, DELETE ON public.tasks TO proxylead_toolserver")
    )
    assert set(verify_script.WORKSPACE_TABLE_INSERTS) == {"tasks", "egress_sink"}


def test_ledger_followup_constraints() -> None:
    """0008: no explicit ledger ids; new rows carry a hex digest and no free text."""
    text = _sql_text()
    assert "ALTER TABLE public.tool_execution_ledger ALTER COLUMN id SET GENERATED ALWAYS" in text
    assert "arguments_sha256 ~ '^[0-9a-f]{64}$'" in text
    assert "error_class ~ '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'" in text
    assert "CHECK (execution_status <> 'not_reached')" in text
    assert "GRANT SELECT, INSERT ON public.tool_call_attempts TO proxylead_toolserver" in text


def test_attempt_outcomes_match_writer() -> None:
    """The SQL outcome sets equal the writer's (security.py codes + accepted)."""
    from services.toolserver.ledger import ATTEMPT_OUTCOMES, LEDGER_VALIDATION_OUTCOMES

    text = _sql_text()
    attempts_sql = text[text.index("CREATE TABLE IF NOT EXISTS public.tool_call_attempts") :]
    outcome_check = attempts_sql[attempts_sql.index("CHECK (outcome IN (") :].split(")", 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", outcome_check)) == set(ATTEMPT_OUTCOMES)
    ledger_check = text[text.index("CHECK (validation_outcome IN (") :].split(")", 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", ledger_check)) == set(LEDGER_VALIDATION_OUTCOMES)


def test_enum_values_match_contracts() -> None:
    text = _sql_text()
    for enum in (
        DataTier,
        ReviewStatus,
        DecisionSource,
        ApprovalState,
        ToolExecutionStatus,
        DeliveryChannel,
        EntryPoint,
    ):
        for member in enum:
            assert f"'{member.value}'" in text, f"{enum.__name__}.{member.name} missing in SQL"


def test_metadata_view_exposes_no_content() -> None:
    text = _sql_text()
    start = text.index("CREATE OR REPLACE VIEW public.draft_review_queue AS")
    view_sql = text[start:].split(";", 1)[0].lower()
    for forbidden in ("title", "chunk_text", "content_text", "message"):
        assert forbidden not in view_sql, f"metadata view leaks {forbidden}"
    for required in (
        "record_id",
        "version",
        "author_member_id",
        "created_at",
        "review_status",
        "content_kind",
    ):
        assert required in view_sql, f"metadata view missing {required}"


def test_backend_can_read_metadata_view() -> None:
    assert "GRANT SELECT ON public.draft_review_queue TO proxylead_backend" in _sql_text()


def test_approved_views_migration_is_schema_and_grants_only() -> None:
    """0011 (P2-08 review B1/M1): no fixture rows (a fresh migrate runs before
    the seed creates the workspace), no runtime grant on the base table."""
    [migration] = sorted(MIGRATIONS_DIR.glob("0011_*.sql"))
    code = "\n".join(
        line.split("--", 1)[0] for line in migration.read_text(encoding="utf-8").splitlines()
    )
    assert "INSERT" not in code.upper(), "fixture rows belong in seed/company_projects.json"
    assert "UNIQUE (workspace_id, project_key)" in code  # the seed upsert key
    assert not re.search(r"GRANT[^;]*\bON\s+public\.company_projects\b", code)
    for role in ("PUBLIC", "proxylead_backend", "proxylead_toolserver"):
        assert f"REVOKE ALL ON public.company_projects FROM {role};" in code
    for tier in DataTier:
        view = f"public.approved_company_data_{tier.value}"
        assert f"GRANT SELECT ON {view} TO proxylead_toolserver;" in code
        assert f"REVOKE ALL ON {view} FROM proxylead_backend;" in code
        assert f"REVOKE ALL ON {view} FROM PUBLIC;" in code
        assert f"WHERE tier = '{tier.value}';" in code
    assert verify_script.COMPANY_PROJECTS_TABLE == "company_projects"
    assert (
        tuple(f"approved_company_data_{tier.value}" for tier in DataTier)
        == verify_script.APPROVED_VIEWS
    )


def test_migrate_without_dsn_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL_MIGRATE", raising=False)
    assert migrate_script.main([]) == 2


def test_verify_without_dsns_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL_TOOLSERVER", raising=False)
    assert verify_script.main() == 2
