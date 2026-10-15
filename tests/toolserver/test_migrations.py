"""Offline migration lint (P2-01): numbering, grants, enum parity.

Runs without any database: it parses the committed SQL text plus the
repository's contract enums. Live behavior is proven separately by
scripts/verify_db_grants.py and tests/toolserver/test_db_grants.py against a
migrated database.
"""

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
    assert "REVOKE ALL ON public.tasks FROM proxylead_backend" in text
    assert "REVOKE ALL ON public.egress_sink FROM proxylead_backend" in text


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


def test_migrate_without_dsn_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL_MIGRATE", raising=False)
    assert migrate_script.main([]) == 2


def test_verify_without_dsns_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL_TOOLSERVER", raising=False)
    assert verify_script.main() == 2
