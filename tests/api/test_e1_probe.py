"""Offline probe tests (no credentials, no network — never E1 evidence).

Covers the probe harness only: settings refusal, redaction, record
completeness, and presentation of real gateway result shapes. Result
classification itself lives in the gateway (tested in
test_armoriq_gateway.py) and is not repeated here. Every ArmorIQ client and
ledger here is a fake; a guard fixture fails any test that would build the
real SDK client.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.core.config import Settings
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

# Fake values: none of them may ever appear in probe output.
FAKE_API_KEY = "ak_fake_value_that_must_not_leak"
FAKE_EMAIL = "maya.fake.person@example.com"
FAKE_CTX_SECRET = "ctx-fake-secret-that-must-not-leak"
FAKE_DSN = "postgresql://fake_role:fake_password@fake-host.invalid:5432/fakedb"
FAKE_VALUES = (FAKE_API_KEY, FAKE_EMAIL, FAKE_CTX_SECRET, FAKE_DSN, "fake_password")


@pytest.fixture(autouse=True)
def _never_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline guard: building the real SDK client fails the test."""

    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline test tried to build the real ArmorIQ client")

    monkeypatch.setattr("services.authorization.armoriq_gateway.ArmorIQClient", _refuse)
    monkeypatch.delenv("DATABASE_URL_TOOLSERVER", raising=False)


def _use_env_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None = None
) -> Path:
    """Point `Settings` and the probe at temporary env files (never the real `.env`)."""
    env_file = tmp_path / ".env"
    if content is not None:
        env_file.write_text(content, encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))
    monkeypatch.setattr(probe, "TOOLSERVER_ENV_FILE", tmp_path / "toolserver.env", raising=False)
    return env_file


def _assert_no_fake_values(*texts: str) -> None:
    for text in texts:
        for value in FAKE_VALUES:
            assert value not in text


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_env_files(monkeypatch, tmp_path)
    monkeypatch.setenv("ARMORIQ_API_KEY", FAKE_API_KEY)
    called: list[object] = []
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: called.append(a))
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert "PERSONA_MAYA_EMAIL" in captured.err
    assert "CTX_SECRET" in captured.err
    assert "ARMORIQ_API_KEY" not in captured.err
    assert not called
    _assert_no_fake_values(captured.out, captured.err)


def test_main_reads_required_values_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """I1: values in `.env` (not exported) satisfy the probe, as `Settings` reads them."""
    _use_env_files(
        monkeypatch,
        tmp_path,
        f"ARMORIQ_API_KEY={FAKE_API_KEY}\n"
        f"PERSONA_MAYA_EMAIL={FAKE_EMAIL}\n"
        f"CTX_SECRET={FAKE_CTX_SECRET}\n"
        f"DATABASE_URL_TOOLSERVER={FAKE_DSN}\n",
    )
    for name in probe.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, object] = {}

    async def fake_run_probe(settings: object = None, **kwargs: object) -> tuple[int, str]:
        seen["settings"] = settings
        seen.update(kwargs)
        return 0, "fake record"

    monkeypatch.setattr(probe, "run_probe", fake_run_probe)
    assert probe.main([]) == 0
    settings = seen["settings"]
    assert settings.armoriq_api_key.get_secret_value() == FAKE_API_KEY  # type: ignore[attr-defined]
    assert settings.persona_maya_email.get_secret_value() == FAKE_EMAIL  # type: ignore[attr-defined]
    assert settings.ctx_secret.get_secret_value() == FAKE_CTX_SECRET  # type: ignore[attr-defined]
    assert seen["ledger_dsn"] == FAKE_DSN
    captured = capsys.readouterr()
    assert "missing" not in captured.err
    _assert_no_fake_values(captured.out, captured.err)


def test_ledger_dsn_from_toolserver_env_file_and_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_env_files(monkeypatch, tmp_path, "APP_ENV=test\n")
    assert probe.load_ledger_dsn() is None
    (tmp_path / "toolserver.env").write_text(f"DATABASE_URL_TOOLSERVER={FAKE_DSN}\n")
    assert probe.load_ledger_dsn() == FAKE_DSN
    monkeypatch.setenv("DATABASE_URL_TOOLSERVER", "postgresql://from-environment.invalid/db")
    assert probe.load_ledger_dsn() == "postgresql://from-environment.invalid/db"


def test_unreadable_settings_print_type_name_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_env_files(
        monkeypatch,
        tmp_path,
        f"ARMORIQ_API_KEY={FAKE_API_KEY}\nTOKEN_VALIDITY_S=not-a-number-{FAKE_CTX_SECRET}\n",
    )
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert "(ValidationError)" in captured.err
    assert "not-a-number" not in captured.err
    _assert_no_fake_values(captured.out, captured.err)


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
    assert probe.correlate_ledger("jti-abc", None) == {
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
    try:
        result = probe.correlate_ledger("jti-abc", "postgresql://secret-dsn")
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
    try:
        result = probe.correlate_ledger("jti-abc", "postgresql://x")
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
