"""Offline E2 direct-call harness tests (fake local secrets, no network).

Never E2 evidence (AGENT_HANDOFF_PROTOCOL §5): no ArmorIQ, no Render, no
Neon. They drive the E2 probe's direct-call group through the REAL tool
server app (`TestClient(create_app(make_settings(ctx_secret=...)))`), so the
probe must read what the deployed server really sends: SSE `data:` frames
(`json_response=False`) and the bearer guard's top-level JSON error.
"""

import time

import pytest
from fastapi.testclient import TestClient

from scripts import probe_v2_e2_offplan as probe
from services.toolserver.main import create_app

from .conftest import MEMORY_TOKEN, make_settings
from .test_tool_dispatch import CTX_SECRET, FakeLedger, Spy, fake_ledger_client, install_handler

# Local fake secret for the test app only; it must never appear in probe output.
FAKE_CTX_SECRET = "ctx-fake-secret-for-e2-direct-offline"


def _app_client() -> TestClient:
    return TestClient(create_app(make_settings(ctx_secret=FAKE_CTX_SECRET)))


def _group(client: TestClient, secret: str = FAKE_CTX_SECRET) -> list[dict]:
    return probe.group_direct(client, MEMORY_TOKEN, ctx_secret=secret, request_id="r-e2-offline")


def test_direct_group_reads_sse_rejections__offline_toolserver_app() -> None:
    """B2: the SSE `data:` frame's §4 code names the tool-server layer."""
    with _app_client() as client:
        rows = _group(client)
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
        rows = _group(client)
    item = next(row for row in rows if row["case"] == "direct-no-bearer")
    assert item["http_status"] == 401
    assert item["code"] == "unauthorized"
    assert item["source"] == "bearer guard json"
    assert item["layer"] == "tool_server_rule"


def test_direct_rows_never_print_secrets__offline_toolserver_app() -> None:
    with _app_client() as client:
        rows = _group(client)
    text = probe.render_table(rows) + repr(rows)
    assert MEMORY_TOKEN not in text
    assert FAKE_CTX_SECRET not in text


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("direct-wrong-secret", "invalid_context"),
        ("direct-cross-tool", "invalid_context"),
        ("direct-expired", "context_expired"),
    ],
)
def test_minted_ctx_cases_reach_their_checks__offline_toolserver_app(case: str, code: str) -> None:
    """I1: a well-formed `_ctx` signed with a wrong secret, a team `_ctx` used
    on confidential search, and an expired `_ctx` (lifetime <= 900 s) are
    each rejected by the check they target."""
    with _app_client() as client:
        rows = _group(client)
    item = next(row for row in rows if row["case"] == case)
    assert item["code"] == code, item
    assert item["layer"] == "tool_server_rule"
    assert item["source"] == "sse data frame"


def test_minted_ctx_shapes() -> None:
    from services.authorization.ctx_envelope import (
        MAX_CTX_LIFETIME_S,
        BadSignature,
        ExpiredContext,
        WrongTool,
        verify_ctx,
    )

    now = time.time()
    expired, _ = probe.mint_ctx(
        FAKE_CTX_SECRET,
        tool="knowledge_search_team",
        request_id="r",
        iat=now - probe.EXPIRED_CTX_AGE_S,
        exp=now - probe.EXPIRED_CTX_AGE_S + probe.EXPIRED_CTX_LIFETIME_S,
    )
    assert probe.EXPIRED_CTX_LIFETIME_S <= MAX_CTX_LIFETIME_S
    with pytest.raises(ExpiredContext):
        verify_ctx(
            expired,
            FAKE_CTX_SECRET,
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_team",
        )
    team, jti = probe.mint_ctx(
        FAKE_CTX_SECRET, tool="knowledge_search_team", request_id="r", iat=now, exp=now + 300
    )
    assert jti
    with pytest.raises(WrongTool):
        verify_ctx(
            team,
            FAKE_CTX_SECRET,
            expected_aud="memory-mcp",
            expected_tool="knowledge_search_confidential",
        )
    with pytest.raises(BadSignature):
        verify_ctx(
            team, "another-secret", expected_aud="memory-mcp", expected_tool="knowledge_search_team"
        )


def test_replay_of_a_used_ctx_names_the_original_entry__offline_toolserver_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: the same signed `_ctx` POSTed again within its lifetime is refused
    as `replay`, pointing at the first call's `_meta.ledger_entry_id`; only
    the first call executes."""
    ledger = FakeLedger()
    spy = Spy()
    install_handler(monkeypatch, spy)
    now = time.time()
    # Emulates the gateway-signed planned call that the proxy forwarded (E1 path).
    token, jti = probe.mint_ctx(
        CTX_SECRET, tool="knowledge_search_team", request_id="r-e2-base", iat=now, exp=now + 300
    )
    arguments = {"query": "quokka", "limit": 3}
    with fake_ledger_client(monkeypatch, ledger) as client:
        first = probe.post_tool_call(
            client, MEMORY_TOKEN, "knowledge_search_team", {**arguments, "_ctx": token}, "base"
        )
        assert first.is_error is False
        assert first.ledger_entry_id
        source = probe.ReplaySource(
            token=token,
            jti=jti,
            ledger_entry_id=first.ledger_entry_id,
            issued_at=now,
            expires_at=now + 300,
            arguments=arguments,
        )
        item = probe.direct_replay_row(client, MEMORY_TOKEN, source)
    assert item["code"] == "replay", item
    assert item["original_entry_id"] == first.ledger_entry_id
    assert item["layer"] == "tool_server_rule"
    assert "matches the first call's ledger entry" in item["observed"]
    assert len(spy.calls) == 1
    replays = [attempt for attempt in ledger.attempts if attempt.outcome == "replay"]
    assert [attempt.jti for attempt in replays] == [jti]
    assert token not in repr(item) + probe.render_table([item])
    assert CTX_SECRET not in repr(item)


def test_replay_is_not_sent_after_the_ctx_expired__offline() -> None:
    now = time.time()
    source = probe.ReplaySource(
        token="unused.token",
        jti="j",
        ledger_entry_id="1",
        issued_at=now - 400,
        expires_at=now - 100,
        arguments={"query": "q", "limit": 1},
    )

    class NoCalls:
        def post(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("an expired capture must not be replayed")

    item = probe.direct_replay_row(NoCalls(), MEMORY_TOKEN, source)
    assert item["layer"] == "unknown"
    assert "not sent" in item["observed"]


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
