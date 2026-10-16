"""Guarded dispatch tests (P2-06). Rejection tests run without a database
(dev path: verification with an in-memory replay guard); ledger tests need a
LOCAL DATABASE_URL_TOOLSERVER and skip otherwise. DSNs are captured at
collection time and never logged.
"""

import json
import os
import time
import uuid
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from services.authorization.ctx_envelope import CtxEnvelope, sign_ctx
from services.contracts import McpEndpoint, ToolName
from services.toolserver.main import create_app
from services.toolserver.servers import PRODUCT_TOOLS

from .conftest import MEMORY_TOKEN, WORKSPACE_TOKEN, auth, make_settings

CTX_SECRET = "test-ctx-secret-for-dispatch"

_RAW_DSN = os.environ.get("DATABASE_URL_TOOLSERVER", "")
_HOST = urlparse(_RAW_DSN).hostname or "" if _RAW_DSN else ""
LIVE_DSN = (
    _RAW_DSN
    if _RAW_DSN
    and (
        "localhost" in _HOST
        or "127.0.0.1" in _HOST
        or os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
    )
    and "neon" not in _HOST
    else ""
)

NEEDS_DB = pytest.mark.skipif(not LIVE_DSN, reason="needs a local DATABASE_URL_TOOLSERVER")

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
    values: dict[str, object] = {"ctx_secret": CTX_SECRET}
    values.update(overrides)
    app = create_app(make_settings(**values))
    return TestClient(app, raise_server_exceptions=False)


def live_client() -> TestClient:
    assert LIVE_DSN is not None
    app = create_app(make_settings(ctx_secret=CTX_SECRET, database_url_toolserver=LIVE_DSN))
    return TestClient(app, raise_server_exceptions=False)


def call(client: TestClient, arguments: dict, token: str = MEMORY_TOKEN) -> dict:
    response = client.post(
        "/memory/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": TOOL, "arguments": arguments},
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
    assert message["result"]["isError"] is True
    return json.loads(message["result"]["content"][0]["text"])


def test_all_product_tools_registered_via_guard() -> None:
    assert set(PRODUCT_TOOLS) == {ToolName.KNOWLEDGE_SEARCH_TEAM}
    with dev_client() as client:
        # No _ctx: rejected by the dispatcher before any handler runs.
        message = call(client, {"query": "quokka", "limit": 5})
        assert result_text(message)["code"] == "invalid_context"


def test_rejects_without_ctx_before_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.toolserver import servers

    called: list[str] = []

    async def spy(verified: object, args: object, runtime: object) -> object:
        called.append("handler")
        raise AssertionError("must not run")

    original = servers.PRODUCT_TOOLS[ToolName.KNOWLEDGE_SEARCH_TEAM]
    monkeypatch.setitem(
        servers.PRODUCT_TOOLS,
        ToolName.KNOWLEDGE_SEARCH_TEAM,
        type(original)(name=original.name, description=original.description, handler=spy),
    )
    with dev_client() as client:
        assert result_text(call(client, {"query": "x"}))["code"] == "invalid_context"
    assert called == []


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
    with dev_client() as client:
        for arguments in (
            {"query": secret_value},
            {"query": "q", "_ctx": "garbage-token"},
            {"query": "q", "limit": 5, "_ctx": make_ctx(secret="wrong-secret")},
        ):
            body = call(client, arguments)
            assert secret_value not in json.dumps(body)
            assert "wrong-secret" not in json.dumps(body)


def test_workspace_endpoint_has_no_product_tools() -> None:
    with dev_client() as client:
        response = client.post(
            "/workspace/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=auth(WORKSPACE_TOKEN),
        )
        assert response.status_code == 200
        for line in response.text.splitlines():
            if line.startswith("data:"):
                names = [t["name"] for t in json.loads(line[6:])["result"]["tools"]]
        assert "knowledge_search_team" not in names


@NEEDS_DB
def test_ledger_row_matches_verified_ctx() -> None:
    import psycopg

    jti = f"jti-dispatch-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
    assert message["result"]["isError"] is False
    body = json.loads(message["result"]["content"][0]["text"])
    assert body["tool"] == TOOL and body["tier"] == "team"
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT request_id, step_index, execution_status, validation_outcome,"
            " requester_member_id FROM public.tool_execution_ledger WHERE jti = %s",
            (jti,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][:4] == ("req-dispatch", 0, "executed", "accepted")
    assert rows[0][4] == "m-sam"


@NEEDS_DB
def test_duplicate_jti_replays_without_executing() -> None:
    jti = f"jti-replay-{uuid.uuid4().hex}"
    with live_client() as client:
        first = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
        assert first["result"]["isError"] is False
        second = call(client, {"query": "quokka", "limit": 5, "_ctx": make_ctx(jti=jti)})
        assert result_text(second)["code"] == "replay"
