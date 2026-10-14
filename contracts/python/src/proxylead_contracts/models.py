"""Preliminary shared contracts (see docs/IMPLEMENTATION_CONTRACT.md).

Only fields established by ProxyLead_PRD_v2.md / ProxyLead_Architecture_v2.1.md
are modeled. Open questions are listed in the module docstrings below and in
docs/ARCHITECTURE_DECISIONS.md ("Unresolved"). Nothing here describes an
ArmorIQ SDK API; ArmorIQ objects are mapped into these models by the backend
gateway after the live experiments define their real shapes.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from .enums import (
    ARMORIQ_DECISION_SOURCES,
    ELEVATED_TIERS,
    AppRole,
    ApprovalState,
    AuditEventType,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    EntryPoint,
    McpEndpoint,
    PersonaKey,
    ReviewStatus,
    ToolExecutionStatus,
    ToolName,
)
from .tools import FORBIDDEN_ARGUMENT_KEYS, SEARCH_TOOLS, tool_spec


class ContractModel(BaseModel):
    """Base: immutable, rejects unknown fields so drift is caught early."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class AuthenticatedUser(ContractModel):
    """A requester resolved by trusted backend code (PRD v2 §4.1).

    Deliberately excludes the raw ArmorIQ member email: it stays inside the
    backend (masked in UI/logs). Identity is never taken from chat text.
    """

    member_id: str = Field(min_length=1)
    persona: PersonaKey
    display_name: str = Field(min_length=1)
    app_role: AppRole
    masked_email: str = Field(
        min_length=1, description="e.g. m***@example.com; never the raw address"
    )
    slack_user_id: str | None = None


class RequestContext(ContractModel):
    """Trusted context created by the backend for every inbound request."""

    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    requester: AuthenticatedUser
    entry_point: EntryPoint
    received_at: AwareDatetime
    slack_team_id: str | None = None
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    slack_event_id: str | None = Field(default=None, description="Used for event deduplication")

    @model_validator(mode="after")
    def _slack_fields_match_entry_point(self) -> Self:
        slack_fields = (self.slack_team_id, self.slack_channel_id, self.slack_event_id)
        if self.entry_point is EntryPoint.WEB:
            if any(slack_fields) or self.slack_thread_ts:
                raise ValueError("web requests must not carry Slack references")
        elif not all(slack_fields):
            raise ValueError(
                "Slack requests require slack_team_id, slack_channel_id and slack_event_id"
            )
        return self


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class PlannedToolCall(ContractModel):
    """One validated step of a plan. Tier is implied by `tool`, never an argument.

    UNRESOLVED (Phase 2): per-tool argument schemas. Until they exist,
    `arguments` is a JSON object screened for forbidden/underscore keys.
    """

    step_index: int = Field(ge=0)
    mcp: McpEndpoint
    tool: ToolName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    purpose: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if tool_spec(self.tool).endpoint is not self.mcp:
            raise ValueError(
                f"{self.tool} is served by {tool_spec(self.tool).endpoint}, not {self.mcp}"
            )
        for key in self.arguments:
            if key.startswith("_"):
                raise ValueError(
                    f"underscore-prefixed argument {key!r} is reserved for trusted code"
                )
            if key.lower() in FORBIDDEN_ARGUMENT_KEYS:
                raise ValueError(f"argument {key!r} may not be chosen by the model")
        return self


class PlanProposal(ContractModel):
    """Planner output, built from the user's message only (PRD v2 §7).

    UNRESOLVED (Phase 2): maximum step count, an `intent` field, and a
    clarifying-question field. Only the PRD's elevated-tier rule is enforced.
    """

    summary: str = Field(min_length=1, max_length=500)
    steps: list[PlannedToolCall] = Field(default_factory=list)
    escalation_user_confirmed: bool = False
    matched_topic_rule_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if [s.step_index for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("step_index values must be 0..n-1 in order")
        elevated = [s for s in self.steps if tool_spec(s.tool).tier in ELEVATED_TIERS]
        if len(elevated) > 1:
            raise ValueError("at most one elevated-tier (confidential/restricted) call per plan")
        return self


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class ArmorIQDecision(ContractModel):
    """Authorization result for one planned step, labeled by the layer that decided.

    Mapped from real ArmorIQ SDK responses/exceptions by the backend gateway.
    `enforcement_action` keeps ArmorIQ's raw value (e.g. "allow_log") verbatim.
    """

    outcome: DecisionOutcome
    source: DecisionSource
    reason: str | None = None
    enforcement_action: str | None = None
    armoriq_plan_id: str | None = None
    delegation_id: str | None = None
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (
            self.source is DecisionSource.ARMORIQ_SDK_PLAN_CHECK
            and self.outcome is not DecisionOutcome.BLOCK
        ):
            raise ValueError("an SDK client-side plan check can only produce BLOCK")
        if self.outcome is DecisionOutcome.HOLD and self.source not in ARMORIQ_DECISION_SOURCES:
            raise ValueError("only ArmorIQ can place a HOLD; the app never simulates approval")
        return self


class ApprovalStatus(ContractModel):
    """State of a real ArmorIQ HOLD for one plan step.

    `delegation_id` may be absent briefly: the SDK reports the hold before the
    delegation request exists. UNRESOLVED: whether approver identity is
    observable through the pinned SDK.
    """

    request_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    tool: ToolName
    state: ApprovalState
    delegation_id: str | None = None
    requested_at: AwareDatetime
    resolved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.state is ApprovalState.APPROVED and not self.delegation_id:
            raise ValueError("an approved hold must reference its delegation_id")
        if self.state is not ApprovalState.PENDING and self.resolved_at is None:
            raise ValueError("resolved states need resolved_at")
        return self


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class ToolInvocationResult(ContractModel):
    """What the tool server actually did. A plan or ALLOW is not proof of execution."""

    request_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    tool: ToolName
    jti: str = Field(
        min_length=1, description="Application-generated invocation ID (not an ArmorIQ claim)"
    )
    status: ToolExecutionStatus
    tool_server_reached: bool | None = Field(description="None when it cannot be determined")
    ledger_entry_id: str | None = None
    result: JsonValue | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.status is ToolExecutionStatus.EXECUTED and (
            self.tool_server_reached is not True or not self.ledger_entry_id
        ):
            raise ValueError("EXECUTED requires tool_server_reached=True and a ledger_entry_id")
        if self.status is ToolExecutionStatus.NOT_REACHED and (
            self.tool_server_reached is not False or self.ledger_entry_id
        ):
            raise ValueError("NOT_REACHED requires tool_server_reached=False and no ledger entry")
        return self


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


class SourceCitation(ContractModel):
    """Provenance for a knowledge record (PRD v2 §8.1)."""

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    tier: DataTier
    author: str = Field(min_length=1)
    created_at: AwareDatetime
    version: int = Field(ge=1)
    review_status: ReviewStatus
    superseded_by: str | None = None
    reference_url: str | None = Field(default=None, description="e.g. Slack permalink")


class KnowledgeSearchHit(ContractModel):
    """UNRESOLVED: snippet length limit and score scale (depends on E17 / RRF design)."""

    source: SourceCitation
    chunk_id: str = Field(min_length=1)
    snippet: str
    score: float
    rank: int = Field(ge=1)


class KnowledgeSearchResult(ContractModel):
    """Output of a `knowledge_search_*` tool. `tier` is set by the server from the tool name."""

    tool: ToolName
    tier: DataTier
    query: str
    hits: list[KnowledgeSearchHit] = Field(default_factory=list)
    min_score: float | None = Field(
        default=None, description="Calibrated threshold; None until E17"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.tool not in SEARCH_TOOLS:
            raise ValueError(f"{self.tool} is not a knowledge search tool")
        if tool_spec(self.tool).tier is not self.tier:
            raise ValueError("result tier must equal the tier bound to the tool name")
        if any(hit.source.tier is not self.tier for hit in self.hits):
            raise ValueError("every hit must belong to the tool's tier")
        return self


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEvent(ContractModel):
    """Append-only audit event. `payload` must already be redacted (no secrets/tokens)."""

    event_id: str = Field(min_length=1)
    occurred_at: AwareDatetime
    event_type: AuditEventType
    request_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    plan_id: str | None = None
    step_index: int | None = Field(default=None, ge=0)
    jti: str | None = None
    decision_source: DecisionSource | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# HTTP API envelopes (backend <-> frontend)
# ---------------------------------------------------------------------------


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str


class ErrorBody(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    request_id: str | None = None
    details: JsonValue | None = None


class ErrorResponse(ContractModel):
    """Every non-2xx JSON response from ProxyLead services uses this envelope."""

    error: ErrorBody
