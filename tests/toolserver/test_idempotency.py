"""Idempotency tests (P2-07). Database tests need DATABASE_URL_TOOLSERVER
(live database) and skip otherwise; DSNs are captured at collection time and
never logged.
"""

import inspect
import os
import threading
import time
import uuid

import pytest

from services.authorization.ctx_envelope import CtxEnvelope, ReplayGuard, sign_ctx
from services.contracts import ToolExecutionStatus
from services.toolserver.ledger import DuplicateJti, Ledger, LedgerEntry
from services.toolserver.security import REPLAY, CtxRejection, verify_tool_call

TOOLSERVER_DSN = os.environ.get("DATABASE_URL_TOOLSERVER")
# The ledger and the claims table are append-only (no DELETE for any runtime
# role), so these tests write PERMANENT rows. Run them only against a local
# or throwaway database, never the shared Neon evidence ledger.
WRITE_TESTS = os.environ.get("PROXYLEAD_LEDGER_WRITE_TESTS") == "1"

NEEDS_DB = pytest.mark.skipif(
    not (TOOLSERVER_DSN and WRITE_TESTS),
    reason="needs DATABASE_URL_TOOLSERVER and PROXYLEAD_LEDGER_WRITE_TESTS=1 "
    "(writes permanent ledger rows: local database only)",
)


def uid(prefix: str) -> str:
    """Unique per run, so re-runs never collide with earlier permanent rows."""
    return f"test-{prefix}-{uuid.uuid4().hex[:12]}"


SECRET_MARKER = "super-secret-query-fragment-xyz"


def make_entry(jti: str, **overrides: object) -> LedgerEntry:
    values: dict[str, object] = {
        "request_id": uid("req"),
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
    jti = uid("jti")
    original = ledger.append(make_entry(jti))
    assert ledger.seen_jti(jti), "a jti already in the ledger is a replay"
    with pytest.raises(DuplicateJti):
        ledger.append(make_entry(jti))
    assert ledger.get_by_jti(jti)["id"] == original.value


@NEEDS_DB
def test_seen_jti_claims_atomically(ledger: Ledger) -> None:
    """Check-and-record like InMemoryReplayGuard: first call False, then True."""
    jti = uid("claim")
    assert ledger.seen_jti(jti) is False
    assert ledger.seen_jti(jti) is True


@NEEDS_DB
def test_concurrent_duplicate_claims_only_one_wins(ledger: Ledger) -> None:
    """Simultaneous calls with the same jti: exactly one may execute."""
    jti = uid("race")
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(ledger.seen_jti(jti))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(False) == 1
    assert results.count(True) == 7


@NEEDS_DB
def test_verify_tool_call_rejects_replay_with_ledger_guard(ledger: Ledger) -> None:
    """P1-04 + P2-07 wired together: the second identical call is a replay."""
    secret = "test-ctx-secret-not-a-real-secret"
    now = time.time()
    token = sign_ctx(
        CtxEnvelope(
            request_id=uid("req"),
            trace_id="trace-1",
            workspace_id="workspace-primary",
            requester_member_id="m-sam",
            armoriq_member_email_ref="PERSONA_SAM_EMAIL",
            app_role="senior",
            plan_id="plan-1",
            step_index=0,
            tool="knowledge_search_team",
            aud="memory-mcp",
            iat=now,
            exp=now + 300,
            jti=uid("ctx"),
        ),
        secret,
    )
    call = {"_ctx": token, "query": "q", "limit": 5}
    verify_tool_call(
        "memory-mcp", "knowledge_search_team", call, ctx_secret=secret, replay_guard=ledger
    )
    with pytest.raises(CtxRejection) as exc_info:
        verify_tool_call(
            "memory-mcp", "knowledge_search_team", call, ctx_secret=secret, replay_guard=ledger
        )
    assert exc_info.value.code == REPLAY


@NEEDS_DB
def test_duplicate_returns_reference_to_original(ledger: Ledger) -> None:
    jti = uid("jti")
    first = make_entry(jti)
    original = ledger.append(first)
    with pytest.raises(DuplicateJti) as exc_info:
        ledger.append(make_entry(jti, request_id=uid("req-other")))
    assert exc_info.value.existing_id.value == original.value
    stored = ledger.get_by_jti(jti)
    assert stored is not None and stored["request_id"] == first.request_id


@NEEDS_DB
def test_no_raw_arguments_stored(ledger: Ledger) -> None:
    jti = uid("jti")
    ledger.append(make_entry(jti))
    stored = ledger.get_by_jti(jti)
    assert stored is not None
    assert stored["arguments_sha256"] is not None and len(stored["arguments_sha256"]) == 64
    haystack = " ".join(str(value) for value in stored.values())
    assert SECRET_MARKER not in haystack
