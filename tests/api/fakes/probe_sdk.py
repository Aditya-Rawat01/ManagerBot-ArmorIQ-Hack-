"""Offline doubles for the P1-08..P1-10 probe tests (check_setup, E3, E4, E5).

Test-only, never evidence. The approach is the E2 probe tests' one
(`tests/api/test_e2_probe.py`): the probes run over the REAL
`ArmorIQGateway`, with a fake SDK client injected through the gateway's
`client_factory` seam. The fake client mirrors the armoriq-sdk 0.6.10
behaviour the gateway relies on (pre-network plan-name check, proxy
enforcement block as `PolicyBlockedException`, HOLD via `options.on_hold`
then `DelegationException` for rejected/expired/timeout or a retry on
approval), and its fake proxy forwards over an httpx `MockTransport` so the
probes' wire hooks see every request. The fake tool server verifies the
REAL `_ctx` (P1-04 `verify_ctx`), applies the role ceiling, dedupes by jti
and keeps rows shaped like `tool_execution_ledger`, `tool_call_attempts`
and `egress_sink`; `FakeEvidenceLookup` reads them like the probes'
read-only Neon lookup. No network, no database, no real credentials.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx

from services.authorization.ctx_envelope import CtxError, ExpiredContext, verify_ctx
from services.contracts import TOOL_CATALOG, ToolName

OFFLINE_PROXY = "https://proxy.offline.invalid"
OFFLINE_BASE_URL = "https://toolserver.offline.invalid"
FAKE_API_KEY = "ak_fake_value_that_must_not_leak"
FAKE_CTX_SECRET = "ctx-fake-secret-that-must-not-leak"
FAKE_BEARER = "bearer-fake-memory-token-that-must-not-leak"
FAKE_BEARER_WORKSPACE = "bearer-fake-workspace-token-that-must-not-leak"
FAKE_DSN = "postgresql://fake_role:fake_password@fake-host.invalid:5432/fakedb"
FAKE_EMAILS = {
    "maya": "maya.fake@example.invalid",
    "sam": "sam.fake@example.invalid",
    "priya": "priya.fake@example.invalid",
}
FAKE_VALUES = (
    FAKE_API_KEY,
    FAKE_CTX_SECRET,
    FAKE_BEARER,
    FAKE_BEARER_WORKSPACE,
    FAKE_DSN,
    "fake_password",
    *FAKE_EMAILS.values(),
)

_RANK = {"team": 0, "confidential": 1, "restricted": 2}
_CEILING = {"manager": "restricted", "senior": "confidential", "junior": "team"}

# The PRD §5.1 matrix + honeypot, keyed by (e-mail, action): what the fake
# ArmorIQ policy answers. Anything unlisted is BLOCK (default block).
POLICY_MATRIX: dict[tuple[str, str], str] = {}
for _persona, _row in {
    "maya": ("allow", "allow", "allow", "block"),
    "sam": ("allow", "hold", "block", "block"),
    "priya": ("allow", "block", "block", "block"),
}.items():
    for _tool, _decision in zip(
        (
            "knowledge_search_team",
            "knowledge_search_confidential",
            "knowledge_search_restricted",
            "send_external_message",
        ),
        _row,
        strict=True,
    ):
        POLICY_MATRIX[(FAKE_EMAILS[_persona], _tool)] = _decision


def matrix_policy(email: str, action: str) -> str:
    return POLICY_MATRIX.get((email, action), "block")


def _now() -> datetime:
    return datetime.now(UTC)


def _text_result(body: dict[str, Any], *, error: bool, meta: dict[str, str] | None = None) -> dict:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(body)}],
        "isError": error,
    }
    if meta:
        result["_meta"] = meta
    return result


class FakeToolServer:
    """In-memory tool server: real `_ctx` verification, ceiling, jti dedupe, rows."""

    def __init__(
        self,
        *,
        secret: str = FAKE_CTX_SECRET,
        bearers: dict[str, str] | None = None,
        tool_lists: dict[str, list[str]] | None = None,
        enforce_ceiling: bool = True,
    ) -> None:
        self.secret = secret
        self.bearers = bearers or {
            "memory-mcp": FAKE_BEARER,
            "workspace-mcp": FAKE_BEARER_WORKSPACE,
        }
        self.tool_lists = tool_lists or {
            endpoint: [n.value for n, s in TOOL_CATALOG.items() if s.endpoint.value == endpoint]
            for endpoint in ("memory-mcp", "workspace-mcp")
        }
        self.enforce_ceiling = enforce_ceiling
        self.ledger_rows: list[dict[str, Any]] = []
        self.attempt_rows: list[dict[str, Any]] = []
        self.egress_rows: list[dict[str, Any]] = []
        self.claimed: dict[str, str] = {}
        self._ids = itertools.count(1)
        self.unknown_names: list[str] = []

    def _attempt(self, endpoint: str, tool: str, outcome: str, ctx: Any = None) -> None:
        self.attempt_rows.append(
            {
                "id": next(self._ids),
                "credential_id": f"toolserver_bearer_{endpoint.split('-')[0]}",
                "mcp_endpoint": endpoint,
                "tool_name": tool,
                "outcome": outcome,
                "jti": getattr(ctx, "jti", None),
                "request_id": getattr(ctx, "request_id", None),
                "created_at": _now(),
            }
        )

    def _ledger(self, ctx: Any, tool: str, outcome: str, status: str) -> str:
        entry = str(next(self._ids))
        self.ledger_rows.append(
            {
                "id": entry,
                "jti": ctx.jti,
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "plan_id": ctx.plan_id,
                "step_index": ctx.step_index,
                "tool_name": tool,
                "requester_member_id": ctx.requester_member_id,
                "validation_outcome": outcome,
                "execution_status": status,
                "created_at": _now(),
            }
        )
        return entry

    def call(self, endpoint: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """One `tools/call` as the deployed server answers it (CallToolResult dict)."""
        try:
            name = ToolName(tool)
        except ValueError:
            # Unknown names never reach a guarded handler (no attempt row).
            self.unknown_names.append(tool)
            return {"content": [{"type": "text", "text": f"Unknown tool: {tool}"}], "isError": True}
        token = params.get("_ctx")
        try:
            ctx = verify_ctx(str(token), self.secret, expected_aud=endpoint, expected_tool=tool)
        except ExpiredContext:
            self._attempt(endpoint, tool, "context_expired")
            return _text_result({"code": "context_expired", "message": "x"}, error=True)
        except CtxError:
            self._attempt(endpoint, tool, "invalid_context")
            return _text_result({"code": "invalid_context", "message": "x"}, error=True)
        if ctx.jti in self.claimed:
            self._attempt(endpoint, tool, "replay", ctx)
            return _text_result(
                {"code": "replay", "message": "x"},
                error=True,
                meta={"original_entry_id": self.claimed[ctx.jti]},
            )
        self._attempt(endpoint, tool, "accepted", ctx)
        tier = TOOL_CATALOG[name].tier
        ceiling = _CEILING.get(ctx.app_role, "team")
        if self.enforce_ceiling and tier is not None and _RANK[tier.value] > _RANK[ceiling]:
            entry = self._ledger(ctx, tool, "out_of_scope", "rejected_by_tool_server")
            self.claimed[ctx.jti] = entry
            return _text_result(
                {"code": "out_of_scope", "message": "x"},
                error=True,
                meta={"ledger_entry_id": entry},
            )
        entry = self._ledger(ctx, tool, "accepted", "executed")
        self.claimed[ctx.jti] = entry
        if tool == "send_external_message":
            self.egress_rows.append(
                {
                    "id": next(self._ids),
                    "request_id": ctx.request_id,
                    "tool_name": tool,
                    "jti": ctx.jti,
                    "created_at": _now(),
                }
            )
        body = {"tool": tool, "hits": []}
        return _text_result(body, error=False, meta={"ledger_entry_id": entry})

    def http(self, request: httpx.Request) -> httpx.Response:
        """Direct HTTP (bearer only): `/health`, `tools/list`, `tools/call` as SSE."""
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        endpoint = {"/memory/mcp": "memory-mcp", "/workspace/mcp": "workspace-mcp"}.get(
            request.url.path
        )
        if endpoint is None:
            return httpx.Response(404)
        if request.headers.get("authorization") != f"Bearer {self.bearers[endpoint]}":
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        body = json.loads(request.content)
        if body.get("method") == "tools/list":
            result: dict[str, Any] = {
                "tools": [{"name": name} for name in self.tool_lists[endpoint]]
            }
        else:
            params = body["params"]
            result = self.call(endpoint, params["name"], dict(params.get("arguments") or {}))
        message = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        return httpx.Response(
            200,
            text=f"event: message\ndata: {json.dumps(message)}\n\n",
            headers={"content-type": "text/event-stream"},
        )


class FakeSdkClient:
    """Fake armoriq-sdk client for one member (see the module docstring).

    ``policy(email, action)`` answers allow / block / hold / proxy404.
    ``hold_resolution`` decides what a HOLD becomes: approved / rejected /
    expired / timeout. ``mint_policy(email, names)`` may block at mint.
    """

    def __init__(
        self,
        server: FakeToolServer,
        *,
        policy: Callable[[str, str], str] = matrix_policy,
        hold_resolution: str = "timeout",
        mint_policy: Callable[[str, list[str]], str | None] | None = None,
        on_hold_hook: Callable[[], None] | None = None,
    ) -> None:
        self.server = server
        self.policy = policy
        self.hold_resolution = hold_resolution
        self.mint_policy = mint_policy
        self.on_hold_hook = on_hold_hook
        self.email = ""
        self.minted: list[list[str]] = []
        self.invoked: list[tuple[str, str]] = []
        self._delegations = itertools.count(1)
        self.http_client = httpx.Client(transport=httpx.MockTransport(self._proxy))

    def for_user(self, email: str) -> FakeSdkClient:
        self.email = email
        return self

    def start_session(self, options: object = None) -> SimpleNamespace:
        return SimpleNamespace(start_plan=self._start_plan, close=lambda: None)

    def _start_plan(self, tool_calls: list[dict[str, Any]], goal: object = None) -> Any:
        from armoriq_sdk import exceptions as sdk_exceptions

        names = [str(call["name"]) for call in tool_calls]
        self.minted.append(names)
        if self.mint_policy is not None:
            reason = self.mint_policy(self.email, names)
            if reason:
                raise sdk_exceptions.PolicyBlockedException(
                    "blocked", enforcement_action="block", reason=reason
                )
        steps = [dict(zip(("mcp", "action"), name.split("__", 1), strict=True)) for name in names]
        return SimpleNamespace(
            plan_id=f"ap-{len(self.minted)}",
            expires_at=_now() + timedelta(seconds=600),
            is_expired=False,
            raw_token={"plan": {"steps": steps}},
        )

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        return self.http_client.post(f"{OFFLINE_PROXY}/invoke", json=body)

    def invoke(
        self,
        mcp: str,
        action: str,
        intent_token: Any,
        params: dict[str, Any] | None = None,
        merkle_proof: object = None,
        user_email: str | None = None,
    ) -> Any:
        from armoriq_sdk import exceptions as sdk_exceptions

        actions = [step["action"] for step in intent_token.raw_token["plan"]["steps"]]
        if action not in actions:
            raise sdk_exceptions.IntentMismatchException(f"Action '{action}' not in plan")
        self.invoked.append((mcp, action))
        response = self._post({"mcp": mcp, "action": action, "params": dict(params or {})})
        return self._result(mcp, action, response)

    def _result(self, mcp: str, action: str, response: httpx.Response) -> Any:
        from armoriq_sdk import exceptions as sdk_exceptions

        if response.status_code >= 400:
            raise sdk_exceptions.MCPInvocationException(
                f"MCP invocation failed: {response.status_code}",
                mcp=mcp,
                action=action,
                status_code=response.status_code,
            )
        data = response.json()
        if data.get("enforcement"):
            raise sdk_exceptions.PolicyBlockedException(
                "Enforcement: block",
                enforcement_action="block",
                reason=data["enforcement"]["reason"],
            )
        result = data["result"]
        status = "error" if result.get("isError") else "success"
        return SimpleNamespace(result=result, status=status)

    def invoke_with_policy(
        self,
        mcp: str,
        action: str,
        intent_token: Any,
        params: dict[str, Any] | None = None,
        options: Any = None,
    ) -> Any:
        from armoriq_sdk import exceptions as sdk_exceptions

        actions = [step["action"] for step in intent_token.raw_token["plan"]["steps"]]
        if action not in actions:
            raise sdk_exceptions.IntentMismatchException(f"Action '{action}' not in plan")
        enriched = {**dict(params or {}), "_policy_context": {"is_financial": False}}
        self.invoked.append((mcp, action))
        response = self._post({"mcp": mcp, "action": action, "params": enriched})
        if response.status_code == 200 and response.json().get("hold"):
            delegation_id = f"dlg-{next(self._delegations)}"
            if options is not None and getattr(options, "on_hold", None):
                options.on_hold(SimpleNamespace(delegation_id=delegation_id))
            if self.on_hold_hook is not None:
                self.on_hold_hook()
            if self.hold_resolution == "approved":
                retry = {**enriched, "_delegation_id": delegation_id, "_approvals": 1}
                response = self._post(
                    {"mcp": mcp, "action": action, "params": retry, "approved": True}
                )
                return self._result(mcp, action, response)
            if self.hold_resolution == "approved_token_expired":
                # Approved, but the retry carries an expired intent token.
                raise sdk_exceptions.MCPInvocationException(
                    "MCP invocation failed: 401", mcp=mcp, action=action, status_code=401
                )
            message = {
                "rejected": "Delegation rejected",
                "expired": "Delegation expired",
            }.get(self.hold_resolution, "Delegation approval timed out after 1s")
            raise sdk_exceptions.DelegationException(message, delegation_id=delegation_id)
        return self._result(mcp, action, response)

    def _proxy(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        decision = "allow" if body.get("approved") else self.policy(self.email, body["action"])
        if decision == "block":
            return httpx.Response(
                200, json={"enforcement": {"reason": f"blocked for {self.email}"}}
            )
        if decision == "hold":
            return httpx.Response(200, json={"hold": True})
        if decision == "proxy404":
            return httpx.Response(404, json={"error": "unknown tool"})
        result = self.server.call(body["mcp"], body["action"], dict(body["params"]))
        return httpx.Response(200, json={"result": result})


class FakeEvidenceLookup:
    """The probes' read-only Neon lookup over the fake server's rows."""

    def __init__(self, server: FakeToolServer, *, fail: bool = False) -> None:
        self.server = server
        self.fail = fail
        self.closed = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("fake database down")

    def ping(self) -> None:
        self._check()

    @staticmethod
    def _within(rows: list[dict], start: Any, end: Any) -> list[dict]:
        return [dict(row) for row in rows if start <= row["created_at"] <= end]

    @staticmethod
    def _mine(rows: list[dict], request_ids: list[str], jtis: list[str]) -> list[dict]:
        return [dict(r) for r in rows if r.get("request_id") in request_ids or r.get("jti") in jtis]

    def ledger_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        self._check()
        return self._mine(self.server.ledger_rows, request_ids, jtis)

    def attempts_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        self._check()
        return self._mine(self.server.attempt_rows, request_ids, jtis)

    def egress_for(self, request_ids: list[str], jtis: list[str]) -> list[dict]:
        return self._mine(self.server.egress_rows, request_ids, jtis)

    def ledger_window(self, start: Any, end: Any, tools: list[str]) -> list[dict]:
        self._check()
        rows = self._within(self.server.ledger_rows, start, end)
        return [row for row in rows if row["tool_name"] in tools]

    def attempts_window(self, start: Any, end: Any, tools: list[str]) -> list[dict]:
        rows = self._within(self.server.attempt_rows, start, end)
        return [row for row in rows if row["tool_name"] in tools]

    def egress_window(self, start: Any, end: Any) -> list[dict]:
        return self._within(self.server.egress_rows, start, end)

    def close(self) -> None:
        self.closed = True


def offline_settings(**overrides: Any) -> Any:
    from apps.api.core.config import Settings

    values: dict[str, Any] = {
        "app_env": "test",
        "armoriq_api_key": FAKE_API_KEY,
        "persona_maya_email": FAKE_EMAILS["maya"],
        "persona_sam_email": FAKE_EMAILS["sam"],
        "persona_priya_email": FAKE_EMAILS["priya"],
        "ctx_secret": FAKE_CTX_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def assert_no_fake_values(*texts: str) -> None:
    for text in texts:
        for value in FAKE_VALUES:
            assert value not in text, "a secret or e-mail leaked into probe output"
