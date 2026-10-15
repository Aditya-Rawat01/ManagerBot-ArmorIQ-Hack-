"""Hash-chained, append-only execution ledger (P2-07).

Records what the tool server actually did: one row per call that passed
verification, with its validation and execution outcome. A plan or ALLOW is
never proof of execution (PRD §10.1) — only a ledger row is.

Guarantees: `verify_chain` recomputes every hash in id order and reports the
first broken position, so it detects edited fields, swapped row order, and
deleted interior rows (any of those breaks a `prev_hash` link). `jti` is
unique: a duplicate never executes twice; the caller gets `DuplicateJti`
carrying the original entry id for the deduplicated rejection.

Limitations (read before relying on this): the chain detects changes by
anyone who cannot recompute it, but NOT by a database superuser who rewrites
the whole chain consistently — never call it "tamper-proof" (R-04). Removed
newest rows (tail truncation) are undetectable without an external anchor;
there is none in MVP (non-goal). Verification-failed calls (bad credential,
bad `_ctx`, replay) never reach the ledger: with no UPDATE/DELETE allowed,
rejections leave no new row and duplicates reference the original entry.

Concurrency: `append` holds a transaction-scoped advisory lock, so parallel
appends serialize into one chain instead of forking it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from psycopg import errors as _pg_errors

from services.contracts import ToolExecutionStatus
from services.toolserver import db

GENESIS_PREV_HASH = "0" * 64
_LEDGER_TABLE = "public.tool_execution_ledger"
_LOCK_KEY = "tool_execution_ledger_append"

_HASH_FIELDS: tuple[str, ...] = (
    "request_id",
    "trace_id",
    "plan_id",
    "armoriq_plan_id",
    "step_index",
    "tool_name",
    "jti",
    "requester_member_id",
    "workspace_id",
    "validation_outcome",
    "execution_status",
    "result_id",
    "error_class",
    "arguments_sha256",
    "prev_hash",
)


@dataclass(frozen=True)
class LedgerEntry:
    """One verified call and its outcome. `arguments` holds business arguments
    only; the writer stores their sha256, never the raw mapping."""

    request_id: str
    trace_id: str
    tool_name: str
    jti: str
    execution_status: ToolExecutionStatus
    plan_id: str | None = None
    armoriq_plan_id: str | None = None
    step_index: int | None = None
    requester_member_id: str | None = None
    workspace_id: str | None = None
    validation_outcome: str = "accepted"
    result_id: str | None = None
    error_class: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerEntryId:
    value: int


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    checked: int
    broken_at: int | None
    detail: str


class DuplicateJti(Exception):
    """The `jti` was already recorded: do not execute; reject as deduplicated."""

    def __init__(self, existing_id: LedgerEntryId) -> None:
        super().__init__(f"duplicate jti, original entry {existing_id.value}")
        self.existing_id = existing_id


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def arguments_sha256(arguments: Mapping[str, Any]) -> str:
    """sha256 of the canonical business arguments (never the raw mapping)."""
    return hashlib.sha256(_canonical(arguments).encode("utf-8")).hexdigest()


def compute_entry_hash(fields: Mapping[str, Any], prev_hash: str) -> str:
    """`sha256(prev_hash ‖ canonical_json(entry_without_hash))`."""
    payload = {name: fields.get(name) for name in _HASH_FIELDS if name != "prev_hash"}
    payload["prev_hash"] = prev_hash
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


def verify_rows(rows: list[dict[str, Any]]) -> VerificationReport:
    """Recompute a fetched chain in order. Pure: no database needed."""
    expected_prev = GENESIS_PREV_HASH
    for position, row in enumerate(rows):
        row_id = row.get("id")
        if row.get("prev_hash") != expected_prev:
            return VerificationReport(
                ok=False,
                checked=position,
                broken_at=row_id if isinstance(row_id, int) else None,
                detail=f"prev_hash link broken at row {row_id}",
            )
        if compute_entry_hash(row, row["prev_hash"]) != row.get("entry_hash"):
            return VerificationReport(
                ok=False,
                checked=position,
                broken_at=row_id if isinstance(row_id, int) else None,
                detail=f"entry_hash mismatch at row {row_id}",
            )
        entry_hash = row.get("entry_hash")
        expected_prev = entry_hash if isinstance(entry_hash, str) else expected_prev
    return VerificationReport(ok=True, checked=len(rows), broken_at=None, detail="chain valid")


class Ledger:
    """Toolserver-role ledger access. `seen_jti` implements P1-04's
    `ReplayGuard` protocol (structural: same name, signature, and semantics)."""

    def __init__(self, dsn: str) -> None:
        self._pool = db.create_pool(dsn, max_size=10)

    def close(self) -> None:
        self._pool.close()

    def append(self, entry: LedgerEntry) -> LedgerEntryId:
        """Append one verified call; serialize concurrent appends with an
        advisory lock. Duplicate `jti` raises `DuplicateJti` (never executes)."""
        args_hash = arguments_sha256(entry.arguments)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK_KEY,))
            cur.execute(
                f"SELECT entry_hash FROM {_LEDGER_TABLE} ORDER BY id DESC LIMIT 1"  # noqa: S608
            )
            latest = cur.fetchone()
            prev_hash = latest[0] if latest else GENESIS_PREV_HASH
            payload = {
                "request_id": entry.request_id,
                "trace_id": entry.trace_id,
                "plan_id": entry.plan_id,
                "armoriq_plan_id": entry.armoriq_plan_id,
                "step_index": entry.step_index,
                "tool_name": entry.tool_name,
                "jti": entry.jti,
                "requester_member_id": entry.requester_member_id,
                "workspace_id": entry.workspace_id,
                "validation_outcome": entry.validation_outcome,
                "execution_status": entry.execution_status.value,
                "result_id": entry.result_id,
                "error_class": entry.error_class,
                "arguments_sha256": args_hash,
            }
            entry_hash = compute_entry_hash(payload, prev_hash)
            try:
                cur.execute(
                    f"""INSERT INTO {_LEDGER_TABLE}
                        (request_id, trace_id, plan_id, armoriq_plan_id, step_index,
                         tool_name, jti, requester_member_id, workspace_id,
                         validation_outcome, execution_status, result_id, error_class,
                         arguments_sha256, prev_hash, entry_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id""",  # noqa: S608
                    (
                        entry.request_id,
                        entry.trace_id,
                        entry.plan_id,
                        entry.armoriq_plan_id,
                        entry.step_index,
                        entry.tool_name,
                        entry.jti,
                        entry.requester_member_id,
                        entry.workspace_id,
                        entry.validation_outcome,
                        entry.execution_status.value,
                        entry.result_id,
                        entry.error_class,
                        args_hash,
                        prev_hash,
                        entry_hash,
                    ),
                )
                row = cur.fetchone()
                if row is None:  # unreachable: RETURNING id always yields a row
                    raise RuntimeError("ledger insert returned no id")
                return LedgerEntryId(row[0])
            except _pg_errors.UniqueViolation:
                conn.rollback()
                original = self.get_by_jti(entry.jti)
                if original is None:  # unreachable: the violation names this jti
                    raise RuntimeError("duplicate jti has no original entry") from None
                raise DuplicateJti(LedgerEntryId(original["id"])) from None

    def seen_jti(self, jti: str) -> bool:
        """True when `jti` was already recorded (caller must reject: replay)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {_LEDGER_TABLE} WHERE jti = %s", (jti,))  # noqa: S608
            return cur.fetchone() is not None

    def get_by_jti(self, jti: str) -> dict[str, Any] | None:
        """Fetch one entry by `jti` (P1-06 evidence query; dedupe reference)."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, request_id, trace_id, plan_id, armoriq_plan_id,
                    step_index, tool_name, jti, requester_member_id, workspace_id,
                    validation_outcome, execution_status, result_id, error_class,
                    arguments_sha256, prev_hash, entry_hash, created_at
                    FROM {_LEDGER_TABLE} WHERE jti = %s""",  # noqa: S608
                (jti,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            names = [d.name for d in cur.description or []]
            if len(names) != len(row):  # unreachable: same query produced both
                raise RuntimeError("ledger row shape mismatch")
            return dict(zip(names, row, strict=True))

    def verify_chain(self) -> VerificationReport:
        """Fetch the whole chain in id order and verify it."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, request_id, trace_id, plan_id, armoriq_plan_id,
                    step_index, tool_name, jti, requester_member_id, workspace_id,
                    validation_outcome, execution_status, result_id, error_class,
                    arguments_sha256, prev_hash, entry_hash
                    FROM {_LEDGER_TABLE} ORDER BY id""",  # noqa: S608
            )
            names = [d.name for d in cur.description or []]
            fetched = cur.fetchall()
            if any(len(names) != len(row) for row in fetched):
                raise RuntimeError("ledger row shape mismatch")
            rows = [dict(zip(names, row, strict=True)) for row in fetched]
        return verify_rows(rows)
