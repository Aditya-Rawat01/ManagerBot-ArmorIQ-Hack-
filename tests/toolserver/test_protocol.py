"""MCP protocol behavior of the scaffold, using ArmorIQ's documented request shape.

These are local tests against the MCP SDK transport. They do NOT show that
ArmorIQ's hosted proxy can reach or call this server; that is experiment E1.
"""

import json

import pytest
from fastapi.testclient import TestClient

from services.toolserver.main import create_app

from .conftest import (
    INITIALIZE,
    MCP_HEADERS,
    MEMORY_TOKEN,
    WORKSPACE_TOKEN,
    auth,
    make_settings,
    rpc,
)

ENDPOINTS = [
    ("/memory/mcp", MEMORY_TOKEN, "memory-mcp", "memory_scaffold_ping"),
    ("/workspace/mcp", WORKSPACE_TOKEN, "workspace-mcp", "workspace_scaffold_ping"),
]


@pytest.mark.parametrize(("path", "token", "server_name", "_tool"), ENDPOINTS)
def test_initialize_negotiates_documented_protocol_version(
    client: TestClient, path: str, token: str, server_name: str, _tool: str
) -> None:
    result = rpc(client, path, token, INITIALIZE)["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == server_name
    assert "tools" in result["capabilities"]


@pytest.mark.parametrize(("path", "token", "_server", "tool"), ENDPOINTS)
def test_tools_list_exposes_only_this_endpoints_diagnostic_tool(
    client: TestClient, path: str, token: str, _server: str, tool: str
) -> None:
    # Stateless transport: no session or prior initialize is required.
    tools = rpc(client, path, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in tools["result"]["tools"]] == [tool]


@pytest.mark.parametrize(("path", "token", "server_name", "tool"), ENDPOINTS)
def test_tools_call_returns_json_text_content(
    client: TestClient, path: str, token: str, server_name: str, tool: str
) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    result = rpc(client, path, token, call)["result"]
    assert result["isError"] is False
    # ArmorIQ's MCP format: content[] of {type: "text", text: "<JSON string>"}.
    content = result["content"][0]
    assert content["type"] == "text"
    assert json.loads(content["text"]) == {
        "ok": True,
        "endpoint": server_name,
        "server_version": "0.1.0",
    }


def test_unknown_tool_is_reported_as_error(client: TestClient) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "knowledge_search_team", "arguments": {}},
    }
    message = rpc(client, "/memory/mcp", MEMORY_TOKEN, call)
    # Product tools are not implemented yet; the SDK reports the failure either
    # as a JSON-RPC error or as a tool result with isError=true.
    assert "error" in message or message["result"]["isError"] is True


def test_diagnostic_tools_can_be_disabled() -> None:
    with TestClient(create_app(make_settings(mcp_enable_diagnostic_tools=False))) as client:
        tools = rpc(
            client, "/memory/mcp", MEMORY_TOKEN, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
        )
        assert tools["result"]["tools"] == []


def test_unlisted_host_header_is_rejected(client: TestClient) -> None:
    headers = {**auth(MEMORY_TOKEN), "Host": "attacker.example"}
    assert client.post("/memory/mcp", json=INITIALIZE, headers=headers).status_code == 421


def test_browser_origin_is_rejected(client: TestClient) -> None:
    headers = {**auth(MEMORY_TOKEN), "Origin": "https://evil.example"}
    assert client.post("/memory/mcp", json=INITIALIZE, headers=headers).status_code == 403


def test_json_only_accept_header_is_not_acceptable(client: TestClient) -> None:
    """Characterizes an E1 risk (ADR-006, U-2).

    The SDK transport requires clients to accept both JSON and SSE. If
    ArmorIQ's proxy sends `Accept: application/json` only, calls will fail
    with 406 and the documented fallback transport is needed.
    """
    headers = {
        **MCP_HEADERS,
        "Accept": "application/json",
        "Authorization": f"Bearer {MEMORY_TOKEN}",
    }
    assert client.post("/memory/mcp", json=INITIALIZE, headers=headers).status_code == 406
