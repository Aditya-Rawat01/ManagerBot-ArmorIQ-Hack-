"""Request orchestrator tests (P3-04 §11).

Offline tests (``__offline_fakes``) run the real orchestrator, planner and
responder against the doubles in ``tests/api/fakes/`` (scripted gateway,
recording audit store, P3-06 delivery doubles bound to the real signatures,
canned planner JSON and responder drafts). They are NOT evidence of any
ArmorIQ, Slack, Anthropic or database behaviour.

The ``test_live_*`` tests at the end use a LOCAL migrated and seeded database
with the real audit store and real P3-06 delivery, still with the scripted
gateway and fake models: ``DATABASE_URL_BACKEND`` +
``PROXYLEAD_DB_WRITE_TESTS=1`` (they append permanent audit rows; the DSN is
captured at import because the API conftest scrubs settings variables).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.api.orchestrator import (
    ERROR_MESSAGE,
    UNVERIFIED_MESSAGE,
    ConfirmationOffer,
    ConfirmationStore,
    Orchestrator,
    RequestNotRecordedError,
    RequestOptions,
    RequestRejectedError,
    SlackSenderSlot,
    missing_orchestrator_settings,
    plan_fingerprint,
)
from apps.api.planner import EscalationConfirmation, plan_digest
from services.contracts import (
    ApprovalState,
    ArmorIQDecision,
    AuditEventType,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    SaveTeamNoteResult,
    ToolName,
)
from tests.api.fakes.audit import RecordingAudit
from tests.api.fakes.gateway import (
    Allow,
    AllowUnknown,
    Block,
    HoldPending,
    HoldThen,
    Raise,
    ScriptedGateway,
    ToolServerReject,
    Unverified,
)
from tests.api.fakes.llm import GatedModelClient, conf_search, draft, team_search, wire
from tests.api.fakes.scenario import (
    CONF_HIT,
    CONF_SECRET,
    CONF_TITLE,
    TEAM_HIT,
    build_harness,
    orchestrator_settings,
    request_ctx,
    search,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
QUESTION = "What did we decide about the atlas budget?"
RULE = ("rule-7", "atlas budget", "confidential")
TWO_STEP_PLAN = wire(team_search("atlas budget"), conf_search("atlas budget"))
TEAM_ONLY_PLAN = wire(team_search("atlas budget"))
TEAM_RESULT = search(DataTier.TEAM, TEAM_HIT)
CONF_RESULT = search(DataTier.CONFIDENTIAL, CONF_HIT)
TEAM_ANSWER = draft(f"The budget is fixed [{TEAM_HIT[0]}].", [TEAM_HIT[0]])


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def two_step(**scripts):  # type: ignore[no-untyped-def]
    steps = {
        ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT),
        ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Allow(CONF_RESULT),
    }
    steps.update(scripts)
    return steps


# --- §11 required tests --------------------------------------------------------


def test_one_plan_per_request__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN, TWO_STEP_PLAN],
        steps=two_step(),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        first = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        second = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        assert first != second
        assert h.gateway.start_plans_for(first) == 1
        assert h.gateway.start_plans_for(second) == 1
        assert len(h.gateway.start_plan_calls) == 2
        for request_id in (first, second):
            calls = [c for c in h.gateway.invoke_calls if c.request_id == request_id]
            # Steps in order, all under the one minted plan (one plan_id).
            assert [c.step_index for c in calls] == [0, 1]
            assert len({c.plan_id for c in calls}) == 1

    run(scenario())


def test_block_stops_protected_steps__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_TEAM: Block()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.PRIYA), QUESTION)
        assert [c.step_index for c in h.gateway.invoke_calls] == [0]  # step 1 never ran
        assert h.generator.calls == []  # nothing answered
        [text] = h.delivered_to(request_id)
        assert "ArmorIQ policy blocked" in text and "armoriq_policy_decision" in text
        assert CONF_SECRET not in text and TEAM_HIT[2] not in text
        assert h.audit.named("record_answer") == []

    run(scenario())


def test_block_on_elevated_step_returns_no_partial_answer__offline_fakes() -> None:
    """Priya's confidential step is blocked (SC-01): no team-tier partial answer either."""
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Block()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.PRIYA), QUESTION)
        assert [c.step_index for c in h.gateway.invoke_calls] == [0, 1]
        assert h.generator.calls == []
        [text] = h.delivered_to(request_id)
        assert "ArmorIQ policy blocked" in text
        assert TEAM_HIT[2] not in text and CONF_TITLE not in text

    run(scenario())


def test_hold_waits_and_no_replan_on_expiry__offline_fakes() -> None:
    release = asyncio.Event()
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(
            **{
                ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(
                    ApprovalState.EXPIRED, CONF_RESULT, release=release
                )
            }
        ),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.orchestrator.handle(request_ctx(PersonaKey.SAM), QUESTION)
        await asyncio.wait_for(h.gateway.hold_waiting.wait(), 5)
        for _ in range(50):  # the HOLD is recorded and Maya told while the wait runs
            if h.delivery.holds:
                break
            await asyncio.sleep(0.01)
        assert len(h.delivery.holds) == 1
        assert h.delivery.holds[0].approval.state is ApprovalState.PENDING
        assert h.delivery.deliveries == []  # no content before approval
        assert "hold_created" in h.audit.timeline
        release.set()
        await h.orchestrator.wait_idle(5)
        assert h.gateway.start_plans_for(request_id) == 1  # never a new plan
        assert len(h.gateway.start_plan_calls) == 1
        assert h.generator.calls == []
        [text] = h.delivered_to(request_id)
        assert "expired" in text and "new request" in text
        assert CONF_SECRET not in text and TEAM_HIT[2] not in text
        resolved = [kw["approval"] for kw in h.audit.named("record_approval")]
        assert [a.state for a in resolved] == [ApprovalState.PENDING, ApprovalState.EXPIRED]
        timeline = h.audit.timeline
        assert timeline.index("hold_created") < timeline.index("approval_resolved")

    run(scenario())


@pytest.mark.parametrize(
    "state", [ApprovalState.REJECTED, ApprovalState.TIMEOUT], ids=["rejected", "timeout"]
)
def test_hold_not_approved_never_discloses__offline_fakes(state: ApprovalState) -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(state, CONF_RESULT)}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert len(h.gateway.start_plan_calls) == 1
        assert h.generator.calls == []
        [text] = h.delivered_to(request_id)
        assert CONF_SECRET not in text
        assert "new request" in text

    run(scenario())


def test_hold_approved_resumes_same_plan_and_delivers_privately__offline_fakes() -> None:
    answer = draft(f"Cut plan exists [{CONF_HIT[0]}].", [CONF_HIT[0]])
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(
            **{
                ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(
                    ApprovalState.APPROVED, CONF_RESULT
                )
            }
        ),
        topic_rules=[RULE],
        answer=answer,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert len(h.gateway.start_plan_calls) == 1
        assert len({c.plan_id for c in h.gateway.invoke_calls}) == 1
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        assert delivered.tier is DataTier.CONFIDENTIAL
        assert delivered.channel is DeliveryChannel.SLACK_DM
        approvals = [kw["approval"].state for kw in h.audit.named("record_approval")]
        assert approvals == [ApprovalState.PENDING, ApprovalState.APPROVED]
        decided = h.audit.named("record_step_decision")[1]["outcome"]
        assert decided.approval.state is ApprovalState.APPROVED

    run(scenario())


def test_hold_returned_without_wait_is_not_resumed__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldPending()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert len(h.delivery.holds) == 1  # Maya is still told
        states = [kw["approval"].state for kw in h.audit.named("record_approval")]
        assert states == [ApprovalState.PENDING, ApprovalState.INTERRUPTED]
        assert h.generator.calls == []
        [text] = h.delivered_to(request_id)
        assert "could not be verified" in text
        assert len(h.gateway.start_plan_calls) == 1

    run(scenario())


def test_off_plan_requests_never_execute__offline_fakes() -> None:
    answer = draft(
        f"The budget is fixed [{TEAM_HIT[0]}].",
        [TEAM_HIT[0]],
        requested=[
            ("send_external_message", {"destination": "synthetic_sink", "message": "leak it"}),
            ("knowledge_search_team", {"query": "tampered query", "limit": 50}),
        ],
        malformed=["exfiltrate_everything"],
    )
    h = build_harness(plans=[TEAM_ONLY_PLAN], steps=two_step(), answer=answer)

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        # Only the off-plan name reached the SDK plan-check path.
        assert [c.tool for c in h.gateway.off_plan_calls] == [ToolName.SEND_EXTERNAL_MESSAGE]
        assert h.gateway.off_plan_would_execute == []
        # invoke_step ran only the planned step, with the planned arguments.
        assert [(c.step_index, c.arguments) for c in h.gateway.invoke_calls] == [
            (0, {"query": "atlas budget", "limit": 5})
        ]
        off_plan = [
            e
            for e in h.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
            if e.payload.get("off_plan")
        ]
        labels = {(e.payload.get("tool_name"), e.decision_source) for e in off_plan}
        assert labels == {
            ("send_external_message", DecisionSource.ARMORIQ_SDK_PLAN_CHECK),
            ("knowledge_search_team", DecisionSource.APPLICATION_RULE),
            (None, DecisionSource.APPLICATION_RULE),
        }
        assert all(e.payload["executed"] is False for e in off_plan)
        assert all(e.payload["outcome"] == "block" for e in off_plan)
        [text] = h.delivered_to(request_id)
        assert "was not run" in text and "send_external_message" in text
        assert "exfiltrate_everything" not in text

    run(scenario())


def test_delivery_tier_uses_all_returned_data__offline_fakes() -> None:
    """A confidential result returned but NOT cited still forces confidential delivery."""
    h = build_harness(
        plans=[TWO_STEP_PLAN], steps=two_step(), topic_rules=[RULE], answer=TEAM_ANSWER
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        assert delivered.answer.citations == [TEAM_HIT[0]]  # only team cited
        assert delivered.tier is DataTier.CONFIDENTIAL
        assert delivered.channel is DeliveryChannel.SLACK_DM  # never the shared thread
        [answer] = h.audit.named("record_answer")
        assert answer["highest_tier"] is DataTier.CONFIDENTIAL
        assert [(c.source_id, c.tier) for c in answer["citations"]] == [
            (TEAM_HIT[0], DataTier.TEAM)
        ]

    run(scenario())


def test_unanswered_queue_has_no_protected_snippets__offline_fakes() -> None:
    weak_team = search(DataTier.TEAM, ("team-weak", "Weak team note", "barely related", 0.1))
    weak_conf = search(DataTier.CONFIDENTIAL, ("conf-weak", CONF_TITLE, CONF_SECRET, 0.1))
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(
            **{
                ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(weak_team),
                ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Allow(weak_conf),
            }
        ),
        topic_rules=[RULE],
    )

    async def scenario() -> None:
        await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        [queued] = h.audit.named("add_unanswered")
        assert queued["reason"] == "no_evidence"
        assert queued["question"] == QUESTION
        assert queued["safe_refs"] == ["team-weak"]  # team-tier source IDs only
        text = repr(queued)
        for protected in (CONF_SECRET, CONF_TITLE, "conf-weak", "barely related"):
            assert protected not in text
        [answer] = h.audit.named("record_answer")
        assert answer["abstained"] is True and answer["citations"] == []
        assert h.generator.calls == []  # abstained before any model call

    run(scenario())


def test_audit_event_sequence__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN], steps=two_step(), topic_rules=[RULE], answer=TEAM_ANSWER
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        assert h.audit.timeline == [
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
        [plan] = h.audit.named("record_plan")
        plan_id = plan["plan_id"]
        assert plan["chosen_tier"] is DataTier.CONFIDENTIAL
        assert plan["matched_topic_rule_id"] == "rule-7"
        assert plan["escalation_confirmed"] is False
        trace_id = plan["trace_id"]
        for name, kwargs in h.audit.calls:
            if name == "append_event":
                event = kwargs["event"]
                assert (event.request_id, event.trace_id, event.plan_id) == (
                    request_id,
                    trace_id,
                    plan_id,
                )
            elif name != "create_request":
                assert (kwargs["request_id"], kwargs["trace_id"]) == (request_id, trace_id)
        decisions = h.audit.named("record_step_decision")
        assert [(d["plan_id"], d["step_index"]) for d in decisions] == [(plan_id, 0), (plan_id, 1)]
        assert all(
            d["outcome"].decision.source is DecisionSource.ARMORIQ_POLICY_DECISION
            for d in decisions
        )
        refs = h.audit.named("record_execution_ref")
        assert [(r["plan_id"], r["step_index"]) for r in refs] == [(plan_id, 0), (plan_id, 1)]
        assert all(r["jti"] and r["ledger_entry_id"] for r in refs)
        [minted] = h.audit.events(AuditEventType.PLAN_MINTED)
        assert minted.payload["decision_source"] == "armoriq_issuance"

    run(scenario())


def test_gateway_exception_fails_closed__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(**{ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: Raise()}),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        assert h.generator.calls == []
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]
        [error] = [
            e
            for e in h.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
            if e.payload.get("request_outcome") == "error"
        ]
        assert error.decision_source is DecisionSource.UNKNOWN
        assert error.payload["outcome"] == "unverified"
        assert error.payload["error_class"] == "RuntimeError"
        assert h.audit.named("record_answer") == []

    run(scenario())


def test_start_plan_exception_or_failed_mint_fails_closed__offline_fakes() -> None:
    raising = build_harness(
        plans=[TEAM_ONLY_PLAN],
        gateway=ScriptedGateway(mint_raises=RuntimeError("sdk down")),
        answer=TEAM_ANSWER,
    )
    unverified = ArmorIQDecision(
        outcome=DecisionOutcome.UNVERIFIED,
        source=DecisionSource.ARMORIQ_ISSUANCE,
        reason="mint failed",
        decided_at=datetime.now(UTC),
    )
    failed_mint = build_harness(
        plans=[TEAM_ONLY_PLAN],
        gateway=ScriptedGateway(mint_decision=unverified),
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        first = await raising.ask(request_ctx(), QUESTION)
        assert raising.delivered_to(first) == [ERROR_MESSAGE]
        assert raising.gateway.invoke_calls == []
        second = await failed_mint.ask(request_ctx(), QUESTION)
        assert failed_mint.delivered_to(second) == [UNVERIFIED_MESSAGE]
        assert failed_mint.gateway.invoke_calls == []
        [decided] = failed_mint.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
        assert decided.payload["stage"] == "start_plan"
        assert decided.decision_source is DecisionSource.ARMORIQ_ISSUANCE
        assert len(failed_mint.audit.named("record_plan")) == 1

    run(scenario())


def test_audit_write_failure_fails_closed__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
        audit=RecordingAudit(fail={"record_step_decision": 1}),
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        assert [c.step_index for c in h.gateway.invoke_calls] == [0]  # stopped
        assert h.generator.calls == []
        assert h.audit.named("record_answer") == []
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]
        assert TEAM_HIT[2] not in repr(h.delivery.deliveries)

    run(scenario())


def test_unrecorded_request_is_refused__offline_fakes() -> None:
    h = build_harness(plans=[TEAM_ONLY_PLAN], audit=RecordingAudit(fail={"create_request": 1}))

    async def scenario() -> None:
        with pytest.raises(RequestNotRecordedError):
            await h.orchestrator.handle(request_ctx(), QUESTION)
        await h.orchestrator.wait_idle(5)
        assert h.gateway.start_plan_calls == [] and h.delivery.deliveries == []

    run(scenario())


def test_hold_record_failure_never_discloses_even_if_approved__offline_fakes() -> None:
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(
            **{
                ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(
                    ApprovalState.APPROVED, CONF_RESULT
                )
            }
        ),
        topic_rules=[RULE],
        answer=draft(f"x [{CONF_HIT[0]}]", [CONF_HIT[0]]),
        audit=RecordingAudit(fail={"record_approval": 1}),
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert h.generator.calls == []
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]

    run(scenario())


# --- E2 invariant: arguments only from the validated plan --------------------------


def test_arguments_only_from_validated_plan__offline_fakes() -> None:
    """invoke_step must receive the exact validated plan object and its arguments."""
    from apps.api import planner as planner_module

    validated: list[object] = []

    async def recording_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = await planner_module.plan(*args, **kwargs)
        validated.append(result)
        return result

    answer = draft(
        f"ok [{TEAM_HIT[0]}]",
        [TEAM_HIT[0]],
        requested=[("knowledge_search_confidential", {"query": "everything", "limit": 50})],
    )
    h = build_harness(
        plans=[TWO_STEP_PLAN],
        steps=two_step(),
        topic_rules=[RULE],
        answer=answer,
        plan_fn=recording_plan,
    )

    async def scenario() -> None:
        await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        [planned] = validated
        proposal = planned.proposal  # type: ignore[attr-defined]
        [minted] = h.gateway.start_plan_calls
        assert minted.plan is proposal
        assert len(h.gateway.invoke_calls) == len(proposal.steps)
        for call in h.gateway.invoke_calls:
            assert call.plan is proposal  # the exact validated object
            assert call.arguments == proposal.steps[call.step_index].arguments
        # The responder's tool request never reached invoke_step.
        assert all(c.arguments.get("query") != "everything" for c in h.gateway.invoke_calls)

    run(scenario())


def test_plan_argument_drift_stops_before_the_next_step__offline_fakes() -> None:
    def tamper(plan, index):  # type: ignore[no-untyped-def]
        if index == 0:  # any code path patching arguments after validation
            plan.steps[1].arguments["query"] = "tampered after validation"

    h = build_harness(
        plans=[TWO_STEP_PLAN],
        gateway=ScriptedGateway(two_step(), after_invoke=tamper),
        topic_rules=[RULE],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(PersonaKey.MAYA), QUESTION)
        assert [c.step_index for c in h.gateway.invoke_calls] == [0]
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]
        [error] = [
            e
            for e in h.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
            if e.payload.get("request_outcome") == "error"
        ]
        assert error.payload["error_class"] == "PlanIntegrityError"

    run(scenario())


def test_plan_fingerprint_covers_arguments__offline_fakes() -> None:
    from tests.api.audit_fakes import make_plan

    plan = make_plan()
    before = plan_fingerprint(plan)
    plan.steps[0].arguments["query"] = "changed"
    assert plan_fingerprint(plan) != before


# --- other outcomes ------------------------------------------------------------------


def test_tool_server_rejection_is_labeled_and_recorded__offline_fakes() -> None:
    h = build_harness(
        plans=[TEAM_ONLY_PLAN],
        steps={ToolName.KNOWLEDGE_SEARCH_TEAM: ToolServerReject("out_of_scope")},
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(), QUESTION)
        [text] = h.delivered_to(request_id)
        assert "tool_server_rule" in text
        [ref] = h.audit.named("record_execution_ref")
        assert ref["status"].value == "rejected_by_tool_server"
        assert ref["validation_outcome"] == "out_of_scope" and ref["ledger_entry_id"]

    run(scenario())


def test_unverified_and_unledgered_allow_return_nothing__offline_fakes() -> None:
    for script in (Unverified(), AllowUnknown(TEAM_RESULT)):
        h = build_harness(
            plans=[TEAM_ONLY_PLAN],
            steps={ToolName.KNOWLEDGE_SEARCH_TEAM: script},
            answer=TEAM_ANSWER,
        )

        async def scenario(h=h) -> None:  # type: ignore[no-untyped-def]
            request_id = await h.ask(request_ctx(), QUESTION)
            assert h.delivered_to(request_id) == [UNVERIFIED_MESSAGE]
            assert h.generator.calls == []

        run(scenario())


def test_unparsable_or_relabeled_result_fails_closed__offline_fakes() -> None:
    relabeled = search(DataTier.CONFIDENTIAL, CONF_HIT)  # a confidential result for a team step
    for script in (Allow(relabeled), Allow(payload={"content": [{"type": "text", "text": "{"}]})):
        h = build_harness(
            plans=[TEAM_ONLY_PLAN],
            steps={ToolName.KNOWLEDGE_SEARCH_TEAM: script},
            answer=TEAM_ANSWER,
        )

        async def scenario(h=h) -> None:  # type: ignore[no-untyped-def]
            request_id = await h.ask(request_ctx(), QUESTION)
            assert h.delivered_to(request_id) == [ERROR_MESSAGE]
            assert h.generator.calls == []

        run(scenario())


def test_not_found_lookup_is_empty_evidence__offline_fakes() -> None:
    h = build_harness(plans=[TEAM_ONLY_PLAN], steps={ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(None)})

    async def scenario() -> None:
        await h.ask(request_ctx(), QUESTION)
        [ref] = h.audit.named("record_execution_ref")
        assert ref["status"].value == "executed"
        [queued] = h.audit.named("add_unanswered")
        assert queued["reason"] == "no_evidence"

    run(scenario())


def test_responder_error_is_error_not_abstention__offline_fakes() -> None:
    h = build_harness(plans=[TEAM_ONLY_PLAN], steps=two_step(), answer=RuntimeError("model down"))

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(), QUESTION)
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]
        assert h.audit.named("add_unanswered") == []
        assert h.audit.named("record_answer") == []

    run(scenario())


def test_planner_outcomes_are_audited_and_answered__offline_fakes() -> None:
    rejected = build_harness(plans=[wire(conf_search("atlas budget"))])
    clarified = build_harness(plans=[wire()])
    failed = build_harness(plans=[RuntimeError("model down")])

    async def scenario() -> None:
        rid = await rejected.ask(request_ctx(PersonaKey.SAM), QUESTION)
        [event] = rejected.audit.events(AuditEventType.PLAN_REJECTED)
        assert event.payload["reason_code"] == "team_first"
        assert event.payload["decision_source"] == "application_rule"
        assert len(rejected.delivered_to(rid)) == 1
        assert rejected.gateway.start_plan_calls == []

        rid = await clarified.ask(request_ctx(), QUESTION)
        assert clarified.audit.named("record_answer")[0]["reason"] == "clarification"
        assert "rephrase" in clarified.delivered_to(rid)[0]

        rid = await failed.ask(request_ctx(), QUESTION)
        [error] = failed.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
        assert error.payload["request_outcome"] == "error"
        assert "try again" in failed.delivered_to(rid)[0]
        assert failed.gateway.start_plan_calls == []

    run(scenario())


def test_store_this_note_is_confirmed_without_the_responder__offline_fakes() -> None:
    saved = SaveTeamNoteResult(
        tool=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE, record_id="note-42", version=1
    )
    h = build_harness(steps={ToolName.KNOWLEDGE_SAVE_TEAM_NOTE: Allow(saved)})

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(), "store this the deploy freeze moved to Monday")
        [text] = h.delivered_to(request_id)
        assert "note-42" in text and "draft" in text
        assert h.generator.calls == []
        [answer] = h.audit.named("record_answer")
        assert answer["abstained"] is False

    run(scenario())


def test_global_timeout_is_an_error_outcome__offline_fakes() -> None:
    h = build_harness(model=GatedModelClient([TEAM_ONLY_PLAN]), request_timeout_s=0.2)

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(), QUESTION)
        assert h.delivered_to(request_id) == [ERROR_MESSAGE]
        [error] = h.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
        assert error.payload["error_class"] == "TimeoutError"
        assert h.gateway.start_plan_calls == []

    run(scenario())


def test_handle_returns_before_the_plan_runs__offline_fakes() -> None:
    model = GatedModelClient([TEAM_ONLY_PLAN])
    h = build_harness(model=model, steps=two_step(), answer=TEAM_ANSWER)

    async def scenario() -> None:
        # The planner is blocked until released: handle must not wait for it.
        request_id = await asyncio.wait_for(h.orchestrator.handle(request_ctx(), QUESTION), 2)
        assert request_id.startswith("req-")
        assert h.audit.timeline == ["request_received"]  # recorded before returning
        assert model.finished == 0 and h.gateway.start_plan_calls == []
        assert h.delivery.deliveries == []
        model.release.set()
        await h.orchestrator.wait_idle(5)
        assert h.gateway.start_plans_for(request_id) == 1
        assert h.delivered_to(request_id)

    run(scenario())


def test_concurrency_is_bounded__offline_fakes() -> None:
    model = GatedModelClient([TEAM_ONLY_PLAN, TEAM_ONLY_PLAN])
    h = build_harness(model=model, steps=two_step(), answer=TEAM_ANSWER, max_concurrency=1)

    async def scenario() -> None:
        await h.orchestrator.handle(request_ctx(), QUESTION)
        await h.orchestrator.handle(request_ctx(), QUESTION)
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert model.started == 1  # the second request waits for a slot
        model.release.set()
        await h.orchestrator.wait_idle(5)
        assert model.finished == 2 and len(h.delivery.deliveries) == 2

    run(scenario())


def test_web_answer_is_recorded_pending__offline_fakes() -> None:
    h = build_harness(plans=[TEAM_ONLY_PLAN], steps=two_step(), answer=TEAM_ANSWER)

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(entry=EntryPoint.WEB), QUESTION)
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == request_id]
        assert delivered.channel is DeliveryChannel.WEB_PRIVATE
        assert h.audit.timeline[-1] == "delivery_attempted"

    run(scenario())


# --- context and options -----------------------------------------------------------


def test_context_and_options_are_validated__offline_fakes() -> None:
    h = build_harness(plans=[TEAM_ONLY_PLAN])
    slack = request_ctx()
    impostor = request_ctx(PersonaKey.SAM, member_id="m-maya")

    async def scenario() -> None:
        with pytest.raises(RequestRejectedError):
            await h.orchestrator.handle(
                slack, QUESTION, RequestOptions(simulate_hijacked_model=True)
            )
        with pytest.raises(RequestRejectedError):
            await h.orchestrator.handle(
                slack, QUESTION, RequestOptions(confirmation_token="x" * 43)
            )
        with pytest.raises(RequestRejectedError):
            await h.orchestrator.handle(impostor, QUESTION)
        with pytest.raises(RequestRejectedError):
            await h.orchestrator.handle("not a context", QUESTION)  # type: ignore[arg-type]
        with pytest.raises(RequestRejectedError):
            await h.orchestrator.confirm(slack, "x", accept="yes")  # type: ignore[arg-type]
        assert h.audit.calls == []

    run(scenario())


def test_simulation_option_is_inert_for_web__offline_fakes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = build_harness(plans=[TEAM_ONLY_PLAN], steps=two_step(), answer=TEAM_ANSWER)

    async def scenario() -> None:
        with caplog.at_level(logging.INFO, logger="apps.api.orchestrator"):
            request_id = await h.ask(
                request_ctx(entry=EntryPoint.WEB),
                QUESTION,
                RequestOptions(simulate_hijacked_model=True),
            )
        assert "inert until P3-07" in caplog.text
        assert h.gateway.off_plan_calls == []
        assert h.delivered_to(request_id)

    run(scenario())


def test_slack_handle_signature_and_dispatch_compatibility__offline_fakes() -> None:
    from contextlib import contextmanager

    from services.slack_socket.events import IncomingSlackEvent, dispatch

    class _Db:
        def __init__(self) -> None:
            self.statements: list[str] = []

        @contextmanager
        def tx(self):  # type: ignore[no-untyped-def]
            db = self

            class _Cursor:
                def execute(self, sql: str, params: object = None) -> None:
                    db.statements.append(sql)

            yield _Cursor()

    h = build_harness(plans=[TEAM_ONLY_PLAN], steps=two_step(), answer=TEAM_ANSWER)
    inspect.signature(h.orchestrator.handle).bind(request_ctx(), QUESTION)

    async def scenario() -> None:
        ctx = request_ctx()
        incoming = IncomingSlackEvent(
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            ctx=ctx,
            message=QUESTION,
            store_this_content=None,
            event_id=ctx.slack_event_id or "",
        )
        db = _Db()
        request_id = await dispatch(h.orchestrator.handle, incoming, db)  # type: ignore[arg-type]
        await h.orchestrator.wait_idle(5)
        assert request_id.startswith("req-") and request_id != ctx.request_id
        assert any("processed" in sql for sql in db.statements)
        assert h.delivered_to(request_id)

    run(scenario())


# --- D-14 confirmations ----------------------------------------------------------


ESCALATION_PLAN = wire(team_search("atlas budget"), conf_search("atlas budget"))


def _escalated(**kwargs):  # type: ignore[no-untyped-def]
    """A harness whose first request escalates (no topic rule covers it)."""
    return build_harness(
        plans=[ESCALATION_PLAN],
        steps=two_step(),
        answer=TEAM_ANSWER,
        **kwargs,
    )


async def _offer(h, ctx=None):  # type: ignore[no-untyped-def]
    ctx = ctx or request_ctx(PersonaKey.SAM)
    original = await h.ask(ctx, QUESTION)
    [(_, offer)] = h.presented
    return original, offer, ctx


def test_escalation_mints_confirmation_and_presents_offer__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        original, offer, _ = await _offer(h)
        assert h.gateway.start_plan_calls == []  # nothing authorized yet
        [created] = h.audit.events(AuditEventType.ESCALATION_CREATED)
        assert created.request_id == original
        assert set(created.payload) == {"escalation_id", "requested_tier", "plan_digest"}
        assert created.payload["escalation_id"] == offer.escalation_id
        assert created.payload["requested_tier"] == "confidential"
        assert offer.requested_tier is DataTier.CONFIDENTIAL
        assert offer.expires_at - datetime.now(UTC) <= timedelta(seconds=300)
        [prompt] = h.delivered_to(original)
        assert "confirmation" in prompt and offer.token not in prompt
        assert offer.token not in repr(offer)

    run(scenario())


def test_confirmation_accepted_runs_a_new_plan_without_model_call__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        original, offer, _ = await _offer(h)
        prompts_before = len(h.model.prompts)  # type: ignore[attr-defined]
        confirmed = await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        assert confirmed != original
        assert len(h.model.prompts) == prompts_before  # type: ignore[attr-defined]
        [resolved] = h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)
        assert resolved.request_id == confirmed
        assert resolved.payload == {
            "confirmation_state": "accepted",
            "escalation_id": offer.escalation_id,
        }
        [plan] = h.audit.named("record_plan")
        assert plan["request_id"] == confirmed and plan["escalation_confirmed"] is True
        assert h.gateway.start_plans_for(confirmed) == 1
        assert h.gateway.start_plans_for(original) == 0
        [delivered] = [d for d in h.delivery.deliveries if d.ctx.request_id == confirmed]
        assert delivered.tier is DataTier.CONFIDENTIAL

    run(scenario())


def test_confirmation_is_single_use__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        replay = await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        states = [
            e.payload["confirmation_state"]
            for e in h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)
        ]
        assert states == ["accepted", "replayed"]
        assert h.gateway.start_plans_for(replay) == 0
        assert len(h.gateway.start_plan_calls) == 1

    run(scenario())


def test_concurrent_confirmations_run_once__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        await asyncio.gather(
            h.orchestrator.confirm(request_ctx(PersonaKey.SAM), offer.token, True),
            h.orchestrator.confirm(request_ctx(PersonaKey.SAM), offer.token, True),
        )
        await h.orchestrator.wait_idle(5)
        states = sorted(
            e.payload["confirmation_state"]
            for e in h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)
        )
        assert states == ["accepted", "replayed"]
        assert len(h.gateway.start_plan_calls) == 1

    run(scenario())


def test_confirmation_expires__offline_fakes() -> None:
    now = [datetime.now(UTC)]
    store = ConfirmationStore(clock=lambda: now[0])
    h = _escalated(confirmations=store)

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        now[0] += timedelta(seconds=301)
        await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        [resolved] = h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)
        assert resolved.payload["confirmation_state"] == "expired"
        assert h.gateway.start_plan_calls == []

    run(scenario())


def test_confirmation_bound_to_member_workspace_and_channel__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        await h.confirm(request_ctx(PersonaKey.MAYA), offer.token)  # another member
        await h.confirm(request_ctx(PersonaKey.SAM, channel="C2"), offer.token)  # other channel
        await h.confirm(request_ctx(PersonaKey.SAM, entry=EntryPoint.WEB), offer.token)
        await h.confirm(request_ctx(PersonaKey.SAM, workspace_id="ws-other"), offer.token)
        states = [
            e.payload["confirmation_state"]
            for e in h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)
        ]
        assert states == ["mismatch"] * 4
        assert h.gateway.start_plan_calls == []
        # A mismatch never spends the owner's confirmation.
        await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        assert (
            h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)[-1].payload["confirmation_state"]
            == "accepted"
        )

    run(scenario())


def test_verifier_checks_every_binding__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        store = h.orchestrator.confirmations
        record = next(iter(store._by_hash.values()))
        escalation = record.escalation
        digest = escalation.plan_digest
        conf = EscalationConfirmation(tier=DataTier.CONFIDENTIAL, token=offer.token)
        same = request_ctx(PersonaKey.SAM)
        assert digest == plan_digest(same, escalation.proposal)
        verify = h.orchestrator.confirmation_verifier
        assert await verify(conf, request_ctx(PersonaKey.MAYA), digest) is False
        assert await verify(conf, request_ctx(PersonaKey.SAM, workspace_id="w2"), digest) is False
        wrong_tier = EscalationConfirmation(tier=DataTier.RESTRICTED, token=offer.token)
        assert await verify(wrong_tier, same, digest) is False
        assert await verify(conf, same, "0" * 64) is False
        bogus = EscalationConfirmation(tier=DataTier.CONFIDENTIAL, token="A" * 43)
        assert await verify(bogus, same, digest) is False
        assert await verify(conf, same, digest) is True  # finally the right one
        assert await verify(conf, same, digest) is False  # and only once
        assert record.consumed is True

    run(scenario())


def test_verifier_exception_fails_closed__offline_fakes() -> None:
    def broken_clock() -> datetime:
        raise RuntimeError("clock exploded")

    h = _escalated()

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        store = h.orchestrator.confirmations
        record = next(iter(store._by_hash.values()))
        store._clock = broken_clock
        conf = EscalationConfirmation(tier=DataTier.CONFIDENTIAL, token=offer.token)
        verdict = await store.verify(conf, request_ctx(PersonaKey.SAM), record.plan_digest)
        assert verdict is False
        assert record.consumed is False

    run(scenario())


def test_new_confirmation_supersedes_old__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        original, first, ctx = await _offer(h)
        store = h.orchestrator.confirmations
        record = next(iter(store._by_hash.values()))
        original_ctx = ctx.model_copy(update={"request_id": original})
        second = await store.mint(original_ctx, record.escalation, QUESTION)
        assert second.escalation_id != first.escalation_id
        await h.confirm(request_ctx(PersonaKey.SAM), first.token)
        assert h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)[-1].payload == {
            "confirmation_state": "unbound",
            "escalation_id": first.escalation_id,
        }
        await h.confirm(request_ctx(PersonaKey.SAM), second.token)
        assert (
            h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)[-1].payload["confirmation_state"]
            == "accepted"
        )

    run(scenario())


def test_declined_and_invalid_confirmations__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        _, offer, _ = await _offer(h)
        declined = await h.confirm(request_ctx(PersonaKey.SAM), offer.token, accept=False)
        assert h.delivered_to(declined) == ["OK, I won't run that lookup."]
        await h.confirm(request_ctx(PersonaKey.SAM), offer.token)  # declined = spent
        await h.confirm(request_ctx(PersonaKey.SAM), "not-a-token")
        payloads = [e.payload for e in h.audit.events(AuditEventType.CONFIRMATION_RESOLVED)]
        assert payloads == [
            {"confirmation_state": "rejected", "escalation_id": offer.escalation_id},
            {"confirmation_state": "replayed", "escalation_id": offer.escalation_id},
            {"confirmation_state": "invalid"},
        ]
        assert h.gateway.start_plan_calls == []

    run(scenario())


def test_web_confirmation_through_request_options__offline_fakes() -> None:
    h = _escalated()

    async def scenario() -> None:
        web = request_ctx(PersonaKey.SAM, entry=EntryPoint.WEB)
        _, offer, _ = await _offer(h, web)
        confirmed = await h.ask(
            request_ctx(PersonaKey.SAM, entry=EntryPoint.WEB),
            "ignored",
            RequestOptions(confirmation_token=offer.token),
        )
        assert h.gateway.start_plans_for(confirmed) == 1
        assert offer.token not in repr(RequestOptions(confirmation_token=offer.token))

    run(scenario())


def test_confirmation_token_never_in_audit_or_logs__offline_fakes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = _escalated()

    async def scenario() -> ConfirmationOffer:
        _, offer, _ = await _offer(h)
        await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        await h.confirm(request_ctx(PersonaKey.SAM), offer.token)
        await h.confirm(request_ctx(PersonaKey.MAYA), offer.token)
        return offer

    with caplog.at_level(logging.DEBUG):
        offer = run(scenario())
    token = offer.token
    assert token not in caplog.text
    assert token not in h.audit.text()
    assert all(token not in text for text in h.delivery.texts())
    store = h.orchestrator.confirmations
    assert all(token not in repr(record) for record in store._by_hash.values())
    assert token not in repr(store._by_hash)  # only its sha256 is a key


def test_confirmation_store_bounds__offline_fakes() -> None:
    assert ConfirmationStore()._ttl == timedelta(seconds=300)
    ConfirmationStore(ttl_s=600)
    for bad in (0, 601, True, 1.5):
        with pytest.raises(ValueError):
            ConfirmationStore(ttl_s=bad)  # type: ignore[arg-type]


def test_presenter_failure_revokes_the_offer__offline_fakes() -> None:
    h = _escalated(present=False)

    def broken(ctx, offer):  # type: ignore[no-untyped-def]
        raise RuntimeError("button render failed")

    h.orchestrator.set_confirmation_presenter(EntryPoint.SLACK_MENTION, broken)

    async def scenario() -> None:
        await h.ask(request_ctx(PersonaKey.SAM), QUESTION)
        assert len(h.orchestrator.confirmations) == 0

    run(scenario())


# --- gateway HOLD observer (P1-02 hook used by P3-04) ------------------------------


def test_gateway_reports_hold_while_waiting__offline_fake_sdk() -> None:
    from tests.api.test_armoriq_gateway import _ctx, _plan, _user
    from tests.api.test_armoriq_gateway import _gateway as make_gateway

    seen = []
    gateway, _ = make_gateway("hold_then_allow")
    ctx = _ctx(_user(PersonaKey.SAM, "m-sam"))
    plan = _plan(ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL)

    async def scenario():  # type: ignore[no-untyped-def]
        handle = await gateway.start_plan(ctx, plan)
        return handle, await gateway.invoke_step(handle, 0, ctx, plan, on_hold=seen.append)

    handle, outcome = run(scenario())
    [pending] = seen
    assert pending.state is ApprovalState.PENDING
    assert (pending.delegation_id, pending.plan_id, pending.step_index) == (
        "dlg-9",
        handle.plan_id,
        0,
    )
    assert outcome.decision.outcome is DecisionOutcome.ALLOW
    assert outcome.approval is not None and outcome.approval.state is ApprovalState.APPROVED

    def explode(approval):  # type: ignore[no-untyped-def]
        raise RuntimeError("observer failed")

    async def failing():  # type: ignore[no-untyped-def]
        handle = await gateway.start_plan(ctx, plan)
        return await gateway.invoke_step(handle, 0, ctx, plan, on_hold=explode)

    outcome = run(failing())
    assert outcome.decision.outcome is DecisionOutcome.ALLOW  # unaffected


# --- static guard ------------------------------------------------------------------


def test_orchestrator_reads_no_protected_content__offline_fakes() -> None:
    from tests.api.audit_fakes import protected_name_offenders

    source = (REPO_ROOT / "apps" / "api" / "orchestrator.py").read_text(encoding="utf-8")
    assert protected_name_offenders(source.splitlines()) == []
    for forbidden in (".tx(", "SELECT ", "INSERT ", "import psycopg", "armoriq_sdk"):
        assert forbidden not in source


# --- main.py wiring ----------------------------------------------------------------


def _no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def refuse(self, address):  # type: ignore[no-untyped-def]
        attempts.append(address)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def test_main_leaves_handle_unset_without_config__offline_fakes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from tests.api.conftest import make_settings

    attempts = _no_network(monkeypatch)
    monkeypatch.setattr("apps.api.main.configure_logging", lambda level: None)
    partial = make_settings(ctx_secret="ctx-offline-fake-secret-0000")
    app = create_app(partial)
    with caplog.at_level(logging.INFO, logger="apps.api.main"), TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert getattr(app.state, "orchestrator_handle", None) is None
        assert getattr(app.state, "orchestrator", None) is None
    assert "ARMORIQ_API_KEY" in caplog.text and "ANTHROPIC_API_KEY" in caplog.text
    assert "CTX_SECRET" not in caplog.text  # configured settings are not listed
    assert "ctx-offline-fake-secret" not in caplog.text
    assert attempts == []
    assert missing_orchestrator_settings(orchestrator_settings()) == []


def test_main_sets_handle_with_config_and_no_network__offline_fakes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from tests.api.fakes.scenario import FAKE_SECRETS

    attempts = _no_network(monkeypatch)
    monkeypatch.setattr("apps.api.main.configure_logging", lambda level: None)
    app = create_app(orchestrator_settings())
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        assert client.get("/health").status_code == 200
        orchestrator = app.state.orchestrator
        assert isinstance(orchestrator, Orchestrator)
        assert app.state.orchestrator_handle == orchestrator.handle
        from services.authorization.armoriq_gateway import ArmorIQGateway

        assert isinstance(orchestrator._gateway, ArmorIQGateway)
    assert attempts == []  # nothing connected at startup or shutdown
    for value in FAKE_SECRETS.values():
        assert value not in caplog.text


def test_main_binds_the_running_slack_sender__offline_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from fastapi.testclient import TestClient

    import services.slack_socket.app as slack_app
    from apps.api.main import create_app
    from tests.api.fakes.slack import RecordingSlackSender

    _no_network(monkeypatch)
    sender = RecordingSlackSender()
    captured: dict[str, object] = {}

    class _Handler:
        def disconnect(self) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_start(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return slack_app.SlackRuntime(
            handler=_Handler(), thread=threading.Thread(target=lambda: None), sender=sender
        )

    monkeypatch.setattr(slack_app, "start_slack_socket", fake_start)
    monkeypatch.setattr("services.slack_socket.events.resolve_workspace_id", lambda db: "ws-1")
    monkeypatch.setattr("apps.api.main.configure_logging", lambda level: None)
    settings = orchestrator_settings(
        slack_bot_token="xoxb-offline-fake-token",
        slack_app_token="xapp-offline-fake-token",
        slack_team_id="T1",
        slack_allowed_channels="C1",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        orchestrator = app.state.orchestrator
        assert captured["handle"] == orchestrator.handle
        assert captured["db"] is orchestrator._db  # one shared backend pool
        assert orchestrator._sender_provider() is sender
    assert orchestrator._sender_provider() is None  # cleared at shutdown


def test_sender_slot__offline_fakes() -> None:
    slot = SlackSenderSlot()
    assert slot() is None
    marker = object()
    slot.set(marker)  # type: ignore[arg-type]
    assert slot() is marker
    slot.clear()
    assert slot() is None


# --- live (local database only): real audit store + real P3-06 delivery --------------

LIVE_DSN = os.environ.get("DATABASE_URL_BACKEND")
DB_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
needs_live_write = pytest.mark.skipif(
    not (LIVE_DSN and DB_WRITE_TESTS),
    reason="needs DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1 "
    "(writes permanent append-only audit rows: local database only)",
)


def _live_orchestrator(db, gateway, answer, plans, sender):  # type: ignore[no-untyped-def]
    from apps.api.identity import build_member_table
    from tests.api.fakes.llm import DraftRecorder, FakeModelClient

    settings = orchestrator_settings()
    orchestrator = Orchestrator(
        settings=settings,
        gateway=gateway,
        db=db,
        plan_model=FakeModelClient(plans),
        members=build_member_table(settings),
        generate=DraftRecorder(answer),
        sender_provider=lambda: sender,
    )
    presented: list[ConfirmationOffer] = []
    for entry in EntryPoint:
        orchestrator.set_confirmation_presenter(entry, lambda ctx, offer: presented.append(offer))
    return orchestrator, presented


def _live_events(db, request_id):  # type: ignore[no-untyped-def]
    with db.tx() as cursor:
        cursor.execute(
            "SELECT event_type FROM request_events WHERE request_id = %s ORDER BY id",
            [request_id],
        )
        request_events = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            "SELECT event_type, decision_source, step_index, jti FROM decision_events "
            "WHERE request_id = %s ORDER BY id",
            [request_id],
        )
        decision_events = [tuple(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT delivery_channel, delivery_status FROM delivery_events "
            "WHERE request_id = %s ORDER BY id",
            [request_id],
        )
        deliveries = [tuple(row) for row in cursor.fetchall()]
    return request_events, decision_events, deliveries


@needs_live_write
def test_live_audit_sequence_with_fake_gateway() -> None:
    from apps.api.core.db import BackendDatabase
    from services.audit.queries import request_status
    from tests.api.fakes.slack import RecordingSlackSender

    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    sender = RecordingSlackSender()
    gateway = ScriptedGateway({ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT)})
    orchestrator, _ = _live_orchestrator(db, gateway, TEAM_ANSWER, [TEAM_ONLY_PLAN], sender)

    async def scenario() -> str:
        request_id = await orchestrator.handle(request_ctx(PersonaKey.SAM), QUESTION)
        await orchestrator.wait_idle(30)
        return request_id

    try:
        request_id = run(scenario())
        request_events, decision_events, deliveries = _live_events(db, request_id)
        assert request_events == [
            "request_received",
            "plan_proposed",
            "plan_minted",
            "answer_generated",
            "delivery_attempted",
            "delivery_succeeded",
        ]
        assert [(e[0], e[1], e[2]) for e in decision_events] == [
            ("authorization_decided", "armoriq_policy_decision", 0),
            ("tool_executed", None, 0),
        ]
        assert decision_events[0][3] == decision_events[1][3]  # same jti
        assert deliveries == [("slack_thread", "pending"), ("slack_thread", "delivered")]
        assert sender.thread_posts and TEAM_HIT[0] in sender.thread_posts[0][2]
        assert request_status(db, request_id, "m-sam").delivered is True
    finally:
        db.close()


@needs_live_write
def test_live_confirmed_escalation_with_hold_sequence() -> None:
    from apps.api.core.db import BackendDatabase
    from apps.api.delivery import GENERIC_THREAD_NOTICE
    from tests.api.fakes.slack import RecordingSlackSender

    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    sender = RecordingSlackSender()
    gateway = ScriptedGateway(
        two_step(
            **{
                ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: HoldThen(
                    ApprovalState.APPROVED, CONF_RESULT
                )
            }
        )
    )
    answer = draft(f"Cut plan [{CONF_HIT[0]}].", [CONF_HIT[0]])
    orchestrator, presented = _live_orchestrator(db, gateway, answer, [ESCALATION_PLAN], sender)
    tag = uuid.uuid4().hex[:6]
    question = f"What did we decide about the atlas budget {tag}?"

    async def scenario() -> tuple[str, str]:
        original = await orchestrator.handle(request_ctx(PersonaKey.SAM), question)
        await orchestrator.wait_idle(30)
        [offer] = presented
        confirmed = await orchestrator.confirm(request_ctx(PersonaKey.SAM), offer.token, True)
        await orchestrator.wait_idle(30)
        return original, confirmed

    try:
        original, confirmed = run(scenario())
        first, _, first_deliveries = _live_events(db, original)
        assert first == [
            "request_received",
            "escalation_created",
            "delivery_attempted",
            "delivery_succeeded",
        ]
        assert first_deliveries == [("slack_thread", "pending"), ("slack_thread", "delivered")]
        events, decisions, deliveries = _live_events(db, confirmed)
        assert events[:4] == [
            "request_received",
            "confirmation_resolved",
            "plan_proposed",
            "plan_minted",
        ]
        assert "hold_created" in events and "approval_resolved" in events
        held, resolved = events.index("hold_created"), events.index("approval_resolved")
        # Maya's HOLD DM is audited (delivery_attempted) while the hold is pending.
        assert held < events.index("delivery_attempted") < resolved
        # The answer (DM) is recorded after the approval, then the thread notice.
        answered = events.index("answer_generated")
        assert resolved < answered < events.index("delivery_succeeded")
        assert [d[0] for d in decisions] == [
            "authorization_decided",
            "tool_executed",
            "authorization_decided",
            "tool_executed",
        ]
        assert deliveries == [("slack_dm", "pending"), ("slack_dm", "delivered")]
        assert all(CONF_SECRET not in text for _, _, text in sender.thread_posts)
        assert GENERIC_THREAD_NOTICE in [text for _, _, text in sender.thread_posts]
        assert any(user == "U-maya" for user, _ in sender.dms)  # HOLD notice to Maya
        assert any(user == "U-sam" and CONF_HIT[0] in text for user, text in sender.dms)
        with db.tx() as cursor:
            cursor.execute(
                "SELECT payload FROM request_events WHERE request_id = %s "
                "AND event_type = 'plan_proposed'",
                [confirmed],
            )
            [(payload,)] = cursor.fetchall()
            cursor.execute(
                "SELECT ar.state, ar.delegation_id FROM approval_requests ar "
                "JOIN plans p ON p.id = ar.plan_id WHERE p.request_id = %s",
                [confirmed],
            )
            approvals = cursor.fetchall()
        assert payload["escalation_confirmed"] is True
        assert [tuple(row) for row in approvals] == [("approved", "dlg-fake-1")]
    finally:
        db.close()


@needs_live_write
def test_live_block_records_no_answer() -> None:
    from apps.api.core.db import BackendDatabase
    from tests.api.fakes.slack import RecordingSlackSender

    db = BackendDatabase(LIVE_DSN)  # type: ignore[arg-type]
    sender = RecordingSlackSender()
    gateway = ScriptedGateway({ToolName.KNOWLEDGE_SEARCH_TEAM: Block()})
    orchestrator, _ = _live_orchestrator(db, gateway, TEAM_ANSWER, [TEAM_ONLY_PLAN], sender)

    async def scenario() -> str:
        request_id = await orchestrator.handle(request_ctx(PersonaKey.PRIYA), QUESTION)
        await orchestrator.wait_idle(30)
        return request_id

    try:
        request_id = run(scenario())
        events, decisions, _ = _live_events(db, request_id)
        assert "answer_generated" not in events and "answer_abstained" not in events
        assert [(d[0], d[1]) for d in decisions] == [
            ("authorization_decided", "armoriq_policy_decision"),
            ("tool_rejected", None),
        ]
        assert "ArmorIQ policy blocked" in sender.thread_posts[0][2]
    finally:
        db.close()
