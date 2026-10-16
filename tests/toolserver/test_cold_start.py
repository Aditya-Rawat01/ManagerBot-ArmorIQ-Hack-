"""Neon cold-start survival tests (fix for the CP-4 probe finding).

Offline tests use an in-memory fake pool whose first checkouts raise
OperationalError (a dead pooled connection after a Neon suspend); the live
restart test needs a LOCAL database plus PROXYLEAD_DB_WRITE_TESTS=1 and
restarts the Postgres container between two calls. DSNs are captured at
collection time and never logged.
"""

import os
import time
import uuid
from urllib.parse import urlparse

import pytest
from psycopg import errors as pg_errors

from services.contracts import McpEndpoint, ToolName
from services.toolserver.ledger import (
    Ledger,
    ToolCallAttempt,
    verify_and_record,
)
from services.toolserver.security import CtxRejection
from tests.toolserver.test_tool_dispatch import CTX_SECRET, call, live_client, make_ctx

TOOL = ToolName.KNOWLEDGE_SEARCH_TEAM.value
ENDPOINT = McpEndpoint.MEMORY.value

_RAW_DSN = os.environ.get("DATABASE_URL_TOOLSERVER", "")
_HOST = urlparse(_RAW_DSN).hostname or "" if _RAW_DSN else ""
_LIVE_DSN = (
    _RAW_DSN
    if _RAW_DSN and ("localhost" in _HOST or "127.0.0.1" in _HOST) and "neon" not in _HOST
    else ""
)
LIVE_RESTART = bool(_LIVE_DSN) and os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"

NEEDS_RESTART = pytest.mark.skipif(
    not LIVE_RESTART, reason="needs a local DSN plus PROXYLEAD_DB_WRITE_TESTS=1"
)


def _dead() -> pg_errors.OperationalError:
    return pg_errors.OperationalError("connection failed")


class FakeCursor:
    def __init__(self, store: dict):
        self.store = store

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple = ()) -> None:
        q = " ".join(query.split())
        if q.startswith("SET LOCAL"):
            return
        if "FROM public.tool_execution_ledger WHERE jti" in q:
            self._one = (1,) if params[0] in self.store["rows"] else None
        elif "INSERT INTO public.tool_jti_claims" in q:
            if params[0] in self.store["claims"]:
                self._one = None
            else:
                self.store["claims"].add(params[0])
                self._one = (params[0],)
        elif q.startswith("INSERT INTO public.tool_call_attempts"):
            self.store["attempts"].append(params)
            self._one = (len(self.store["attempts"]),)
        elif "FROM public.tool_execution_ledger" in q:
            self._one = None
        else:
            self._one = None

    def fetchone(self) -> tuple | None:
        return self._one


class FakeConn:
    def __init__(self, store: dict):
        self.store = store

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def transaction(self):  # type: ignore[no-untyped-def]
        from contextlib import nullcontext

        return nullcontext()

    def execute(self, query: str, params: tuple = ()) -> None:
        return None


class FakePool:
    """Fails the first `failures` checkouts with OperationalError, then serves."""

    def __init__(self, failures: int = 1):
        self.store: dict = {"claims": set(), "attempts": [], "rows": {}}
        self.failures = failures
        self.checkouts = 0

    def connection(self):  # type: ignore[no-untyped-def]
        from contextlib import contextmanager

        @contextmanager
        def _connect():
            self.checkouts += 1
            if self.checkouts <= self.failures:
                raise _dead()
            yield FakeConn(self.store)

        return _connect()


def test_claim_succeeds_after_one_disconnect() -> None:
    ledger = Ledger(pool=FakePool(failures=1))
    assert ledger.seen_jti("a") is False
    assert ledger.seen_jti("a") is True


def test_verify_and_record_succeeds_after_disconnect() -> None:
    ledger = Ledger(pool=FakePool(failures=1))
    verified = verify_and_record(
        ledger,
        ENDPOINT,
        TOOL,
        {"query": "quokka", "_ctx": make_ctx()},
        credential_id="toolserver_bearer_memory",
        ctx_secret=CTX_SECRET,
    )
    assert verified.ctx.tool == TOOL
    assert len(ledger._pool.store["attempts"]) == 1


def test_double_disconnect_is_internal() -> None:
    ledger = Ledger(pool=FakePool(failures=99))
    with pytest.raises(CtxRejection) as exc_info:
        verify_and_record(
            ledger,
            ENDPOINT,
            TOOL,
            {"query": "quokka", "_ctx": make_ctx()},
            credential_id="toolserver_bearer_memory",
            ctx_secret=CTX_SECRET,
        )
    assert exc_info.value.code == "internal"


def test_non_connection_errors_are_not_retried() -> None:
    pool = FakePool(failures=0)
    ledger = Ledger(pool=pool)
    checkouts_before = pool.checkouts
    with pytest.raises(ValueError, match="outcome is not an attempt outcome"):
        ledger.record_attempt(
            ToolCallAttempt(
                credential_id="toolserver_bearer_memory",
                mcp_endpoint=ENDPOINT,
                tool_name=TOOL,
                outcome="bogus-outcome",
            )
        )
    assert pool.checkouts == checkouts_before


@pytest.mark.parametrize(
    ("make_error", "retried"),
    [
        (lambda: pg_errors.OperationalError("server closed the connection unexpectedly"), True),
        (
            lambda: pg_errors.AdminShutdown("terminating connection due to administrator command"),
            True,
        ),
        (lambda: pg_errors.QueryCanceled("canceling statement due to statement timeout"), False),
        (lambda: __import__("psycopg_pool").PoolTimeout("couldn't get a connection"), False),
    ],
)
def test_only_dead_connections_are_retried(make_error, retried) -> None:
    """Pool and statement timeouts are not retried (they would double the wait)."""
    from services.toolserver.ledger import _retry_once_on_disconnect

    calls: list[int] = []

    @_retry_once_on_disconnect
    def flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise make_error()
        return "ok"

    if retried:
        assert flaky() == "ok"
        assert len(calls) == 2
    else:
        with pytest.raises(pg_errors.OperationalError):
            flaky()
        assert len(calls) == 1


@NEEDS_RESTART
def test_container_restart_between_calls(real_model: object) -> None:
    """Live: restart Postgres between two valid calls; both record and run."""
    import shutil
    import subprocess

    del real_model  # the fixture warms the real model; the value is unused
    docker = shutil.which("docker")
    assert docker, "docker is required for the restart test"

    jtis = [f"jti-restart-{uuid.uuid4().hex}", f"jti-restart-{uuid.uuid4().hex}"]
    with live_client() as client:
        first = call(client, {"query": "quokka", "limit": 3, "_ctx": make_ctx(jti=jtis[0])})
        assert first["result"]["isError"] is False
        subprocess.run(  # noqa: S603 — fixed argv (docker CLI + literals), no shell, tests only
            [docker, "compose", "restart", "db"],
            check=True,
            capture_output=True,
            timeout=120,
        )
        deadline = time.time() + 90
        while True:
            probe = subprocess.run(  # noqa: S603 — fixed argv, no shell, tests only
                [
                    docker,
                    "compose",
                    "exec",
                    "-T",
                    "db",
                    "pg_isready",
                    "-U",
                    "proxylead",
                    "-d",
                    "proxylead",
                ],
                capture_output=True,
                timeout=30,
            )
            if probe.returncode == 0:
                break
            assert time.time() < deadline, "db did not come back after restart"
            time.sleep(3)
        second = call(client, {"query": "quokka", "limit": 3, "_ctx": make_ctx(jti=jtis[1])})
        assert second["result"]["isError"] is False
    import psycopg

    with psycopg.connect(_LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.tool_call_attempts WHERE jti = ANY(%s)",
            (jtis,),
        )
        assert cur.fetchone()[0] == 2
