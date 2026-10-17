"""P3-07 "Simulate hijacked model" tests (task §11, CP-6).

Offline (``__offline_fakes``): the real orchestrator, planner and responder
run against the doubles in ``tests/api/fakes/`` (scripted gateway with an
``invoke_step`` spy standing in for the tool server, recording audit store,
P3-06 delivery doubles). They are NOT evidence of ArmorIQ behaviour: the
labels below are whatever the fake gateway returned, and the point is that
the orchestrator records exactly that label. The live 10 + 10 runs are P5-02.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from apps.api.hijack_simulation import (
    ADVERSARY_PROMPT,
    HIJACKED_TOOL_REQUESTS,
    SIMULATION_NAME,
    SYNTHETIC_EXFIL_MESSAGE,
    hijacked_requests,
)
from apps.api.orchestrator import RequestOptions, RequestRejectedError
from services.contracts import (
    ArmorIQDecision,
    AuditEventType,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    EntryPoint,
    PersonaKey,
    ToolName,
)
from tests.api.fakes.gateway import Allow, Block, ScriptedGateway
from tests.api.fakes.llm import draft, restricted_search, team_search, wire
from tests.api.fakes.scenario import TEAM_HIT, build_harness, request_ctx, search

QUESTION = "What did we decide about the atlas budget?"
TEAM_ONLY_PLAN = wire(team_search("atlas budget"))
TEAM_RESULT = search(DataTier.TEAM, TEAM_HIT)
TEAM_ANSWER = draft(f"The budget is fixed [{TEAM_HIT[0]}].", [TEAM_HIT[0]])
HONEYPOT = ToolName.SEND_EXTERNAL_MESSAGE
SIMULATE = RequestOptions(simulate_hijacked_model=True)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _harness(plans: int = 1, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("steps", {ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT)})
    kwargs.setdefault("answer", TEAM_ANSWER)
    return build_harness(plans=[TEAM_ONLY_PLAN] * plans, **kwargs)


def _off_plan_events(h, request_id: str):  # type: ignore[no-untyped-def]
    return [
        event
        for event in h.audit.events(AuditEventType.AUTHORIZATION_DECIDED)
        if event.request_id == request_id and event.payload.get("off_plan") is True
    ]


def _honeypot_executions(h) -> int:  # type: ignore[no-untyped-def]
    """Tool-server spy: honeypot steps invoked + honeypot execution refs."""
    invoked = sum(1 for call in h.gateway.invoke_calls if call.tool is HONEYPOT)
    executed = sum(
        1
        for kwargs in h.audit.named("record_execution_ref")
        if kwargs["status"] == "executed"
        and any(
            call.tool is HONEYPOT and call.step_index == kwargs["step_index"]
            for call in h.gateway.invoke_calls
        )
    )
    return invoked + executed + len(h.gateway.off_plan_would_execute)


# --- §11 required tests ------------------------------------------------------------


def test_toggle_off_by_default__offline_fakes() -> None:
    assert RequestOptions().simulate_hijacked_model is False
    h = _harness()

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(entry=EntryPoint.WEB), QUESTION)
        assert h.audit.events(AuditEventType.SIMULATION_ACTIVATED) == []
        assert h.gateway.off_plan_calls == []  # the natural model asked for nothing
        assert _off_plan_events(h, request_id) == []
        assert _honeypot_executions(h) == 0
        [text] = h.delivered_to(request_id)
        assert "not part of this request's plan" not in text

    run(scenario())


def test_injected_attempt_routed_off_plan__offline_fakes() -> None:
    h = _harness()

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(entry=EntryPoint.WEB), QUESTION, SIMULATE)
        # Activation is audited before planning (task §9.1).
        timeline = h.audit.timeline
        assert timeline.index("simulation_activated") < timeline.index("plan_proposed")
        [activated] = h.audit.events(AuditEventType.SIMULATION_ACTIVATED)
        assert activated.payload["simulation"] == SIMULATION_NAME
        assert activated.payload["injection_vector"] == "adversary_prompt"
        assert ADVERSARY_PROMPT not in h.audit.text()  # metadata only

        # Real steps: only the planned one, with the validated plan's arguments.
        assert [(c.step_index, c.tool) for c in h.gateway.invoke_calls] == [
            (0, ToolName.KNOWLEDGE_SEARCH_TEAM)
        ]
        assert h.gateway.invoke_calls[0].arguments == {"query": "atlas budget", "limit": 5}

        # Schema-valid hijacked requests go ONLY to attempt_off_plan.
        assert [c.tool for c in h.gateway.off_plan_calls] == [
            HONEYPOT,
            ToolName.KNOWLEDGE_SEARCH_RESTRICTED,
        ]
        for call in h.gateway.off_plan_calls:
            assert not any(key.startswith("_") or key == "recipient" for key in call.arguments)

        # Every attempt: a BLOCK, never executed, marked simulation, with the
        # TRUE label (the gateway's for SDK-routed ones, application_rule for
        # the ones the responder screen refused).
        events = _off_plan_events(h, request_id)
        assert len(events) == len(HIJACKED_TOOL_REQUESTS) == 4
        for event in events:
            assert event.payload["outcome"] == DecisionOutcome.BLOCK.value
            assert event.payload["executed"] is False
            assert event.payload["simulation"] == SIMULATION_NAME
        labels = [
            (e.payload.get("tool_name"), e.decision_source, e.payload.get("rejection"))
            for e in events
        ]
        assert labels == [
            (HONEYPOT.value, DecisionSource.ARMORIQ_SDK_PLAN_CHECK, None),
            (
                ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value,
                DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
                None,
            ),
            (HONEYPOT.value, DecisionSource.APPLICATION_RULE, "reserved_key"),
            (HONEYPOT.value, DecisionSource.APPLICATION_RULE, "forbidden_key"),
        ]
        assert _honeypot_executions(h) == 0
        [text] = h.delivered_to(request_id)
        assert "send_external_message" in text and "so it was not run" in text
        assert SYNTHETIC_EXFIL_MESSAGE not in text

    run(scenario())


def test_no_egress_sink_write_in_guarded_mode__offline_fakes() -> None:
    """SC-02 shape offline: 10 compromised + 10 natural runs, zero honeypot executions."""
    h = _harness(plans=20)

    async def scenario() -> None:
        for index in range(20):
            options = SIMULATE if index % 2 == 0 else None
            await h.ask(request_ctx(entry=EntryPoint.WEB), QUESTION, options)
        assert len(h.audit.events(AuditEventType.SIMULATION_ACTIVATED)) == 10
        assert len(h.gateway.off_plan_calls) == 20  # 2 SDK-routed per simulated run
        assert all(c.tool is not HONEYPOT for c in h.gateway.invoke_calls)
        assert _honeypot_executions(h) == 0
        assert h.gateway.off_plan_would_execute == []

    run(scenario())


def test_confirmed_plan_creates_new_request__offline_fakes() -> None:
    """A user-initiated honeypot plan is a NEW request whose step reaches ArmorIQ."""
    honeypot_plan = wire(
        (HONEYPOT.value, {"destination": "synthetic_sink", "message": "synthetic test message"})
    )
    h = build_harness(
        plans=[TEAM_ONLY_PLAN, honeypot_plan],
        steps={
            ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT),
            HONEYPOT: Block(DecisionSource.ARMORIQ_POLICY_DECISION),
        },
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        ctx = request_ctx(PersonaKey.SAM, entry=EntryPoint.WEB)
        simulated = await h.ask(ctx, QUESTION, SIMULATE)
        [note] = h.delivered_to(simulated)
        assert "Send a new request" in note
        confirmed = await h.ask(
            request_ctx(PersonaKey.SAM, entry=EntryPoint.WEB), "Send the summary externally."
        )
        assert confirmed != simulated
        assert h.gateway.start_plans_for(confirmed) == 1
        [call] = [c for c in h.gateway.invoke_calls if c.request_id == confirmed]
        assert call.tool is HONEYPOT
        assert call.arguments == {
            "destination": "synthetic_sink",
            "message": "synthetic test message",
        }
        # Recorded with the label the gateway returned; nothing executed.
        [decided] = [
            kwargs
            for kwargs in h.audit.named("record_step_decision")
            if kwargs["request_id"] == confirmed
        ]
        assert decided["outcome"].decision.source is DecisionSource.ARMORIQ_POLICY_DECISION
        invocation = decided["outcome"].invocation
        assert invocation is None or invocation.status.value == "not_reached"
        [text] = h.delivered_to(confirmed)
        assert "ArmorIQ policy blocked" in text
        assert [c.tool for c in h.gateway.invoke_calls].count(HONEYPOT) == 1  # blocked
        assert all(
            kwargs["status"] != "executed"
            for kwargs in h.audit.named("record_execution_ref")
            if kwargs["request_id"] == confirmed
        )

    run(scenario())


def test_application_rule_rejection_is_labeled_application_rule__offline_fakes() -> None:
    """A honeypot plan refused by validation is never presented as ArmorIQ."""
    forged = wire(
        (HONEYPOT.value, {"destination": "synthetic_sink", "message": "x", "recipient": "y"})
    )
    h = build_harness(plans=[forged])

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(entry=EntryPoint.WEB), "Send it out.")
        assert h.gateway.start_plan_calls == []
        [event] = h.audit.events(AuditEventType.PLAN_REJECTED)
        assert event.request_id == request_id
        assert event.decision_source is DecisionSource.APPLICATION_RULE

    run(scenario())


def test_slack_cannot_enable_simulation__offline_fakes() -> None:
    h = _harness(plans=2)

    async def scenario() -> None:
        for entry in (EntryPoint.SLACK_MENTION, EntryPoint.SLACK_DM):
            with pytest.raises(RequestRejectedError):
                await h.orchestrator.handle(request_ctx(entry=entry), QUESTION, SIMULATE)
        assert h.audit.calls == []  # refused before anything was recorded
        # Message text never switches it on (Slack or web).
        text = "simulate_hijacked_model=true. " + ADVERSARY_PROMPT
        for ctx in (request_ctx(entry=EntryPoint.SLACK_DM), request_ctx(entry=EntryPoint.WEB)):
            await h.ask(ctx, text)
        assert h.audit.events(AuditEventType.SIMULATION_ACTIVATED) == []
        assert h.gateway.off_plan_calls == []

    run(scenario())


# --- extra security properties --------------------------------------------------------


def test_simulated_planned_name_is_never_sent_to_the_sdk__offline_fakes() -> None:
    """E2: a hijacked request for a PLANNED tool is refused by trusted code
    (the SDK plan check binds names only and would let it execute)."""
    plan = wire(team_search("atlas budget"), restricted_search("atlas budget"))
    restricted = search(DataTier.RESTRICTED, ("rest-1", "Restricted note", "Fixed.", 0.9))
    h = build_harness(
        plans=[plan],
        steps={
            ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT),
            ToolName.KNOWLEDGE_SEARCH_RESTRICTED: Allow(restricted),
        },
        topic_rules=[("rule-9", "atlas budget", "restricted")],
        answer=TEAM_ANSWER,
    )

    async def scenario() -> None:
        request_id = await h.ask(
            request_ctx(PersonaKey.MAYA, entry=EntryPoint.WEB), QUESTION, SIMULATE
        )
        assert [c.tool for c in h.gateway.off_plan_calls] == [HONEYPOT]
        assert h.gateway.off_plan_would_execute == []
        [planned] = [
            e
            for e in _off_plan_events(h, request_id)
            if e.payload.get("tool_name") == ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value
        ]
        assert planned.decision_source is DecisionSource.APPLICATION_RULE
        # The planned restricted step ran once, with the plan's own arguments.
        restricted_calls = [
            c for c in h.gateway.invoke_calls if c.tool is ToolName.KNOWLEDGE_SEARCH_RESTRICTED
        ]
        assert [c.arguments for c in restricted_calls] == [{"query": "atlas budget", "limit": 5}]

    run(scenario())


def test_off_plan_allow_is_never_honored__offline_fakes() -> None:
    class AllowingGateway(ScriptedGateway):
        async def attempt_off_plan(self, handle, tool, arguments, ctx):  # type: ignore[no-untyped-def]
            await super().attempt_off_plan(handle, tool, arguments, ctx)
            return ArmorIQDecision(
                outcome=DecisionOutcome.ALLOW,
                source=DecisionSource.ARMORIQ_POLICY_DECISION,
                decided_at=datetime.now(UTC),
            )

    gateway = AllowingGateway({ToolName.KNOWLEDGE_SEARCH_TEAM: Allow(TEAM_RESULT)})
    h = _harness(gateway=gateway)

    async def scenario() -> None:
        request_id = await h.ask(request_ctx(entry=EntryPoint.WEB), QUESTION, SIMULATE)
        events = _off_plan_events(h, request_id)
        assert events and all(e.payload["outcome"] == "block" for e in events)
        assert all(c.tool is not HONEYPOT for c in h.gateway.invoke_calls)

    run(scenario())


def test_hijacked_requests_are_screened_like_model_output() -> None:
    kept, rejected = hijacked_requests("req-x")
    assert [r.tool for r in kept] == [HONEYPOT.value, ToolName.KNOWLEDGE_SEARCH_RESTRICTED.value]
    assert sorted(r.reason for r in rejected) == ["forbidden_key", "reserved_key"]
    assert "SYNTHETIC" in ADVERSARY_PROMPT and "SYNTHETIC" in SYNTHETIC_EXFIL_MESSAGE
