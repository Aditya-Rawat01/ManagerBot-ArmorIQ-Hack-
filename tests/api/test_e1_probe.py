"""Offline probe tests (no credentials, no network — never E1 evidence).

Covers the probe harness only: env refusal, redaction, record completeness,
and presentation of real gateway result shapes. Result classification itself
lives in the gateway (tested in test_armoriq_gateway.py) and is not repeated
here.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts import probe_v2_e1_allowed as probe
from services.authorization.armoriq_gateway import StepOutcome
from services.contracts import (
    ArmorIQDecision,
    DecisionOutcome,
    DecisionSource,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def _decision(outcome: DecisionOutcome, source: DecisionSource) -> ArmorIQDecision:
    return ArmorIQDecision(outcome=outcome, source=source, reason="test reason", decided_at=NOW)


def _invocation(status: ToolExecutionStatus, **extra: object) -> ToolInvocationResult:
    reached = extra.pop("tool_server_reached", True)
    fields: dict[str, object] = {
        "request_id": "r1",
        "plan_id": "p-1",
        "step_index": 0,
        "tool": ToolName.KNOWLEDGE_SEARCH_TEAM,
        "jti": "jti-abc",
        "status": status,
        "tool_server_reached": reached,
    }
    fields.update(extra)
    return ToolInvocationResult(**fields)  # type: ignore[arg-type]


def _outcome(
    decision: ArmorIQDecision, invocation: ToolInvocationResult | None = None
) -> StepOutcome:
    return StepOutcome(decision=decision, invocation=invocation)


def test_missing_env_refuses_with_names_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in probe.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARMORIQ_API_KEY", "ak_test_value_that_must_not_leak")
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert "PERSONA_MAYA_EMAIL" in captured.err
    assert "CTX_SECRET" in captured.err
    assert "ak_test_value_that_must_not_leak" not in captured.err
    assert "ak_test_value_that_must_not_leak" not in captured.out


def test_rejects_arguments() -> None:
    assert probe.main(["--live"]) == 2


def test_executed_outcome_keeps_ledger_reference() -> None:
    described = probe.describe_outcome(
        _outcome(
            _decision(DecisionOutcome.ALLOW, DecisionSource.ARMORIQ_POLICY_DECISION),
            _invocation(ToolExecutionStatus.EXECUTED, ledger_entry_id="7"),
        )
    )
    assert described["invocation_status"] == "executed"
    assert described["ledger_entry_id"] == "7"
    assert described["decision_source"] == "armoriq_policy_decision"
    assert "ledger" in described["assessment"]


def test_missing_ledger_reference_stays_unknown() -> None:
    described = probe.describe_outcome(
        _outcome(
            _decision(DecisionOutcome.ALLOW, DecisionSource.ARMORIQ_POLICY_DECISION),
            _invocation(ToolExecutionStatus.UNKNOWN),
        )
    )
    assert described["invocation_status"] == "unknown"
    assert described["ledger_entry_id"] is None
    assert "never EXECUTED" in described["assessment"]


def test_rejection_reports_tool_server_layer() -> None:
    described = probe.describe_outcome(
        _outcome(
            _decision(DecisionOutcome.BLOCK, DecisionSource.TOOL_SERVER_RULE),
            _invocation(
                ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
                ledger_entry_id="9",
                error_code="invalid_context",
            ),
        )
    )
    assert "invalid_context" in described["assessment"]
    assert "tool server" in described["assessment"]
    assert described["decision_source"] == "tool_server_rule"


def test_replay_keeps_original_entry_reference() -> None:
    described = probe.describe_outcome(
        _outcome(
            _decision(DecisionOutcome.BLOCK, DecisionSource.TOOL_SERVER_RULE),
            _invocation(
                ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
                ledger_entry_id="3",
                error_code="replay",
            ),
        )
    )
    assert described["ledger_entry_id"] == "3"
    assert described["error_code"] == "replay"


def test_no_invocation_record_is_indeterminate() -> None:
    described = probe.describe_outcome(
        _outcome(_decision(DecisionOutcome.UNVERIFIED, DecisionSource.UNKNOWN))
    )
    assert described["tool_server_reached"] is None
    assert "unknown" in described["assessment"]


def test_summarize_result_redacts_content() -> None:
    payload = {
        "tool": "knowledge_search_team",
        "hits": [{"source_id": "s1", "snippet": "super secret snippet", "score": 0.9}],
        "secret_note": "must not appear",
    }
    shape = probe.summarize_result(payload)
    text = str(shape)
    assert "super secret snippet" not in text
    assert "must not appear" not in text
    assert "hits" in shape
    assert shape["hits"]["length"] == 1


def test_build_record_is_complete_and_redacted() -> None:
    described = {
        "decision_outcome": "allow",
        "decision_source": "armoriq_policy_decision",
        "decision_reason": "test",
        "tool_server_reached": True,
        "invocation_status": "executed",
        "ledger_entry_id": "7",
        "error_code": None,
        "jti": "jti-abc",
        "assessment": "test assessment",
    }
    record = probe.build_record(
        run_at="2026-10-16T12:00:00+00:00",
        sdk_version="0.6.10",
        agent_id="proxylead",
        endpoint_url="https://example.invalid",
        request_id="r1",
        masked_email="m***@e***.com",
        plan_id="p-1",
        armoriq_plan_id="ap-1",
        query=probe.E1_QUERY,
        described=described,
        result_shape={"hits": {"type": "list", "length": 1}},
        ledger={"ledger": {"id": 7}},
    )
    for field in (
        "Run at (UTC)",
        "SDK version",
        "Endpoint URL",
        "Plan / session IDs",
        "Decision:",
        "Tool server reached",
        "Invocation:",
        "Result shape",
        "Ledger correlation",
        "Credential path",
        "`_ctx` arrival",
        "Assessment",
        "Outcome:",
        "Evidence:",
        "Fallback",
    ):
        assert field in record
    assert "jti-abc" in record
    assert "m***@e***.com" in record


def test_correlate_ledger_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL_TOOLSERVER", raising=False)
    assert probe.correlate_ledger("jti-abc") == {
        "ledger": "unavailable (no DATABASE_URL_TOOLSERVER)"
    }


def test_correlate_ledger_reports_classes_not_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomLedger:
        def __init__(self, dsn: str) -> None:
            self.dsn = dsn

        def get_by_jti(self, jti: str) -> object:
            raise ConnectionError("db down")

        def close(self) -> None:
            pass

    monkeypatch.setattr("services.toolserver.ledger.Ledger", BoomLedger, raising=False)
    monkeypatch.setenv("DATABASE_URL_TOOLSERVER", "postgresql://secret-dsn")
    try:
        result = probe.correlate_ledger("jti-abc")
    finally:
        monkeypatch.undo()
    assert result == {"ledger": "lookup error (ConnectionError)"}
    assert "secret-dsn" not in str(result)


def test_correlate_ledger_returns_safe_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "id": 7,
        "request_id": "r1",
        "tool_name": "knowledge_search_team",
        "execution_status": "executed",
        "created_at": "2026-10-16",
        "arguments": {"query": "private"},
        "entry_hash": "aa",
        "prev_hash": "bb",
    }

    class RowLedger:
        def __init__(self, dsn: str) -> None:
            pass

        def get_by_jti(self, jti: str) -> dict:
            assert jti == "jti-abc"
            return dict(row)

        def close(self) -> None:
            pass

    monkeypatch.setattr("services.toolserver.ledger.Ledger", RowLedger, raising=False)
    monkeypatch.setenv("DATABASE_URL_TOOLSERVER", "postgresql://x")
    try:
        result = probe.correlate_ledger("jti-abc")
    finally:
        monkeypatch.undo()
    assert result == {
        "ledger": {
            "id": 7,
            "request_id": "r1",
            "tool_name": "knowledge_search_team",
            "execution_status": "executed",
            "created_at": "2026-10-16",
        }
    }


class _FakeSession:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def start_plan(self, tool_calls: object, goal: object = None) -> object:
        self.calls.append(("start_plan", tool_calls))
        return SimpleNamespace(
            plan_id="ap-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=600),
            is_expired=False,
        )

    def close(self) -> None:
        pass


class _FakeScope:
    def __init__(self, client: "_FakeClient", email: str) -> None:
        self.client = client
        self.email = email

    def start_session(self, opts: object = None) -> _FakeSession:
        return _FakeSession(self.client.calls)


class _FakeClient:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def for_user(self, email: str) -> _FakeScope:
        self.calls.append(("for_user", email))
        return _FakeScope(self, email)

    def invoke_with_policy(
        self,
        mcp: str,
        action: str,
        token: object,
        params: object = None,
        options: object = None,
    ) -> object:
        self.calls.append(("invoke", mcp, action, dict(params or {})))
        return SimpleNamespace(result={"_meta": {"ledger_entry_id": "7"}, "tool": action})


def test_probe_drives_real_gateway_path() -> None:
    import asyncio

    from apps.api.core.config import Settings
    from apps.api.identity import mask_email
    from services.authorization.armoriq_gateway import ArmorIQGateway
    from services.contracts import (
        AppRole,
        AuthenticatedUser,
        EntryPoint,
        PersonaKey,
    )

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
    user = AuthenticatedUser(
        member_id="m-maya",
        persona=PersonaKey.MAYA,
        display_name="Maya",
        app_role=AppRole.MANAGER,
        masked_email=mask_email("maya@example.com"),
    )
    from services.contracts import (
        DEFAULT_WORKSPACE_ID,
        McpEndpoint,
        PlannedToolCall,
        PlanProposal,
        RequestContext,
        ToolName,
    )

    ctx = RequestContext(
        request_id="r-e1",
        trace_id="t-e1",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester=user,
        entry_point=EntryPoint.WEB,
        received_at=NOW,
    )
    plan = PlanProposal(
        summary="E1 probe",
        steps=[
            PlannedToolCall(
                step_index=0,
                mcp=McpEndpoint.MEMORY,
                tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
                arguments={"query": probe.E1_QUERY, "limit": 5},
                purpose="E1 probe step",
            )
        ],
    )

    async def run() -> object:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        return await gateway.invoke_step(handle, 0, ctx, plan)

    outcome = asyncio.run(run())
    assert outcome.decision.outcome.value == "allow"
    assert outcome.invocation is not None
    assert outcome.invocation.status.value == "executed"
    assert outcome.invocation.ledger_entry_id == "7"
    invoke_params = next(args for name, *args in calls if name == "invoke")[2]
    assert "_ctx" in invoke_params
    assert invoke_params["query"] == probe.E1_QUERY
    described = probe.describe_outcome(outcome)
    assert described["jti"] == outcome.invocation.jti
    assert described["ledger_entry_id"] == "7"
