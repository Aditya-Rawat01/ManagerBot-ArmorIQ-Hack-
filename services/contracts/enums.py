"""Enumerations shared by every ProxyLead component.

Values come from ProxyLead_PRD_v2.md and ProxyLead_Architecture_v2.1.md.
Anything not settled by those documents is marked as provisional.
"""

from enum import StrEnum


class PersonaKey(StrEnum):
    """Fictional demo members (PRD v2 §1). Labels only — never identity proof."""

    MAYA = "maya"
    SAM = "sam"
    PRIYA = "priya"


class AppRole(StrEnum):
    """Application role label. ArmorIQ policy, not this label, authorizes tools."""

    MANAGER = "manager"
    SENIOR = "senior"
    JUNIOR = "junior"


class EntryPoint(StrEnum):
    """Where a request entered ProxyLead."""

    SLACK_MENTION = "slack_mention"
    SLACK_DM = "slack_dm"
    WEB = "web"


class DeliveryChannel(StrEnum):
    """Where an answer was delivered (Architecture v2.1 §7.2)."""

    SLACK_THREAD = "slack_thread"
    SLACK_DM = "slack_dm"
    WEB_PRIVATE = "web_private"


class DataTier(StrEnum):
    """Knowledge sensitivity tiers (PRD v2 §5.1). Encoded in tool names, never in arguments."""

    TEAM = "team"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


ELEVATED_TIERS: frozenset[DataTier] = frozenset({DataTier.CONFIDENTIAL, DataTier.RESTRICTED})


class McpEndpoint(StrEnum):
    """Registered MCP endpoint names (PRD v2 §2). Must match the ArmorIQ console exactly."""

    MEMORY = "memory-mcp"
    WORKSPACE = "workspace-mcp"


class ToolName(StrEnum):
    """Logical tool/action names from PRD v2 §6.

    Exact registry representation must be confirmed by experiment E5.
    """

    KNOWLEDGE_SEARCH_TEAM = "knowledge_search_team"
    KNOWLEDGE_SEARCH_CONFIDENTIAL = "knowledge_search_confidential"
    KNOWLEDGE_SEARCH_RESTRICTED = "knowledge_search_restricted"
    KNOWLEDGE_GET_SOURCE_TEAM = "knowledge_get_source_team"
    KNOWLEDGE_GET_SOURCE_CONFIDENTIAL = "knowledge_get_source_confidential"
    KNOWLEDGE_GET_SOURCE_RESTRICTED = "knowledge_get_source_restricted"
    READ_SOURCE_TEAM = "read_source_team"
    READ_SOURCE_CONFIDENTIAL = "read_source_confidential"
    READ_SOURCE_RESTRICTED = "read_source_restricted"
    KNOWLEDGE_SAVE_TEAM_NOTE = "knowledge_save_team_note"
    KNOWLEDGE_CONFIRM_MAYA_DECISION = "knowledge_confirm_maya_decision"
    KNOWLEDGE_MARK_SUPERSEDED = "knowledge_mark_superseded"
    CREATE_FOLLOWUP_TASK = "create_followup_task"
    SEND_EXTERNAL_MESSAGE = "send_external_message"


class ToolKind(StrEnum):
    """Coarse tool classification used for validation and audit."""

    READ = "read"
    MAINTENANCE = "maintenance"
    ACTION = "action"
    HONEYPOT = "honeypot"


class DecisionOutcome(StrEnum):
    """Authorization outcome for one planned tool call."""

    ALLOW = "allow"
    BLOCK = "block"
    HOLD = "hold"
    UNVERIFIED = "unverified"


class DecisionSource(StrEnum):
    """Which layer produced a decision.

    Union of the labels in PRD v2 §10.1 and Architecture v2.1 §5.3 (see ADR-009).
    Never label an SDK client-side mismatch as a hosted-proxy decision.
    """

    ARMORIQ_ISSUANCE = "armoriq_issuance"
    ARMORIQ_POLICY_DECISION = "armoriq_policy_decision"
    ARMORIQ_SDK_PLAN_CHECK = "armoriq_sdk_plan_check"
    ARMORIQ_PROXY_DECISION = "armoriq_proxy_decision"
    TOOL_SERVER_RULE = "tool_server_rule"
    APPLICATION_RULE = "application_rule"
    UNKNOWN = "unknown"


ARMORIQ_DECISION_SOURCES: frozenset[DecisionSource] = frozenset(
    {
        DecisionSource.ARMORIQ_ISSUANCE,
        DecisionSource.ARMORIQ_POLICY_DECISION,
        DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
        DecisionSource.ARMORIQ_PROXY_DECISION,
    }
)


ARMORIQ_POLICY_SOURCES: frozenset[DecisionSource] = frozenset(
    {
        DecisionSource.ARMORIQ_ISSUANCE,
        DecisionSource.ARMORIQ_POLICY_DECISION,
        DecisionSource.ARMORIQ_PROXY_DECISION,
    }
)
"""Sources that may back an "ArmorIQ allowed, blocked or held" claim (ADR-026).

Deliberately excludes `ARMORIQ_SDK_PLAN_CHECK` (client-side only),
`TOOL_SERVER_RULE`, `APPLICATION_RULE`, and `UNKNOWN`: those layers fail closed
but never prove an ArmorIQ policy decision.
"""


class ApprovalState(StrEnum):
    """HOLD lifecycle state.

    `pending`, `approved`, `rejected`, `expired` mirror statuses the pinned SDK
    reports for a delegation. `timeout`, `interrupted`, `unverified` are
    application states (never treated as approval).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    UNVERIFIED = "unverified"


class ToolExecutionStatus(StrEnum):
    """What actually happened at the tool server, as evidenced by its ledger."""

    EXECUTED = "executed"
    REJECTED_BY_TOOL_SERVER = "rejected_by_tool_server"
    NOT_REACHED = "not_reached"
    ERROR = "error"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    """Knowledge authority (PRD v2 §8.4). Supersession is tracked separately."""

    DRAFT = "draft"
    AUTHORITATIVE = "authoritative"


class AuditEventType(StrEnum):
    """Provisional audit event types (PRD v2 §7 step 14). Extend in Phase 2."""

    REQUEST_RECEIVED = "request_received"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_REJECTED = "plan_rejected"
    PLAN_MINTED = "plan_minted"
    AUTHORIZATION_DECIDED = "authorization_decided"
    HOLD_CREATED = "hold_created"
    APPROVAL_RESOLVED = "approval_resolved"
    # User-confirmation lifecycle (P3-04 ICR, issue #25; SHARED_INTERFACES §10):
    # metadata-only events for minting an escalation's confirmation and for
    # resolving a presented confirmation. Payloads never carry the raw token
    # (services.audit.store validates the allowlist).
    ESCALATION_CREATED = "escalation_created"
    CONFIRMATION_RESOLVED = "confirmation_resolved"
    TOOL_EXECUTED = "tool_executed"
    TOOL_REJECTED = "tool_rejected"
    ANSWER_GENERATED = "answer_generated"
    ANSWER_ABSTAINED = "answer_abstained"
    DELIVERY_ATTEMPTED = "delivery_attempted"
    DELIVERY_SUCCEEDED = "delivery_succeeded"
    DELIVERY_FAILED = "delivery_failed"
    # A Slack event dropped before it became a request (P3-05 ICR): metadata
    # only (event/team/channel IDs + reason code), written by
    # services.audit.store.record_slack_event_rejected, never a request row.
    SLACK_EVENT_REJECTED = "slack_event_rejected"
    # "Simulate hijacked model" was switched on for a web request (P3-07 ICR):
    # metadata only (simulation name + injection vector), written with
    # append_event into request_events before planning.
    SIMULATION_ACTIVATED = "simulation_activated"
