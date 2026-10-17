"""Call-counting ArmorIQ gateway double for orchestrator tests (offline only).

Mirrors the real ``ArmorIQGateway`` surface and outcome shapes (P1-02,
``services/authorization/armoriq_gateway.py``): one ``PlanHandle`` per
``start_plan``, arguments read from the plan object passed to
``invoke_step``, HOLDs reported through ``on_hold`` while the wait goes on.
Its decisions carry ArmorIQ labels, so it lives under ``tests/`` only and is
never evidence of an ArmorIQ behaviour.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from services.authorization.armoriq_gateway import PlanHandle, StepOutcome
from services.contracts import (
    ApprovalState,
    ApprovalStatus,
    ArmorIQDecision,
    DecisionOutcome,
    DecisionSource,
    PlanProposal,
    RequestContext,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)


def _now() -> datetime:
    return datetime.now(UTC)


def mcp_payload(result: BaseModel, ledger_entry_id: str) -> dict[str, Any]:
    """The tool server's success ``CallToolResult`` (SHARED_INTERFACES §4)."""
    return {
        "content": [{"type": "text", "text": result.model_dump_json()}],
        "isError": False,
        "_meta": {"ledger_entry_id": ledger_entry_id},
    }


# --- step scripts ----------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    """ALLOW + ledger-backed EXECUTED with ``result`` (``None`` = not_found)."""

    result: BaseModel | None = None
    payload: Any = None  # raw payload override (shape tests)


@dataclass(frozen=True)
class AllowUnknown:
    """ALLOW but no ledger reference surfaced (status ``unknown``)."""

    result: BaseModel | None = None


@dataclass(frozen=True)
class Block:
    source: DecisionSource = DecisionSource.ARMORIQ_POLICY_DECISION
    reason: str = "fake policy block (offline)"


@dataclass(frozen=True)
class ToolServerReject:
    code: str = "invalid_arguments"


@dataclass(frozen=True)
class Unverified:
    reason: str = "fake unverified (offline)"


@dataclass(frozen=True)
class HoldThen:
    """HOLD reported via ``on_hold``, then resolved to ``state`` after ``release``.

    ``threaded=True`` reports the hold from a worker thread, like the SDK.
    """

    state: ApprovalState
    result: BaseModel | None = None
    release: asyncio.Event | None = None
    delegation_id: str = "dlg-fake-1"
    threaded: bool = True


@dataclass(frozen=True)
class HoldPending:
    """A HOLD returned without waiting (two-phase shape; nothing can resume it)."""

    delegation_id: str | None = "dlg-fake-2"


@dataclass(frozen=True)
class Raise:
    exc: BaseException = field(default_factory=lambda: RuntimeError("fake gateway exploded"))


StepScript = (
    Allow | AllowUnknown | Block | ToolServerReject | Unverified | HoldThen | HoldPending | Raise
)


@dataclass(frozen=True)
class StartPlanCall:
    request_id: str
    plan: PlanProposal


@dataclass(frozen=True)
class InvokeCall:
    request_id: str
    plan_id: str
    step_index: int
    tool: ToolName
    plan: PlanProposal
    arguments: dict[str, Any]


@dataclass(frozen=True)
class OffPlanCall:
    request_id: str
    tool: ToolName
    arguments: dict[str, Any]


class ScriptedGateway:
    """Offline gateway: scripted per tool, every call recorded (P3-04, P3-09)."""

    def __init__(
        self,
        steps: Mapping[ToolName, StepScript] | None = None,
        *,
        mint_decision: ArmorIQDecision | None = None,
        mint_raises: BaseException | None = None,
        off_plan_raises: BaseException | None = None,
        after_invoke: Callable[[PlanProposal, int], None] | None = None,
    ) -> None:
        self.steps: dict[ToolName, StepScript] = dict(steps or {})
        self.mint_decision = mint_decision
        self.mint_raises = mint_raises
        self.off_plan_raises = off_plan_raises
        self.after_invoke = after_invoke
        self.start_plan_calls: list[StartPlanCall] = []
        self.invoke_calls: list[InvokeCall] = []
        self.off_plan_calls: list[OffPlanCall] = []
        # E2: a PLANNED name sent to attempt_off_plan passes the SDK check and
        # would reach the proxy and execute. Must stay empty.
        self.off_plan_would_execute: list[str] = []
        self.holds_reported: list[ApprovalStatus] = []
        self.hold_waiting = asyncio.Event()
        self._plans: dict[str, PlanProposal] = {}
        self._ledger = itertools.count(1)

    # -- helpers -------------------------------------------------------------

    def start_plans_for(self, request_id: str) -> int:
        return sum(1 for call in self.start_plan_calls if call.request_id == request_id)

    def _decision(
        self,
        outcome: DecisionOutcome,
        source: DecisionSource,
        reason: str,
        delegation_id: str | None = None,
    ) -> ArmorIQDecision:
        return ArmorIQDecision(
            outcome=outcome,
            source=source,
            reason=reason,
            armoriq_plan_id="fake-armoriq-plan",
            delegation_id=delegation_id,
            decided_at=_now(),
        )

    def _invocation(
        self,
        ctx: RequestContext,
        handle: PlanHandle,
        index: int,
        tool: ToolName,
        jti: str,
        status: ToolExecutionStatus,
        *,
        result: Any = None,
        ledger: str | None = None,
        error_code: str | None = None,
    ) -> ToolInvocationResult:
        reached = status is not ToolExecutionStatus.NOT_REACHED
        return ToolInvocationResult(
            request_id=ctx.request_id,
            plan_id=handle.plan_id,
            step_index=index,
            tool=tool,
            jti=jti,
            status=status,
            tool_server_reached=reached,
            ledger_entry_id=ledger,
            result=result,
            error_code=error_code,
        )

    def _executed(
        self,
        ctx: RequestContext,
        handle: PlanHandle,
        index: int,
        tool: ToolName,
        jti: str,
        result: BaseModel | None,
        payload: Any = None,
    ) -> ToolInvocationResult:
        ledger = f"fake-ledger-{next(self._ledger)}"
        if payload is not None:
            body = payload
        elif result is not None:
            body = mcp_payload(result, ledger)
        else:
            body = None
        return self._invocation(
            ctx,
            handle,
            index,
            tool,
            jti,
            ToolExecutionStatus.EXECUTED,
            result=body,
            ledger=ledger,
            error_code=None if body is not None else "not_found",
        )

    # -- gateway surface -------------------------------------------------------

    async def start_plan(self, ctx: RequestContext, plan: PlanProposal) -> PlanHandle:
        self.start_plan_calls.append(StartPlanCall(ctx.request_id, plan))
        if self.mint_raises is not None:
            raise self.mint_raises
        plan_id = f"p-{uuid.uuid4().hex[:16]}"
        self._plans[plan_id] = plan
        if self.mint_decision is not None:
            return PlanHandle(
                request_id=ctx.request_id,
                plan_id=plan_id,
                member_id=ctx.requester.member_id,
                step_count=len(plan.steps),
                token_expires_at=None,
                decision=self.mint_decision,
            )
        return PlanHandle(
            request_id=ctx.request_id,
            plan_id=plan_id,
            member_id=ctx.requester.member_id,
            step_count=len(plan.steps),
            token_expires_at=_now() + timedelta(seconds=1200),
            armoriq_plan_id="fake-armoriq-plan",
            decision=None,
            _token=object(),
            _email="fake-member@example.test",
        )

    async def invoke_step(
        self,
        handle: PlanHandle,
        step_index: int,
        ctx: RequestContext,
        plan: PlanProposal,
        *,
        on_hold: Callable[[ApprovalStatus], None] | None = None,
    ) -> StepOutcome:
        step = plan.steps[step_index]
        tool = step.tool
        self.invoke_calls.append(
            InvokeCall(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step_index,
                tool=tool,
                plan=plan,
                arguments=copy.deepcopy(dict(step.arguments)),
            )
        )
        try:
            return await self._outcome(handle, step_index, ctx, tool, on_hold)
        finally:
            if self.after_invoke is not None:
                self.after_invoke(plan, step_index)

    async def _outcome(
        self,
        handle: PlanHandle,
        index: int,
        ctx: RequestContext,
        tool: ToolName,
        on_hold: Callable[[ApprovalStatus], None] | None,
    ) -> StepOutcome:
        script = self.steps.get(tool, Allow())
        jti = uuid.uuid4().hex
        allow = self._decision(
            DecisionOutcome.ALLOW,
            DecisionSource.ARMORIQ_POLICY_DECISION,
            "fake allow (offline; never gate evidence)",
        )
        if isinstance(script, Raise):
            raise script.exc
        if isinstance(script, Allow):
            return StepOutcome(
                decision=allow,
                invocation=self._executed(
                    ctx, handle, index, tool, jti, script.result, script.payload
                ),
            )
        if isinstance(script, AllowUnknown):
            body = mcp_payload(script.result, "x") if script.result is not None else None
            if isinstance(body, dict):
                body.pop("_meta")
            return StepOutcome(
                decision=allow,
                invocation=self._invocation(
                    ctx, handle, index, tool, jti, ToolExecutionStatus.UNKNOWN, result=body
                ),
            )
        if isinstance(script, Block):
            return StepOutcome(
                decision=self._decision(DecisionOutcome.BLOCK, script.source, script.reason),
                invocation=self._invocation(
                    ctx, handle, index, tool, jti, ToolExecutionStatus.NOT_REACHED
                ),
            )
        if isinstance(script, ToolServerReject):
            return StepOutcome(
                decision=self._decision(
                    DecisionOutcome.BLOCK,
                    DecisionSource.TOOL_SERVER_RULE,
                    f"tool server rejected the call: {script.code}",
                ),
                invocation=self._invocation(
                    ctx,
                    handle,
                    index,
                    tool,
                    jti,
                    ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
                    ledger=f"fake-ledger-{next(self._ledger)}",
                    error_code=script.code,
                ),
            )
        if isinstance(script, Unverified):
            return StepOutcome(
                decision=self._decision(
                    DecisionOutcome.UNVERIFIED, DecisionSource.UNKNOWN, script.reason
                )
            )
        if isinstance(script, HoldPending):
            approval = ApprovalStatus(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=index,
                tool=tool,
                state=ApprovalState.PENDING,
                delegation_id=script.delegation_id,
                requested_at=_now(),
            )
            return StepOutcome(
                decision=self._decision(
                    DecisionOutcome.HOLD,
                    DecisionSource.ARMORIQ_POLICY_DECISION,
                    "fake hold (offline)",
                    delegation_id=script.delegation_id,
                ),
                approval=approval,
            )
        return await self._hold_then(handle, index, ctx, tool, jti, script, on_hold)

    async def _hold_then(
        self,
        handle: PlanHandle,
        index: int,
        ctx: RequestContext,
        tool: ToolName,
        jti: str,
        script: HoldThen,
        on_hold: Callable[[ApprovalStatus], None] | None,
    ) -> StepOutcome:
        requested_at = _now()
        pending = ApprovalStatus(
            request_id=ctx.request_id,
            plan_id=handle.plan_id,
            step_index=index,
            tool=tool,
            state=ApprovalState.PENDING,
            delegation_id=script.delegation_id,
            requested_at=requested_at,
        )
        self.holds_reported.append(pending)
        if on_hold is not None:
            if script.threaded:
                await asyncio.to_thread(on_hold, pending)
            else:
                on_hold(pending)
        self.hold_waiting.set()
        if script.release is not None:
            await script.release.wait()
        resolved = ApprovalStatus(
            request_id=ctx.request_id,
            plan_id=handle.plan_id,
            step_index=index,
            tool=tool,
            state=script.state,
            delegation_id=script.delegation_id,
            requested_at=requested_at,
            resolved_at=_now(),
        )
        if script.state is ApprovalState.APPROVED:
            return StepOutcome(
                decision=self._decision(
                    DecisionOutcome.ALLOW,
                    DecisionSource.ARMORIQ_POLICY_DECISION,
                    "fake allow after approval (offline)",
                ),
                approval=resolved,
                invocation=self._executed(ctx, handle, index, tool, jti, script.result),
            )
        if script.state in (ApprovalState.REJECTED, ApprovalState.EXPIRED):
            decision = self._decision(
                DecisionOutcome.BLOCK,
                DecisionSource.ARMORIQ_POLICY_DECISION,
                f"hold {script.state.value}: no execution, no disclosure",
                delegation_id=script.delegation_id,
            )
        else:
            decision = self._decision(
                DecisionOutcome.UNVERIFIED,
                DecisionSource.UNKNOWN,
                f"hold {script.state.value}: no execution, no disclosure",
                delegation_id=script.delegation_id,
            )
        return StepOutcome(
            decision=decision,
            approval=resolved,
            invocation=self._invocation(
                ctx, handle, index, tool, jti, ToolExecutionStatus.NOT_REACHED
            ),
        )

    async def attempt_off_plan(
        self,
        handle: PlanHandle,
        tool: ToolName,
        arguments: Mapping[str, Any],
        ctx: RequestContext,
    ) -> ArmorIQDecision:
        self.off_plan_calls.append(OffPlanCall(ctx.request_id, tool, dict(arguments)))
        if self.off_plan_raises is not None:
            raise self.off_plan_raises
        plan = self._plans.get(handle.plan_id)
        if plan is not None and tool in {step.tool for step in plan.steps}:
            self.off_plan_would_execute.append(tool.value)
            return self._decision(
                DecisionOutcome.BLOCK,
                DecisionSource.UNKNOWN,
                "off-plan call was not stopped client-side (would execute: E2)",
            )
        return self._decision(
            DecisionOutcome.BLOCK,
            DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
            "fake off-plan block by the SDK plan check (offline)",
        )


__all__ = [
    "Allow",
    "AllowUnknown",
    "Block",
    "HoldPending",
    "HoldThen",
    "InvokeCall",
    "OffPlanCall",
    "Raise",
    "ScriptedGateway",
    "StartPlanCall",
    "StepScript",
    "ToolServerReject",
    "Unverified",
    "mcp_payload",
]
