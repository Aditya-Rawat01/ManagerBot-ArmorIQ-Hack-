"""Guarded dispatch tests (P2-06).

Rejection tests run without a database (no DSN: verification with an
in-memory replay guard, and no product tool executes). Success and
ledger-outcome tests inject `FakeLedger` (same entry validation and claim
semantics as `Ledger`). Live tests write PERMANENT rows (ledger, jti claims,
attempts are append-only), so they need DATABASE_URL_TOOLSERVER plus
PROXYLEAD_DB_WRITE_TESTS=1 and refuse Neon hosts. DSNs are captured at
collection time and never logged.
"""

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from services.authorization.ctx_envelope import CtxEnvelope, sign_ctx
from services.contracts import (
    DataTier,
    KnowledgeSearchResult,
    McpEndpoint,
    ToolExecutionStatus,
    ToolName,
    tool_spec,
)
from services.toolserver import main
from services.toolserver.ledger import (
    DuplicateJti,
    LedgerEntry,
    LedgerEntryId,
    ToolCallAttempt,
    validate_attempt,
    validate_entry,
)
from services.toolserver.main import create_app
from services.toolserver.schemas import NotFound
from services.toolserver.tools_memory import PRODUCT_TOOLS, ProductTool

from .conftest import MEMORY_TOKEN, WORKSPACE_TOKEN, auth, make_settings

CTX_SECRET = "test-ctx-secret-for-dispatch"
FAKE_DSN = "postgresql://fake-user@fake-host.invalid/fake"


def live_write_dsn(raw: str, write_flag: str | None) -> str:
    """The DSN for tests that write permanent rows, or "" to skip them.

    Requires PROXYLEAD_DB_WRITE_TESTS=1 and refuses Neon: the parsed host
    must not have a `neon` label (no substring matching on the DSN)."""
    if not raw or write_flag != "1":
        return ""
    host = (urlparse(raw).hostname or "").lower()
    if not host or "neon" in host.split("."):
        return ""
    return raw


LIVE_DSN = live_write_dsn(
    os.environ.get("DATABASE_URL_TOOLSERVER", ""), os.environ.get("PROXYLEAD_DB_WRITE_TESTS")
)

NEEDS_DB = pytest.mark.skipif(
    not LIVE_DSN,
    reason="needs DATABASE_URL_TOOLSERVER and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent ledger rows: local database only, never Neon)",
)

NOW = time.time()
TOOL = ToolName.KNOWLEDGE_SEARCH_TEAM.value


def make_ctx(*, jti: str | None = None, secret: str = CTX_SECRET, **overrides: object) -> str:
    values: dict[str, object] = {
        "request_id": "req-dispatch",
        "trace_id": "trace-dispatch",
        "workspace_id": "workspace-primary",
        "requester_member_id": "m-sam",
        "armoriq_member_email_ref": "PERSONA_SAM_EMAIL",
        "app_role": "senior",
        "plan_id": "plan-dispatch",
        "step_index": 0,
        "tool": TOOL,
        "aud": McpEndpoint.MEMORY.value,
        "iat": NOW - 60.0,
        "exp": NOW + 600.0,
        "jti": jti or f"jti-{uuid.uuid4().hex}",
    }
    values.update(overrides)
    return sign_ctx(CtxEnvelope(**values), secret)  # type: ignore[arg-type]


def dev_client(**overrides: object) -> TestClient:
    """No DSN: verification runs, no product tool executes."""
    values: dict[str, object] = {"ctx_secret": CTX_SECRET}
    values.update(overrides)
    app = create_app(make_settings(**values))
    return TestClient(app, raise_server_exceptions=False)


def live_client() -> TestClient:
    assert LIVE_DSN, "live_client() is only used by NEEDS_DB tests"
    app = create_app(make_settings(ctx_secret=CTX_SECRET, database_url_toolserver=LIVE_DSN))
    return TestClient(app, raise_server_exceptions=False)


class FakePool:
    def close(self) -> None:
        pass


class FakeLedger:
    """In-memory `Ledger` double: `validate_entry`/`validate_attempt` like the
    real writer, claim-once jti semantics, append-time duplicate detection."""

    def __init__(self, *, fail: dict[str, Exception] | None = None) -> None:
        self.claimed: set[str] = set()
        self.entries: dict[int, LedgerEntry] = {}
        self.attempts: list[ToolCallAttempt] = []
        self.fail = fail or {}
        self._next_id = 100

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail:
            raise self.fail[method]

    def seen_jti(self, jti: str) -> bool:
        self._maybe_fail("seen_jti")
        if jti in self.claimed:
            return True
        self.claimed.add(jti)
        return False

    def record_attempt(self, attempt: ToolCallAttempt) -> int:
        self._maybe_fail("record_attempt")
        validate_attempt(attempt)
        self.attempts.append(attempt)
        return len(self.attempts)

    def find_entry_id(self, jti: str) -> LedgerEntryId | None:
        for entry_id, entry in self.entries.items():
            if entry.jti == jti:
                return LedgerEntryId(entry_id)
        return None

    def append(self, entry: LedgerEntry) -> LedgerEntryId:
        self._maybe_fail("append")
        validate_entry(entry)
        existing = self.find_entry_id(entry.jti)
        if existing is not None:
            raise DuplicateJti(existing)
        self._next_id += 1
        self.entries[self._next_id] = entry
        return LedgerEntryId(self._next_id)

    def rows(self, jti: str) -> list[tuple[int, LedgerEntry]]:
        return [(i, e) for i, e in self.entries.items() if e.jti == jti]


def fake_ledger_client(monkeypatch: pytest.MonkeyPatch, ledger: FakeLedger) -> TestClient:
    """App whose lifespan gets `ledger` and a fake pool instead of a database."""
    monkeypatch.setattr(main.db, "create_pool", lambda dsn, **kwargs: FakePool())
    monkeypatch.setattr(main, "Ledger", lambda pool: ledger)
    app = create_app(make_settings(ctx_secret=CTX_SECRET, database_url_toolserver=FAKE_DSN))
    return TestClient(app, raise_server_exceptions=False)


Handler = Callable[..., Any]


def install_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
    tool: ToolName = ToolName.KNOWLEDGE_SEARCH_TEAM,
) -> None:
    monkeypatch.setitem(
        PRODUCT_TOOLS, tool, ProductTool(name=tool, description="test tool", handler=handler)
    )


class Spy:
    """Handler double that records calls and returns an empty team result."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self.error = error

    async def __call__(self, verified: Any, args: Any, runtime: Any) -> KnowledgeSearchResult:
        self.calls.append(verified)
        if self.error is not None:
            raise self.error
        return KnowledgeSearchResult(
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM, tier=DataTier.TEAM, query=args.query, hits=[]
        )


def call(
    client: TestClient,
    arguments: dict,
    token: str = MEMORY_TOKEN,
    *,
    tool: str = TOOL,
    path: str = "/memory/mcp",
) -> dict:
    response = client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError("no SSE data frame")


def result_text(message: dict) -> dict:
    """The §4 error body of an isError result."""
    assert message["result"]["isError"] is True
    return json.loads(message["result"]["content"][0]["text"])


def result_meta(message: dict) -> dict:
    return message["result"].get("_meta") or {}


def list_tools(client: TestClient, path: str, token: str) -> list[dict]:
    response = client.post(
        path, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth(token)
    )
    assert response.status_code == 200
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())["result"]["tools"]
    raise AssertionError("no SSE data frame")


# --- registration ------------------------------------------------------------


def test_all_product_tools_registered_via_guard() -> None:
    assert set(PRODUCT_TOOLS) == {
        ToolName.KNOWLEDGE_SEARCH_TEAM,
        ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        ToolName.KNOWLEDGE_SEARCH_RESTRICTED,
        ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
        ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
        ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED,
        ToolName.READ_SOURCE_TEAM,
        ToolName.READ_SOURCE_CONFIDENTIAL,
        ToolName.READ_SOURCE_RESTRICTED,
    }
    with dev_client() as client:
        # No _ctx: rejected by the dispatcher before any handler runs.
        message = call(client, {"query": "quokka", "limit": 5})
        assert result_text(message)["code"] == "invalid_context"


@pytest.mark.parametrize("tool", list(ToolName))
def test_every_tool_name_is_dispatched_never_forwarded(tool: ToolName) -> None:
    """A ToolName without a guarded handler on the endpoint it is called on
    answers `internal`; nothing reaches the SDK's unguarded tool path."""
    endpoint = tool_spec(tool).endpoint
    for path, token, served_by in (
        ("/memory/mcp", MEMORY_TOKEN, McpEndpoint.MEMORY),
        ("/workspace/mcp", WORKSPACE_TOKEN, McpEndpoint.WORKSPACE),
    ):
        implemented = tool in PRODUCT_TOOLS and endpoint is served_by
        # Manager clears every ceiling: implemented tools reach the
        # no-ledger refusal, unimplemented ones are refused earlier.
        ctx = make_ctx(
            tool=tool.value,
            aud=served_by.value,
            app_role="manager",
            requester_member_id="m-maya",
            armoriq_member_email_ref="PERSONA_MAYA_EMAIL",
        )
        if "read_source" in tool.value:
            view = f"approved_company_data_{tool.value.rsplit('_', 1)[-1]}"
            arguments = {"view": view, "limit": 1}
        elif "get_source" in tool.value:
            arguments = {"source_ref": "team-friday-deploy-freeze"}
        else:
            arguments = {"query": "q", "limit": 1}
        with dev_client() as client:
            message = call(client, {**arguments, "_ctx": ctx}, token, tool=tool.value, path=path)
        # Implemented here: verified, then refused for lack of a ledger.
        # Not implemented here: refused before verification. Both `internal`.
        assert result_text(message)["code"] == "internal", (tool.value, path, implemented)


def test_unregistered_tool_names_are_refused_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ToolName registered on the server outside PRODUCT_TOOLS (e.g. a
    future `server.tool(...)` line) must never run unguarded.

    NOTE for P2-09: `knowledge_save_team_note` stands in for a fake
    UNREGISTERED tool here only because it has no guarded handler yet. When
    P2-09 adds it to PRODUCT_TOOLS, switch this test to a ToolName that is
    still unimplemented (e.g. a workspace tool before P2-10), or it stops
    testing the unregistered path."""
    ran: list[str] = []
    real_build = main.build_server

    def build_with_rogue_tool(endpoint: McpEndpoint, **kwargs: Any) -> Any:
        server = real_build(endpoint, **kwargs)

        def rogue(query: str = "") -> str:
            ran.append(endpoint.value)
            return "leaked"

        # A memory tool with no guarded handler (P2-09 owns it later).
        server.add_tool(rogue, name=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE.value)
        return server

    monkeypatch.setattr(main, "build_server", build_with_rogue_tool)
    ledger = FakeLedger()
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(
            client,
            {
                "query": "q",
                "_ctx": make_ctx(tool=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE.value),
            },
            tool=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE.value,
        )
    assert result_text(message)["code"] == "internal"
    assert ran == []
    assert ledger.attempts == [] and ledger.entries == {}


def test_input_schema_is_the_enforced_argument_model() -> None:
    with dev_client() as client:
        tools = {t["name"]: t for t in list_tools(client, "/memory/mcp", MEMORY_TOKEN)}
    schema = tools[TOOL]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["query", "limit"]
    assert schema["properties"]["query"]["maxLength"] == 500
    assert schema["properties"]["query"]["minLength"] == 1
    assert schema["properties"]["limit"]["maximum"] == 50
    assert schema["properties"]["limit"]["minimum"] == 1
    # `_ctx` is transport metadata (D-05): admitted, never declared.
    assert "_ctx" not in schema["properties"]
    assert schema["patternProperties"] == {"^_": {}}
    assert "outputSchema" not in tools[TOOL]


def test_runtime_repr_hides_ctx_secret() -> None:
    from services.authorization.ctx_envelope import InMemoryReplayGuard
    from services.toolserver.servers import ToolRuntime

    runtime = ToolRuntime(
        endpoint=McpEndpoint.MEMORY,
        ledger=None,
        pool=None,
        ctx_secret=CTX_SECRET,
        credential_id="toolserver_bearer_memory",
        dev_guard=InMemoryReplayGuard(),
    )
    assert CTX_SECRET not in repr(runtime)


def test_bypassing_the_interceptor_never_runs_a_product_tool() -> None:
    """The SDK's own call path (no interceptor) hits the fail-closed canary."""
    import anyio

    from services.toolserver.servers import build_server

    server = build_server(McpEndpoint.MEMORY, enable_diagnostic_tools=False, holder={})

    async def direct() -> Any:
        return await server.call_tool(TOOL, {"query": "q", "limit": 1, "_ctx": make_ctx()})

    with pytest.raises(Exception, match="knowledge_search_team") as exc_info:
        anyio.run(direct)
    assert "only through guarded dispatch" in str(exc_info.value.__cause__)


# --- rejections never run the handler ------------------------------------------


REJECTIONS: dict[str, tuple[Callable[[], dict], str, str]] = {
    # name: (arguments factory, tool called, expected code)
    "no_ctx": (lambda: {"query": "q", "limit": 5}, TOOL, "invalid_context"),
    "forged": (
        lambda: {"query": "q", "limit": 5, "_ctx": make_ctx(secret="forger-secret")},
        TOOL,
        "invalid_context",
    ),
    "expired": (
        lambda: {"query": "q", "limit": 5, "_ctx": make_ctx(iat=NOW - 7200, exp=NOW - 3600)},
        TOOL,
        "context_expired",
    ),
    "wrong_audience": (
        lambda: {"query": "q", "limit": 5, "_ctx": make_ctx(aud=McpEndpoint.WORKSPACE.value)},
        TOOL,
        "invalid_context",
    ),
    "invalid_arguments": (
        lambda: {"query": "q", "limit": 5, "tier": "restricted", "_ctx": make_ctx()},
        TOOL,
        "invalid_arguments",
    ),
    "over_ceiling": (
        lambda: {
            "query": "q",
            "limit": 5,
            "_ctx": make_ctx(tool=ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value),
        },
        ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value,
        "out_of_scope",
    ),
    "no_ledger": (lambda: {"query": "q", "limit": 5, "_ctx": make_ctx()}, TOOL, "internal"),
}


@pytest.mark.parametrize("kind", sorted(REJECTIONS))
def test_rejections_never_run_the_handler(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    spy = Spy()
    install_handler(monkeypatch, spy)
    arguments, tool, code = REJECTIONS[kind]
    with dev_client() as client:
        message = call(client, arguments(), tool=tool)
    assert result_text(message)["code"] == code
    assert spy.calls == []


def test_replay_never_runs_the_handler_and_references_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy()
    install_handler(monkeypatch, spy)
    ledger = FakeLedger()
    jti = f"jti-replay-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        first = call(client, {"query": "q", "limit": 5, "_ctx": make_ctx(jti=jti)})
        second = call(client, {"query": "q", "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert first["result"]["isError"] is False
    original = result_meta(first)["ledger_entry_id"]
    assert result_text(second)["code"] == "replay"
    assert result_meta(second) == {"original_entry_id": original}
    assert len(spy.calls) == 1, "the replay must not execute"
    assert [a.outcome for a in ledger.attempts] == ["accepted", "replay"]
    assert len(ledger.rows(jti)) == 1


# --- ledger outcome rows and the result shape (fake ledger) --------------------


def test_success_carries_its_ledger_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy()
    install_handler(monkeypatch, spy)
    ledger = FakeLedger()
    jti = f"jti-ok-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert message["result"]["isError"] is False
    body = KnowledgeSearchResult.model_validate_json(message["result"]["content"][0]["text"])
    assert body.tool is ToolName.KNOWLEDGE_SEARCH_TEAM and body.tier is DataTier.TEAM
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.execution_status is ToolExecutionStatus.EXECUTED
    assert entry.validation_outcome == "accepted"
    assert (entry.request_id, entry.trace_id, entry.step_index, entry.plan_id) == (
        "req-dispatch",
        "trace-dispatch",
        0,
        "plan-dispatch",
    )
    assert (entry.requester_member_id, entry.workspace_id) == ("m-sam", "workspace-primary")
    assert dict(entry.arguments) == {"query": "quokka", "limit": 5}, "no _ctx in the row"


def test_handler_error_is_recorded_and_not_leaked(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(error=RuntimeError("boom: secret-detail-zzz"))
    install_handler(monkeypatch, spy)
    ledger = FakeLedger()
    jti = f"jti-err-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(client, {"query": "q", "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert result_text(message)["code"] == "internal"
    assert "secret-detail-zzz" not in json.dumps(message)
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.validation_outcome == "accepted"
    assert entry.execution_status is ToolExecutionStatus.ERROR
    assert entry.error_class == "RuntimeError"


def test_invalid_arguments_are_recorded_as_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy()
    install_handler(monkeypatch, spy)
    ledger = FakeLedger()
    jti = f"jti-args-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(client, {"query": "q", "limit": 500, "_ctx": make_ctx(jti=jti)})
    assert result_text(message)["code"] == "invalid_arguments"
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.validation_outcome == "invalid_arguments"
    assert entry.execution_status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER
    assert spy.calls == []


def test_tier_ceiling_denies_before_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec step 1.3: an over-ceiling tool is refused (`out_of_scope`) and
    recorded as a rejection before any handler logic (P2-08 per-role ceiling:
    senior clears confidential but not restricted)."""
    tool = ToolName.KNOWLEDGE_SEARCH_RESTRICTED
    spy = Spy()
    install_handler(monkeypatch, spy, tool)
    ledger = FakeLedger()
    jti = f"jti-scope-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(
            client,
            {"query": "q", "limit": 5, "_ctx": make_ctx(jti=jti, tool=tool.value)},
            tool=tool.value,
        )
    assert result_text(message)["code"] == "out_of_scope"
    assert spy.calls == []
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.validation_outcome == "out_of_scope"
    assert entry.execution_status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER


async def _nothing_visible(verified: Any, args: Any, runtime: Any) -> Any:
    raise NotFound("no visible source: secret-detail-zzz")


def test_not_found_is_an_executed_lookup_with_its_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-11 `not_found`: the authorized lookup ran (accepted/executed row,
    referenced in `_meta`) and found nothing; fixed body, no exception text."""
    tool = ToolName.KNOWLEDGE_GET_SOURCE_TEAM
    install_handler(monkeypatch, _nothing_visible, tool)
    ledger = FakeLedger()
    jti = f"jti-nf-{uuid.uuid4().hex}"
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(
            client,
            {"source_ref": "no-such-source", "_ctx": make_ctx(jti=jti, tool=tool.value)},
            tool=tool.value,
        )
    assert result_text(message) == {"code": "not_found", "message": "No matching source found."}
    assert "secret-detail-zzz" not in json.dumps(message)
    [(entry_id, entry)] = ledger.rows(jti)
    assert result_meta(message) == {"ledger_entry_id": str(entry_id)}
    assert entry.validation_outcome == "accepted"
    assert entry.execution_status is ToolExecutionStatus.EXECUTED
    assert entry.error_class is None


def test_unstored_not_found_is_internal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A not_found whose ledger row could not be stored is never reported as
    an executed lookup: `internal`, no `_meta`."""
    tool = ToolName.KNOWLEDGE_GET_SOURCE_TEAM
    install_handler(monkeypatch, _nothing_visible, tool)
    ledger = FakeLedger(fail={"append": ValueError("entry rejected")})
    with caplog.at_level(logging.ERROR), fake_ledger_client(monkeypatch, ledger) as client:
        message = call(
            client,
            {"source_ref": "no-such-source", "_ctx": make_ctx(tool=tool.value)},
            tool=tool.value,
        )
    assert result_text(message)["code"] == "internal"
    assert result_meta(message) == {}
    assert any(r.levelno >= logging.ERROR and "ledger" in r.getMessage() for r in caplog.records)


def test_unstored_execution_is_never_reported_as_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_handler(monkeypatch, Spy())
    ledger = FakeLedger(fail={"append": ValueError("entry rejected")})
    with caplog.at_level(logging.ERROR), fake_ledger_client(monkeypatch, ledger) as client:
        message = call(client, {"query": "q", "limit": 5, "_ctx": make_ctx()})
    assert result_text(message)["code"] == "internal"
    assert result_meta(message) == {}
    assert any(r.levelno >= logging.ERROR and "ledger" in r.getMessage() for r in caplog.records)


def test_driver_errors_never_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database error during the jti claim (e.g. a revoked grant) is
    `internal`: no driver text in the result, no handler run."""
    from psycopg import errors

    spy = Spy()
    install_handler(monkeypatch, spy)
    ledger = FakeLedger(
        fail={
            "seen_jti": errors.InsufficientPrivilege("permission denied for table tool_jti_claims")
        }
    )
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(client, {"query": "q", "limit": 5, "_ctx": make_ctx()})
    assert result_text(message)["code"] == "internal"
    assert "permission denied" not in json.dumps(message)
    assert "tool_jti_claims" not in json.dumps(message)
    assert spy.calls == []


def test_missing_dsn_is_warned_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING), dev_client():
        pass
    assert any("DATABASE_URL_TOOLSERVER" in r.getMessage() for r in caplog.records)


# --- arguments and secrets -------------------------------------------------------


def test_rejects_forbidden_and_unknown_args() -> None:
    with dev_client() as client:
        assert (
            result_text(
                call(client, {"query": "q", "limit": 5, "_ctx": make_ctx(), "tier": "confidential"})
            )["code"]
            == "invalid_arguments"
        )
        assert (
            result_text(call(client, {"query": "q", "limit": 5, "_ctx": make_ctx(), "bogus": 1}))[
                "code"
            ]
            == "invalid_arguments"
        )


def test_unset_ctx_secret_rejects_everything() -> None:
    with dev_client(ctx_secret=None) as client:
        assert (
            result_text(call(client, {"query": "q", "_ctx": make_ctx()}))["code"]
            == "invalid_context"
        )


def test_errors_do_not_leak_details() -> None:
    secret_value = "super-secret-query-fragment-zzz"
    cases = (
        ({"query": secret_value}, "invalid_context"),
        ({"query": secret_value, "_ctx": "garbage-token"}, "invalid_context"),
        ({"query": secret_value, "limit": 5, "_ctx": make_ctx(secret="wrong")}, "invalid_context"),
        ({"query": secret_value, "limit": 999, "_ctx": make_ctx()}, "invalid_arguments"),
    )
    with dev_client() as client:
        for arguments, code in cases:
            body = call(client, arguments)
            assert result_text(body)["code"] == code
            assert secret_value not in json.dumps(body)


def test_workspace_endpoint_has_no_product_tools() -> None:
    with dev_client() as client:
        names = [t["name"] for t in list_tools(client, "/workspace/mcp", WORKSPACE_TOKEN)]
    assert not set(names) & {name.value for name in ToolName}


@pytest.mark.parametrize(
    ("dsn", "flag", "allowed"),
    [
        ("postgresql://u:p@localhost:5432/db", None, False),
        ("postgresql://u:p@localhost:5432/db", "0", False),
        ("postgresql://u:p@localhost:5432/db", "1", True),
        ("postgresql://u:p@fx-db:5432/db", "1", True),
        ("postgresql://u:p@ep-x-pooler.us-east-2.aws.neon.tech/db?sslmode=require", "1", False),
        ("postgresql://u:p@NEON.TECH/db", "1", False),
        ("postgresql://u:p@neonatal-records.internal/db", "1", True),
        ("host=localhost dbname=db", "1", False),
        ("", "1", False),
    ],
)
def test_live_write_tests_need_opt_in_and_never_neon(
    dsn: str, flag: str | None, allowed: bool
) -> None:
    assert live_write_dsn(dsn, flag) == (dsn if allowed else "")


# --- live database (permanent rows) ------------------------------------------------


@NEEDS_DB
def test_ledger_row_matches_verified_ctx(real_model: Any) -> None:
    import psycopg

    jti = f"jti-dispatch-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert message["result"]["isError"] is False
    body = json.loads(message["result"]["content"][0]["text"])
    assert body["tool"] == TOOL and body["tier"] == "team"
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, request_id, step_index, execution_status, validation_outcome,"
            " requester_member_id FROM public.tool_execution_ledger WHERE jti = %s",
            (jti,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1:5] == ("req-dispatch", 0, "executed", "accepted")
    assert rows[0][5] == "m-sam"
    assert result_meta(message) == {"ledger_entry_id": str(rows[0][0])}


@NEEDS_DB
def test_duplicate_jti_replays_without_executing(real_model: Any) -> None:
    jti = f"jti-replay-{uuid.uuid4().hex}"
    with live_client() as client:
        first = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
        assert first["result"]["isError"] is False
        second = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
        assert result_text(second)["code"] == "replay"
    assert result_meta(second) == {"original_entry_id": result_meta(first)["ledger_entry_id"]}


@NEEDS_DB
def test_handler_failure_leaves_an_error_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    install_handler(monkeypatch, Spy(error=RuntimeError("handler exploded")))
    jti = f"jti-error-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert result_text(message)["code"] == "internal"
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, validation_outcome, execution_status, error_class"
            " FROM public.tool_execution_ledger WHERE jti = %s",
            (jti,),
        )
        rows = cur.fetchall()
    assert [row[1:] for row in rows] == [("accepted", "error", "RuntimeError")]
    assert result_meta(message) == {"ledger_entry_id": str(rows[0][0])}
