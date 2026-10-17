"""Offline E2 direct-call harness tests (fake local secrets, no network).

Never E2 evidence (AGENT_HANDOFF_PROTOCOL §5): no ArmorIQ, no Render, no
Neon. They drive the E2 probe's direct-call group through the REAL tool
server app (`TestClient(create_app(make_settings(ctx_secret=...)))`), so the
probe must read what the deployed server really sends: SSE `data:` frames
(`json_response=False`) and the bearer guard's top-level JSON error.
"""

from fastapi.testclient import TestClient

from scripts import probe_v2_e2_offplan as probe
from services.toolserver.main import create_app

from .conftest import MEMORY_TOKEN, make_settings

# Local fake secret for the test app only; it must never appear in probe output.
FAKE_CTX_SECRET = "ctx-fake-secret-for-e2-direct-offline"


def _app_client() -> TestClient:
    return TestClient(create_app(make_settings(ctx_secret=FAKE_CTX_SECRET)))


def test_direct_group_reads_sse_rejections__offline_toolserver_app() -> None:
    """B2: the SSE `data:` frame's §4 code names the tool-server layer."""
    with _app_client() as client:
        rows = probe.group_direct(client, MEMORY_TOKEN)
    by_case = {item["case"]: item for item in rows}
    for case in ("direct-no-ctx", "direct-forged-ctx"):
        item = by_case[case]
        assert item["code"] == "invalid_context", item
        assert item["http_status"] == 200
        assert item["source"] == "sse data frame"
        assert item["layer"] == "tool_server_rule"
        assert "code=invalid_context" in item["observed"]


def test_direct_group_reads_the_bearer_guard_json__offline_toolserver_app() -> None:
    """B2: a 401 from the bearer guard carries `{"error": {"code": ...}}`."""
    with _app_client() as client:
        rows = probe.group_direct(client, MEMORY_TOKEN)
    item = next(row for row in rows if row["case"] == "direct-no-bearer")
    assert item["http_status"] == 401
    assert item["code"] == "unauthorized"
    assert item["source"] == "bearer guard json"
    assert item["layer"] == "tool_server_rule"


def test_direct_rows_never_print_secrets__offline_toolserver_app() -> None:
    with _app_client() as client:
        rows = probe.group_direct(client, MEMORY_TOKEN)
    text = probe.render_table(rows) + repr(rows)
    assert MEMORY_TOKEN not in text
    assert FAKE_CTX_SECRET not in text


def test_parse_mcp_response_shapes() -> None:
    sse = (
        "event: message\r\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"isError":true,"content":[{"type":"text",'
        '"text":"{\\"code\\": \\"replay\\", \\"message\\": \\"x\\"}"}],'
        '"_meta":{"original_entry_id":"7"}}}\r\n\r\n'
    )
    parsed = probe.parse_mcp_response(200, "text/event-stream", sse)
    assert parsed.code == "replay"
    assert parsed.original_entry_id == "7"
    assert parsed.meta_keys == ("original_entry_id",)
    guard = probe.parse_mcp_response(
        401, "application/json", '{"error":{"code":"unauthorized","message":"m"}}'
    )
    assert (guard.code, guard.source) == ("unauthorized", "bearer guard json")
    executed = probe.parse_mcp_response(
        200,
        "text/event-stream",
        'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"isError":false,'
        '"content":[],"_meta":{"ledger_entry_id":"12"}}}\n',
    )
    assert executed.code is None
    assert executed.is_error is False
    assert executed.ledger_entry_id == "12"
    rpc_error = probe.parse_mcp_response(
        200,
        "text/event-stream",
        'data: {"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"bad"}}\n',
    )
    assert rpc_error.jsonrpc_error == -32602
    assert rpc_error.code is None
    for hostile in ("", "data: not-json\n", "data: [1, 2]\n", "<html>421</html>"):
        assert probe.parse_mcp_response(421, "text/html", hostile).code is None
    assert probe.parse_mcp_response(200, "text/event-stream", "data: {}\n").code is None


def test_direct_layer_needs_a_server_code() -> None:
    def layer(status: int | None, content_type: str, text: str) -> str:
        return probe.direct_layer(probe.parse_mcp_response(status, content_type, text))

    rejection = (
        'data: {"result":{"isError":true,"content":[{"type":"text",'
        '"text":"{\\"code\\": \\"invalid_context\\"}"}]}}\n'
    )
    assert layer(200, "text/event-stream", rejection) == "tool_server_rule"
    assert layer(401, "application/json", '{"error":{"code":"unauthorized"}}') == (
        "tool_server_rule"
    )
    # Transport outcomes and unknown codes name no layer.
    assert layer(503, "text/plain", "upstream down") == "unknown"
    assert layer(None, "", "") == "unknown"
    mystery = 'data: {"result":{"isError":true,"content":[{"type":"text","text":"{}"}]}}\n'
    assert layer(200, "text/event-stream", mystery) == "unknown"
