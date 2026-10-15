"""Idempotency tests (P2-07). Database tests need DATABASE_URL_TOOLSERVER
(live database) and skip otherwise; DSNs are captured at collection time and
never logged.
"""

import inspect
import os

import pytest

from services.authorization.ctx_envelope import ReplayGuard
from services.contracts import ToolExecutionStatus
from services.toolserver.ledger import DuplicateJti, Ledger, LedgerEntry

TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")

NEEDS_DB = pytest.mark.skipif(
    not TOOLSERVER_DSN,
    reason="needs DATABASE_URL_TOOLSERVER (live database)",
)

SECRET_MARKER = "super-secret-query-fragment-xyz"


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
        "arguments": {"query": SECRET_MARKER},
    }
    values.update(overrides)
    return LedgerEntry(**values)  # type: ignore[arg-type]


@pytest.fixture
def ledger() -> Ledger:
    assert TOOLSERVER_DSN is not None
    handle = Ledger(TOOLSERVER_DSN)
    yield handle
    handle.close()


def test_seen_jti_matches_replay_guard_protocol() -> None:
    """P1-04 compatibility: Ledger.seen_jti has the protocol's signature."""
    assert list(inspect.signature(Ledger.seen_jti).parameters) == list(
        inspect.signature(ReplayGuard.seen_jti).parameters
    )


@NEEDS_DB
def test_duplicate_jti_not_executed(ledger: Ledger) -> None:
    original = ledger.append(make_entry("idem-jti-1"))
    assert not ledger.seen_jti("idem-jti-fresh")
    with pytest.raises(DuplicateJti):
        ledger.append(make_entry("idem-jti-1"))
    assert ledger.get_by_jti("idem-jti-1")["id"] == original.value


@NEEDS_DB
def test_duplicate_returns_reference_to_original(ledger: Ledger) -> None:
    original = ledger.append(make_entry("idem-jti-2"))
    with pytest.raises(DuplicateJti) as exc_info:
        ledger.append(make_entry("idem-jti-2", request_id="req-other"))
    assert exc_info.value.existing_id.value == original.value
    stored = ledger.get_by_jti("idem-jti-2")
    assert stored is not None and stored["request_id"] == "req-1"


@NEEDS_DB
def test_no_raw_arguments_stored(ledger: Ledger) -> None:
    ledger.append(make_entry("idem-jti-3"))
    stored = ledger.get_by_jti("idem-jti-3")
    assert stored is not None
    assert stored["arguments_sha256"] is not None and len(stored["arguments_sha256"]) == 64
    haystack = " ".join(str(value) for value in stored.values())
    assert SECRET_MARKER not in haystack
