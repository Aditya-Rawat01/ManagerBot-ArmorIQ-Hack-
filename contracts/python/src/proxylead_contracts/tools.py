"""Static tool catalog: which endpoint serves each tool and which tier it is bound to.

The tier is a property of the tool *name*. Trusted server code hard-codes the
matching filter; a model can never supply or change it (PRD v2 §6.1).
"""

from dataclasses import dataclass

from .enums import DataTier, McpEndpoint, ToolKind, ToolName


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: ToolName
    endpoint: McpEndpoint
    kind: ToolKind
    tier: DataTier | None


def _spec(name: ToolName, endpoint: McpEndpoint, kind: ToolKind, tier: DataTier | None) -> ToolSpec:
    return ToolSpec(name=name, endpoint=endpoint, kind=kind, tier=tier)


_M, _W = McpEndpoint.MEMORY, McpEndpoint.WORKSPACE
_T, _C, _R = DataTier.TEAM, DataTier.CONFIDENTIAL, DataTier.RESTRICTED

TOOL_CATALOG: dict[ToolName, ToolSpec] = {
    spec.name: spec
    for spec in (
        _spec(ToolName.KNOWLEDGE_SEARCH_TEAM, _M, ToolKind.READ, _T),
        _spec(ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, _M, ToolKind.READ, _C),
        _spec(ToolName.KNOWLEDGE_SEARCH_RESTRICTED, _M, ToolKind.READ, _R),
        _spec(ToolName.KNOWLEDGE_GET_SOURCE_TEAM, _M, ToolKind.READ, _T),
        _spec(ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL, _M, ToolKind.READ, _C),
        _spec(ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED, _M, ToolKind.READ, _R),
        _spec(ToolName.READ_SOURCE_TEAM, _M, ToolKind.READ, _T),
        _spec(ToolName.READ_SOURCE_CONFIDENTIAL, _M, ToolKind.READ, _C),
        _spec(ToolName.READ_SOURCE_RESTRICTED, _M, ToolKind.READ, _R),
        _spec(ToolName.KNOWLEDGE_SAVE_TEAM_NOTE, _M, ToolKind.MAINTENANCE, None),
        _spec(ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION, _M, ToolKind.MAINTENANCE, None),
        _spec(ToolName.KNOWLEDGE_MARK_SUPERSEDED, _M, ToolKind.MAINTENANCE, None),
        _spec(ToolName.CREATE_FOLLOWUP_TASK, _W, ToolKind.ACTION, None),
        _spec(ToolName.SEND_EXTERNAL_MESSAGE, _W, ToolKind.HONEYPOT, None),
    )
}

SEARCH_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.KNOWLEDGE_SEARCH_TEAM,
        ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        ToolName.KNOWLEDGE_SEARCH_RESTRICTED,
    }
)

# Argument keys a model may never supply: identity, tier, scope and delivery
# destination are chosen by trusted code only (Architecture v2.1 §0.5,
# PRD v2 §7 step 3). Initial list — extend when tool argument schemas land.
FORBIDDEN_ARGUMENT_KEYS: frozenset[str] = frozenset(
    {
        "tier",
        "sensitivity",
        "classification",
        "resource_scope",
        "subject_user_id",
        "user_id",
        "user_email",
        "email",
        "member_id",
        "requester",
        "requester_id",
        "requester_email",
        "role",
        "persona",
        "workspace_id",
        "recipient",
        "recipient_id",
        "channel",
        "channel_id",
    }
)


def tool_spec(name: ToolName) -> ToolSpec:
    return TOOL_CATALOG[name]
