"""ProxyLead shared data contracts (preliminary, v0.1.0).

Pydantic models are the single source of truth. The frontend consumes the
generated JSON Schema (services/contracts/schemas/) as TypeScript types.
"""

from .api_session import SessionCreateRequest, SessionInfo
from .enums import (
    ARMORIQ_DECISION_SOURCES,
    ARMORIQ_POLICY_SOURCES,
    ELEVATED_TIERS,
    AppRole,
    ApprovalState,
    AuditEventType,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    McpEndpoint,
    PersonaKey,
    ReviewStatus,
    ToolExecutionStatus,
    ToolKind,
    ToolName,
)
from .models import (
    ApprovalStatus,
    ArmorIQDecision,
    AuditEvent,
    AuthenticatedUser,
    ContractModel,
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    KnowledgeSearchHit,
    KnowledgeSearchResult,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    SourceCitation,
    ToolInvocationResult,
)
from .tools import FORBIDDEN_ARGUMENT_KEYS, SEARCH_TOOLS, TOOL_CATALOG, ToolSpec, tool_spec

CONTRACTS_VERSION = "0.1.0"

__all__ = [
    "ARMORIQ_DECISION_SOURCES",
    "ARMORIQ_POLICY_SOURCES",
    "CONTRACTS_VERSION",
    "ELEVATED_TIERS",
    "FORBIDDEN_ARGUMENT_KEYS",
    "SEARCH_TOOLS",
    "TOOL_CATALOG",
    "AppRole",
    "ApprovalState",
    "ApprovalStatus",
    "ArmorIQDecision",
    "AuditEvent",
    "AuditEventType",
    "AuthenticatedUser",
    "ContractModel",
    "DataTier",
    "DecisionOutcome",
    "DecisionSource",
    "DeliveryChannel",
    "EntryPoint",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "KnowledgeSearchHit",
    "KnowledgeSearchResult",
    "McpEndpoint",
    "PersonaKey",
    "PlanProposal",
    "PlannedToolCall",
    "RequestContext",
    "ReviewStatus",
    "SessionCreateRequest",
    "SessionInfo",
    "SourceCitation",
    "ToolExecutionStatus",
    "ToolInvocationResult",
    "ToolKind",
    "ToolName",
    "ToolSpec",
    "tool_spec",
]
