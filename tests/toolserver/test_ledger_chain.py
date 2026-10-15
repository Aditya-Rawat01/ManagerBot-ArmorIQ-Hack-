"""Hash-chain tests (P2-07). Pure chain-logic tests run anywhere; database
tests need DATABASE_URL_TOOLSERVER (live database) and skip otherwise, so CI
(without secrets) stays green. DSNs are captured at collection time because
the conftest scrubs the environment during test runs; they are never logged.
"""

import os
import threading

import pytest

from services.contracts import ToolExecutionStatus
from services.toolserver.ledger import (
    GENESIS_PREV_HASH,
    Ledger,
    LedgerEntry,
    VerificationReport,
    arguments_sha256,
    compute_entry_hash,
    verify_rows,
)

TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")

NEEDS_DB = pytest.mark.skipif(
    not TOOLSERVER_DSN,
    reason="needs DATABASE_URL_TOOLSERVER (live database)",
)

BASE_FIELDS = {
    "request_id": "req-1",
    "trace_id": "trace-1",
    "plan_id": "plan-1",
    "armoriq_plan_id": None,
    "step_index": 0,
    "tool_name": "knowledge_search_team",
    "jti": "jti-1",
    "requester_member_id": "m-sam",
    "workspace_id": "workspace-primary",
    "validation_outcome": "accepted",
    "execution_status": "executed",
    "result_id": None,
    "error_class": None,
    "arguments_sha256": arguments_sha256({"query": "decisions"}),
}


def make_rows(count: int) -> list[dict]:
    rows = []
    prev = GENESIS_PREV_HASH
    for index in range(count):
        fields = dict(BASE_FIELDS, jti=f"jti-{index}")
        entry_hash = compute_entry_hash(fields, prev)
        rows.append({"id": index + 1, **fields, "prev_hash": prev, "entry_hash": entry_hash})
        prev = entry_hash
    return rows


def make_entry(jti: str, **overrides: object) -> LedgerEntry:
    values: dict[str, object] = {
        "request_id": "req-1",
        "trace_id": "trace-1",
        "tool_name": "knowledge_search_team",
        "jti": jti,
        "execution_status": ToolExecutionStatus.EXECUTED,
        "plan_id": "plan-1",
        "step_index": 0,
        "requester_member_id": "m-sam",
        "workspace_id": "workspace-primary",
        "arguments": {"query": "decisions"},
    }
    values.update(overrides)
    return LedgerEntry(**values)  # type: ignore[arg-type]


@pytest.fixture
def ledger() -> Ledger:
    assert TOOLSERVER_DSN is not None
    handle = Ledger(TOOLSERVER_DSN)
    yield handle
    handle.close()


# --- pure chain logic (no database) -----------------------------------------


def test_chain_valid() -> None:
    report = verify_rows(make_rows(3))
    assert report == VerificationReport(ok=True, checked=3, broken_at=None, detail="chain valid")


def test_detects_modification() -> None:
    rows = make_rows(3)
    rows[1]["tool_name"] = "knowledge_search_restricted"
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 2


def test_detects_reordering() -> None:
    rows = make_rows(3)
    rows[1], rows[2] = rows[2], rows[1]
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 3


def test_detects_interior_deletion() -> None:
    rows = make_rows(3)
    del rows[1]
    report = verify_rows(rows)
    assert not report.ok and report.broken_at == 3


def test_tail_truncation_is_undetectable() -> None:
    """Documents the limitation: removed newest rows leave a valid prefix."""
    assert verify_rows(make_rows(3)[:2]).ok


# --- live database ----------------------------------------------------------


@NEEDS_DB
def test_append_and_verify_chain(ledger: Ledger) -> None:
    first = ledger.append(make_entry("chain-jti-1"))
    second = ledger.append(make_entry("chain-jti-2"))
    assert second.value > first.value
    report = ledger.verify_chain()
    assert report.ok and report.checked >= 2


@NEEDS_DB
def test_concurrent_appends_single_chain(ledger: Ledger) -> None:
    ids: list[int] = []
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            entry_id = ledger.append(make_entry(f"concurrent-jti-{index}"))
            ids.append(entry_id.value)
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(ids)) == 20
    assert ledger.verify_chain().ok
