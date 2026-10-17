"""Offline E2 probe tests (no credentials, no network — never E2 evidence).

Covers the harness only: settings refusal, evidence-row completeness,
redaction, and honest layer/outcome presentation from real gateway and
dispatch result shapes. Enforcement classification itself lives in the
gateway, the tool server, and their tests — it is used here, never
reimplemented. Every ArmorIQ client, tool server, and ledger here is a fake;
guard fixtures fail any test that would build the real SDK client, a real
HTTP client to the tool server, or a real Neon pool, and point `Settings`
at temporary env files so a developer `.env` can never make a test go live.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.api.core.config import Settings
from scripts import probe_v2_e2_offplan as probe
from services.contracts import ToolName

# Fake values: none of them may ever appear in probe output.
FAKE_API_KEY = "ak_fake_value_that_must_not_leak"
FAKE_EMAIL = "maya.fake.person@example.com"
FAKE_CTX_SECRET = "ctx-fake-secret-that-must-not-leak"
FAKE_BEARER = "bearer-fake-memory-token-that-must-not-leak"
FAKE_DSN = "postgresql://fake_role:fake_password@fake-host.invalid:5432/fakedb"
FAKE_VALUES = (
    FAKE_API_KEY,
    FAKE_EMAIL,
    FAKE_CTX_SECRET,
    FAKE_BEARER,
    FAKE_DSN,
    "fake_password",
)
# Every setting the probe reads: a developer shell never leaks into a test.
PROBE_ENV = (
    "ARMORIQ_API_KEY",
    "PERSONA_MAYA_EMAIL",
    "CTX_SECRET",
    "TOOLSERVER_BEARER_MEMORY",
    "DATABASE_URL_TOOLSERVER",
    "TOOLSERVER_BASE_URL",
    "ARMORIQ_AGENT_ID",
)


def _use_env_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None = None
) -> Path:
    """Point `Settings` and the probe at temporary env files (never the real `.env`)."""
    env_file = tmp_path / ".env"
    if content is not None:
        env_file.write_text(content, encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", (env_file,))
    monkeypatch.setattr(probe.e1, "TOOLSERVER_ENV_FILE", tmp_path / "toolserver.env")
    return env_file


@pytest.fixture(autouse=True)
def _never_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Offline guard: building the real SDK client, a real HTTP client to the
    tool server, or a real Neon pool fails the test; env files are temporary."""

    def _refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline test tried to go live")

    monkeypatch.setattr("services.authorization.armoriq_gateway.ArmorIQClient", _refuse)
    monkeypatch.setattr(probe, "build_direct_client", _refuse, raising=False)
    monkeypatch.setattr(probe, "E2EvidenceLookup", _refuse, raising=False)
    for name in PROBE_ENV:
        monkeypatch.delenv(name, raising=False)
    _use_env_files(monkeypatch, tmp_path / "isolated")


def _assert_no_fake_values(*texts: str) -> None:
    for text in texts:
        for value in FAKE_VALUES:
            assert value not in text


def test_no_settings_exit_2_naming_only_missing_variables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    for name in (
        "ARMORIQ_API_KEY",
        "PERSONA_MAYA_EMAIL",
        "CTX_SECRET",
        "TOOLSERVER_BEARER_MEMORY",
        "DATABASE_URL_TOOLSERVER",
    ):
        assert name in captured.err
    # The base URL has a documented default, so it is never "missing".
    assert "TOOLSERVER_BASE_URL" not in captured.err


def test_missing_settings_refuse_with_names_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_env_files(monkeypatch, tmp_path)
    monkeypatch.setenv("ARMORIQ_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("CTX_SECRET", FAKE_CTX_SECRET)
    called: list[object] = []
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: called.append(a))
    assert probe.main([]) == 2
    captured = capsys.readouterr()
    assert "PERSONA_MAYA_EMAIL" in captured.err
    assert "TOOLSERVER_BEARER_MEMORY" in captured.err
    assert "DATABASE_URL_TOOLSERVER" in captured.err
    assert "ARMORIQ_API_KEY" not in captured.err
    assert "CTX_SECRET" not in captured.err
    assert not called
    _assert_no_fake_values(captured.out, captured.err)


def _write_all_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: str = "") -> None:
    _use_env_files(
        monkeypatch,
        tmp_path,
        f"ARMORIQ_API_KEY={FAKE_API_KEY}\n"
        f"PERSONA_MAYA_EMAIL={FAKE_EMAIL}\n"
        f"CTX_SECRET={FAKE_CTX_SECRET}\n"
        f"TOOLSERVER_BEARER_MEMORY={FAKE_BEARER}\n"
        f"DATABASE_URL_TOOLSERVER={FAKE_DSN}\n" + extra,
    )


def test_main_reads_required_values_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """I5: values in `.env` (not exported) satisfy the probe, as `Settings` reads them."""
    _write_all_settings(monkeypatch, tmp_path)
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
    direct = seen["direct"]
    assert direct.bearer == FAKE_BEARER  # type: ignore[attr-defined]
    assert direct.base_url == probe.DEFAULT_TOOLSERVER_BASE_URL  # type: ignore[attr-defined]
    assert seen["ledger_dsn"] == FAKE_DSN
    captured = capsys.readouterr()
    assert "missing" not in captured.err
    assert "fake record" in captured.out
    _assert_no_fake_values(captured.out, captured.err)


def test_bearer_and_dsn_from_toolserver_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tool server's own `.env` supplies the bearer and the DSN (environment wins)."""
    _use_env_files(monkeypatch, tmp_path, "APP_ENV=test\n")
    settings = probe.e1.load_settings()
    assert probe.load_direct_config(settings).bearer is None
    (tmp_path / "toolserver.env").write_text(
        f"TOOLSERVER_BEARER_MEMORY={FAKE_BEARER}\nDATABASE_URL_TOOLSERVER={FAKE_DSN}\n",
        encoding="utf-8",
    )
    assert probe.load_direct_config(settings).bearer == FAKE_BEARER
    assert probe.e1.load_ledger_dsn() == FAKE_DSN
    monkeypatch.setenv("TOOLSERVER_BEARER_MEMORY", "bearer-from-environment")
    assert probe.load_direct_config(settings).bearer == "bearer-from-environment"


def test_base_url_from_settings_or_documented_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_env_files(monkeypatch, tmp_path, "APP_ENV=test\n")
    default = probe.load_direct_config(probe.e1.load_settings())
    assert default.base_url == "https://managerbot-armoriq-hack.onrender.com"
    assert default.base_url_source == "documented default"
    monkeypatch.setenv("TOOLSERVER_BASE_URL", "https://toolserver.example.invalid/")
    configured = probe.load_direct_config(probe.e1.load_settings())
    assert configured.base_url == "https://toolserver.example.invalid"
    assert configured.base_url_source == "TOOLSERVER_BASE_URL"


@pytest.mark.parametrize(
    "url",
    ["https://user:pw@toolserver.example.invalid", "ftp://toolserver.example.invalid", "nonsense"],
)
def test_bad_base_url_is_a_prerequisite_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    _write_all_settings(monkeypatch, tmp_path, f"TOOLSERVER_BASE_URL={url}\n")
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: pytest.fail("must not run"))
    assert probe.main([]) == 2
    err = capsys.readouterr().err
    assert "TOOLSERVER_BASE_URL" in err
    assert "pw@" not in err
    _assert_no_fake_values(err)


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


def test_proxy_shape_is_always_not_tested() -> None:
    (only,) = probe.group_proxy_shape()
    assert only["observed"] == "not tested"
    assert only["layer"] == "unknown"
    assert "no verified" in only["evidence"]


def test_direct_calls_read_sse_frames__offline_mock_transport() -> None:
    """B2 (api job, no `mcp` installed): an SSE `data:` frame and the bearer
    guard's JSON are read; `response.json()` on SSE would have lost both."""
    import httpx

    def toolserver(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "m"}})
        frame = (
            'event: message\ndata: {"jsonrpc":"2.0","id":"x","result":{"isError":true,'
            '"content":[{"type":"text","text":"{\\"code\\": \\"invalid_context\\"}"}]}}\n\n'
        )
        return httpx.Response(200, text=frame, headers={"content-type": "text/event-stream"})

    with httpx.Client(
        base_url="https://toolserver.offline.invalid", transport=httpx.MockTransport(toolserver)
    ) as client:
        rows = probe.group_direct(client, FAKE_BEARER)
    by_case = {item["case"]: item for item in rows if "case" in item}
    assert by_case["direct-no-ctx"]["code"] == "invalid_context"
    assert by_case["direct-no-ctx"]["layer"] == "tool_server_rule"
    assert by_case["direct-no-bearer"]["code"] == "unauthorized"
    _assert_no_fake_values(probe.render_table(rows), repr(rows))


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
