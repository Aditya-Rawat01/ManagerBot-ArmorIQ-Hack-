"""Hash-chained, append-only execution ledger (P2-07).

Records what the tool server actually did: one row per call that passed
verification, with its validation and execution outcome. A plan or ALLOW is
never proof of execution (PRD §10.1) — only a ledger row is.

Guarantees: `verify_chain` recomputes every hash in id order and reports the
first broken position, so it detects edited fields (including `created_at`,
which is hashed as a normalized UTC ISO-8601 string with microseconds),
swapped row order, and deleted interior rows (any of those breaks a
`prev_hash` link). `jti` is claimed atomically BEFORE execution (`seen_jti`,
backed by the append-only `tool_jti_claims` table), so a replayed or
concurrent duplicate never executes twice; a duplicate `append` additionally
raises `DuplicateJti` carrying the original entry id for the deduplicated
rejection.

Rejected attempts: every call that passes the credential check gets one row
in the append-only `tool_call_attempts` table (0008) with its verification
outcome (`accepted`, `invalid_context`, `context_expired`, `replay`) —
`verify_and_record` wraps P1-04's `verify_tool_call` and records it. Replays
reference the original ledger entry. Identity fields there come only from a
verified `_ctx`; raw arguments and `_ctx` tokens are never stored. Attempts
are append-only by grants but not hash-chained.

Limitations (read before relying on this): the chain detects changes by
anyone who cannot recompute it, but NOT by a database superuser who rewrites
the whole chain consistently — never call it "tamper-proof" (R-04). Removed
newest rows (tail truncation) are undetectable without an external anchor;
there is none in MVP (non-goal). `arguments_sha256` is an unsalted SHA-256:
guessable for low-entropy arguments, so no role other than the tool server
may read it (see migrations/README.md).

Hash payload change (0008 follow-up): `created_at` joined the hashed fields.
Rows written before that change do not verify under this code; the shared
Neon ledger held zero rows when it changed, so no stored chain broke. Never
mix writer versions on one ledger.

Concurrency: `append` forces READ COMMITTED (so the chain tip read after the
lock is the latest committed row, never an older snapshot) and holds a
transaction-scoped advisory lock on the fixed key `APPEND_LOCK_KEY`, so
parallel appends serialize into one chain instead of forking it.

Pools: one pool per process. Pass the process's toolserver pool
(`Ledger(pool=...)`); the DSN form creates a private pool (tests, scripts).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg import errors as _pg_errors
from psycopg_pool import ConnectionPool

from services.authorization.ctx_envelope import ReplayGuard
from services.contracts import McpEndpoint, ToolExecutionStatus
from services.toolserver import db
from services.toolserver.security import (
    CONTEXT_EXPIRED,
    INVALID_CONTEXT,
    REPLAY,
    CtxRejection,
    VerifiedCall,
    verify_tool_call,
)

logger = logging.getLogger(__name__)

GENESIS_PREV_HASH = "0" * 64
_LEDGER_TABLE = "public.tool_execution_ledger"
_ATTEMPTS_TABLE = "public.tool_call_attempts"
# Key of the transaction-scoped advisory lock that serializes appends: ASCII
# "pxledger" read as a big-endian signed 64-bit integer. A fixed documented
# constant (not `hashtext(...)`, whose output is an undocumented internal);
# every writer of the ledger must use this exact key.
APPEND_LOCK_KEY = 0x70786C6564676572
# Name PostgreSQL gave `jti TEXT NOT NULL UNIQUE` in 0004. Only a violation of
# this constraint is a duplicate jti; any other unique violation is re-raised.
_JTI_CONSTRAINT = "tool_execution_ledger_jti_key"

# Verification outcomes. The first three are P1-04's rejection codes.
ACCEPTED = "accepted"
# Post-verification rejections (SHARED_INTERFACES §4, raised by P2-06 dispatch
# after `verify_tool_call`): the jti is verified and claimed, so the call gets
# a ledger row with `execution_status=rejected_by_tool_server`.
INVALID_ARGUMENTS = "invalid_arguments"
OUT_OF_SCOPE = "out_of_scope"
PRECONDITION_FAILED = "precondition_failed"
# Returned by `verify_and_record` when an accepted call cannot be recorded.
INTERNAL = "internal"

LEDGER_VALIDATION_OUTCOMES = frozenset(
    {ACCEPTED, INVALID_ARGUMENTS, OUT_OF_SCOPE, PRECONDITION_FAILED}
)
ATTEMPT_OUTCOMES = frozenset({ACCEPTED, INVALID_CONTEXT, CONTEXT_EXPIRED, REPLAY})
# Outcomes whose `_ctx` signature verified, so its identity fields are trusted.
_VERIFIED_ATTEMPT_OUTCOMES = frozenset({ACCEPTED, REPLAY})
_MCP_ENDPOINTS = frozenset(endpoint.value for endpoint in McpEndpoint)
# Names (never values) of the service credentials that can pass the bearer
# check: the `services/toolserver/config.py` setting that authenticated the
# call. A fixed set, so a token value can never be stored by mistake. A new
# credential path (E1: ArmorIQ proxy) adds its setting name here.
SERVICE_CREDENTIAL_IDS = frozenset({"toolserver_bearer_memory", "toolserver_bearer_workspace"})
# Class names and tool/credential identifiers only: no free text (which could
# carry protected content or secrets) reaches the ledger.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")

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
    "created_at",
    "prev_hash",
)
_ROW_COLUMNS = (
    "id, request_id, trace_id, plan_id, armoriq_plan_id, step_index, tool_name, jti, "
    "requester_member_id, workspace_id, validation_outcome, execution_status, result_id, "
    "error_class, arguments_sha256, prev_hash, entry_hash, created_at"
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
    validation_outcome: str = ACCEPTED
    result_id: str | None = None
    error_class: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerEntryId:
    value: int


@dataclass(frozen=True)
class ToolCallAttempt:
    """One call that passed the credential check, with its verification
    outcome. `credential_id` is the service identity that passed: the NAME
    of the bearer setting (`SERVICE_CREDENTIAL_IDS`), never its value.
    `jti`, `request_id`, `workspace_id`, and `requester_member_id` only ever
    come from a verified `_ctx`."""

    credential_id: str
    mcp_endpoint: str
    tool_name: str
    outcome: str
    jti: str | None = None
    request_id: str | None = None
    workspace_id: str | None = None
    requester_member_id: str | None = None
    original_entry_id: LedgerEntryId | None = None


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


def canonical_timestamp(value: datetime | None) -> str | None:
    """`created_at` as hashed: UTC ISO-8601 with microseconds, e.g.
    `2026-10-16T09:30:00.000001+00:00`. Naive datetimes are rejected."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("created_at must be a timezone-aware datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def compute_entry_hash(fields: Mapping[str, Any], prev_hash: str) -> str:
    """`sha256(prev_hash ‖ canonical_json(entry_without_hash))`."""
    payload = {name: fields.get(name) for name in _HASH_FIELDS if name != "prev_hash"}
    payload["created_at"] = canonical_timestamp(fields.get("created_at"))
    payload["prev_hash"] = prev_hash
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


def verify_rows(rows: list[dict[str, Any]]) -> VerificationReport:
    """Recompute a fetched chain in order. Pure: no database needed."""
    expected_prev = GENESIS_PREV_HASH
    for position, row in enumerate(rows):
        row_id = row.get("id")
        broken_at = row_id if isinstance(row_id, int) else None
        if row.get("prev_hash") != expected_prev:
            return VerificationReport(
                ok=False,
                checked=position,
                broken_at=broken_at,
                detail=f"prev_hash link broken at row {row_id}",
            )
        try:
            recomputed = compute_entry_hash(row, row["prev_hash"])
        except ValueError:
            recomputed = None
        if recomputed != row.get("entry_hash"):
            return VerificationReport(
                ok=False,
                checked=position,
                broken_at=broken_at,
                detail=f"entry_hash mismatch at row {row_id}",
            )
        entry_hash = row.get("entry_hash")
        expected_prev = entry_hash if isinstance(entry_hash, str) else expected_prev
    return VerificationReport(ok=True, checked=len(rows), broken_at=None, detail="chain valid")


def _check_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match {_IDENTIFIER.pattern}")


def validate_entry(entry: LedgerEntry) -> None:
    """Reject entries that are meaningless or could carry free text.
    Messages name the field only, never its value."""
    if not isinstance(entry.execution_status, ToolExecutionStatus):
        raise ValueError("execution_status must be a ToolExecutionStatus")
    if entry.execution_status is ToolExecutionStatus.NOT_REACHED:
        raise ValueError("a ledger row means the tool server was reached: not_reached is invalid")
    if entry.validation_outcome not in LEDGER_VALIDATION_OUTCOMES:
        raise ValueError("validation_outcome is not a ledger validation outcome")
    if (
        entry.validation_outcome != ACCEPTED
        and entry.execution_status is not ToolExecutionStatus.REJECTED_BY_TOOL_SERVER
    ):
        raise ValueError("a call that failed validation must be rejected_by_tool_server")
    if entry.error_class is not None:
        _check_identifier("error_class", entry.error_class)
    _check_identifier("tool_name", entry.tool_name)


def validate_attempt(attempt: ToolCallAttempt) -> None:
    """Fixed outcome set; identity only for outcomes whose `_ctx` verified."""
    if attempt.outcome not in ATTEMPT_OUTCOMES:
        raise ValueError("outcome is not an attempt outcome")
    if attempt.mcp_endpoint not in _MCP_ENDPOINTS:
        raise ValueError("mcp_endpoint is not a registered endpoint")
    _check_identifier("tool_name", attempt.tool_name)
    if attempt.credential_id not in SERVICE_CREDENTIAL_IDS:
        raise ValueError("credential_id is not a known service credential name")
    identity = (
        attempt.jti,
        attempt.request_id,
        attempt.workspace_id,
        attempt.requester_member_id,
    )
    if attempt.outcome in _VERIFIED_ATTEMPT_OUTCOMES:
        if not attempt.jti:
            raise ValueError("accepted and replay attempts carry the verified jti")
    elif any(value is not None for value in identity):
        raise ValueError("unverified attempts carry no identity fields")
    if attempt.original_entry_id is not None and attempt.outcome != REPLAY:
        raise ValueError("only a replay references an original entry")


class Ledger:
    """Toolserver-role ledger access. `seen_jti` implements P1-04's
    `ReplayGuard` protocol with the same check-and-record semantics as
    `InMemoryReplayGuard`: the first call claims the jti and returns False,
    every later (or concurrent) call returns True."""

    def __init__(self, dsn: str | None = None, *, pool: ConnectionPool | None = None) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("pass exactly one of dsn or pool")
        # An injected pool belongs to the process (one pool per process) and
        # is not closed here; a DSN creates a private pool this ledger owns.
        self._owns_pool = pool is None
        self._pool = pool if pool is not None else db.create_pool(dsn or "", max_size=10)

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

    def append(self, entry: LedgerEntry) -> LedgerEntryId:
        """Append one verified call; serialize concurrent appends with an
        advisory lock. Duplicate `jti` raises `DuplicateJti` (never executes)."""
        validate_entry(entry)
        args_hash = arguments_sha256(entry.arguments)
        try:
            # READ COMMITTED is load-bearing: under REPEATABLE READ the snapshot
            # is taken at the first statement, before the lock wait, so the tip
            # read below could be stale and two appends would fork the chain.
            with (
                db.transaction(self._pool, read_committed=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (APPEND_LOCK_KEY,))
                cur.execute(
                    f"SELECT entry_hash FROM {_LEDGER_TABLE} ORDER BY id DESC LIMIT 1"  # noqa: S608
                )
                latest = cur.fetchone()
                prev_hash = latest[0] if latest else GENESIS_PREV_HASH
                # Taken under the lock, so created_at follows chain order.
                cur.execute("SELECT clock_timestamp()")
                now_row = cur.fetchone()
                if now_row is None:  # unreachable: SELECT without FROM yields a row
                    raise RuntimeError("clock_timestamp returned no row")
                created_at = now_row[0]
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
                    "created_at": created_at,
                }
                entry_hash = compute_entry_hash(payload, prev_hash)
                cur.execute(
                    f"""INSERT INTO {_LEDGER_TABLE}
                        (request_id, trace_id, plan_id, armoriq_plan_id, step_index,
                         tool_name, jti, requester_member_id, workspace_id,
                         validation_outcome, execution_status, result_id, error_class,
                         arguments_sha256, created_at, prev_hash, entry_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                        created_at,
                        prev_hash,
                        entry_hash,
                    ),
                )
                row = cur.fetchone()
                if row is None:  # unreachable: RETURNING id always yields a row
                    raise RuntimeError("ledger insert returned no id")
                return LedgerEntryId(row[0])
        except _pg_errors.UniqueViolation as exc:
            # Only the jti constraint means "duplicate"; a primary-key (or any
            # other) clash is a real fault and must not look like a replay.
            if exc.diag.constraint_name != _JTI_CONSTRAINT:
                raise
        # The failed transaction is rolled back and its connection returned,
        # so this lookup never holds two pooled connections at once.
        original = self.find_entry_id(entry.jti)
        if original is None:  # unreachable: the violation names this jti
            raise RuntimeError("duplicate jti has no original entry")
        raise DuplicateJti(original)

    def seen_jti(self, jti: str) -> bool:
        """Atomically claim `jti`. False = first use (claimed now; the caller may
        execute). True = replay or concurrent duplicate (caller must reject).

        Called by `verify_tool_call` BEFORE any handler logic, so at most one
        execution per jti. A claim without a ledger row means the outcome is
        unknown (crash between claim and append); the jti stays used.
        """
        with db.transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {_LEDGER_TABLE} WHERE jti = %s", (jti,))  # noqa: S608
            if cur.fetchone() is not None:
                return True
            cur.execute(
                "INSERT INTO public.tool_jti_claims (jti) VALUES (%s) "
                "ON CONFLICT (jti) DO NOTHING RETURNING jti",
                (jti,),
            )
            return cur.fetchone() is None

    def record_attempt(self, attempt: ToolCallAttempt) -> int:
        """Append one verification outcome to `tool_call_attempts`; return its
        id. Never stores raw arguments or `_ctx` tokens (there is no column for
        them). Raises on invalid input or database errors — callers fail
        closed (`verify_and_record`)."""
        validate_attempt(attempt)
        original = attempt.original_entry_id.value if attempt.original_entry_id else None
        with db.transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {_ATTEMPTS_TABLE}
                    (credential_id, mcp_endpoint, tool_name, outcome, jti, request_id,
                     workspace_id, requester_member_id, original_entry_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id""",  # noqa: S608
                (
                    attempt.credential_id,
                    attempt.mcp_endpoint,
                    attempt.tool_name,
                    attempt.outcome,
                    attempt.jti,
                    attempt.request_id,
                    attempt.workspace_id,
                    attempt.requester_member_id,
                    original,
                ),
            )
            row = cur.fetchone()
            if row is None:  # unreachable: RETURNING id always yields a row
                raise RuntimeError("attempt insert returned no id")
            return int(row[0])

    def find_entry_id(self, jti: str) -> LedgerEntryId | None:
        """Id of the ledger entry for `jti`, or None (not yet appended)."""
        with db.transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {_LEDGER_TABLE} WHERE jti = %s", (jti,))  # noqa: S608
            row = cur.fetchone()
            return LedgerEntryId(row[0]) if row is not None else None

    def get_by_jti(self, jti: str) -> dict[str, Any] | None:
        """Fetch one entry by `jti` (P1-06 evidence query; dedupe reference)."""
        with db.transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ROW_COLUMNS} FROM {_LEDGER_TABLE} WHERE jti = %s",  # noqa: S608
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
        with db.transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_ROW_COLUMNS} FROM {_LEDGER_TABLE} ORDER BY id")  # noqa: S608
            names = [d.name for d in cur.description or []]
            fetched = cur.fetchall()
            if any(len(names) != len(row) for row in fetched):
                raise RuntimeError("ledger row shape mismatch")
            rows = [dict(zip(names, row, strict=True)) for row in fetched]
        return verify_rows(rows)


class AttemptLedger(ReplayGuard, Protocol):
    """What `verify_and_record` needs: `Ledger` (or a test double)."""

    def record_attempt(self, attempt: ToolCallAttempt) -> int: ...
    def find_entry_id(self, jti: str) -> LedgerEntryId | None: ...


class _CapturingGuard:
    """Delegates to the ledger and remembers the jti it was asked about.
    `verify_tool_call` consults the guard only after the `_ctx` signature,
    audience, tool, and window verified, so a captured jti is trusted."""

    def __init__(self, inner: ReplayGuard) -> None:
        self._inner = inner
        self.jti: str | None = None

    def seen_jti(self, jti: str) -> bool:
        self.jti = jti
        return self._inner.seen_jti(jti)


def verify_and_record(
    ledger: AttemptLedger,
    endpoint: str,
    tool: str,
    raw_arguments: Mapping[str, Any],
    *,
    credential_id: str,
    ctx_secret: str | None,
    now: float | None = None,
) -> VerifiedCall:
    """`verify_tool_call` plus a durable `tool_call_attempts` row (P2-06 calls
    this instead of `verify_tool_call`, after the bearer check, before any
    handler logic).

    Fail closed: a rejection is always re-raised, even when recording it
    fails (logged by error class only). An accepted call whose attempt cannot
    be recorded raises `CtxRejection(INTERNAL)` and must not execute (its jti
    is already claimed, so it cannot be retried with the same `_ctx`).
    """
    guard = _CapturingGuard(ledger)
    try:
        verified = verify_tool_call(
            endpoint, tool, raw_arguments, ctx_secret=ctx_secret, replay_guard=guard, now=now
        )
    except CtxRejection as rejection:
        try:
            replay_jti = guard.jti if rejection.code == REPLAY else None
            ledger.record_attempt(
                ToolCallAttempt(
                    credential_id=credential_id,
                    mcp_endpoint=endpoint,
                    tool_name=tool,
                    outcome=rejection.code,
                    jti=replay_jti,
                    original_entry_id=ledger.find_entry_id(replay_jti) if replay_jti else None,
                )
            )
        except Exception as exc:
            logger.error("could not record rejected attempt (%s)", type(exc).__name__)
        raise
    ctx = verified.ctx
    try:
        ledger.record_attempt(
            ToolCallAttempt(
                credential_id=credential_id,
                mcp_endpoint=endpoint,
                tool_name=tool,
                outcome=ACCEPTED,
                jti=ctx.jti,
                request_id=ctx.request_id,
                workspace_id=ctx.workspace_id,
                requester_member_id=ctx.requester_member_id,
            )
        )
    except Exception as exc:
        logger.error("could not record accepted attempt (%s); rejecting", type(exc).__name__)
        raise CtxRejection(INTERNAL) from None
    return verified
