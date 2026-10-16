"""Offline probe tests (no credentials, no network — never E1 evidence).

Covers the probe harness only: settings refusal, redaction, record
completeness, and presentation of real gateway result shapes. Result
classification itself lives in the gateway (tested in
test_armoriq_gateway.py) and is not repeated here. Every ArmorIQ client and
ledger here is a fake; a guard fixture fails any test that would build the
real SDK client.
"""

import asyncio
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
# The real coroutine, kept before any test replaces `probe.run_probe`.
REAL_RUN_PROBE = probe.run_probe

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
        ledger={"correlated": True, "lookup": "by jti", "row": {"id": 7}, "mismatches": []},
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
        "Ledger correlation: correlated: true",
        "Ledger mismatches: none",
        "PASS candidate (EXECUTED and correlated): yes",
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


RUN = probe.RunIdentifiers(
    request_id="r1",
    trace_id="t1",
    plan_id="p-1",
    requester_member_id="m-maya",
    jti="jti-abc",
    ledger_entry_id="7",
)


def _ledger_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "jti": "jti-abc",
        "request_id": "r1",
        "trace_id": "t1",
        "plan_id": "p-1",
        "step_index": 0,
        "tool_name": "knowledge_search_team",
        "requester_member_id": "m-maya",
        "validation_outcome": "accepted",
        "execution_status": "executed",
        "created_at": NOW,
        "arguments_sha256": "d" * 64,
        "prev_hash": "b" * 64,
        "entry_hash": "e" * 64,
    }
    row.update(overrides)
    return row


ATTEMPT_ROW = {
    "id": 11,
    "credential_id": "toolserver_bearer_memory",
    "mcp_endpoint": "memory-mcp",
    "tool_name": "knowledge_search_team",
    "outcome": "accepted",
    "jti": "jti-abc",
    "request_id": "r1",
    "workspace_id": "ws-hidden",
    "requester_member_id": "m-maya",
    "created_at": NOW,
}


class _RowLookup:
    """Fake toolserver ledger returning fixed rows (offline, no Neon)."""

    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.closed = False
        self.calls: list[tuple[str, object]] = []

    def by_jti(self, jti: str) -> dict[str, object] | None:
        self.calls.append(("by_jti", jti))
        return dict(self.row) if self.row is not None and self.row["jti"] == jti else None

    def by_request_id(self, request_id: str) -> list[dict[str, object]]:
        self.calls.append(("by_request_id", request_id))
        if self.row is not None and self.row["request_id"] == request_id:
            return [dict(self.row)]
        return []

    def attempts(self, jti: str | None, request_id: str) -> list[dict[str, object]]:
        self.calls.append(("attempts", (jti, request_id)))
        return [dict(ATTEMPT_ROW)]

    def close(self) -> None:
        self.closed = True


def test_correlate_ledger_without_dsn() -> None:
    report = probe.correlate_ledger(None, RUN)
    assert report["correlated"] is False
    assert report["lookup"] == "unavailable (no DATABASE_URL_TOOLSERVER)"


def test_correlate_ledger_reports_classes_not_secrets__offline_fake_ledger() -> None:
    def boom(dsn: str) -> object:
        raise ConnectionError(f"db down at {dsn}")

    report = probe.correlate_ledger(FAKE_DSN, RUN, lookup_factory=boom)
    assert report["correlated"] is False
    assert report["lookup"] == "lookup error (ConnectionError)"
    _assert_no_fake_values(str(report))


def test_correlate_ledger_matches_every_identifier__offline_fake_ledger() -> None:
    lookup = _RowLookup(_ledger_row())
    report = probe.correlate_ledger(FAKE_DSN, RUN, lookup_factory=lambda dsn: lookup)
    assert report["correlated"] is True
    assert report["mismatches"] == []
    assert lookup.closed
    row = report["row"]
    for key in (
        "id",
        "jti",
        "plan_id",
        "trace_id",
        "step_index",
        "requester_member_id",
        "validation_outcome",
        "execution_status",
    ):
        assert key in row
    assert row["created_at"] == NOW.isoformat()
    for hidden in ("arguments_sha256", "prev_hash", "entry_hash"):
        assert hidden not in row
    assert "d" * 64 not in str(report)


@pytest.mark.parametrize(
    ("overrides", "run_changes", "expected"),
    [
        ({"id": 8}, {}, "row.id=8 vs _meta.ledger_entry_id='7'"),
        ({"plan_id": "p-other"}, {}, "row.plan_id='p-other' vs handle.plan_id='p-1'"),
        ({"request_id": "r-other"}, {}, "row.request_id='r-other' vs ctx.request_id='r1'"),
        ({}, {"ledger_entry_id": None}, "row.id=7 vs _meta.ledger_entry_id=None"),
    ],
)
def test_correlate_ledger_reports_mismatches__offline_fake_ledger(
    overrides: dict[str, object], run_changes: dict[str, object], expected: str
) -> None:
    from dataclasses import replace

    run = replace(RUN, **run_changes)  # type: ignore[arg-type]
    lookup = _RowLookup(_ledger_row(**overrides))
    report = probe.correlate_ledger(FAKE_DSN, run, lookup_factory=lambda dsn: lookup)
    assert report["correlated"] is False
    assert expected in report["mismatches"]


def test_correlate_ledger_missing_row_is_not_correlated__offline_fake_ledger() -> None:
    report = probe.correlate_ledger(FAKE_DSN, RUN, lookup_factory=lambda dsn: _RowLookup(None))
    assert report["correlated"] is False
    assert report["row"] is None
    assert report["mismatches"]
    assert report["lookup"] == "no row for jti; by request_id: 0 row(s)"


def test_correlate_ledger_falls_back_to_request_id__offline_fake_ledger() -> None:
    """I4: no invocation/jti from the gateway → the ledger is still searched by request_id."""
    from dataclasses import replace

    lookup = _RowLookup(_ledger_row())
    run = replace(RUN, jti=None, ledger_entry_id=None)
    report = probe.correlate_ledger(FAKE_DSN, run, lookup_factory=lambda dsn: lookup)
    assert ("by_request_id", "r1") in lookup.calls
    assert report["lookup"] == "no jti returned; by request_id: 1 row(s)"
    assert report["rows_by_request_id"][0]["jti"] == "jti-abc"
    assert report["row"]["id"] == 7
    # A row exists for the run, but without the gateway's jti it never correlates.
    assert report["correlated"] is False
    assert "row.jti='jti-abc' vs jti=None" in report["mismatches"]


def test_correlate_ledger_reports_attempts_safely__offline_fake_ledger() -> None:
    lookup = _RowLookup(_ledger_row())
    report = probe.correlate_ledger(FAKE_DSN, RUN, lookup_factory=lambda dsn: lookup)
    assert ("attempts", ("jti-abc", "r1")) in lookup.calls
    (attempt,) = report["attempts"]
    assert attempt["credential_id"] == "toolserver_bearer_memory"
    assert attempt["outcome"] == "accepted"
    assert "workspace_id" not in attempt
    assert "requester_member_id" not in attempt


def test_pass_candidate_needs_executed_and_correlated() -> None:
    executed = {"invocation_status": "executed"}
    assert probe.pass_candidate(executed, {"correlated": True})
    assert not probe.pass_candidate(executed, {"correlated": False})
    assert not probe.pass_candidate({"invocation_status": "unknown"}, {"correlated": True})


EXECUTED_RESULT = {
    "content": [
        {
            "type": "text",
            "text": '{"hits": [{"snippet": "private snippet text"}], "abstained": false}',
        }
    ],
    "isError": False,
    "_meta": {"ledger_entry_id": "7"},
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
    """Fake ArmorIQ SDK client: returns `result` or raises `raises` on invoke."""

    def __init__(
        self, calls: list, result: object = None, raises: BaseException | None = None
    ) -> None:
        self.calls = calls
        self.result = EXECUTED_RESULT if result is None else result
        self.raises = raises

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
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(result=self.result, status="success", verified=True)


class _Signer:
    """Fake `_ctx` signer that remembers what it signed (jti, plan_id, ...)."""

    def __init__(self) -> None:
        self.fields: list[dict[str, object]] = []

    def __call__(self, fields: object, secret: str) -> str:
        assert secret == FAKE_CTX_SECRET
        signed = dict(fields)  # type: ignore[call-overload]
        self.fields.append(signed)
        return f"signed-{signed['jti']}"


class _CtxLedgerLookup:
    """Fake toolserver ledger: the row the tool server would write for the
    signed `_ctx` (identity from the envelope), plus `overrides`."""

    def __init__(self, signer: _Signer, **overrides: object) -> None:
        self.signer = signer
        self.overrides = overrides
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def _row(self) -> dict[str, object]:
        signed = self.signer.fields[-1]
        row = _ledger_row(
            jti=signed["jti"],
            request_id=signed["request_id"],
            trace_id=signed["trace_id"],
            plan_id=signed["plan_id"],
            step_index=signed["step_index"],
            tool_name=signed["tool"],
            requester_member_id=signed["requester_member_id"],
        )
        row.update(self.overrides)
        return row

    def by_jti(self, jti: str) -> dict[str, object] | None:
        self.calls.append(("by_jti", jti))
        if self.signer.fields and self.signer.fields[-1]["jti"] == jti:
            return self._row()
        return None

    def by_request_id(self, request_id: str) -> list[dict[str, object]]:
        self.calls.append(("by_request_id", request_id))
        if self.signer.fields and self.signer.fields[-1]["request_id"] == request_id:
            return [self._row()]
        return []

    def attempts(self, jti: str | None, request_id: str) -> list[dict[str, object]]:
        self.calls.append(("attempts", (jti, request_id)))
        return [dict(ATTEMPT_ROW, jti=jti, request_id=request_id)]

    def close(self) -> None:
        self.closed = True


def _probe_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        armoriq_api_key=FAKE_API_KEY,
        persona_maya_email=FAKE_EMAIL,
        ctx_secret=FAKE_CTX_SECRET,
    )


def _run_probe_offline(
    *,
    result: object = None,
    raises: BaseException | None = None,
    ledger_overrides: dict[str, object] | None = None,
    ledger_dsn: str | None = FAKE_DSN,
) -> tuple[int, str, _CtxLedgerLookup, list]:
    """`probe.run_probe` over the real gateway with a fake SDK and fake ledger."""
    from services.authorization.armoriq_gateway import ArmorIQGateway

    settings = _probe_settings()
    calls: list = []
    signer = _Signer()
    client = _FakeClient(calls, result=result, raises=raises)
    gateway = ArmorIQGateway(settings, client_factory=lambda member_id: client, ctx_signer=signer)
    lookup = _CtxLedgerLookup(signer, **(ledger_overrides or {}))
    code, record = asyncio.run(
        REAL_RUN_PROBE(
            settings,
            ledger_dsn=ledger_dsn,
            gateway=gateway,
            lookup_factory=lambda dsn: lookup,
        )
    )
    return code, record, lookup, calls


def test_run_probe_executed_and_correlated__offline_fake_sdk_and_ledger() -> None:
    _, record, lookup, _ = _run_probe_offline()
    assert "Ledger correlation: correlated: true (lookup: by jti)" in record
    assert "Ledger mismatches: none" in record
    assert "PASS candidate (EXECUTED and correlated): yes" in record
    assert lookup.calls and lookup.closed
    assert "arguments_sha256" not in record
    assert "d" * 64 not in record
    assert "private snippet text" not in record
    _assert_no_fake_values(record)


def _main_with_offline_probe(
    monkeypatch: pytest.MonkeyPatch, **run_kwargs: object
) -> tuple[int, dict[str, object]]:
    """Run `probe.main([])` with fake settings and the offline `run_probe`."""
    for name, value in (
        ("ARMORIQ_API_KEY", FAKE_API_KEY),
        ("PERSONA_MAYA_EMAIL", FAKE_EMAIL),
        ("CTX_SECRET", FAKE_CTX_SECRET),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(probe, "load_settings", _probe_settings, raising=False)
    monkeypatch.setattr(probe, "load_ledger_dsn", lambda: FAKE_DSN, raising=False)
    seen: dict[str, object] = {}

    async def offline_run_probe(*args: object, **kwargs: object) -> tuple[int, str]:
        code, record, lookup, calls = await asyncio.to_thread(_run_probe_offline, **run_kwargs)
        seen.update(record=record, lookup=lookup, calls=calls)
        return code, record

    monkeypatch.setattr(probe, "run_probe", offline_run_probe)
    return probe.main([]), seen


def test_main_shows_gateway_log_lines_on_stderr__offline_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I4: the gateway's INFO line with jti and ledger reference reaches stderr."""
    import logging

    root_handlers = list(logging.getLogger().handlers)
    gateway_logger = logging.getLogger(probe.GATEWAY_LOGGER)
    gateway_handlers = list(gateway_logger.handlers)
    _, seen = _main_with_offline_probe(monkeypatch)
    err = capsys.readouterr().err
    assert "[gateway INFO] gateway step ok request_id=e1-" in err
    assert "ledger=7" in err
    jti = seen["lookup"].calls[0][1]  # type: ignore[attr-defined]
    assert f"jti={jti}" in err
    _assert_no_fake_values(err)
    # Handler scoped to the gateway logger and removed afterwards.
    assert logging.getLogger().handlers == root_handlers
    assert gateway_logger.handlers == gateway_handlers
    for name in ("armoriq_sdk", "httpx"):
        assert not logging.getLogger(name).handlers


def test_main_keeps_jti_when_invocation_fails__offline_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """I4: a proxy error leaves no invocation record; stderr still carries jti
    and the exception class, and the ledger is searched by request_id."""
    from armoriq_sdk import exceptions as sdk_exceptions

    _, seen = _main_with_offline_probe(
        monkeypatch,
        raises=sdk_exceptions.MCPInvocationException(
            "proxy returned 502", mcp="memory-mcp", action="knowledge_search_team"
        ),
    )
    captured = capsys.readouterr()
    assert "[gateway WARNING] gateway invoke sdk error request_id=e1-" in captured.err
    assert "jti=" in captured.err
    assert "err=MCPInvocationException" in captured.err
    lookup_calls = seen["lookup"].calls  # type: ignore[attr-defined]
    assert lookup_calls[0][0] == "by_request_id"
    assert "no jti returned; by request_id: 1 row(s)" in captured.out
    _assert_no_fake_values(captured.out, captured.err)


def test_build_maya_request_ids_are_unique_per_run() -> None:
    settings = _probe_settings()
    first, _ = probe.build_maya_request(settings)
    second, _ = probe.build_maya_request(settings)
    assert first.request_id != second.request_id
    assert first.request_id.startswith("e1-")


def test_run_probe_ledger_mismatch_is_not_pass__offline_fake_sdk_and_ledger() -> None:
    _, record, _, _ = _run_probe_offline(ledger_overrides={"plan_id": "p-someone-else"})
    assert "Ledger correlation: correlated: false" in record
    assert "row.plan_id='p-someone-else'" in record
    assert "PASS candidate (EXECUTED and correlated): no" in record


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
