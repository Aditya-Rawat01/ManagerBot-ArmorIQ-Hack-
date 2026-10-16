"""Offline E2 probe tests (no credentials, no network — never E2 evidence).

Covers the harness only: env refusal, evidence-row completeness, redaction,
and honest layer/outcome presentation from real gateway and dispatch result
shapes. Enforcement classification itself lives in the gateway, the
tool server, and their tests — it is used here, never reimplemented.
"""

import asyncio
from types import SimpleNamespace

import pytest

from scripts import probe_v2_e2_offplan as probe
from services.contracts import ToolName


def test_missing_env_refuses_with_names_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in probe.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CTX_SECRET", "sekrit-value-that-must-not-leak")
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert "ARMORIQ_API_KEY" in captured.err
    assert "PERSONA_MAYA_EMAIL" in captured.err
    assert "sekrit-value-that-must-not-leak" not in captured.err
    assert "sekrit-value-that-must-not-leak" not in captured.out


def test_rejects_arguments() -> None:
    assert probe.main(["--live"]) == 2


def test_proxy_shape_is_always_not_tested() -> None:
    (only,) = probe.group_proxy_shape()
    assert only["observed"] == "not tested"
    assert only["layer"] == "unknown"
    assert "no verified" in only["evidence"]


def test_direct_layer_needs_server_code() -> None:
    assert probe.direct_layer(200, "invalid_context") == "tool_server_rule"
    assert probe.direct_layer(200, "replay") == "tool_server_rule"
    assert probe.direct_layer(401, None) == "unknown"
    assert probe.direct_layer(503, None) == "unknown"
    assert probe.direct_layer(None, None) == "unknown"
    assert probe.direct_layer(200, "mystery") == "unknown"


def test_parse_direct_result_shapes() -> None:
    import json

    body = (
        200,
        {
            "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": json.dumps({"code": "replay", "message": "x"})}
                ],
            }
        },
    )
    assert probe.parse_direct_result(body) == (200, "replay")
    assert probe.parse_direct_result((401, {"error": {"code": -32000}})) == (401, None)
    assert probe.parse_direct_result((200, {"result": {"isError": False}})) == (200, None)
    assert probe.parse_direct_result((None, None)) == (None, None)
    assert probe.parse_direct_result((200, "not-a-dict")) == (200, None)


def test_row_has_all_evidence_fields() -> None:
    item = probe.row("g", "a", "e", "o", "l", "ev")
    assert set(item) == {"group", "attempt", "expected", "observed", "layer", "evidence"}
    table = probe.render_table([item])
    assert "| attempt | expected | observed | enforcing layer | evidence reference |" in table
    assert "| a | e | o | l | ev |" in table


class _FakeScope:
    def __init__(self, client: "_FakeClient") -> None:
        self.client = client

    def start_session(self, opts: object = None) -> object:
        return _FakeSession()

    def resolve(self) -> dict:
        return {}


class _FakeSession:
    def start_plan(self, tool_calls: object, goal: object = None) -> object:
        from datetime import UTC, datetime, timedelta

        return SimpleNamespace(
            plan_id="ap-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=600),
            is_expired=False,
        )

    def close(self) -> None:
        pass


class _FakeClient:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def for_user(self, email: str) -> _FakeScope:
        return _FakeScope(self)

    def invoke_with_policy(self, *args: object, **kwargs: object) -> object:
        self.calls.append(("policy", args, kwargs))
        return SimpleNamespace(result={"ok": True})

    def invoke(self, *args: object, **kwargs: object) -> object:
        from armoriq_sdk import exceptions as sdk_exceptions

        self.calls.append(("invoke", args, kwargs))
        raise sdk_exceptions.IntentMismatchException("not in plan")


def _gateway() -> tuple[object, list]:
    from apps.api.core.config import Settings
    from services.authorization.armoriq_gateway import ArmorIQGateway

    settings = Settings(
        _env_file=None,
        app_env="test",
        armoriq_api_key="ak_test",
        persona_maya_email="maya@example.com",
        ctx_secret="test-ctx-secret",
    )
    calls: list = []
    gateway = ArmorIQGateway(
        settings,
        client_factory=lambda member_id: _FakeClient(calls),
        ctx_signer=lambda fields, secret: f"signed-{fields['jti']}",
    )
    return gateway, calls


def _maya_ctx_plan() -> tuple[object, object]:
    from apps.api.identity import mask_email
    from services.contracts import (
        AppRole,
        AuthenticatedUser,
        EntryPoint,
        PersonaKey,
    )

    user = AuthenticatedUser(
        member_id="m-maya",
        persona=PersonaKey.MAYA,
        display_name="Maya",
        app_role=AppRole.MANAGER,
        masked_email=mask_email("maya@example.com"),
    )
    from datetime import UTC, datetime

    from services.contracts import (
        DEFAULT_WORKSPACE_ID,
        McpEndpoint,
        PlannedToolCall,
        PlanProposal,
        RequestContext,
    )

    ctx = RequestContext(
        request_id="r-e2",
        trace_id="t-e2",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester=user,
        entry_point=EntryPoint.WEB,
        received_at=datetime.now(UTC),
    )
    plan = PlanProposal(
        summary="E2 probe plan",
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={"query": "quokka", "limit": 3},
                purpose="E2 probe step",
            )
        ],
    )
    return ctx, plan


def test_off_plan_rows_use_gateway_verdicts() -> None:
    async def run() -> list[dict[str, str]]:
        gateway, calls = _gateway()
        ctx, plan = _maya_ctx_plan()
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        rows = await probe.group_off_plan(gateway, handle, ctx)
        rows.extend(await probe.group_tamper(gateway, handle, ctx))
        return rows, calls

    rows, calls = asyncio.run(run())
    assert len(rows) == 5  # 2 off-plan + 3 tamper
    for item in rows:
        assert item["layer"] == "armoriq_sdk_plan_check"
        assert "block" in item["observed"]
    # Only the SDK plan-check path ran; the policy/tool-server path never did.
    assert [name for name, *_ in calls] == ["invoke"] * 5


def test_tamper_values_never_printed() -> None:
    async def run() -> list[dict[str, str]]:
        gateway, _ = _gateway()
        ctx, plan = _maya_ctx_plan()
        handle = await gateway.start_plan(ctx, plan)
        return await probe.group_tamper(gateway, handle, ctx)

    rows = asyncio.run(run())
    table = probe.render_table(rows)
    # Smuggled keys and values stay out of the report; only honest labels remain.
    for leaked in ("_approvals", "_delegation_id", "_policy_context", "restricted"):
        assert leaked not in table
    assert "forged underscore fields" in table
