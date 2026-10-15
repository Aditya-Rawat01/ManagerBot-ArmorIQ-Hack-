"""Apply numbered SQL migrations in order (ADR-028, P2-01).

Usage (operator only, repository root):
    DATABASE_URL_MIGRATE='<owner DSN>' python scripts/migrate.py

Reads only DATABASE_URL_MIGRATE. Applies every pending file in migrations/
exactly once, tracked in the schema_migrations table. Re-running applies
nothing. Exit 0 on success (or already-current), 1 on migration failure, 2 on
usage/configuration errors. DSNs are never printed or logged.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIGRATIONS_DIR = REPO_ROOT / "migrations"

MIGRATION_RE = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    """Bad migration directory (naming, duplicates) — operator must fix."""


def discover(directory: Path) -> list[tuple[int, Path]]:
    """Return (number, path) pairs, ordered; raise MigrationError on defects."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    numbered: dict[int, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_RE.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name (expected NNNN_name.sql): {path.name}")
        number = int(match.group(1))
        if number in numbered:
            raise MigrationError(
                f"duplicate migration number {number:04d}: {numbered[number].name} and {path.name}"
            )
        numbered[number] = path
    if not numbered:
        raise MigrationError(f"no migrations found in {directory}")
    return [(number, numbered[number]) for number in sorted(numbered)]


def _connect(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError:
        print(
            "migrate: psycopg is not installed; run "
            'python -m pip install -e ".[api,toolserver,dev]" first.',
            file=sys.stderr,
        )
        raise
    return psycopg.connect(dsn)


def apply_pending(dsn: str, directory: Path) -> tuple[list[str], list[str]]:
    """Apply pending migrations; return (applied, already_applied) filenames."""
    import psycopg

    pending = discover(directory)
    applied: list[str] = []
    already: list[str] = []
    with _connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
        done = {row[0] for row in rows}
        for _, path in pending:
            if path.name in done:
                already.append(path.name)
                continue
            # No parameters are passed, so psycopg uses the simple query
            # protocol, which runs multi-statement files (including DO and
            # plpgsql bodies) as one implicit unit.
            sql = path.read_text(encoding="utf-8")
            try:
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                raise MigrationError(f"{path.name} failed: {exc}") from exc
            applied.append(path.name)
    return applied, already


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="directory of NNNN_name.sql files",
    )
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL_MIGRATE")
    if not dsn:
        print(
            "migrate: DATABASE_URL_MIGRATE is not set (operator only).",
            file=sys.stderr,
        )
        return 2
    try:
        applied, already = apply_pending(dsn, args.migrations_dir)
    except MigrationError as exc:
        print(f"migrate: {exc}", file=sys.stderr)
        return 1
    except ImportError:
        return 2
    except Exception as exc:
        # Never include the DSN: only the error class is reported.
        print(f"migrate: connection or SQL error ({type(exc).__name__}).", file=sys.stderr)
        return 1
    for name in applied:
        print(f"migrate: applied {name}")
    print(f"migrate: {len(applied)} applied, {len(already)} already applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
