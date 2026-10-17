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
    # memory-mcp additionally lists the product tools (P2-06, P2-08).
    tools = rpc(client, path, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in tools["result"]["tools"]]
    assert tool in names
    if path == "/memory/mcp":
        assert sorted(names) == sorted(
            [
                tool,
                "knowledge_search_team",
                "knowledge_search_confidential",
                "knowledge_search_restricted",
                "knowledge_get_source_team",
                "knowledge_get_source_confidential",
                "knowledge_get_source_restricted",
                "read_source_team",
                "read_source_confidential",
                "read_source_restricted",
                "knowledge_save_team_note",
                "knowledge_confirm_maya_decision",
                "knowledge_mark_superseded",
            ]
        )
    else:
        # workspace-mcp lists the workspace product tools next to its diagnostic (P2-10).
        assert sorted(names) == sorted([tool, "send_external_message", "create_followup_task"])


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
        "params": {"name": "no_such_tool", "arguments": {}},
    }
    message = rpc(client, "/memory/mcp", MEMORY_TOKEN, call)
    # Unknown names never reach dispatch; the SDK reports the failure either
    # as a JSON-RPC error or as a tool result with isError=true.
    assert "error" in message or message["result"]["isError"] is True


def test_diagnostic_tools_can_be_disabled() -> None:
    with TestClient(create_app(make_settings(mcp_enable_diagnostic_tools=False))) as client:
        tools = rpc(
            client, "/memory/mcp", MEMORY_TOKEN, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
        )
        # Product tools are always registered; only diagnostics are gated.
        assert [t["name"] for t in tools["result"]["tools"]] == [
            "knowledge_search_team",
            "knowledge_search_confidential",
            "knowledge_search_restricted",
            "knowledge_get_source_team",
            "knowledge_get_source_confidential",
            "knowledge_get_source_restricted",
            "read_source_team",
            "read_source_confidential",
            "read_source_restricted",
            "knowledge_save_team_note",
            "knowledge_confirm_maya_decision",
            "knowledge_mark_superseded",
        ]


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


def test_product_tool_end_to_end_over_sse(real_model: object) -> None:
    """initialize → list → call with a signed _ctx (P2-06 §9-step-5).

    Needs a LOCAL DATABASE_URL_TOOLSERVER with seeded data and
    PROXYLEAD_DB_WRITE_TESTS=1 (the call writes permanent rows); skips otherwise.
    """
    import json as _json

    from .test_tool_dispatch import LIVE_DSN, TOOL, call, live_client, make_ctx

    if not LIVE_DSN:
        pytest.skip("needs DATABASE_URL_TOOLSERVER and PROXYLEAD_DB_WRITE_TESTS=1")
    with live_client() as client:
        initialized = rpc(client, "/memory/mcp", MEMORY_TOKEN, INITIALIZE)
        assert initialized["result"]["serverInfo"]["name"] == "memory-mcp"
        listed = rpc(
            client, "/memory/mcp", MEMORY_TOKEN, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}
        )
        assert TOOL in [t["name"] for t in listed["result"]["tools"]]
        message = call(client, {"query": "quokka", "limit": 3, "_ctx": make_ctx()})
    assert message["result"]["isError"] is False
    body = _json.loads(message["result"]["content"][0]["text"])
    assert body["tool"] == TOOL and body["tier"] == "team" and body["hits"]
    assert message["result"]["_meta"]["ledger_entry_id"].isdigit()
