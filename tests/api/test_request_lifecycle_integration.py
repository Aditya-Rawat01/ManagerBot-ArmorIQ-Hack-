"""Request lifecycle integration tests (Wave 7 S1, CP-7).

Offline only: the real :class:`~apps.api.orchestrator.Orchestrator` wired to
the doubles in ``tests/api/fakes/`` (scripted ArmorIQ gateway, recording audit
store, P3-06 delivery doubles bound to the real signatures, canned planner
JSON and responder drafts). Nothing here contacts ArmorIQ, Slack, Gemini or a
database, and none of it is evidence about those services.

Scope (``docs/Todos/WAVE7_UI_PLAN.md``, S1) is the lifecycle proofs CP-7
lists, asserted through observable behaviour: what reached the gateway, what
the audit recorded, and what was allowed to leave. The finer-grained unit
breakdown of the same orchestrator lives in ``tests/api/test_orchestrator.py``
(P3-04); this file exercises the flows end to end and pins the delivery
routing and audit ordering the plan requires.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from apps.api.orchestrator import ERROR_MESSAGE
from services.contracts import (
    ApprovalState,
    AuditEventType,
    DataTier,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    ToolName,
)
from tests.api.fakes.gateway import Allow, Block, HoldPending, HoldThen, Raise, StepScript
from tests.api.fakes.llm import conf_search, draft, team_search, wire
from tests.api.fakes.scenario import (
    CONF_HIT,
    CONF_SECRET,
    CONF_TITLE,
    TEAM_HIT,
    build_harness,
    request_ctx,
    search,
)

QUESTION = "What did we decide about the atlas budget?"
RULE = ("rule-7", "atlas budget", "confidential")
TWO_STEP_PLAN = wire(team_search(), conf_search())
TEAM_ONLY_PLAN = wire(team_search())
TEAM_RESULT = search(DataTier.TEAM, TEAM_HIT)
CONF_RESULT = search(DataTier.CONFIDENTIAL, CONF_HIT)
TEAM_ANSWER = draft(f"The budget is fixed [{TEAM_HIT[0]}].", [TEAM_HIT[0]])
CONF_ANSWER = draft(f"Cut plan exists [{CONF_HIT[0]}].", [CONF_HIT[0]])
# The gateway script for a HOLD the manager never resolved in time (P3-04).
EXPIRED_HOLD = HoldThen(ApprovalState.EXPIRED, CONF_RESULT)

# P3-06 routing: a team answer in a mention stays in the shared thread; every
# elevated answer leaves by DM or the private web channel.
PUBLIC_CHANNEL = DeliveryChannel.SLACK_THREAD
PRIVATE_CHANNELS = (DeliveryChannel.SLACK_DM, DeliveryChannel.WEB_PRIVATE)

ALLOW_SEQUENCE = [
    "request_received",
    "plan_proposed",
    "plan_minted",
    "authorization_decided",
    "tool_executed",
    "authorization_decided",
    "tool_executed",
    "answer_generated",
    "delivery_succeeded",
]
# A single-step (team-only) run has no second authorization pair.
TEAM_ONLY_SEQUENCE = [
    "request_received",
    "plan_proposed",
    "plan_minted",
    "authorization_decided",
    "tool_executed",
    "answer_generated",
    "delivery_succeeded",
]
# A blocked step is recorded as not reached (``tool_rejected``), never executed.
BLOCK_SEQUENCE = [
    "request_received",
    "plan_proposed",
    "plan_minted",
    "authorization_decided",
    "tool_rejected",
    "delivery_succeeded",
]
HOLD_SEQUENCE_PREFIX = [
    "request_received",
    "plan_proposed",
    "plan_minted",
    "authorization_decided",
    "tool_executed",
    "hold_created",
    "hold_notified",
    "approval_resolved",
]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def two_step(**scripts: StepScript) -> Mapping[ToolName, StepScript]:
    steps: dict[ToolName, StepScript] = {
        ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT),
        ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Allow(CONF_RESULT),
    }
    steps.update(scripts)
    return steps


# --- 1-2. ALLOW: answered, and delivered on the permitted path ----------------


def test_team_allow_is_answered_and_delivered_to_the_shared_thread__offline() -> None:
    h = build_harness(
        plans=[TEAM_ONLY_PLAN],
        steps=two_step(),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        assert delivered.tier is DataTier.TEAM
        assert delivered.channel is PUBLIC_CHANNEL  # a team answer may be shared
        assert h.delivered_to(request_id) == [TEAM_ANSWER.answer_text]
        assert h.audit.timeline == TEAM_ONLY_SEQUENCE
        [answer] = h.audit.named("record_answer")
        assert answer["abstained"] is False
        assert answer["highest_tier"] is DataTier.TEAM

    run(scenario())


@pytest.mark.parametrize(
    "entry",
    [EntryPoint.SLACK_MENTION, EntryPoint.SLACK_DM, EntryPoint.WEB],
    ids=["mention", "dm", "web"],
)
def test_elevated_data_never_reaches_a_public_channel__offline(entry: EntryPoint) -> None:
    """A confidential tool result forces private delivery whatever the entry is."""
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,  # cites only the team source
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM, entry), QUESTION)
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        assert delivered.tier is DataTier.CONFIDENTIAL
        assert delivered.channel in PRIVATE_CHANNELS
        assert delivered.channel is not PUBLIC_CHANNEL
        [answer] = h.audit.named("record_answer")
        assert answer["highest_tier"] is DataTier.CONFIDENTIAL

    run(scenario())


def test_web_entry_delivers_only_to_the_requesting_member__offline() -> None:
    h = build_harness(
        plans=[TEAM_ONLY_PLAN],
        steps=two_step(),
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        ctx = request_ctx(PersonaKey.PRIYA, EntryPoint.WEB)
        request_id = await h.ask(ctx, QUESTION)
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        assert delivered.channel is DeliveryChannel.WEB_PRIVATE
        assert delivered.ctx.requester.member_id == ctx.requester.member_id
        # No second member is ever named as a recipient of a private answer.
        assert h.delivery.holds == []

    run(scenario())


# --- 3. BLOCK: execution stops, nothing is disclosed --------------------------


def test_block_stops_every_later_step_and_leaks_nothing__offline() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_TEAM: Block()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.PRIYA), QUESTION)
        # The blocked first step stopped the request before the elevated one.
        assert [c.step_index for c in h.gateway.invoke_calls] == [0]
        assert h.generator.calls == []  # no model draft, so nothing to leak
        assert h.audit.timeline == BLOCK_SEQUENCE
        assert h.audit.named("record_answer") == []
        assert "tool_executed" not in h.audit.timeline
        [text] = h.delivered_to(request_id)
        assert "ArmorIQ policy blocked" in text
        assert "armoriq_policy_decision" in text
        assert CONF_SECRET not in text and CONF_TITLE not in text and TEAM_HIT[2] not in text

    run(scenario())


# --- 4. HOLD: data only when the approval really was granted ------------------


@pytest.mark.parametrize(
    ("state", "expect_answer"),
    [
        (ApprovalState.APPROVED, True),
        (ApprovalState.REJECTED, False),
        (ApprovalState.EXPIRED, False),
    ],
    ids=["approved", "rejected", "expired"],
)
def test_hold_outcome_matrix_delivers_only_when_approved__offline(
    state: ApprovalState, expect_answer: bool
) -> None:
    answer = CONF_ANSWER if expect_answer else TEAM_ANSWER
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(state, CONF_RESULT)}),
        topic_rules=[RULE],
        answer=answer,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert h.audit.timeline[:8] == HOLD_SEQUENCE_PREFIX
        states = [kw["approval"].state for kw in h.audit.named("record_approval")]
        assert states == [ApprovalState.PENDING, state]
        assert h.gateway.start_plans_for(request_id) == 1
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        [text] = h.delivered_to(request_id)
        if expect_answer:
            # Approved: the answer citing the confidential source leaves, by DM,
            # at the tier of the data it was built from.
            assert text == CONF_ANSWER.answer_text
            assert delivered.tier is DataTier.CONFIDENTIAL
            assert delivered.channel is DeliveryChannel.SLACK_DM
            [answer] = h.audit.named("record_answer")
            assert answer["highest_tier"] is DataTier.CONFIDENTIAL
            assert [c.source_id for c in answer["citations"]] == [CONF_HIT[0]]
        else:
            # Declined or expired: nothing protected was shared.
            assert CONF_SECRET not in text and CONF_TITLE not in text
            assert "no data was shared" in text
            assert delivered.tier is DataTier.TEAM
            assert h.generator.calls == []

    run(scenario())


def test_hold_notices_reach_the_manager_while_no_content_is_sent__offline() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldPending()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert len(h.delivery.holds) == 1  # Maya is told to review it
        assert h.delivery.holds[0].ctx.request_id == request_id
        # The confidential step never resumed, so no protected answer exists.
        assert h.generator.calls == []
        assert "hold_notified" in h.audit.timeline
        assert "answer_generated" not in h.audit.timeline
        # Nothing protected reached the requester's channel.
        for text in h.delivered_to(request_id):
            assert CONF_SECRET not in text and CONF_TITLE not in text
        assert h.gateway.start_plans_for(request_id) == 1

    run(scenario())


# --- 5. off-plan: recorded, never executed ------------------------------------


def test_off_plan_action_is_routed_to_the_gateway_and_never_executes__offline() -> None:
    answer = draft(
        f"The budget is fixed [{TEAM_HIT[0]}].",
        [TEAM_HIT[0]],
        requested=[("send_external_message", {"destination": "synthetic_sink", "message": "leak"})],
    )
    h = build_harness(plans=[TEAM_ONLY_PLAN], steps=two_step(), answer=answer)

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        # The gateway saw the request for an SDK plan check...
        assert [c.tool for c in h.gateway.off_plan_calls] == [ToolName.SEND_EXTERNAL_MESSAGE]
        # ...but nothing was allowed to execute.
        assert h.gateway.off_plan_would_execute == []
        assert [c.tool for c in h.gateway.invoke_calls] == [ToolName.KNOWLEDGE_SEARCH_TEAM]
        off_plan = [
            e
            for e in h.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
            if e.payload.get("off_plan")
        ]
        assert off_plan and all(e.payload["executed"] is False for e in off_plan)
        assert all(e.payload["outcome"] == "block" for e in off_plan)
        assert any(e.decision_source is DecisionSource.ARMORIQ_SDK_PLAN_CHECK for e in off_plan)
        [text] = h.delivered_to(request_id)
        assert "was not run" in text and "send_external_message" in text

    run(scenario())


# --- 6-7. audit ordering and the one-plan-per-request rule -------------------


@pytest.mark.parametrize("case", ["allow", "block", "hold_approved", "hold_expired"], ids=str)
def test_audit_sequence_is_complete_for_each_lifecycle__offline(case: str) -> None:
    specs: dict[str, tuple[object, object, Sequence[tuple[str, str, str]]]] = {
        "allow": (two_step(), TEAM_ANSWER, [RULE]),
        "block": (two_step(**{ToolName.KNOWLEDGE_SEARCH_TEAM: Block()}), TEAM_ANSWER, [RULE]),
        "hold_approved": (
            two_step(
                **{
                    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(
                        ApprovalState.APPROVED, CONF_RESULT
                    )
                }
            ),
            CONF_ANSWER,
            [RULE],
        ),
        "hold_expired": (
            two_step(
                **{
                    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(
                        ApprovalState.EXPIRED, CONF_RESULT
                    )
                }
            ),
            TEAM_ANSWER,
            [RULE],
        ),
    }
    steps, answer, rules = specs[case]
    h = build_harness(plans=[TWO_STEP_PLAN], steps=steps, topic_rules=list(rules), answer=answer)

    async def scenario() -> None:
        await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        timeline = h.audit.timeline
        expected = {
            "allow": ALLOW_SEQUENCE,
            "block": BLOCK_SEQUENCE,
        }.get(case)
        if expected is not None:
            assert timeline == expected
        else:
            assert timeline[:8] == HOLD_SEQUENCE_PREFIX
            # Every hold still ends in a delivery; only an approved one
            # generates an answer, an expired one is rejected before it.
            assert timeline[-1] == "delivery_succeeded"
            if case == "hold_expired":
                assert "answer_generated" not in timeline
                assert timeline[-2] == "tool_rejected"
            else:
                assert timeline[-2:] == ["answer_generated", "delivery_succeeded"]
        # Every lifecycle records its request, its plan and its delivery.
        for required in ("request_received", "plan_proposed", "plan_minted"):
            assert required in timeline
        assert timeline.index("plan_proposed") < timeline.index("plan_minted")
        # Approval events only appear on a HOLD path.
        assert ("approval_resolved" in timeline) is case.startswith("hold_")

    run(scenario())


@pytest.mark.parametrize(
    ("steps", "answer"),
    [
        (two_step(), TEAM_ANSWER),
        (two_step(**{ToolName.KNOWLEDGE_SEARCH_TEAM: Block()}), TEAM_ANSWER),
        (two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: EXPIRED_HOLD}), TEAM_ANSWER),
        (two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Raise()}), TEAM_ANSWER),
    ],
    ids=["allow", "block", "hold_expired", "gateway_error"],
)
def test_exactly_one_start_plan_per_request__offline(
    steps: Mapping[ToolName, StepScript], answer: object
) -> None:
    h = build_harness(plans=[TWO_STEP_PLAN], steps=steps, topic_rules=[RULE], answer=answer)

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert h.gateway.start_plans_for(request_id) == 1
        assert len(h.gateway.start_plan_calls) == 1
        # A failed or held request is never replanned.
        assert len(h.audit.named("record_plan")) == 1

    run(scenario())


def test_gateway_failure_is_an_error_and_discloses_nothing__offline() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_TEAM: Raise()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]
        assert h.generator.calls == []
        assert h.audit.named("record_answer") == []
        assert TEAM_HIT[2] not in ERROR_MESSAGE and CONF_SECRET not in ERROR_MESSAGE

    run(scenario())
