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
from collections.abc import Callable
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
    assert only["layer"] == "not tested"
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

    def __init__(
        self,
        secret: str = FAKE_CTX_SECRET,
        bearer: str = FAKE_BEARER,
        *,
        strict_arguments: bool = True,
    ) -> None:
        self.secret = secret
        self.bearer = bearer
        # False emulates a broken deployment that skips the strict schema.
        self.strict_arguments = strict_arguments
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

        from services.contracts import tool_args, tool_spec
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
            if self.strict_arguments:
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
    a proxy 409 raised as IntentMismatchException(action=...) (976-981), and
    a proxy enforcement block raised as PolicyBlockedException (893, 932-941).
    The fake proxy forwards to the fake tool server over a MockTransport, so
    the probe's wire hooks see every request. ``proxy_refuse(body)`` returns
    an HTTP status to answer instead of forwarding; ``policy_block(body)``
    returns a block reason."""

    def __init__(
        self,
        server: _OfflineToolServer__offline_fake,
        *,
        proxy_refuse: Callable[[dict], int | None] | None = None,
        policy_block: Callable[[dict], str | None] | None = None,
        expose_http_client: bool = True,
    ) -> None:
        self.server = server
        self.proxy_refuse = proxy_refuse
        self.policy_block = policy_block
        self.minted: list[list[dict[str, object]]] = []
        self.invoked: list[tuple[str, str, list[str]]] = []
        self._wire = httpx.Client(transport=httpx.MockTransport(self._proxy))
        if expose_http_client:
            self.http_client = self._wire

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
        response = self._wire.post(
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
        data = response.json()
        enforcement = data.get("enforcement")
        if enforcement:
            raise sdk_exceptions.PolicyBlockedException(
                data.get("message") or "Enforcement: block",
                enforcement_action=enforcement.get("action"),
                reason=enforcement.get("reason"),
            )
        result = data["result"]
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

        body = json.loads(request.content)
        refused = self.proxy_refuse(body) if self.proxy_refuse else None
        if refused:
            return httpx.Response(refused, json={"error": "fake proxy refusal"})
        reason = self.policy_block(body) if self.policy_block else None
        if reason:
            return httpx.Response(
                200, json={"enforcement": {"action": "block", "reason": reason}, "message": "m"}
            )
        result = self.server.call(body["action"], dict(body["params"]))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def _offline_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        armoriq_api_key=FAKE_API_KEY,
        persona_maya_email=FAKE_EMAIL,
        ctx_secret=FAKE_CTX_SECRET,
    )


class _OfflineEvidenceLookup__offline_fake:
    """Fake read-only evidence lookup over the fake tool server's rows (the
    window queries of `E2EvidenceLookup`, no Neon)."""

    def __init__(self, server: _OfflineToolServer__offline_fake) -> None:
        self.server = server
        self.calls: list[str] = []
        self.closed = False

    def ping(self) -> None:
        self.calls.append("ping")

    @staticmethod
    def _within(rows: list[dict[str, object]], start: object, end: object) -> list[dict]:
        return [dict(row) for row in rows if start <= row["created_at"] <= end]  # type: ignore[operator]

    def ledger_window(self, start: object, end: object, tools: list[str]) -> list[dict]:
        self.calls.append("ledger_window")
        rows = self._within(self.server.ledger_rows, start, end)
        return [row for row in rows if row["tool_name"] in tools]

    def attempts_window(self, start: object, end: object, tools: list[str]) -> list[dict]:
        self.calls.append("attempts_window")
        rows = self._within(self.server.attempt_rows, start, end)
        return [row for row in rows if row["tool_name"] in tools]

    def egress_window(self, start: object, end: object) -> list[dict]:
        self.calls.append("egress_window")
        return self._within(self.server.egress_rows, start, end)

    def egress_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        self.calls.append("egress_for")
        return [
            dict(row)
            for row in self.server.egress_rows
            if row["request_id"] in request_ids or row["jti"] in jtis
        ]

    def close(self) -> None:
        self.closed = True


def _offline_run(
    *,
    server: _OfflineToolServer__offline_fake | None = None,
    proxy_refuse: Callable[[dict], int | None] | None = None,
    policy_block: Callable[[dict], str | None] | None = None,
    expose_http_client: bool = True,
    ledger_dsn: str | None = FAKE_DSN,
    **kwargs: object,
) -> tuple[int, str, _OfflineToolServer__offline_fake, _OfflineSdkClient__offline_fake]:
    """`probe.run_probe` over the real gateway with the offline fakes."""
    server = server or _OfflineToolServer__offline_fake()
    sdk = _OfflineSdkClient__offline_fake(
        server,
        proxy_refuse=proxy_refuse,
        policy_block=policy_block,
        expose_http_client=expose_http_client,
    )
    kwargs.setdefault("lookup_factory", lambda dsn: _OfflineEvidenceLookup__offline_fake(server))
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


# --- B1: tampering targets the PLANNED step; off-plan stays a separate case --------

# Injected values: key NAMES may be printed, values never.
INJECTED_VALUES = ("restricted", "attacker@example.invalid", "dlg-forged-e2", "forged-policy")


def _rows_by_case(record: str) -> dict[str, str]:
    """`case-id -> table line` from the record (the case id is in each row)."""
    lines: dict[str, str] = {}
    for line in record.splitlines():
        if line.startswith("| ") and "`" in line:
            case = line.split("`")[1]
            lines.setdefault(case, line)
    return lines


@pytest.mark.parametrize(
    "key", ["tier", "user_email", "_approvals", "_delegation_id", "_policy_context"]
)
def test_validator_rejects_each_injected_key_first(key: str) -> None:
    """B1: the PlannedToolCall validator refuses the key before any network call."""
    value = dict(probe.TAMPER_INJECTIONS)[key]
    item = probe.validator_row(key, value)
    assert item["case"] == f"validator-{key}"
    assert item["layer"] == "application_rule"
    assert "ValidationError" in item["observed"]
    rendered = probe.render_table([item]) + repr(item)
    for leaked in INJECTED_VALUES:
        assert leaked not in rendered


def test_tamper_cases_tamper_the_planned_step__offline_fake_sdk() -> None:
    """B1: every injection reaches `invoke_with_policy` for the PLANNED tool
    (never the off-plan path), so the SDK's name check cannot be what stops it."""
    _, record, _, sdk = _offline_run()
    planned_calls = [keys for _, action, keys in sdk.invoked if action == "knowledge_search_team"]
    for key in ("tier", "user_email", "_approvals", "_delegation_id"):
        assert any(key in keys for keys in planned_calls), key
    # `_policy_context` is always present: the SDK writes its own.
    assert all("_policy_context" in keys for keys in planned_calls)
    rows = _rows_by_case(record)
    for key in ("tier", "user_email", "_approvals", "_delegation_id", "_policy_context"):
        assert "armoriq_sdk_plan_check" not in rows[f"inject-{key}"]
    for leaked in INJECTED_VALUES:
        assert leaked not in record


def test_forbidden_business_keys_are_rejected_by_the_tool_server__offline_fake_sdk() -> None:
    """`tier`/`user_email` injected after validation: the tool server's strict
    schema answers `invalid_arguments` with its own rejected ledger row."""
    _, record, server, _ = _offline_run()
    rows = _rows_by_case(record)
    for key in ("tier", "user_email"):
        line = rows[f"inject-{key}"]
        assert "error_code=invalid_arguments" in line
        assert "tool_server_rule" in line
    rejected = [
        row for row in server.ledger_rows if row["validation_outcome"] == "invalid_arguments"
    ]
    assert len(rejected) == 2
    assert all(row["execution_status"] == "rejected_by_tool_server" for row in rejected)


def test_forged_underscore_fields_are_stripped__offline_fake_sdk() -> None:
    """`_approvals`/`_delegation_id`/`_policy_context` grant nothing: the
    tool server strips them and executes exactly the planned arguments."""
    _, record, server, _ = _offline_run()
    planned = {"query": probe.E2_QUERY, "limit": probe.E2_LIMIT}
    team_runs = [args for tool, args in server.executions if tool == "knowledge_search_team"]
    assert team_runs.count(planned) >= 4  # baseline + three forged-field injections
    assert not any(key.startswith("_") for args in team_runs for key in args)
    rows = _rows_by_case(record)
    for key in ("_approvals", "_delegation_id", "_policy_context"):
        assert "status=executed" in rows[f"inject-{key}"]


def test_changed_query_answers_server_side_argument_binding__offline_fake_sdk() -> None:
    """A changed query under a valid plan that reaches the tool server and
    executes disproves server-side argument binding (recorded as such)."""
    _, record, server, _ = _offline_run()
    assert ("knowledge_search_team", {"query": probe.TAMPERED_QUERY, "limit": probe.E2_LIMIT}) in (
        server.executions
    )
    assert "status=executed" in _rows_by_case(record)["tamper-query"]
    assert "arguments: DISPROVEN" in record


def test_off_plan_cases_are_separate_and_never_reach_the_proxy__offline_fake_sdk() -> None:
    _, record, server, sdk = _offline_run()
    rows = _rows_by_case(record)
    for case in ("offplan-confidential", "offplan-honeypot"):
        assert "armoriq_sdk_plan_check" in rows[case]
    proxied = {action for _, action, _ in sdk.invoked}
    assert proxied == {"knowledge_search_team"}
    assert not server.egress_rows
    assert all(tool != "send_external_message" for tool, _ in server.executions)


# --- I2: independent evidence for every label ----------------------------------------


def _details(record: str, case: str) -> str:
    """The "Attempt details" block of one case."""
    block: list[str] = []
    inside = False
    for line in record.splitlines():
        if line.startswith("- #"):
            inside = f"`{case}`" in line
        elif line.startswith(("#", "- ")) and not line.startswith("  "):
            inside = False
        if inside:
            block.append(line)
    assert block, f"no details for {case}"
    return "\n".join(block)


def test_off_plan_label_is_backed_by_zero_requests_and_the_exception__offline_fake_sdk() -> None:
    """I2: the pre-network claim is checked on the wire (httpx hooks on the SDK
    client via the `client_factory` seam) and by the exception's form."""
    _, record, _, _ = _offline_run()
    for case in ("offplan-confidential", "offplan-honeypot"):
        block = _details(record, case)
        assert "0 request(s) during the attempt (0 to the proxy /invoke)" in block
        assert "IntentMismatchException (pre-network form: no action attribute)" in block
        assert "not found in the original plan" in block
        assert "FLAG" not in block
    assert "FLAG" not in record


def test_proxy_409_contradicting_the_sdk_label_is_flagged__offline_fake_sdk() -> None:
    """I2 + ICR: the SDK also raises IntentMismatchException on a proxy 409,
    which the gateway labels `armoriq_sdk_plan_check`. The wire shows the
    request, so the probe flags the label, names the proxy, and exits 3."""

    def refuse_changed_query(body: dict) -> int | None:
        return 409 if body["params"].get("query") == probe.TAMPERED_QUERY else None

    code, record, _, _ = _offline_run(proxy_refuse=refuse_changed_query)
    block = _details(record, "tamper-query")
    assert "gateway: block (armoriq_sdk_plan_check)" in block
    assert "1 request(s) to the proxy /invoke (HTTP 409)" in block
    assert "IntentMismatchException (proxy-response form: action attribute set)" in block
    assert "FLAG: the label armoriq_sdk_plan_check claims a pre-network rejection" in block
    assert "armoriq_proxy_decision (HTTP 409" in _rows_by_case(record)["tamper-query"]
    assert "Gateway ICR" in record
    assert code == 3


def test_policy_block_reason_is_masked__offline_fake_sdk() -> None:
    def block_user_email(body: dict) -> str | None:
        return f"policy denied {FAKE_EMAIL}" if "user_email" in body["params"] else None

    _, record, _, _ = _offline_run(policy_block=block_user_email)
    block = _details(record, "inject-user_email")
    assert "gateway: block (armoriq_policy_decision)" in block
    assert "reason: policy denied m***@e***.com" in block
    assert "1 request(s) to the proxy /invoke (HTTP 200)" in block
    assert "armoriq_policy_decision" in _rows_by_case(record)["inject-user_email"]
    _assert_no_fake_values(record)


def test_every_attempt_has_utc_timestamps__offline_fake_sdk() -> None:
    import re

    _, record, _, _ = _offline_run()
    stamps = re.findall(
        r"- #\d+ `[a-z_-]+` \[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) → "
        r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z)\]",
        record,
    )
    assert len(stamps) == 22
    assert all(start <= end for start, end in stamps)


def test_forged_values_on_the_wire__offline_fake_sdk() -> None:
    """The `/invoke` body shows which injected values left the SDK unchanged
    (key names and booleans only): `_policy_context` is replaced by the SDK."""
    _, record, _, _ = _offline_run()
    assert "`_policy_context` sent as injected: no" in _details(record, "inject-_policy_context")
    assert "`_approvals` sent as injected: yes" in _details(record, "inject-_approvals")
    assert "`tier` sent as injected: yes" in _details(record, "inject-tier")
    for leaked in INJECTED_VALUES:
        assert leaked not in record


def test_pre_network_claim_without_wire_hooks_is_flagged__offline_fake_sdk() -> None:
    code, record, _, _ = _offline_run(expose_http_client=False)
    assert "FLAG: no wire hooks on the SDK client" in _details(record, "offplan-confidential")
    assert code == 3


def test_main_shows_gateway_log_lines_on_stderr__offline_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run is wrapped in `e1.gateway_logs_to_stderr()`: jti and exception
    class lines reach stderr; the handler is removed afterwards."""
    import logging

    _write_all_settings(monkeypatch, tmp_path)
    gateway_logger = logging.getLogger(probe.e1.GATEWAY_LOGGER)
    handlers = list(gateway_logger.handlers)
    real_run_probe = probe.run_probe

    async def offline_run_probe(settings: object, **kwargs: object) -> tuple[int, str]:
        server = _OfflineToolServer__offline_fake()
        sdk = _OfflineSdkClient__offline_fake(server)
        with httpx.Client(
            base_url=OFFLINE_BASE_URL, transport=httpx.MockTransport(server.http)
        ) as client:
            return await real_run_probe(
                _offline_settings(),
                direct=kwargs["direct"],  # type: ignore[arg-type]
                ledger_dsn=None,
                sdk_client_factory=lambda member_id: sdk,
                direct_client=client,
                spacing_s=0.0,
            )

    monkeypatch.setattr(probe, "run_probe", offline_run_probe)
    probe.main([])
    captured = capsys.readouterr()
    assert "[gateway INFO] gateway step ok request_id=" in captured.err
    assert "[gateway INFO] gateway off-plan blocked" in captured.err
    assert gateway_logger.handlers == handlers
    _assert_no_fake_values(captured.out, captured.err)


# --- I3: zero-execution evidence from the ledger, attempts and egress sink ----------


def test_zero_executions_are_confirmed_from_window_queries__offline_fake_ledger() -> None:
    """I3: read-only window queries (ledger + attempts by time window and
    tool_name, egress_sink by window and by request_id/jti) attribute every
    row to an attempt; nothing unattributed executed."""
    _, record, server, _ = _offline_run()
    assert "- DB evidence (read-only, toolserver role): ok" in record
    assert "Zero unintended executions: CONFIRMED" in record
    assert f"ledger rows: {len(server.ledger_rows)}" in record
    assert f"attempt rows: {len(server.attempt_rows)}" in record
    assert "egress_sink rows: 0 in the window, 0 by request_id/jti" in record
    assert "unattributed rows: none" in record
    block = _details(record, "offplan-honeypot")
    assert "DB: no ledger, attempt, or egress_sink row for send_external_message" in block


def test_rejected_attempts_are_attributed_without_jti__offline_fake_ledger() -> None:
    """Rejected `tool_call_attempts` rows carry NULL jti/request_id; they are
    attributed by (tool, outcome) in run order, never by jti."""
    _, record, server, _ = _offline_run()
    for case, tool, outcome in (
        ("direct-no-ctx", "knowledge_search_team", "invalid_context"),
        ("direct-forged-ctx", "knowledge_search_team", "invalid_context"),
        ("direct-wrong-secret", "knowledge_search_team", "invalid_context"),
        ("direct-cross-tool", "knowledge_search_confidential", "invalid_context"),
        ("direct-expired", "knowledge_search_team", "context_expired"),
    ):
        block = _details(record, case)
        assert "DB: attempt row #" in block, case
        assert f"({tool}, {outcome}, jti NULL)" in block, case
    baseline = server.ledger_rows[0]
    replay = _details(record, "direct-replay")
    assert f"(knowledge_search_team, replay, jti {baseline['jti']}" in replay
    assert f"original_entry_id={baseline['id']}" in replay
    assert "DB: no attempt row (the bearer guard answers before verification)" in _details(
        record, "direct-no-bearer"
    )


def test_strip_and_tamper_evidence_from_the_business_argument_hash__offline_fake_ledger() -> None:
    """The ledger's `arguments_sha256` (never printed) is compared with the
    hash of the expected business arguments: the forged `_` keys were
    stripped, and the changed query is what executed."""
    import re

    _, record, _, _ = _offline_run()
    for case in ("inject-_approvals", "inject-_delegation_id", "inject-_policy_context"):
        assert "business-argument hash = planned arguments: yes" in _details(record, case)
    assert "business-argument hash = tampered arguments: yes" in _details(record, "tamper-query")
    assert "ledger row #" in _details(record, "inject-tier")
    assert "validation_outcome=invalid_arguments" in _details(record, "inject-tier")
    assert not re.search(r"\b[0-9a-f]{64}\b", record)


def test_unintended_execution_is_not_confirmed__offline_fake_ledger() -> None:
    """A deployment without the strict schema executes the injected `tier`:
    the ledger shows it, zero executions is NOT confirmed, exit 3."""
    code, record, _, _ = _offline_run(
        server=_OfflineToolServer__offline_fake(strict_arguments=False)
    )
    assert "Zero unintended executions: NOT CONFIRMED" in record
    assert "#14 inject-tier executed" in record
    assert code == 3


def test_foreign_rows_in_the_window_block_confirmation__offline_fake_ledger() -> None:
    from datetime import UTC, datetime

    server = _OfflineToolServer__offline_fake()
    server.ledger_rows.append(
        {
            "id": 900,
            "jti": "someone-else",
            "request_id": "other-traffic",
            "tool_name": "knowledge_search_confidential",
            "validation_outcome": "accepted",
            "execution_status": "executed",
            "arguments_sha256": "0" * 64,
            "created_at": datetime.now(UTC),
        }
    )
    server.egress_rows.append(
        {
            "id": 5,
            "request_id": "other-traffic",
            "tool_name": "send_external_message",
            "jti": "someone-else",
            "created_at": datetime.now(UTC),
        }
    )
    code, record, _, _ = _offline_run(server=server)
    assert "Zero unintended executions: NOT CONFIRMED" in record
    assert "unattributed executed ledger row #900 (knowledge_search_confidential)" in record
    assert "1 egress_sink row(s) in the window" in record
    assert code == 3


def test_db_lookup_error_reports_the_class_only__offline_fake_ledger() -> None:
    def boom(dsn: str) -> object:
        raise ConnectionError(f"db down at {dsn}")

    code, record, _, _ = _offline_run(lookup_factory=boom)
    assert "- DB evidence (read-only, toolserver role): lookup error (ConnectionError)" in record
    assert "Zero unintended executions: NOT CONFIRMED" in record
    assert code == 3
    _assert_no_fake_values(record)


def test_without_a_dsn_zero_executions_is_never_confirmed__offline_fake_sdk() -> None:
    code, record, _, _ = _offline_run(ledger_dsn=None)
    assert "unavailable (no DATABASE_URL_TOOLSERVER)" in record
    assert "Zero unintended executions: NOT CONFIRMED" in record
    assert code == 3


def test_db_lookup_mutes_driver_logs__offline_fake_ledger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pool/driver warnings (they quote the Neon host and role) never print."""
    import logging

    leak = "connection to server at fake-host.invalid failed for user fake_role"

    class Noisy(_OfflineEvidenceLookup__offline_fake):
        def ledger_window(self, *args: object) -> list[dict]:
            logging.getLogger("psycopg.pool").warning("error connecting: %s", leak)
            return super().ledger_window(*args)  # type: ignore[arg-type]

    server = _OfflineToolServer__offline_fake()
    with caplog.at_level(logging.DEBUG):
        _offline_run(server=server, lookup_factory=lambda dsn: Noisy(server))
    assert leak not in caplog.text


def test_evidence_queries_select_no_content_columns() -> None:
    for sql in (
        probe._LEDGER_WINDOW_SQL,
        probe._ATTEMPTS_WINDOW_SQL,
        probe._EGRESS_WINDOW_SQL,
        probe._EGRESS_BY_ID_SQL,
    ):
        assert sql.lstrip().upper().startswith("SELECT ")
        for column in ("message", "arguments,", "prev_hash", "entry_hash", "destination"):
            assert column not in sql
