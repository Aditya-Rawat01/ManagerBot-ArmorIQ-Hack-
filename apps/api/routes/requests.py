"""Web request API (P4-01): submit a message, poll its status.

``POST /api/v1/requests`` and ``GET /api/v1/requests/{request_id}``
(SHARED_INTERFACES §1.1; D-21 default: polling). The web demo uses the same
identity, orchestrator, gateway and tool path as Slack, with no demo bypass
(Arch §9):

- Identity comes ONLY from the signed session cookie (P1-03). The body
  (:class:`CreateRequestBody`, ``extra="forbid"``) has no persona, member,
  email, tier or recipient field: any such key is a ``422`` envelope that
  never echoes input. No session -> ``401``.
- ``POST`` builds a web :class:`RequestContext` from the session and calls
  ``app.state.orchestrator.handle`` (``503`` when the orchestrator is not
  configured), then answers ``202 {request_id}``. ``simulate_hijacked_model``
  (P3-07) and ``confirmation_token`` (D-14) become :class:`RequestOptions`
  here, the only place they can be set.
- ``GET`` is owner-only: ``services.audit.queries.request_status`` and
  ``request_detail`` compare the stored requester with the session member, and
  "not yours", "does not exist" and a malformed ID give the same ``404``.
  Status values come only from persisted audit and delivery rows (R-72); the
  private answer text, citation titles and a pending confirmation offer come
  from the owner's in-process web inbox (``apps/api/web_inbox.py``; lost on a
  restart). No email, ``_ctx``, ``jti`` or another member's data is returned.

Polling guidance (P4-02/P4-03): poll every 2 s while ``state`` is
``processing``; every 5 s while ``pending_approval`` (a HOLD may last
``APPROVAL_TIMEOUT_MS``, 15 min); stop at ``completed`` or
``awaiting_confirmation`` (confirming posts a NEW request with
``confirmation_token``).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from apps.api.core.config import Settings
from apps.api.core.errors import ApiError
from apps.api.deps import get_app_settings
from apps.api.identity import SESSION_COOKIE_NAME, build_member_table, resolve_web_session
from services.audit import queries as default_queries
from services.audit.errors import AuditNotAuthorizedError, AuditNotFoundError
from services.contracts import (
    ARMORIQ_POLICY_SOURCES,
    DEFAULT_WORKSPACE_ID,
    ApprovalState,
    AuthenticatedUser,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    RequestContext,
    ToolExecutionStatus,
    ToolName,
)
from services.contracts.api_requests import (
    CitationView,
    ConfirmationOfferView,
    CreateRequestBody,
    CreateRequestResponse,
    OffPlanAttemptView,
    RequestOutcome,
    RequestState,
    RequestStatusView,
    StepStatusView,
)

router = APIRouter(tags=["requests"])

_REQUEST_ID_RE = re.compile(r"req-[0-9a-f]{32}")
_NOT_FOUND = "Request not found."
_UNAVAILABLE = "Requests are not available on this deployment."

HOLD_NOTE = (
    "Waiting for Maya's decision in the ArmorIQ console. Approval happens only there; "
    "this page cannot approve it."
)
CONFIRM_NOTE = (
    "This needs your confirmation. Confirming sends a new request; ignoring it lets it expire."
)

# Server wording for each decision source (P3-07 display; ADR-026 wording:
# only ARMORIQ_POLICY_SOURCES are "ArmorIQ policy").
ENFORCEMENT_LAYERS: dict[DecisionSource, str] = {
    DecisionSource.ARMORIQ_ISSUANCE: "ArmorIQ plan issuance",
    DecisionSource.ARMORIQ_POLICY_DECISION: "ArmorIQ policy",
    DecisionSource.ARMORIQ_PROXY_DECISION: "ArmorIQ proxy policy",
    DecisionSource.ARMORIQ_SDK_PLAN_CHECK: "ArmorIQ SDK plan check (client-side)",
    DecisionSource.TOOL_SERVER_RULE: "Tool server rule",
    DecisionSource.APPLICATION_RULE: "ProxyLead application rule (not ArmorIQ)",
    DecisionSource.UNKNOWN: "Unknown layer (unverified)",
}

_NOT_APPROVED = {
    ApprovalState.REJECTED.value: "block",
    ApprovalState.EXPIRED.value: "unverified",
    ApprovalState.TIMEOUT.value: "unverified",
    ApprovalState.INTERRUPTED.value: "unverified",
    ApprovalState.UNVERIFIED.value: "unverified",
}


def require_session(
    request: Request, settings: Annotated[Settings, Depends(get_app_settings)]
) -> AuthenticatedUser:
    """The session member (P1-03), else ``401``. Runs before body validation."""
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    user = resolve_web_session(
        build_member_table(settings), request.cookies.get(SESSION_COOKIE_NAME), secret
    )
    if user is None:
        raise ApiError(401, "unauthenticated", "No valid session.")
    return user


SessionUser = Annotated[AuthenticatedUser, Depends(require_session)]


def _orchestrator(request: Request) -> Any:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise ApiError(503, "requests_unavailable", _UNAVAILABLE)
    return orchestrator


@router.post(
    "/requests",
    status_code=202,
    response_model=CreateRequestResponse,
    summary="Submit a web request (session identity only)",
)
async def create_request(
    user: SessionUser, body: CreateRequestBody, request: Request
) -> CreateRequestResponse:
    orchestrator = _orchestrator(request)
    from apps.api.orchestrator import (
        RequestNotRecordedError,
        RequestOptions,
        RequestRejectedError,
    )

    ctx = RequestContext(
        request_id=f"web-{uuid.uuid4().hex}",  # replaced by the orchestrator
        trace_id=f"web-trace-{uuid.uuid4().hex}",
        workspace_id=DEFAULT_WORKSPACE_ID,
        requester=user,
        entry_point=EntryPoint.WEB,
        received_at=datetime.now(UTC),
    )
    options = RequestOptions(
        confirmation_token=body.confirmation_token,
        simulate_hijacked_model=body.simulate_hijacked_model,
    )
    try:
        request_id = await orchestrator.handle(ctx, body.message, options)
    except RequestRejectedError:
        raise ApiError(400, "request_rejected", "The request was refused.") from None
    except RequestNotRecordedError:
        raise ApiError(503, "request_not_recorded", "The request could not be recorded.") from None
    return CreateRequestResponse(request_id=request_id)


@router.get(
    "/requests/{request_id}",
    response_model=RequestStatusView,
    summary="Status of your own web request (owner only)",
)
def read_request(request_id: str, user: SessionUser, request: Request) -> RequestStatusView:
    orchestrator = _orchestrator(request)
    db = getattr(request.app.state, "audit_db", None)
    if db is None:
        raise ApiError(503, "requests_unavailable", _UNAVAILABLE)
    if _REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ApiError(404, "not_found", _NOT_FOUND)
    reader = getattr(request.app.state, "request_reader", None) or default_queries
    try:
        # Owner-only reads (P3-01): the member comes from the session only.
        reader.request_status(db, request_id, user.member_id)
        detail = reader.request_detail(db, request_id, user.member_id)
    except (AuditNotAuthorizedError, AuditNotFoundError):
        raise ApiError(404, "not_found", _NOT_FOUND) from None
    inbox = getattr(orchestrator, "web_inbox", None)
    item = inbox.get(request_id, user.member_id, DEFAULT_WORKSPACE_ID) if inbox else None
    return build_status_view(detail, item, now=datetime.now(UTC))


# --- view building (persisted detail + the owner's inbox item) -----------------


def _enum(kind: Any, value: object) -> Any:
    try:
        return kind(value) if value is not None else None
    except ValueError:
        return None


def _source(value: object) -> DecisionSource:
    return _enum(DecisionSource, value) or DecisionSource.UNKNOWN  # fail closed


def _outcome(detail: Any) -> RequestOutcome | None:
    if detail.error:
        return "error"
    if detail.plan_decision is not None:
        return "block" if detail.plan_decision[0] == "block" else "unverified"
    if detail.plan_rejected:
        return "block"
    for step in detail.steps:
        if step.outcome in ("block", "unverified"):
            return step.outcome  # type: ignore[no-any-return]
    if detail.pending_holds:
        return "hold"
    for step in detail.steps:
        if step.approval_state in _NOT_APPROVED:
            return _NOT_APPROVED[step.approval_state]  # type: ignore[return-value]
    answer = detail.answer
    if answer is not None:
        if answer.abstained:
            return "abstain"
        return "clarification" if answer.reason == "clarification" else "allow"
    return None


def _state(detail: Any) -> RequestState:
    if detail.pending_holds:
        return "pending_approval"
    if detail.delivery_status is None:
        return "processing"
    if detail.escalated and detail.answer is None and not detail.error:
        return "awaiting_confirmation"
    return "completed"


def build_status_view(detail: Any, item: Any, *, now: datetime) -> RequestStatusView:
    """Map ``request_detail`` (persisted) + the owner's inbox item to the view."""
    state = _state(detail)
    answered = detail.delivery_channel == DeliveryChannel.WEB_PRIVATE.value
    titles = {c.source_id: c.title for c in item.citations} if item and answered else {}
    citations: list[CitationView] = []
    answer = detail.answer
    if answer is not None and not answer.abstained:
        for source_id, tier in answer.citations:
            parsed = _enum(DataTier, tier)
            if parsed is not None:
                citations.append(
                    CitationView(source_id=source_id, tier=parsed, title=titles.get(source_id))
                )
    offer = item.offer if item is not None else None
    confirmation = None
    if state == "awaiting_confirmation" and offer is not None and offer.expires_at > now:
        confirmation = ConfirmationOfferView(
            escalation_id=offer.escalation_id,
            requested_tier=offer.requested_tier,
            expires_at=offer.expires_at,
            token=offer.token,
        )
    note = None
    if state == "pending_approval":
        note = HOLD_NOTE
    elif confirmation is not None:
        note = CONFIRM_NOTE
    off_plan = []
    for attempt in detail.off_plan:
        source = _source(attempt.decision_source)
        off_plan.append(
            OffPlanAttemptView(
                tool=_enum(ToolName, attempt.tool_name),
                outcome=_enum(DecisionOutcome, attempt.outcome) or DecisionOutcome.UNVERIFIED,
                decision_source=source,
                enforcement_layer=ENFORCEMENT_LAYERS[source],
                is_armoriq_policy=source in ARMORIQ_POLICY_SOURCES,
                simulated=attempt.simulated,
                rejection=attempt.rejection,
            )
        )
    steps = []
    for step in detail.steps:
        source = _enum(DecisionSource, step.decision_source)
        steps.append(
            StepStatusView(
                step_index=step.step_index,
                tool=_enum(ToolName, step.tool_name),
                outcome=_enum(DecisionOutcome, step.outcome),
                decision_source=source,
                is_armoriq_policy=source in ARMORIQ_POLICY_SOURCES,
                execution_status=_enum(ToolExecutionStatus, step.execution_status),
                approval_state=_enum(ApprovalState, step.approval_state),
            )
        )
    return RequestStatusView(
        request_id=detail.request_id,
        state=state,
        outcome=_outcome(detail),
        note=note,
        steps=steps,
        off_plan_attempts=off_plan,
        simulation_active=detail.simulation_active,
        egress_sink_executions=detail.honeypot_executions,
        answer_text=item.answer_text if item is not None and answered else None,
        citations=citations,
        abstained=bool(answer and answer.abstained),
        abstain_reason=answer.reason if answer is not None and answer.abstained else None,
        delivery_channel=_enum(DeliveryChannel, detail.delivery_channel),
        delivery_status=detail.delivery_status,
        confirmation=confirmation,
        last_event_at=detail.last_event_at,
    )


__all__ = [
    "CONFIRM_NOTE",
    "ENFORCEMENT_LAYERS",
    "HOLD_NOTE",
    "build_status_view",
    "require_session",
    "router",
]
