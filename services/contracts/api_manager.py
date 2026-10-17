"""Manager console API contracts (P4-04 API half, SHARED_INTERFACES §1.1).

``GET``/``PUT /api/v1/manager/*`` (SHARED_INTERFACES §1.1, PROPOSED → FIXED
on merge) serve Maya's console (PRD §10.2, Arch §9) from persisted audit and
topic-map state only: the P3-01 readers (``recent_requests``, ``timeline``,
``pending_holds``, ``unanswered``) and the P3-02 topic map. No model here
carries protected content, and none carries an approval action: approvals
happen only in the ArmorIQ console (R-40), so :class:`ManagerHoldView` links
there and deliberately has no approve/reject field.

:class:`ManagerTimelineEntry` keeps only whitelisted, already-redacted audit
fields (``jti``, ``ledger_entry_id``, execution and validation status,
delivery records, tier delivered): the raw payload dict is never echoed.
``jti`` and ``requester_member_id`` on the two queue views are the P3-01
additive fields (SHARED_INTERFACES §10).

The review-queue and decisions panels are **not** part of this surface:
they ship only through the D-12 path, after the P4-04 ICR recorded in
DECISIONS_AND_OPEN_QUESTIONS (D-12 follow-up, 17 Oct 2026).

``ManagerTopicRuleUpdate`` is a request body: ``extra="forbid"`` rejects
identity, role or authority keys (``member_id``, ``app_role``, ``actor``,
...) with a ``422`` envelope that never echoes input values (PRD §4.1).
Topic validity (length, specificity) is enforced by
``apps.api.topic_map.validate_topic``, the single source for the rule shape.
"""

from typing import Literal

from pydantic import AwareDatetime, StrictStr

from .enums import (
    ApprovalState,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    ToolExecutionStatus,
    ToolName,
)
from .models import ContractModel

#: Why the manager API cannot resolve a hold (server wording, R-40).
HOLD_NOTE = (
    "Approval happens only in the ArmorIQ console. This API cannot approve or reject a hold."
)

#: Why a topic rule is not an authorization (Arch §5.3.1, P4-04 §9.5).
TOPIC_NOTE = "Topic rules configure retrieval only and grant no access."

#: Connector labels (P4-04 §9.6): the only three states the console shows.
CONNECTED_LABEL = "Connected and tested"
UNAVAILABLE_LABEL = "Unavailable"
PLANNED_LABEL = "Planned"


class ManagerRequestSummary(ContractModel):
    """One row of ``GET /api/v1/manager/requests`` (newest first).

    Mirrors the P3-01 ``RequestSummary``; only IDs that have a
    ``request_received`` event are listed. Identity fields are empty when
    the stored origin payload is unreadable.
    """

    request_id: str
    entry_point: EntryPoint | None
    requester_persona: PersonaKey | None
    last_event_at: AwareDatetime


class ManagerTimelineEntry(ContractModel):
    """One event of ``GET /api/v1/manager/requests/{request_id}/timeline``.

    Merged request / decision / delivery events, oldest first, with the
    console's correlation fields: ``jti`` and ``ledger_entry_id`` line up
    with the execution ledger (SHARED_INTERFACES §13). Every optional field
    is a whitelisted extraction from the reader's redacted payload; missing
    or unknown values stay ``None`` rather than failing the page.
    """

    occurred_at: AwareDatetime
    kind: Literal["request", "decision", "delivery"]
    event_type: str
    plan_id: str | None
    step_index: int | None
    tool: ToolName | None
    decision_source: DecisionSource | None
    enforcement_layer: str | None
    is_armoriq_policy: bool
    jti: str | None
    ledger_entry_id: str | None
    execution_status: ToolExecutionStatus | None
    tool_server_reached: bool | None
    validation_outcome: str | None
    error_class: str | None
    outcome: DecisionOutcome | None
    reason: str | None
    delegation_id: str | None
    approval_state: ApprovalState | None
    delivery_channel: DeliveryChannel | None
    delivery_status: str | None
    delivered_to_persona: PersonaKey | None
    tier_delivered: DataTier | None
    simulated: bool


class ManagerHoldView(ContractModel):
    """One unresolved HOLD of ``GET /api/v1/manager/holds``.

    ``armoriq_console_url`` is the D-22 target where Maya resolves it;
    ``note`` repeats that approval happens only there. There is no approve
    or reject field by design (R-40, Arch §5.6).
    """

    request_id: str
    plan_id: str
    step_index: int
    tool: ToolName | None
    delegation_id: str | None
    approval_state: ApprovalState
    requester_persona: PersonaKey | None
    requested_at: AwareDatetime
    armoriq_console_url: str | None
    note: str


class ManagerUnansweredView(ContractModel):
    """One queue row of ``GET /api/v1/manager/unanswered``.

    The question is stored redacted at write time and passed through
    unchanged; no protected snippet is ever part of this view.
    """

    question_id: str
    request_id: str
    question: str
    reason: str | None
    status: str
    requester_persona: PersonaKey | None
    created_at: AwareDatetime


class ManagerTopicRuleView(ContractModel):
    """One topic-map rule (P3-02): retrieval configuration, not authority."""

    rule_id: str
    topic: str
    max_tier: DataTier


class ManagerTopicRulesView(ContractModel):
    """``GET /api/v1/manager/topic-rules``: the rules plus the access note."""

    rules: list[ManagerTopicRuleView]
    note: str


class ManagerTopicRuleUpdate(ContractModel):
    """``PUT /api/v1/manager/topic-rules`` body: topic + ceiling only.

    A request body, so it can never carry identity or authority: unknown
    keys (``member_id``, ``app_role``, ``actor``, ...) are refused with a
    ``422`` envelope. ``topic`` validity is enforced by
    ``apps.api.topic_map.validate_topic`` (normalization, length,
    specificity), mapped to the same ``422``; the stored
    ``created_by_member_id`` always comes from the session.
    """

    topic: StrictStr
    max_tier: DataTier


class ManagerConnectorStatus(ContractModel):
    """One connector tile of ``GET /api/v1/manager/health``.

    ``status`` is the machine value; ``label`` is the exact wording the
    console shows (P4-04 §9.6). ``planned`` is never a fake health result:
    it marks a connector that does not exist yet (Gmail, Drive, Meet).
    """

    name: str
    status: Literal["connected", "unavailable", "planned"]
    label: str


class ManagerHealthView(ContractModel):
    """``GET /api/v1/manager/health``: real checks, one row per connector.

    Every ``connected`` / ``unavailable`` verdict comes from a live probe
    (tool-server ``/health``, a backend ``SELECT 1``, the Slack socket
    thread); an unconfigured or failed probe reports ``unavailable``, never
    a fake ``connected``.
    """

    service: str
    version: str
    environment: str
    checked_at: AwareDatetime
    connectors: list[ManagerConnectorStatus]


__all__ = [
    "CONNECTED_LABEL",
    "HOLD_NOTE",
    "PLANNED_LABEL",
    "TOPIC_NOTE",
    "UNAVAILABLE_LABEL",
    "ManagerConnectorStatus",
    "ManagerHealthView",
    "ManagerHoldView",
    "ManagerRequestSummary",
    "ManagerTimelineEntry",
    "ManagerTopicRuleUpdate",
    "ManagerTopicRuleView",
    "ManagerTopicRulesView",
    "ManagerUnansweredView",
]
