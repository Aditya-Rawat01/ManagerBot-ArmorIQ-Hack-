"""Web request API contracts (P4-01, SHARED_INTERFACES §1.1).

``POST /api/v1/requests`` takes :class:`CreateRequestBody` and answers ``202``
with :class:`CreateRequestResponse`; ``GET /api/v1/requests/{request_id}``
returns :class:`RequestStatusView` to the requesting member only.

Identity never comes from a body (PRD §4.1): ``extra="forbid"`` rejects
``persona``, ``member_id``, ``email``, ``tier``, ``recipient`` and any other
key with a ``422`` envelope. Status fields mirror persisted audit and delivery
records (P3-01); the answer text, citation titles and a pending confirmation
offer come from the requester's own in-process web inbox
(``apps/api/web_inbox.py``) and are returned to the owner only.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, StrictBool, StrictStr, model_validator

from .enums import (
    ApprovalState,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    ToolExecutionStatus,
    ToolName,
)
from .models import ContractModel

# The planner refuses longer messages (`message_too_long`, P3-02).
MAX_REQUEST_MESSAGE_CHARS = 4000
# `secrets.token_urlsafe(32)` is 43 characters (D-14); anything longer is junk.
MAX_CONFIRMATION_TOKEN_CHARS = 64

RequestState = Literal["processing", "pending_approval", "awaiting_confirmation", "completed"]
"""Lifecycle state. Terminal for polling: ``completed`` and
``awaiting_confirmation`` (a confirmation is a NEW request)."""

RequestOutcome = Literal[
    "allow", "block", "hold", "abstain", "error", "unverified", "clarification"
]
"""Request-level outcome derived from persisted records (Arch §1)."""


class CreateRequestBody(ContractModel):
    """Web request body. No identity, tier, or recipient field exists (R-10)."""

    message: StrictStr = Field(default="", max_length=MAX_REQUEST_MESSAGE_CHARS)
    confirmation_token: StrictStr | None = Field(
        default=None, min_length=1, max_length=MAX_CONFIRMATION_TOKEN_CHARS
    )
    simulate_hijacked_model: StrictBool = False

    @model_validator(mode="after")
    def _message_or_confirmation(self) -> Self:
        # A confirmation re-runs the stored question (D-14); otherwise a
        # non-blank message is required.
        if self.confirmation_token is None and not self.message.strip():
            raise ValueError("message must not be blank")
        if self.confirmation_token is not None and self.simulate_hijacked_model:
            raise ValueError("a confirmation cannot enable the simulation")
        return self


class CreateRequestResponse(ContractModel):
    """``202`` body: the orchestrator-minted request ID (``req-<32 hex>``)."""

    request_id: str


class StepStatusView(ContractModel):
    """One planned step as persisted (decision, approval, execution reference)."""

    step_index: int = Field(ge=0)
    tool: ToolName | None
    outcome: DecisionOutcome | None
    decision_source: DecisionSource | None
    is_armoriq_policy: bool
    execution_status: ToolExecutionStatus | None
    approval_state: ApprovalState | None


class OffPlanAttemptView(ContractModel):
    """A model-requested tool call routed off-plan (P3-03/P3-07). Never executed.

    ``enforcement_layer`` is the server's wording for ``decision_source``;
    ``is_armoriq_policy`` is true only for ArmorIQ policy sources (ADR-026),
    never for the client-side SDK plan check or application rules.
    """

    tool: ToolName | None
    outcome: DecisionOutcome
    decision_source: DecisionSource
    enforcement_layer: str
    is_armoriq_policy: bool
    executed: Literal[False] = False
    simulated: bool
    rejection: str | None


class CitationView(ContractModel):
    """A validated citation of the delivered answer (source ID + tier persisted;
    the title only from the owner's web inbox)."""

    source_id: str
    tier: DataTier
    title: str | None


class ConfirmationOfferView(ContractModel):
    """A pending D-14 confirmation for the owner's confirm control.

    ``token`` is the single-use value the web confirm button posts back as
    ``confirmation_token``; it is shown only to the requesting member's
    session, only until it expires, and is never logged or audited.
    """

    escalation_id: str
    requested_tier: DataTier
    expires_at: AwareDatetime
    token: str


class RequestStatusView(ContractModel):
    """Owner-only status of one request (P4-01, polled by P4-02/P4-03)."""

    request_id: str
    state: RequestState
    outcome: RequestOutcome | None
    note: str | None
    steps: list[StepStatusView]
    off_plan_attempts: list[OffPlanAttemptView]
    simulation_active: bool
    egress_sink_executions: int = Field(
        ge=0,
        description=(
            "Executed send_external_message steps of this request, counted from the "
            "persisted ledger-backed execution references (the only way the honeypot "
            "writes its synthetic sink). Guarded mode: always 0. The backend cannot "
            "read the sink table itself (grant PROPOSED, SHARED_INTERFACES §7)."
        ),
    )
    answer_text: str | None
    citations: list[CitationView]
    abstained: bool
    abstain_reason: str | None
    delivery_channel: DeliveryChannel | None
    delivery_status: str | None
    confirmation: ConfirmationOfferView | None
    last_event_at: AwareDatetime | None


__all__ = [
    "MAX_CONFIRMATION_TOKEN_CHARS",
    "MAX_REQUEST_MESSAGE_CHARS",
    "CitationView",
    "ConfirmationOfferView",
    "CreateRequestBody",
    "CreateRequestResponse",
    "OffPlanAttemptView",
    "RequestOutcome",
    "RequestState",
    "RequestStatusView",
    "StepStatusView",
]
