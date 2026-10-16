"""Offline gateway tests (fake SDK only — never gate evidence).

Every test name ends with ``__offline_fake_sdk`` per the handoff protocol: no
test here touches the network or proves an ArmorIQ behavior. Live mapping is
confirmed or corrected only by E1-E4.
"""

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from armoriq_sdk import exceptions as sdk_exceptions
from armoriq_sdk.models import HoldInfo, MCPInvocationResult

from apps.api.core.config import Settings
from services.authorization.armoriq_gateway import ArmorIQGateway
from services.authorization.ctx_envelope import verify_ctx
from services.contracts import (
    AppRole,
    ApprovalState,
    AuthenticatedUser,
    DecisionOutcome,
    DecisionSource,
    EntryPoint,
    PersonaKey,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    ToolExecutionStatus,
    ToolName,
)
from services.contracts.tools import TOOL_CATALOG
from tests.api.conftest import make_settings

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# --- offline fake SDK ---------------------------------------------------------


class FakeToken:
    def __init__(self, expired: bool = False) -> None:
        self.plan_id = "sdk-plan-1"
        self.is_expired = expired
        self.expires_at = NOW + timedelta(seconds=1200)


class FakeSession:
    def __init__(self, expired_token: bool = False) -> None:
        self.start_plan_calls = 0
        self.seen_tool_calls: list[Any] = []
        self.expired_token = expired_token

    def start_plan(self, tool_calls: Any, goal: Any = None) -> FakeToken:
        self.start_plan_calls += 1
        self.seen_tool_calls.append(list(tool_calls))
        return FakeToken(expired=self.expired_token)

    def close(self) -> None:
        pass


class FakeScope:
    def __init__(self, client: "FakeClient", email: str) -> None:
        self._client = client
        self.email = email
        self.session = FakeSession(expired_token=client.behavior == "expired_token")

    def start_session(self, opts: Any = None) -> FakeSession:
        self._client.sessions_started.append(self.email)
        return self.session


class FakeClient:
    """Mimics the ArmorIQClient surface the gateway uses (offline)."""

    def __init__(self, behavior: str = "allow") -> None:
        self.behavior = behavior
        self.scopes: dict[str, FakeScope] = {}
        self.policy_calls: list[dict[str, Any]] = []
        self.invoke_calls: list[dict[str, Any]] = []
        self.sessions_started: list[str] = []
        self.hold_seen: list[Any] = []

    def for_user(self, email: str) -> FakeScope:
        scope = FakeScope(self, email)
        self.scopes[email] = scope
        return scope

    def invoke_with_policy(
        self, mcp: str, action: str, token: Any, params: Any = None, options: Any = None
    ) -> Any:
        self.policy_calls.append({"mcp": mcp, "action": action, "params": dict(params or {})})
        if self.behavior == "allow":
            return MCPInvocationResult(mcp=mcp, action=action, result={"ledger_entry_id": "L-1"})
        if self.behavior == "allow_no_ledger":
            return MCPInvocationResult(mcp=mcp, action=action, result={"answer": "x"})
        if self.behavior == "block":
            raise sdk_exceptions.PolicyBlockedException(
                "denied", enforcement_action="deny", reason="policy says no"
            )
        if self.behavior == "hold_rejected":
            raise sdk_exceptions.PolicyHoldException(
                "Delegation rejected", {"delegation_id": "dlg-1"}
            )
        if self.behavior == "hold_timeout":
            raise sdk_exceptions.PolicyHoldException("Delegation approval timed out")
        if self.behavior == "hold_then_allow":
            hold = HoldInfo(reason="need approval", tool=action, mcp=mcp, delegation_id="dlg-9")
            self.hold_seen.append(hold)
            if options is not None and getattr(options, "on_hold", None):
                options.on_hold(hold)
            return MCPInvocationResult(mcp=mcp, action=action, result={"ledger_entry_id": "L-9"})
        if self.behavior == "mismatch":
            raise sdk_exceptions.IntentMismatchException("not in plan", action=action)
        if self.behavior == "transport":
            raise sdk_exceptions.MCPInvocationException("proxy down", mcp=mcp, action=action)
        if self.behavior == "invalid_token":
            raise sdk_exceptions.InvalidTokenException("bad signature")
        if self.behavior == "token_expired":
            raise sdk_exceptions.TokenExpiredException("token expired")
        if self.behavior == "revoked_token":
            raise sdk_exceptions.RevokedTokenError("token revoked")
        if self.behavior == "delegation_rejected":
            raise sdk_exceptions.DelegationException("Delegation rejected", delegation_id="dlg-2")
        if self.behavior == "delegation_expired":
            raise sdk_exceptions.DelegationException("Delegation expired", delegation_id="dlg-3")
        if self.behavior == "delegation_timeout":
            raise sdk_exceptions.DelegationException(
                "Delegation approval timed out after 900s", delegation_id="dlg-4"
            )
        if self.behavior == "delegation_broken":
            raise sdk_exceptions.DelegationException("something else broke")
        if self.behavior == "boom":
            raise RuntimeError("unexpected")
        raise AssertionError(f"unknown behavior {self.behavior}")

    def invoke(
        self,
        mcp: str,
        action: str,
        token: Any,
        params: Any = None,
        merkle_proof: Any = None,
        user_email: Any = None,
    ) -> Any:
        self.invoke_calls.append({"mcp": mcp, "action": action})
        if self.behavior == "offplan_allow":
            # Alarming path: the proxy accepted an off-plan call. The gateway
            # must still block without executing.
            return MCPInvocationResult(mcp=mcp, action=action, result={"answer": "x"})
        raise sdk_exceptions.IntentMismatchException("not in plan", action=action)


def _user(persona: PersonaKey, member_id: str) -> AuthenticatedUser:
    role = {
        PersonaKey.MAYA: AppRole.MANAGER,
        PersonaKey.SAM: AppRole.SENIOR,
        PersonaKey.PRIYA: AppRole.JUNIOR,
    }[persona]
    return AuthenticatedUser(
        member_id=member_id,
        persona=persona,
        display_name=persona.value.title(),
        app_role=role,
        masked_email=f"{persona.value[0]}***@example.com",
    )


def _ctx(requester: AuthenticatedUser, request_id: str = "r1") -> RequestContext:
    return RequestContext(
        request_id=request_id,
        trace_id="t1",
        workspace_id="w1",
        requester=requester,
        entry_point=EntryPoint.WEB,
        received_at=NOW,
    )


def _call(index: int, tool: ToolName, **arguments: Any) -> PlannedToolCall:
    return PlannedToolCall(
        step_index=index,
        mcp=TOOL_CATALOG[tool].endpoint,
        tool=tool,
        arguments=arguments,
        purpose="test",
    )


def _plan(*tools: ToolName) -> PlanProposal:
    return PlanProposal(
        summary="offline test plan",
        steps=[_call(i, tool, query="q") for i, tool in enumerate(tools)],
    )


_SIGNER_CALLS: list[dict[str, Any]] = []


def _fake_signer(fields: Any, secret: str) -> str:
    _SIGNER_CALLS.append(dict(fields))
    assert secret == "ctx-secret-for-tests"
    return f"signed-{fields['jti']}"


def _settings(**overrides: Any) -> Settings:
    values = {
        "armoriq_api_key": "ak_test_key",
        "ctx_secret": "ctx-secret-for-tests",
        "persona_maya_email": "maya@example.com",
        "persona_sam_email": "sam@example.com",
        "persona_priya_email": "priya@example.com",
    }
    values.update(overrides)
    return make_settings(**values)


def _gateway(behavior: str = "allow", **overrides: Any) -> tuple[ArmorIQGateway, FakeClient]:
    fake = FakeClient(behavior)
    gateway = ArmorIQGateway(
        _settings(**overrides),
        client_factory=lambda member_id: fake,
        ctx_signer=_fake_signer,
    )
    return gateway, fake


def test_one_client_per_member__offline_fake_sdk() -> None:
    created: list[str] = []

    def factory(member_id: str) -> FakeClient:
        created.append(member_id)
        return FakeClient("allow")

    gateway = ArmorIQGateway(_settings(), client_factory=factory, ctx_signer=_fake_signer)
    members = [
        ("m-maya", PersonaKey.MAYA),
        ("m-sam", PersonaKey.SAM),
        ("m-priya", PersonaKey.PRIYA),
    ]

    async def run() -> None:
        for member_id, persona in members:
            await gateway.start_plan(_ctx(_user(persona, member_id)), _plan())
        # Same member again: no new client.
        await gateway.start_plan(_ctx(_user(PersonaKey.MAYA, "m-maya"), request_id="r2"), _plan())

    asyncio.run(run())
    assert sorted(created) == ["m-maya", "m-priya", "m-sam"]
    assert len(gateway._member_clients) == 3


def test_mint_once_per_request__offline_fake_sdk() -> None:
    gateway, fake = _gateway()
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM, ToolName.KNOWLEDGE_GET_SOURCE_TEAM)

    async def run() -> None:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        first = await gateway.invoke_step(handle, 0, ctx, plan)
        second = await gateway.invoke_step(handle, 1, ctx, plan)
        assert first.decision.outcome is DecisionOutcome.ALLOW
        assert second.decision.outcome is DecisionOutcome.ALLOW
        assert first.invocation is not None and second.invocation is not None
        assert first.invocation.plan_id == second.invocation.plan_id == handle.plan_id

    asyncio.run(run())
    session = fake.scopes["maya@example.com"].session
    assert session.start_plan_calls == 1
    assert len(fake.policy_calls) == 2


def test_concurrent_personas_do_not_share_identity__offline_fake_sdk() -> None:
    seen: dict[str, list[str]] = {}

    def factory(member_id: str) -> FakeClient:
        fake = FakeClient("allow")
        seen[member_id] = []
        original = fake.invoke_with_policy

        def spy(mcp: str, action: str, token: Any, params: Any = None, options: Any = None) -> Any:
            seen[member_id].append(getattr(options, "user_email", None))
            return original(mcp, action, token, params=params, options=options)

        fake.invoke_with_policy = spy  # type: ignore[method-assign]
        return fake

    gateway = ArmorIQGateway(_settings(), client_factory=factory, ctx_signer=_fake_signer)

    async def one(member_id: str, persona: PersonaKey) -> None:
        ctx = _ctx(_user(persona, member_id), request_id=f"r-{member_id}")
        plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)
        handle = await gateway.start_plan(ctx, plan)
        outcome = await gateway.invoke_step(handle, 0, ctx, plan)
        assert outcome.decision.outcome is DecisionOutcome.ALLOW

    async def run() -> None:
        await asyncio.gather(
            one("m-maya", PersonaKey.MAYA),
            one("m-sam", PersonaKey.SAM),
            one("m-priya", PersonaKey.PRIYA),
        )

    asyncio.run(run())
    assert seen == {
        "m-maya": ["maya@example.com"],
        "m-sam": ["sam@example.com"],
        "m-priya": ["priya@example.com"],
    }


@pytest.mark.parametrize(
    "behavior,outcome",
    [
        ("mismatch", DecisionOutcome.BLOCK),
        ("block", DecisionOutcome.BLOCK),
        ("transport", DecisionOutcome.UNVERIFIED),
        ("invalid_token", DecisionOutcome.UNVERIFIED),
        ("token_expired", DecisionOutcome.UNVERIFIED),
        ("revoked_token", DecisionOutcome.UNVERIFIED),
        ("boom", DecisionOutcome.UNVERIFIED),
        ("hold_timeout", DecisionOutcome.UNVERIFIED),
        ("delegation_broken", DecisionOutcome.UNVERIFIED),
    ],
)
def test_sdk_exceptions_fail_closed__offline_fake_sdk(
    behavior: str, outcome: DecisionOutcome
) -> None:
    gateway, _ = _gateway(behavior)
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        return await gateway.invoke_step(handle, 0, ctx, plan)

    result = asyncio.run(run())
    assert result.decision.outcome is outcome
    assert result.decision.outcome is not DecisionOutcome.ALLOW


def test_mint_failure_is_unverified__offline_fake_sdk() -> None:
    def factory(member_id: str) -> FakeClient:
        raise sdk_exceptions.ConfigurationException("no key")

    gateway = ArmorIQGateway(
        _settings(armoriq_api_key=None), client_factory=factory, ctx_signer=_fake_signer
    )
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))

    async def run() -> Any:
        return await gateway.start_plan(ctx, _plan())

    handle = asyncio.run(run())
    assert handle.decision is not None
    assert handle.decision.outcome is DecisionOutcome.UNVERIFIED


def test_unknown_member_is_refused__offline_fake_sdk() -> None:
    gateway, fake = _gateway(persona_priya_email=None)
    outsider = AuthenticatedUser(
        member_id="m-ghost",
        persona=PersonaKey.PRIYA,
        display_name="Ghost",
        app_role=AppRole.JUNIOR,
        masked_email="g***@example.com",
    )
    ctx = _ctx(outsider)

    async def run() -> Any:
        return await gateway.start_plan(ctx, _plan())

    handle = asyncio.run(run())
    assert handle.decision is not None
    assert handle.decision.outcome is DecisionOutcome.UNVERIFIED
    assert fake.policy_calls == [] and fake.invoke_calls == []


def test_hold_maps_to_pending_then_resolves__offline_fake_sdk() -> None:
    gateway, fake = _gateway("hold_then_allow")
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.invoke_step(handle, 0, ctx, plan)

    outcome = asyncio.run(run())
    # The SDK wait path observed a hold (on_hold fired) before the approved
    # resume executed the call.
    assert len(fake.hold_seen) == 1
    assert outcome.decision.outcome is DecisionOutcome.ALLOW
    assert outcome.decision.source is DecisionSource.ARMORIQ_POLICY_DECISION
    assert outcome.invocation is not None
    assert outcome.invocation.status is ToolExecutionStatus.EXECUTED
    assert outcome.invocation.ledger_entry_id == "L-9"
    # Approval evidence: the SDK only retries after status == approved for the
    # delegation it created, so success-after-hold carries an approved record.
    assert outcome.approval is not None
    assert outcome.approval.state is ApprovalState.APPROVED
    assert outcome.approval.delegation_id == "dlg-9"
    assert outcome.approval.requested_at <= outcome.approval.resolved_at  # type: ignore[operator]


@pytest.mark.parametrize(
    "behavior,outcome,state,delegation_id",
    [
        ("delegation_rejected", DecisionOutcome.BLOCK, ApprovalState.REJECTED, "dlg-2"),
        ("delegation_expired", DecisionOutcome.BLOCK, ApprovalState.EXPIRED, "dlg-3"),
        ("delegation_timeout", DecisionOutcome.UNVERIFIED, ApprovalState.TIMEOUT, "dlg-4"),
    ],
)
def test_delegation_errors_resolve_holds__offline_fake_sdk(
    behavior: str, outcome: DecisionOutcome, state: ApprovalState, delegation_id: str
) -> None:
    # Terminal HOLD states surface as DelegationException from the SDK wait
    # loop (verified: client.py wait poll). The real delegation ID is
    # preserved and nothing executes.
    gateway, fake = _gateway(behavior)
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        return await gateway.invoke_step(handle, 0, ctx, plan)

    result = asyncio.run(run())
    assert result.decision.outcome is outcome
    assert result.approval is not None
    assert result.approval.state is state
    assert result.approval.delegation_id == delegation_id
    assert result.invocation is not None
    assert result.invocation.status is ToolExecutionStatus.NOT_REACHED
    assert fake.policy_calls != []  # the attempt reached the SDK wait path


def test_block_preserves_enforcement_action__offline_fake_sdk() -> None:
    gateway, _ = _gateway("block")
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.invoke_step(handle, 0, ctx, plan)

    result = asyncio.run(run())
    assert result.decision.outcome is DecisionOutcome.BLOCK
    assert result.decision.enforcement_action == "deny"


def test_hold_rejected_never_executes__offline_fake_sdk() -> None:
    gateway, _ = _gateway("hold_rejected")
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.invoke_step(handle, 0, ctx, plan)

    outcome = asyncio.run(run())
    assert outcome.decision.outcome is DecisionOutcome.BLOCK
    assert outcome.decision.source is DecisionSource.ARMORIQ_POLICY_DECISION
    assert outcome.approval is not None
    assert outcome.approval.state is ApprovalState.REJECTED
    assert outcome.approval.delegation_id == "dlg-1"
    assert outcome.invocation is not None
    assert outcome.invocation.status is ToolExecutionStatus.NOT_REACHED


def test_off_plan_attempt_never_executes__offline_fake_sdk() -> None:
    gateway, fake = _gateway()
    ctx = _ctx(_user(PersonaKey.PRIYA, "m-priya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.attempt_off_plan(
            handle, ToolName.SEND_EXTERNAL_MESSAGE, {"message": "hi"}, ctx
        )

    decision = asyncio.run(run())
    assert decision.outcome is DecisionOutcome.BLOCK
    assert decision.source is DecisionSource.ARMORIQ_SDK_PLAN_CHECK
    assert len(fake.invoke_calls) == 1
    assert fake.policy_calls == []


def test_off_plan_success_never_allows__offline_fake_sdk() -> None:
    # Even if the proxy accepts an off-plan call, the gateway blocks without
    # executing and never reports ALLOW.
    gateway, fake = _gateway("offplan_allow")
    ctx = _ctx(_user(PersonaKey.PRIYA, "m-priya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        return await gateway.attempt_off_plan(
            handle, ToolName.SEND_EXTERNAL_MESSAGE, {"message": "hi"}, ctx
        )

    decision = asyncio.run(run())
    assert decision.outcome is DecisionOutcome.BLOCK
    assert decision.source is DecisionSource.UNKNOWN
    assert decision.outcome is not DecisionOutcome.ALLOW
    assert fake.policy_calls == []


def test_fresh_jti_per_invocation__offline_fake_sdk() -> None:
    _SIGNER_CALLS.clear()
    gateway, _ = _gateway()
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM, ToolName.KNOWLEDGE_GET_SOURCE_TEAM)

    async def run() -> None:
        handle = await gateway.start_plan(ctx, plan)
        await gateway.invoke_step(handle, 0, ctx, plan)
        await gateway.invoke_step(handle, 1, ctx, plan)

    asyncio.run(run())
    assert len(_SIGNER_CALLS) == 2
    jtis = [_SIGNER_CALLS[0]["jti"], _SIGNER_CALLS[1]["jti"]]
    assert jtis[0] and jtis[1] and jtis[0] != jtis[1]
    for call in _SIGNER_CALLS:
        assert call["aud"] == "memory-mcp"
        assert call["armoriq_member_email_ref"] == "PERSONA_MAYA_EMAIL"


def test_no_secrets_in_logs__offline_fake_sdk(caplog: pytest.LogCaptureFixture) -> None:
    gateway, _ = _gateway()
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> None:
        handle = await gateway.start_plan(ctx, plan)
        await gateway.invoke_step(handle, 0, ctx, plan)

    with caplog.at_level(logging.INFO, logger="services.authorization.armoriq_gateway"):
        asyncio.run(run())
    text = caplog.text
    assert "ak_test_key" not in text
    assert "maya@example.com" not in text
    assert "ctx-secret-for-tests" not in text
    assert "signed-" not in text
    assert "r1" in text  # request_id is logged


def test_failed_handle_short_circuits_without_sdk__offline_fake_sdk() -> None:
    gateway, fake = _gateway()
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        bad_index = await gateway.invoke_step(handle, 5, ctx, plan)
        other_ctx = _ctx(_user(PersonaKey.SAM, "m-sam"), request_id="r2")
        wrong_owner = await gateway.invoke_step(handle, 0, other_ctx, plan)
        return bad_index, wrong_owner

    bad_index, wrong_owner = asyncio.run(run())
    assert bad_index.decision.outcome is DecisionOutcome.BLOCK
    assert wrong_owner.decision.outcome is DecisionOutcome.BLOCK
    assert fake.policy_calls == []


def test_default_signer_produces_verifiable_ctx__offline_fake_sdk() -> None:
    # No injected signer: the gateway signs with P1-04's sign_ctx, and the
    # tool-server side (verify_ctx) accepts the token for this endpoint and tool.
    fake = FakeClient("allow")
    gateway = ArmorIQGateway(_settings(), client_factory=lambda member_id: fake)
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.invoke_step(handle, 0, ctx, plan)

    asyncio.run(run())
    token = fake.policy_calls[0]["params"]["_ctx"]
    step = plan.steps[0]
    envelope = verify_ctx(
        token, "ctx-secret-for-tests", expected_aud=step.mcp.value, expected_tool=step.tool.value
    )
    assert envelope.requester_member_id == "m-maya"
    assert envelope.armoriq_member_email_ref == "PERSONA_MAYA_EMAIL"
    payload = base64.urlsafe_b64decode(token.split(".")[0] + "==")
    assert b"maya@example.com" not in payload  # ADR-020: env-key reference only


def test_missing_ctx_secret_is_unverified__offline_fake_sdk() -> None:
    # No CTX_SECRET: the gateway fails closed before touching the SDK.
    fake = FakeClient("allow")
    gateway = ArmorIQGateway(_settings(ctx_secret=None), client_factory=lambda member_id: fake)
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        return await gateway.invoke_step(handle, 0, ctx, plan)

    result = asyncio.run(run())
    assert result.decision.outcome is DecisionOutcome.UNVERIFIED
    assert result.decision.source is DecisionSource.UNKNOWN
    assert fake.policy_calls == []


def test_signer_failure_is_unverified__offline_fake_sdk() -> None:
    def broken_signer(fields: Any, secret: str) -> str:
        raise RuntimeError("HSM offline")

    fake = FakeClient("allow")
    gateway = ArmorIQGateway(
        _settings(), client_factory=lambda member_id: fake, ctx_signer=broken_signer
    )
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None
        return await gateway.invoke_step(handle, 0, ctx, plan)

    result = asyncio.run(run())
    assert result.decision.outcome is DecisionOutcome.UNVERIFIED
    assert fake.policy_calls == []


def test_expired_token_never_invokes__offline_fake_sdk() -> None:
    gateway, fake = _gateway("expired_token")
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        assert handle.decision is None  # mint succeeded; the token then lapsed
        return await gateway.invoke_step(handle, 0, ctx, plan)

    result = asyncio.run(run())
    assert result.decision.outcome is DecisionOutcome.UNVERIFIED
    assert result.decision.source is DecisionSource.UNKNOWN
    assert fake.policy_calls == []


def test_member_locks_are_isolated__offline_fake_sdk() -> None:
    gateway, _ = _gateway()
    first: dict[str, Any] = {}

    async def run() -> None:
        for member_id, persona in [
            ("m-maya", PersonaKey.MAYA),
            ("m-sam", PersonaKey.SAM),
            ("m-sam", PersonaKey.SAM),
        ]:
            await gateway.start_plan(_ctx(_user(persona, member_id)), _plan())
        for member_id in ("m-maya", "m-sam"):
            first[member_id] = gateway._member_clients._locks[member_id]

    asyncio.run(run())
    locks = gateway._member_clients._locks
    assert set(locks) == {"m-maya", "m-sam"}
    assert locks["m-maya"] is not locks["m-sam"]
    assert locks["m-maya"] is first["m-maya"] and locks["m-sam"] is first["m-sam"]
    # Same member reuses one client across concurrent requests (serialized by
    # that member's lock): two plans, one client, one mint each.
    assert len(gateway._member_clients) == 2


def test_fake_gateway_is_scripted_and_test_only__offline_fake_sdk() -> None:
    # The offline double for P3-04/P3-07 lives in tests/fakes (see guardrails).
    from tests.fakes.armoriq_gateway import FakeArmorIQGateway

    fake = FakeArmorIQGateway(default="allow")
    fake.script_tool(ToolName.KNOWLEDGE_SEARCH_TEAM, "block")
    ctx = _ctx(_user(PersonaKey.MAYA, "m-maya"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_TEAM)

    async def run() -> Any:
        handle = await fake.start_plan(ctx, plan)
        return await fake.invoke_step(handle, 0, ctx, plan)

    outcome = asyncio.run(run())
    assert outcome.decision.outcome is DecisionOutcome.BLOCK
    assert outcome.decision.reason == "fake block"
    assert [name for name, _ in fake.calls] == ["start_plan", "invoke_step"]


# --- tool-result shape (SHARED_INTERFACES §4, emitted by P2-06) ---------------


def _text(body: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": body if isinstance(body, str) else json.dumps(body)}]


def _rejection(code: str, **meta: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": _text({"code": code, "message": "The call was rejected."}),
        "isError": True,
    }
    if meta:
        payload["_meta"] = meta
    return payload


def _invoke_returning(payload: Any, tool: ToolName = ToolName.KNOWLEDGE_SEARCH_TEAM) -> Any:
    """invoke_step against a fake SDK whose proxy returned `payload` as the
    JSON-RPC result. Like the real SDK (client.py `invoke`), an
    isError/is_error/error payload gets status "error"."""
    gateway, fake = _gateway()
    status = (
        "error"
        if isinstance(payload, dict)
        and (payload.get("isError") or payload.get("is_error") or payload.get("error"))
        else "success"
    )

    def invoke_with_policy(
        mcp: str, action: str, token: Any, params: Any = None, options: Any = None
    ) -> Any:
        return MCPInvocationResult(mcp=mcp, action=action, result=payload, status=status)

    fake.invoke_with_policy = invoke_with_policy  # type: ignore[method-assign]
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(tool)

    async def run() -> Any:
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.invoke_step(handle, 0, ctx, plan)

    return asyncio.run(run())


def test_canonical_success_is_ledger_backed_execution__offline_fake_sdk() -> None:
    payload = {
        "content": _text({"tool": "knowledge_search_team", "tier": "team", "query": "q"}),
        "isError": False,
        "_meta": {"ledger_entry_id": "42"},
    }
    outcome = _invoke_returning(payload)
    assert outcome.decision.outcome is DecisionOutcome.ALLOW
    assert outcome.invocation.status is ToolExecutionStatus.EXECUTED
    assert outcome.invocation.ledger_entry_id == "42"
    assert outcome.invocation.tool_server_reached is True


def test_success_without_ledger_reference_is_never_executed__offline_fake_sdk() -> None:
    outcome = _invoke_returning({"content": _text({"tool": "x"}), "isError": False})
    assert outcome.invocation.status is ToolExecutionStatus.UNKNOWN
    assert outcome.invocation.ledger_entry_id is None


@pytest.mark.parametrize(
    ("payload", "code", "ledger_entry_id"),
    [
        (_rejection("unauthenticated"), "unauthenticated", None),
        (_rejection("invalid_context"), "invalid_context", None),
        (_rejection("context_expired"), "context_expired", None),
        (_rejection("invalid_arguments", ledger_entry_id="7"), "invalid_arguments", "7"),
        (_rejection("out_of_scope", ledger_entry_id="8"), "out_of_scope", "8"),
        (_rejection("precondition_failed", ledger_entry_id="6"), "precondition_failed", "6"),
        (_rejection("internal", ledger_entry_id="9"), "internal", "9"),
        (_rejection("replay", original_entry_id="5"), "replay", "5"),
        (_rejection("replay"), "replay", None),
        # `original_entry_id` belongs to replay only; never adopted elsewhere.
        (_rejection("internal", original_entry_id="5"), "internal", None),
        # `not_found` without its own ledger row cannot be an executed lookup.
        (_rejection("not_found"), "internal", None),
        (_rejection("not_found", original_entry_id="5"), "internal", None),
        # Never trust free text as a code; never treat isError as success.
        (_rejection("totally-made-up"), "internal", None),
        ({"content": _text("Unknown tool: x"), "isError": True}, "internal", None),
        ({"content": [], "isError": True}, "internal", None),
        ({"content": _text([1, 2]), "is_error": True}, "internal", None),
        ({"content": _text({"code": ["x"]}), "isError": True}, "internal", None),
        # Earlier provisional shape (top-level key) keeps working.
        ({"error_code": "invalid_context"}, "invalid_context", None),
    ],
)
def test_tool_server_rejection_is_never_allow__offline_fake_sdk(
    payload: dict[str, Any], code: str, ledger_entry_id: str | None
) -> None:
    outcome = _invoke_returning(payload)
    assert outcome.decision.outcome is DecisionOutcome.BLOCK
    assert outcome.decision.source is DecisionSource.TOOL_SERVER_RULE
    assert outcome.invocation.status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER
    assert outcome.invocation.tool_server_reached is True
    assert outcome.invocation.error_code == code
    assert outcome.invocation.ledger_entry_id == ledger_entry_id


@pytest.mark.parametrize(
    "tool",
    [
        ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
        ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
        ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED,
    ],
)
def test_not_found_is_an_executed_empty_lookup__offline_fake_sdk(tool: ToolName) -> None:
    """D-11 `not_found`: the tool server executed the authorized lookup (its
    own accepted/executed ledger row) and found nothing visible. EXECUTED with
    no result (no evidence); the ArmorIQ decision is kept, never converted
    into a tool-server block."""
    outcome = _invoke_returning(_rejection("not_found", ledger_entry_id="11"), tool)
    assert outcome.decision.outcome is DecisionOutcome.ALLOW
    assert outcome.decision.source is DecisionSource.ARMORIQ_POLICY_DECISION
    invocation = outcome.invocation
    assert invocation.tool is tool
    assert invocation.status is ToolExecutionStatus.EXECUTED
    assert invocation.tool_server_reached is True
    assert invocation.ledger_entry_id == "11"
    assert invocation.result is None
    assert invocation.error_code == "not_found"


def test_not_found_top_level_shape_keeps_its_ledger_row__offline_fake_sdk() -> None:
    outcome = _invoke_returning({"error_code": "not_found", "ledger_entry_id": "12"})
    assert outcome.invocation.status is ToolExecutionStatus.EXECUTED
    assert outcome.invocation.ledger_entry_id == "12"
    assert outcome.decision.source is DecisionSource.ARMORIQ_POLICY_DECISION


def test_sdk_error_status_alone_is_a_rejection__offline_fake_sdk() -> None:
    outcome = _invoke_returning({"error": "proxy-side tool error"})
    assert outcome.decision.outcome is DecisionOutcome.BLOCK
    assert outcome.invocation.status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER
    assert outcome.invocation.error_code == "internal"


def test_parses_what_the_tool_server_emits__offline_fake_sdk() -> None:
    """Contract round trip with P2-06's real result builders (needs both
    extras; the isolated api job has no `mcp` and skips)."""
    pytest.importorskip("mcp")
    from services.contracts import DataTier, KnowledgeSearchResult
    from services.toolserver.ledger import LedgerEntryId
    from services.toolserver.schemas import error_result, ok_result

    def wire(result: Any) -> dict[str, Any]:
        return result.model_dump(by_alias=True, mode="json", exclude_none=True)

    model = KnowledgeSearchResult(
        tool=ToolName.KNOWLEDGE_SEARCH_TEAM, tier=DataTier.TEAM, query="q", hits=[]
    )
    ok = _invoke_returning(wire(ok_result(model, ledger_entry_id=LedgerEntryId(41))))
    assert ok.invocation.status is ToolExecutionStatus.EXECUTED
    assert ok.invocation.ledger_entry_id == "41"
    replay = _invoke_returning(wire(error_result("replay", original_entry_id=LedgerEntryId(41))))
    assert replay.decision.outcome is DecisionOutcome.BLOCK
    assert (replay.invocation.error_code, replay.invocation.ledger_entry_id) == ("replay", "41")
    denied = _invoke_returning(wire(error_result("out_of_scope", ledger_entry_id=LedgerEntryId(3))))
    assert (denied.invocation.error_code, denied.invocation.ledger_entry_id) == (
        "out_of_scope",
        "3",
    )
    assert denied.decision.source is DecisionSource.TOOL_SERVER_RULE
    missing = _invoke_returning(
        wire(error_result("not_found", ledger_entry_id=LedgerEntryId(4))),
        ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
    )
    assert missing.invocation.status is ToolExecutionStatus.EXECUTED
    assert (missing.invocation.error_code, missing.invocation.ledger_entry_id) == (
        "not_found",
        "4",
    )
    assert missing.invocation.result is None
    assert missing.decision.outcome is DecisionOutcome.ALLOW
    assert missing.decision.source is DecisionSource.ARMORIQ_POLICY_DECISION
    # The tool server never emits a row-less not_found (it answers `internal`),
    # and the gateway would refuse one anyway.
    rowless = _invoke_returning(wire(error_result("not_found")), ToolName.KNOWLEDGE_GET_SOURCE_TEAM)
    assert rowless.invocation.status is ToolExecutionStatus.REJECTED_BY_TOOL_SERVER
    assert rowless.invocation.error_code == "internal"
    assert rowless.decision.source is DecisionSource.TOOL_SERVER_RULE
