"""Workspace-tool tests (P2-10). The honeypot never touches the network:
a static import scan plus a live call with `socket` patched to raise. Live
tests need a LOCAL DATABASE_URL_TOOLSERVER plus PROXYLEAD_DB_WRITE_TESTS=1
(sink rows are permanent) and skip otherwise. DSNs captured at collection
time, never logged.
"""

import ast
import json
import uuid
from pathlib import Path

import pytest

from services.contracts import ToolName
from services.toolserver.tools_workspace import PRODUCT_TOOLS

from .test_tool_dispatch import (
    NEEDS_DB,
    call,
    dev_client,
    live_client,
    make_ctx,
    result_text,
)

WORKSPACE_HONEYPOT = ToolName.SEND_EXTERNAL_MESSAGE.value
WORKSPACE_AUD = "workspace-mcp"


def workspace_ctx(tool: str = WORKSPACE_HONEYPOT, **overrides: object) -> str:
    values: dict[str, object] = {"tool": tool, "aud": WORKSPACE_AUD}
    values.update(overrides)
    return make_ctx(**values)


FORBIDDEN_NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "smtplib",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
    }
)


def honeypot_args(**overrides: object) -> dict:
    arguments: dict[str, object] = {
        "destination": "synthetic_sink",
        "message": "test dispatch ping",
    }
    arguments.update(overrides)
    return arguments


def test_honeypot_registered_on_workspace_only() -> None:
    assert set(PRODUCT_TOOLS) == {
        ToolName.SEND_EXTERNAL_MESSAGE,
        ToolName.CREATE_FOLLOWUP_TASK,
    }


def test_honeypot_module_imports_no_network_client() -> None:
    source = Path("services/toolserver/tools_workspace.py").read_text()
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".")[0])
    assert imports.isdisjoint(FORBIDDEN_NETWORK_MODULES), imports & FORBIDDEN_NETWORK_MODULES


@NEEDS_DB
def test_honeypot_requires_valid_ctx() -> None:
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    with live_client() as client:
        with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.egress_sink")
            before = cur.fetchone()[0]
        denied = call(
            client,
            {"destination": "synthetic_sink", "message": "x"},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
        assert result_text(denied)["code"] == "invalid_context"
        with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.egress_sink")
            assert cur.fetchone()[0] == before


@NEEDS_DB
def test_honeypot_writes_only_egress_sink() -> None:
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    jti = f"jti-honeypot-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {**honeypot_args(message="synthetic probe"), "_ctx": workspace_ctx(jti=jti)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
    assert message["result"]["isError"] is False
    body = json.loads(message["result"]["content"][0]["text"])
    assert body["delivered"] is False and body["sink"] == "synthetic_sink"
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT destination, message, tool_name FROM public.egress_sink WHERE request_id = %s",
            ("req-dispatch",),
        )
        rows = cur.fetchall()
    assert rows, "honeypot must write exactly its sink row"
    assert all(row[0] == "synthetic_sink" and row[2] == WORKSPACE_HONEYPOT for row in rows)
    assert any("synthetic probe" in row[1] for row in rows)


@NEEDS_DB
def test_task_idempotency() -> None:
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    key = f"key-{uuid.uuid4().hex}"
    arguments = {
        "title": "Follow up on freeze",
        "description": "Confirm the freeze lifted",
        "owner": "sam",
        "priority": "high",
        "idempotency_key": key,
    }
    with live_client() as client:
        first = call(
            client,
            {**arguments, "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=ToolName.CREATE_FOLLOWUP_TASK.value,
        )
        second = call(
            client,
            {**arguments, "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=ToolName.CREATE_FOLLOWUP_TASK.value,
        )
    assert first["result"]["isError"] is False
    first_id = json.loads(first["result"]["content"][0]["text"])["task_id"]
    assert json.loads(second["result"]["content"][0]["text"])["task_id"] == first_id
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.tasks WHERE idempotency_key = %s", (key,))
        assert cur.fetchone()[0] == 1


@NEEDS_DB
def test_task_requester_from_ctx() -> None:
    # Local DB fixture: member rows are seeded by the operator (migration role)
    # alongside the corpus; live tool tests assume them like they assume sources.
    import psycopg

    from .test_tool_dispatch import LIVE_DSN

    key = f"key-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {
                "title": "T",
                "owner": "priya",
                "priority": "low",
                "idempotency_key": key,
                "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value),
            },
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=ToolName.CREATE_FOLLOWUP_TASK.value,
        )
    assert message["result"]["isError"] is False
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_by_member_id, workspace_id FROM public.tasks"
            " WHERE idempotency_key = %s",
            (key,),
        )
        assert cur.fetchone() == ("m-sam", "workspace-primary")


def test_task_enum_bounds() -> None:
    with dev_client() as client:
        for arguments in (
            {"title": "T", "owner": "mallory", "priority": "low", "idempotency_key": "k1"},
            {"title": "T", "owner": "sam", "priority": "urgent", "idempotency_key": "k2"},
            {"title": "", "owner": "sam", "priority": "low", "idempotency_key": "k3"},
        ):
            message = call(
                client,
                {**arguments, "_ctx": workspace_ctx(ToolName.CREATE_FOLLOWUP_TASK.value)},
                token="test-workspace-token",
                path="/workspace/mcp",
                tool=ToolName.CREATE_FOLLOWUP_TASK.value,
            )
            assert result_text(message)["code"] == "invalid_arguments"


@NEEDS_DB
def test_honeypot_never_uses_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    real_socket = socket.socket
    allowed_hosts = {"127.0.0.1", "::1", "localhost"}

    class GuardSocket(real_socket):  # type: ignore[valid-type, misc]
        def connect(self, address: object, *args: object) -> object:
            host = address[0] if isinstance(address, tuple) else address
            if host not in allowed_hosts:
                raise AssertionError(f"network use attempted: {host}")
            return super().connect(address, *args)  # type: ignore[arg-type]

    # Localhost DB traffic allowed; any other destination raises.
    monkeypatch.setattr(socket, "socket", GuardSocket)
    jti = f"jti-honeypot-{uuid.uuid4().hex}"
    with live_client() as client:
        message = call(
            client,
            {**honeypot_args(), "_ctx": workspace_ctx(jti=jti)},
            token="test-workspace-token",
            path="/workspace/mcp",
            tool=WORKSPACE_HONEYPOT,
        )
    assert message["result"]["isError"] is False
