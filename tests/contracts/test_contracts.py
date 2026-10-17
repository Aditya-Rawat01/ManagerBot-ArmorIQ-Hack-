from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from services.contracts import (
    ARMORIQ_POLICY_SOURCES,
    TOOL_CATALOG,
    AppRole,
    ApprovalState,
    ApprovalStatus,
    ArmorIQDecision,
    AuditEventType,
    AuthenticatedUser,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    EntryPoint,
    KnowledgeSearchHit,
    KnowledgeSearchResult,
    McpEndpoint,
    PersonaKey,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    ReviewStatus,
    SourceCitation,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)
from services.contracts.schema_export import build_schema
from services.contracts.schema_export import main as schema_main

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        member_id="m-priya",
        persona=PersonaKey.PRIYA,
        display_name="Priya",
        app_role=AppRole.JUNIOR,
        masked_email="p***@example.com",
    )


def _call(index: int, tool: ToolName, **arguments: object) -> PlannedToolCall:
    return PlannedToolCall(
        step_index=index,
        mcp=TOOL_CATALOG[tool].endpoint,
        tool=tool,
        arguments=arguments,
        purpose="test",
    )


# --- tool catalog -----------------------------------------------------------


def test_catalog_matches_prd_tool_list() -> None:
    assert set(TOOL_CATALOG) == set(ToolName)
    assert len(TOOL_CATALOG) == 14
    workspace = {
        name for name, spec in TOOL_CATALOG.items() if spec.endpoint is McpEndpoint.WORKSPACE
    }
    assert workspace == {ToolName.CREATE_FOLLOWUP_TASK, ToolName.SEND_EXTERNAL_MESSAGE}


@pytest.mark.parametrize("tier", list(DataTier))
def test_tier_is_encoded_in_tool_name(tier: DataTier) -> None:
    for name, spec in TOOL_CATALOG.items():
        if spec.tier is tier:
            assert name.value.endswith(f"_{tier.value}")


# --- plans ------------------------------------------------------------------


def test_plan_allows_team_search_plus_one_elevated_call() -> None:
    plan = PlanProposal(
        summary="budget question",
        steps=[
            _call(0, ToolName.KNOWLEDGE_SEARCH_TEAM, query="atlas budget"),
            _call(1, ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, query="atlas budget"),
        ],
    )
    assert len(plan.steps) == 2


def test_plan_rejects_two_elevated_calls() -> None:
    with pytest.raises(ValidationError, match="at most one elevated-tier"):
        PlanProposal(
            summary="two holds",
            steps=[
                _call(0, ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, query="x"),
                _call(1, ToolName.READ_SOURCE_CONFIDENTIAL, source_id="s1"),
            ],
        )


def test_plan_rejects_out_of_order_steps() -> None:
    with pytest.raises(ValidationError, match=r"0..n-1"):
        PlanProposal(
            summary="bad order", steps=[_call(1, ToolName.KNOWLEDGE_SEARCH_TEAM, query="x")]
        )


@pytest.mark.parametrize("key", ["tier", "user_email", "recipient", "Role", "_ctx", "_approvals"])
def test_planned_call_rejects_model_chosen_identity_tier_or_control_keys(key: str) -> None:
    with pytest.raises(ValidationError):
        _call(0, ToolName.KNOWLEDGE_SEARCH_TEAM, **{key: "x"})


def test_planned_call_rejects_wrong_endpoint() -> None:
    with pytest.raises(ValidationError, match="served by"):
        PlannedToolCall(
            step_index=0,
            mcp=McpEndpoint.WORKSPACE,
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
            purpose="wrong endpoint",
        )


# --- decisions & approvals --------------------------------------------------


def test_sdk_plan_check_can_only_block() -> None:
    with pytest.raises(ValidationError, match="only produce BLOCK"):
        ArmorIQDecision(
            outcome=DecisionOutcome.ALLOW,
            source=DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
            decided_at=NOW,
        )


@pytest.mark.parametrize(
    "source", [DecisionSource.APPLICATION_RULE, DecisionSource.TOOL_SERVER_RULE]
)
def test_only_armoriq_can_hold(source: DecisionSource) -> None:
    with pytest.raises(ValidationError, match="only ArmorIQ can place a HOLD"):
        ArmorIQDecision(outcome=DecisionOutcome.HOLD, source=source, decided_at=NOW)


def test_armoriq_policy_sources_allowlist() -> None:
    # ADR-026: only these sources may back an "ArmorIQ allowed, blocked or
    # held" claim. Added in P1-02; owned by the contract steward.
    assert {source.value for source in ARMORIQ_POLICY_SOURCES} == {
        "armoriq_issuance",
        "armoriq_policy_decision",
        "armoriq_proxy_decision",
    }
    assert DecisionSource.ARMORIQ_SDK_PLAN_CHECK not in ARMORIQ_POLICY_SOURCES
    assert DecisionSource.TOOL_SERVER_RULE not in ARMORIQ_POLICY_SOURCES
    assert DecisionSource.APPLICATION_RULE not in ARMORIQ_POLICY_SOURCES
    assert DecisionSource.UNKNOWN not in ARMORIQ_POLICY_SOURCES


def test_approved_hold_requires_delegation_and_resolution_time() -> None:
    with pytest.raises(ValidationError):
        ApprovalStatus(
            request_id="r1",
            plan_id="p1",
            step_index=1,
            tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
            state=ApprovalState.APPROVED,
            requested_at=NOW,
            resolved_at=NOW,
        )
    pending = ApprovalStatus(
        request_id="r1",
        plan_id="p1",
        step_index=1,
        tool=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        state=ApprovalState.PENDING,
        requested_at=NOW,
    )
    assert pending.delegation_id is None


# --- execution evidence -----------------------------------------------------


def test_executed_requires_ledger_evidence() -> None:
    with pytest.raises(ValidationError, match="ledger_entry_id"):
        ToolInvocationResult(
            request_id="r1",
            plan_id="p1",
            step_index=0,
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
            jti="j1",
            status=ToolExecutionStatus.EXECUTED,
            tool_server_reached=True,
        )


def test_not_reached_cannot_claim_ledger_entry() -> None:
    with pytest.raises(ValidationError):
        ToolInvocationResult(
            request_id="r1",
            plan_id="p1",
            step_index=0,
            tool=ToolName.SEND_EXTERNAL_MESSAGE,
            jti="j1",
            status=ToolExecutionStatus.NOT_REACHED,
            tool_server_reached=False,
            ledger_entry_id="L-1",
        )


# --- knowledge --------------------------------------------------------------


def _citation(tier: DataTier) -> SourceCitation:
    return SourceCitation(
        source_id="DEC-007",
        title="Postgres 16 migration",
        tier=tier,
        author="Maya",
        created_at=NOW,
        version=1,
        review_status=ReviewStatus.AUTHORITATIVE,
    )


def test_search_result_rejects_cross_tier_hits() -> None:
    hit = KnowledgeSearchHit(
        source=_citation(DataTier.CONFIDENTIAL), chunk_id="c1", snippet="s", score=0.9, rank=1
    )
    with pytest.raises(ValidationError, match="tier"):
        KnowledgeSearchResult(
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM, tier=DataTier.TEAM, query="q", hits=[hit]
        )


def test_search_result_tier_must_match_tool() -> None:
    with pytest.raises(ValidationError, match="tier bound to the tool"):
        KnowledgeSearchResult(
            tool=ToolName.KNOWLEDGE_SEARCH_TEAM, tier=DataTier.RESTRICTED, query="q"
        )


# --- identity ---------------------------------------------------------------


def test_web_request_cannot_carry_slack_refs() -> None:
    with pytest.raises(ValidationError):
        RequestContext(
            request_id="r1",
            trace_id="t1",
            workspace_id="w1",
            requester=_user(),
            entry_point=EntryPoint.WEB,
            received_at=NOW,
            slack_event_id="Ev1",
        )


def test_slack_request_requires_event_and_channel() -> None:
    with pytest.raises(ValidationError):
        RequestContext(
            request_id="r1",
            trace_id="t1",
            workspace_id="w1",
            requester=_user(),
            entry_point=EntryPoint.SLACK_MENTION,
            received_at=NOW,
        )


def test_contracts_are_immutable_and_reject_unknown_fields() -> None:
    user = _user()
    with pytest.raises(ValidationError):
        user.app_role = AppRole.MANAGER  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AuthenticatedUser.model_validate({**user.model_dump(), "email": "raw@example.com"})


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ArmorIQDecision(
            outcome=DecisionOutcome.BLOCK,
            source=DecisionSource.APPLICATION_RULE,
            decided_at=datetime(2026, 10, 14, 12, 0),  # intentionally naive
        )


# --- generated schema -------------------------------------------------------


def test_committed_json_schema_is_up_to_date() -> None:
    assert schema_main(["--check"]) == 0


def test_confirmation_event_types_are_declared() -> None:
    # P3-04 ICR, issue #25 (SHARED_INTERFACES §10).
    assert AuditEventType.ESCALATION_CREATED.value == "escalation_created"
    assert AuditEventType.CONFIRMATION_RESOLVED.value == "confirmation_resolved"
    enum = build_schema()["$defs"]["AuditEventType"]["enum"]
    assert "escalation_created" in enum
    assert "confirmation_resolved" in enum
