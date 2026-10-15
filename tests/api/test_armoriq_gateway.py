"""Offline gateway tests (fake SDK only — never gate evidence).

Every test name ends with ``__offline_fake_sdk`` per the handoff protocol: no
test here touches the network or proves an ArmorIQ behavior. Live mapping is
confirmed or corrected only by E1-E4.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from armoriq_sdk import exceptions as sdk_exceptions
from armoriq_sdk.models import HoldInfo, MCPInvocationResult

from apps.api.core.config import Settings
from services.authorization.armoriq_gateway import ArmorIQGateway
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


def test_missing_signer_is_unverified__offline_fake_sdk() -> None:
    # P1-04 has not merged: with no signer injected the gateway fails closed
    # before touching the SDK.
    fake = FakeClient("allow")
    gateway = ArmorIQGateway(_settings(), client_factory=lambda member_id: fake)
    assert gateway._ctx_signer is None
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


def test_fake_gateway_is_scripted_and_test_only() -> None:
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
