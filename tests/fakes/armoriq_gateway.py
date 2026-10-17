"""Offline ArmorIQ gateway double for tests only (moved out of production code).

`FakeArmorIQGateway` scripts per-tool outcomes for other tasks' offline tests
(P3-04, P3-07). It never touches the network or ``armoriq_sdk`` directly.

It lives under ``tests/`` on purpose: its decisions carry ArmorIQ source labels
(the contract only lets an ArmorIQ source carry a HOLD), so production code must
never be able to construct one and record fake "ArmorIQ" decisions as evidence.
``tests/`` is not an installed package and is not copied into any image;
``tests/api/test_guardrails.py`` fails if ``apps/`` or ``services/`` import it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.authorization.armoriq_gateway import PlanHandle, StepOutcome
from services.contracts import (
    ApprovalStatus,
    ArmorIQDecision,
    DecisionSource,
    PlanProposal,
    RequestContext,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)


def _fake_now() -> datetime:
    return datetime.now(UTC)


def _fake_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class FakeArmorIQGateway:
    """Offline double for other tasks' tests (P3-04, P3-07).

    Never touches the network or ``armoriq_sdk``. Scripted per-tool outcomes;
    records every call for assertions. Test-only: production code must use
    :class:`ArmorIQGateway`.
    """

    def __init__(self, default: str = "allow") -> None:
        self.default = default
        self.script: dict[str, str] = {}
        self.calls: list[tuple[str, Any]] = []

    def script_tool(self, tool: ToolName, outcome: str) -> None:
        self.script[tool.value] = outcome

    async def start_plan(self, ctx: RequestContext, plan: PlanProposal) -> PlanHandle:
        self.calls.append(("start_plan", ctx.request_id))
        return PlanHandle(
            request_id=ctx.request_id,
            plan_id=_fake_id("p"),
            member_id=ctx.requester.member_id,
            step_count=len(plan.steps),
            token_expires_at=_fake_now(),
            armoriq_plan_id=None,
            decision=None,
            _token=object(),
            _email="fake@example.com",
        )

    async def invoke_step(
        self,
        handle: PlanHandle,
        step_index: int,
        ctx: RequestContext,
        plan: PlanProposal,
        *,
        on_hold: Any = None,
    ) -> StepOutcome:
        # `on_hold` (P3-04) is accepted for signature parity and ignored: this
        # double returns a HOLD as a pending outcome instead of waiting.
        step = plan.steps[step_index]
        self.calls.append(("invoke_step", step.tool.value))
        outcome = self.script.get(step.tool.value, self.default)
        jti = f"fake-{uuid.uuid4().hex[:12]}"
        if outcome == "hold":
            approval = ApprovalStatus(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                state="pending",  # type: ignore[arg-type]
                requested_at=_fake_now(),
            )
            return StepOutcome(
                decision=ArmorIQDecision(
                    outcome="hold",  # type: ignore[arg-type]
                    source=DecisionSource.ARMORIQ_POLICY_DECISION,
                    reason="fake hold",
                    decided_at=_fake_now(),
                ),
                approval=approval,
            )
        if outcome == "block":
            return StepOutcome(
                decision=ArmorIQDecision(
                    outcome="block",  # type: ignore[arg-type]
                    source=DecisionSource.ARMORIQ_POLICY_DECISION,
                    reason="fake block",
                    decided_at=_fake_now(),
                )
            )
        return StepOutcome(
            decision=ArmorIQDecision(
                outcome="allow",  # type: ignore[arg-type]
                source=DecisionSource.ARMORIQ_POLICY_DECISION,
                reason="fake allow (offline; never gate evidence)",
                decided_at=_fake_now(),
            ),
            invocation=ToolInvocationResult(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                jti=jti,
                status=ToolExecutionStatus.UNKNOWN,
                tool_server_reached=True,
                result={"fake": True},
            ),
        )

    async def attempt_off_plan(
        self, handle: PlanHandle, tool: ToolName, arguments: Mapping[str, Any], ctx: RequestContext
    ) -> ArmorIQDecision:
        self.calls.append(("attempt_off_plan", tool.value))
        return ArmorIQDecision(
            outcome="block",  # type: ignore[arg-type]
            source=DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
            reason="fake off-plan block (offline)",
            decided_at=_fake_now(),
        )
