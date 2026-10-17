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

import httpx
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
        rows = probe.group_direct(
            client, FAKE_BEARER, ctx_secret=FAKE_CTX_SECRET, request_id="r-e2"
        )
    by_case = {item["case"]: item for item in rows if "case" in item}
    assert by_case["direct-no-ctx"]["code"] == "invalid_context"
    assert by_case["direct-no-ctx"]["layer"] == "tool_server_rule"
    assert by_case["direct-no-bearer"]["code"] == "unauthorized"
    _assert_no_fake_values(probe.render_table(rows), repr(rows))


# --- offline fakes: ArmorIQ SDK client, proxy, and tool server -----------------------

OFFLINE_BASE_URL = "https://toolserver.offline.invalid"
OFFLINE_PROXY = "https://proxy.offline.invalid"


class _OfflineToolServer__offline_fake:
    """Fake deployed tool server (offline): the REAL `_ctx` verification
    (`verify_and_record`) and REAL argument models over in-memory rows shaped
    like `tool_execution_ledger`, `tool_call_attempts` and `egress_sink`."""

    def __init__(self, secret: str = FAKE_CTX_SECRET, bearer: str = FAKE_BEARER) -> None:
        self.secret = secret
        self.bearer = bearer
        self.claimed: set[str] = set()
        self.ledger_rows: list[dict[str, object]] = []
        self.attempt_rows: list[dict[str, object]] = []
        self.egress_rows: list[dict[str, object]] = []
        self.executions: list[tuple[str, dict[str, object]]] = []

    # AttemptLedger protocol (services/toolserver/ledger.py `verify_and_record`).
    def seen_jti(self, jti: str) -> bool:
        if jti in self.claimed:
            return True
        self.claimed.add(jti)
        return False

    def record_attempt(self, attempt: object) -> int:
        from datetime import UTC, datetime

        from services.toolserver.ledger import validate_attempt

        validate_attempt(attempt)  # type: ignore[arg-type]
        original = attempt.original_entry_id  # type: ignore[attr-defined]
        self.attempt_rows.append(
            {
                "id": len(self.attempt_rows) + 1,
                "credential_id": attempt.credential_id,  # type: ignore[attr-defined]
                "mcp_endpoint": attempt.mcp_endpoint,  # type: ignore[attr-defined]
                "tool_name": attempt.tool_name,  # type: ignore[attr-defined]
                "outcome": attempt.outcome,  # type: ignore[attr-defined]
                "jti": attempt.jti,  # type: ignore[attr-defined]
                "request_id": attempt.request_id,  # type: ignore[attr-defined]
                "original_entry_id": original.value if original else None,
                "created_at": datetime.now(UTC),
            }
        )
        return len(self.attempt_rows)

    def find_entry_id(self, jti: str) -> object:
        from services.toolserver.ledger import LedgerEntryId

        for row in self.ledger_rows:
            if row["jti"] == jti:
                return LedgerEntryId(row["id"])  # type: ignore[arg-type]
        return None

    def _append(self, verified: object, outcome: str, status: str) -> int:
        from datetime import UTC, datetime

        from services.toolserver.ledger import arguments_sha256

        ctx = verified.ctx  # type: ignore[attr-defined]
        row_id = len(self.ledger_rows) + 1
        self.ledger_rows.append(
            {
                "id": row_id,
                "jti": ctx.jti,
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "plan_id": ctx.plan_id,
                "step_index": ctx.step_index,
                "tool_name": ctx.tool,
                "requester_member_id": ctx.requester_member_id,
                "validation_outcome": outcome,
                "execution_status": status,
                "arguments_sha256": arguments_sha256(verified.business_arguments),  # type: ignore[attr-defined]
                "created_at": datetime.now(UTC),
            }
        )
        return row_id

    @staticmethod
    def _error(code: str, **meta: object) -> dict[str, object]:
        import json

        result: dict[str, object] = {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps({"code": code, "message": "m"})}],
        }
        refs = {key: str(value) for key, value in meta.items() if value is not None}
        if refs:
            result["_meta"] = refs
        return result

    def call(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        """The MCP CallToolResult (as JSON) the guarded dispatcher would return."""
        import json
        from datetime import UTC, datetime

        from pydantic import ValidationError

        from services.contracts import ToolName, tool_args, tool_spec
        from services.toolserver.ledger import ReplayRejection, verify_and_record
        from services.toolserver.security import CtxRejection

        endpoint = tool_spec(ToolName(tool)).endpoint.value
        try:
            verified = verify_and_record(
                self,  # type: ignore[arg-type]
                endpoint,
                tool,
                arguments,
                credential_id=f"toolserver_bearer_{endpoint.removesuffix('-mcp')}",
                ctx_secret=self.secret,
            )
        except ReplayRejection as replay:
            original = replay.original_entry_id
            return self._error("replay", original_entry_id=original.value if original else None)
        except CtxRejection as rejection:
            return self._error(rejection.code)
        try:
            tool_args.validate_arguments(ToolName(tool), dict(verified.business_arguments))
        except ValidationError:
            entry = self._append(verified, "invalid_arguments", "rejected_by_tool_server")
            return self._error("invalid_arguments", ledger_entry_id=entry)
        entry = self._append(verified, "accepted", "executed")
        self.executions.append((tool, dict(verified.business_arguments)))
        if tool == "send_external_message":
            self.egress_rows.append(
                {
                    "id": len(self.egress_rows) + 1,
                    "request_id": verified.ctx.request_id,
                    "tool_name": tool,
                    "jti": verified.ctx.jti,
                    "created_at": datetime.now(UTC),
                }
            )
        body = json.dumps({"tool": tool, "hits": [{"snippet": "private snippet text"}]})
        return {
            "isError": False,
            "content": [{"type": "text", "text": body}],
            "_meta": {"ledger_entry_id": str(entry)},
        }

    def http(self, request: httpx.Request) -> httpx.Response:
        """Direct HTTP path (MockTransport handler): bearer guard, then SSE."""
        import json

        import httpx

        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.headers.get("authorization") != f"Bearer {self.bearer}":
            return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "m"}})
        body = json.loads(request.content)
        params = body["params"]
        result = self.call(params["name"], dict(params["arguments"]))
        message = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(
            200,
            text=f"event: message\r\ndata: {message}\r\n\r\n",
            headers={"content-type": "text/event-stream"},
        )


class _OfflineSession__offline_fake:
    def __init__(self, client: "_OfflineSdkClient__offline_fake") -> None:
        self.client = client

    def start_plan(self, tool_calls: list[dict[str, object]], goal: object = None) -> object:
        from datetime import UTC, datetime, timedelta

        self.client.minted.append(list(tool_calls))
        steps = []
        for call in tool_calls:
            mcp, _, action = str(call["name"]).partition("__")
            steps.append({"mcp": mcp, "action": action})
        return SimpleNamespace(
            plan_id=f"ap-{len(self.client.minted)}",
            expires_at=datetime.now(UTC) + timedelta(seconds=600),
            is_expired=False,
            raw_token={"plan": {"steps": steps}},
        )

    def close(self) -> None:
        pass


class _OfflineSdkClient__offline_fake:
    """Fake ArmorIQ SDK client mirroring the armoriq-sdk 0.6.10 behaviour the
    probe relies on: the pre-network plan-name check (client.py:804-819),
    the `_policy_context` overwrite in `invoke_with_policy` (1671-1673), and
    a proxy 409 raised as IntentMismatchException(action=...) (976-981).
    The fake proxy forwards to the fake tool server over a MockTransport, so
    the probe's wire hooks see every request."""

    def __init__(
        self,
        server: _OfflineToolServer__offline_fake,
        *,
        proxy_status: dict[str, int] | None = None,
    ) -> None:
        import httpx

        self.server = server
        self.proxy_status = proxy_status or {}
        self.minted: list[list[dict[str, object]]] = []
        self.invoked: list[tuple[str, str, list[str]]] = []
        self.http_client = httpx.Client(transport=httpx.MockTransport(self._proxy))

    def for_user(self, email: str) -> object:
        client = self
        return SimpleNamespace(
            start_session=lambda opts=None: _OfflineSession__offline_fake(client)
        )

    def invoke(
        self,
        mcp: str,
        action: str,
        intent_token: object,
        params: dict[str, object] | None = None,
        merkle_proof: object = None,
        user_email: str | None = None,
    ) -> object:
        from armoriq_sdk import exceptions as sdk_exceptions

        steps = intent_token.raw_token["plan"]["steps"]  # type: ignore[attr-defined]
        actions = [step["action"] for step in steps]
        if action not in actions:
            raise sdk_exceptions.IntentMismatchException(
                f"Action '{action}' not found in the original plan. "
                f"Plan contains actions: {actions}."
            )
        self.invoked.append((mcp, action, sorted((params or {}).keys())))
        response = self.http_client.post(
            f"{OFFLINE_PROXY}/invoke",
            json={"mcp": mcp, "action": action, "params": dict(params or {})},
        )
        if response.status_code == 409:
            raise sdk_exceptions.IntentMismatchException(
                f"Action not in plan: {response.text}", action=action
            )
        if response.status_code >= 400:
            raise sdk_exceptions.MCPInvocationException(
                f"MCP invocation failed: {response.status_code}",
                mcp=mcp,
                action=action,
                status_code=response.status_code,
            )
        result = response.json()["result"]
        return SimpleNamespace(
            result=result, status="error" if result.get("isError") else "success", verified=True
        )

    def invoke_with_policy(
        self,
        mcp: str,
        action: str,
        intent_token: object,
        params: dict[str, object] | None = None,
        options: object = None,
    ) -> object:
        enriched = dict(params or {})
        enriched["_policy_context"] = {"is_financial": False}
        return self.invoke(
            mcp,
            action,
            intent_token,
            params=enriched,
            user_email=getattr(options, "user_email", None),
        )

    def _proxy(self, request: httpx.Request) -> httpx.Response:
        import json

        import httpx

        body = json.loads(request.content)
        action = body["action"]
        if action in self.proxy_status:
            return httpx.Response(self.proxy_status[action], json={"error": "fake proxy refusal"})
        result = self.server.call(action, dict(body["params"]))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _offline_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        armoriq_api_key=FAKE_API_KEY,
        persona_maya_email=FAKE_EMAIL,
        ctx_secret=FAKE_CTX_SECRET,
    )


def _offline_run(
    *,
    server: _OfflineToolServer__offline_fake | None = None,
    proxy_status: dict[str, int] | None = None,
    ledger_dsn: str | None = FAKE_DSN,
    **kwargs: object,
) -> tuple[int, str, _OfflineToolServer__offline_fake, _OfflineSdkClient__offline_fake]:
    """`probe.run_probe` over the real gateway with the offline fakes."""
    import httpx

    server = server or _OfflineToolServer__offline_fake()
    sdk = _OfflineSdkClient__offline_fake(server, proxy_status=proxy_status)
    direct = probe.DirectConfig(
        bearer=FAKE_BEARER, base_url=OFFLINE_BASE_URL, base_url_source="offline test"
    )
    with httpx.Client(
        base_url=OFFLINE_BASE_URL, transport=httpx.MockTransport(server.http)
    ) as direct_client:
        code, record = asyncio.run(
            probe.run_probe(
                _offline_settings(),
                direct=direct,
                ledger_dsn=ledger_dsn,
                sdk_client_factory=lambda member_id: sdk,
                direct_client=direct_client,
                spacing_s=0.0,
                **kwargs,
            )
        )
    return code, record, server, sdk


def test_replay_reuses_the_gateway_signed_ctx__offline_fake_sdk() -> None:
    """I1: the planned call's `_ctx` is captured at the gateway's `ctx_signer`
    seam and POSTed again directly: `replay`, pointing at the first entry."""
    _, record, server, _ = _offline_run()
    baseline = server.ledger_rows[0]
    assert baseline["execution_status"] == "executed"
    replays = [row for row in server.attempt_rows if row["outcome"] == "replay"]
    assert len(replays) == 1
    assert replays[0]["jti"] == baseline["jti"]
    assert replays[0]["original_entry_id"] == baseline["id"]
    assert f"original_entry_id={baseline['id']}" in record
    assert "matches the first call's ledger entry" in record
    _assert_no_fake_values(record)


def test_direct_minted_cases_reach_their_checks__offline_fake_sdk() -> None:
    _, record, server, _ = _offline_run()
    outcomes = [(row["tool_name"], row["outcome"]) for row in server.attempt_rows]
    assert ("knowledge_search_confidential", "invalid_context") in outcomes
    assert ("knowledge_search_team", "context_expired") in outcomes
    # no-ctx, forged, wrong-secret: three unverified team attempts (NULL identity).
    unverified = [
        row
        for row in server.attempt_rows
        if (row["tool_name"], row["outcome"]) == ("knowledge_search_team", "invalid_context")
    ]
    assert len(unverified) == 3
    assert all(row["jti"] is None and row["request_id"] is None for row in unverified)
    assert "direct call, `_ctx` signed with a wrong secret" in record


def test_ctx_capture_never_prints_tokens__offline_fake_sdk() -> None:
    """The captured `_ctx` and every probe-minted token stay out of the record."""
    tokens: list[str] = []

    def spy_signer(fields: object, secret: str) -> str:
        from services.authorization.armoriq_gateway import sign_ctx_fields

        token = sign_ctx_fields(fields, secret)  # type: ignore[arg-type]
        tokens.append(token)
        return token

    _, record, _, _ = _offline_run(ctx_signer_inner=spy_signer)
    assert tokens
    for token in tokens:
        assert token not in record
        assert token.split(".")[0] not in record
    _assert_no_fake_values(record)


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
