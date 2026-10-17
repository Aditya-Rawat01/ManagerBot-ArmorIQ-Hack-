"""Maya-only manager console API (P4-04 API half, SHARED_INTERFACES §1.1).

``GET``/``PUT /api/v1/manager/*`` (SHARED_INTERFACES §1.1, PROPOSED → FIXED
on merge) give Maya the console's read models without a backdoor around
ArmorIQ (Arch §7.5, §9):

- Identity comes ONLY from the signed session cookie (P1-03), reusing the
  P4-01 :func:`apps.api.routes.requests.require_session` dependency: no body
  or query ever names a member, and every endpoint answers ``403`` unless
  the session's ``app_role`` is ``manager`` (P4-04 §9.1). The readers
  enforce the same role again (defence in depth), and that path maps to
  ``403`` too.
- Reads go through the approved readers only (``recent_requests``,
  ``timeline``, ``pending_holds``, ``unanswered``; ``list_rules`` /
  ``upsert_rule``): the backend never ``SELECT``s a protected table, and
  every timeline field is a whitelisted extraction from the reader's
  already-redacted payload.
- There is no approval action: ``/manager/holds`` links to
  ``ARMORIQ_CONSOLE_URL`` (D-22) and cannot approve or reject a hold (R-40,
  Arch §5.6). The review-queue and decisions endpoints are **not** part of
  this surface: the D-12 follow-up (17 Oct 2026) requires the P4-04 ICR
  before the console ships confirm/supersede.
- ``/manager/health`` probes for real: the tool server's ``/health``, a
  backend ``SELECT 1``, and the Slack socket thread's liveness. Gmail,
  Drive and Meet are reported ``planned``, never a fake ``connected``
  (P4-04 §9.6). An unconfigured or failed probe is ``unavailable``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.request import urlopen

from fastapi import APIRouter, Depends, Query, Request

from apps.api import __version__
from apps.api import topic_map as default_topic_map
from apps.api.core.config import Settings
from apps.api.core.errors import ApiError
from apps.api.deps import get_app_settings
from apps.api.routes.requests import ENFORCEMENT_LAYERS, require_session
from services.audit import queries as default_queries
from services.audit.errors import (
    AuditError,
    AuditNotAuthorizedError,
    AuditNotFoundError,
    AuditStoreError,
)
from services.contracts import (
    DEFAULT_WORKSPACE_ID,
    AppRole,
    ApprovalState,
    AuthenticatedUser,
    DataTier,
    DecisionOutcome,
    DecisionSource,
    DeliveryChannel,
    EntryPoint,
    PersonaKey,
    ToolExecutionStatus,
    ToolName,
)
from services.contracts.api_manager import (
    CONNECTED_LABEL,
    HOLD_NOTE,
    PLANNED_LABEL,
    TOPIC_NOTE,
    UNAVAILABLE_LABEL,
    ManagerConnectorStatus,
    ManagerHealthView,
    ManagerHoldView,
    ManagerRequestSummary,
    ManagerTimelineEntry,
    ManagerTopicRulesView,
    ManagerTopicRuleUpdate,
    ManagerTopicRuleView,
    ManagerUnansweredView,
)

router = APIRouter(tags=["manager"])

_REQUEST_ID_RE = re.compile(r"req-[0-9a-f]{32}")
_FORBIDDEN = "Manager access only."
_UNAVAILABLE = "The manager console is not available on this deployment."
_NOT_FOUND = "Request not found."
_BAD_TOPIC = "The topic is not usable as a rule."
_MAX_LIST_LIMIT = 200  # mirrors the reader's clamp (_MAX_LIMIT in queries.py)
_HEALTH_TIMEOUT_SECONDS = 2.0
_PERSONA_PREFIX = "m-"


def require_manager(
    user: Annotated[AuthenticatedUser, Depends(require_session)],
) -> AuthenticatedUser:
    """The session member, only when their ``app_role`` is ``manager``.

    Runs before body validation (like ``require_session``): a Sam or Priya
    session gets ``403`` even for an invalid body, and the check is
    server-side only (P4-04 §9.1).
    """
    if user.app_role is not AppRole.MANAGER:
        raise ApiError(403, "forbidden", _FORBIDDEN)
    return user


ManagerUser = Annotated[AuthenticatedUser, Depends(require_manager)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# --- plumbing ------------------------------------------------------------------


def _db(request: Request) -> Any:
    db = getattr(request.app.state, "audit_db", None)
    if db is None:
        raise ApiError(503, "manager_unavailable", _UNAVAILABLE)
    return db


def _reader(request: Request) -> Any:
    """The audit-query module (tests inject ``app.state.manager_reader``)."""
    return getattr(request.app.state, "manager_reader", None) or default_queries


def _topics(request: Request) -> Any:
    """The topic-map module (tests inject ``app.state.topic_map``)."""
    return getattr(request.app.state, "topic_map", None) or default_topic_map


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one reader call, mapping audit failures to envelope errors.

    ``AuditNotAuthorizedError`` is the readers' own role gate: mapping it to
    ``403`` keeps a drift between the route check and the readers from ever
    leaking manager data. Anything else keeps its meaning: a missing row is
    ``404``; a store/driver failure is ``503`` (fail closed, never partial).
    """
    try:
        return fn(*args, **kwargs)
    except AuditNotAuthorizedError:
        raise ApiError(403, "forbidden", _FORBIDDEN) from None
    except AuditNotFoundError:
        raise ApiError(404, "not_found", _NOT_FOUND) from None
    except AuditError:
        raise ApiError(503, "manager_unavailable", _UNAVAILABLE) from None


def _enum(kind: Any, value: object) -> Any:
    try:
        return kind(value) if value is not None else None
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _persona(member_id: object) -> PersonaKey | None:
    """``m-sam`` → ``PersonaKey.SAM``; unknown or missing members → None.

    Labels only (PRD §1): an unmapped member id is shown as unknown rather
    than guessed.
    """
    if not isinstance(member_id, str) or not member_id.startswith(_PERSONA_PREFIX):
        return None
    try:
        return PersonaKey(member_id[len(_PERSONA_PREFIX) :])
    except ValueError:
        return None


def _simulated(payload: dict[str, object]) -> bool:
    from apps.api.hijack_simulation import SIMULATION_NAME  # lazy: keep imports light

    return payload.get("simulation") == SIMULATION_NAME


# --- view building -------------------------------------------------------------


def build_summary_view(summary: Any) -> ManagerRequestSummary:
    return ManagerRequestSummary(
        request_id=str(summary.request_id),
        entry_point=_enum(EntryPoint, summary.entry_point),
        requester_persona=_persona(summary.requester_member_id),
        last_event_at=summary.last_event_at,
    )


def build_timeline_view(entry: Any) -> ManagerTimelineEntry:
    """Whitelisted fields of one redacted :class:`TimelineEntry`."""
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    source = _enum(DecisionSource, entry.decision_source)
    delivered_to = payload.get("delivered_to_member_id") or payload.get("recipient_member_id")
    return ManagerTimelineEntry(
        occurred_at=entry.occurred_at,
        kind=entry.kind,
        event_type=entry.event_type,
        plan_id=entry.plan_id,
        step_index=entry.step_index,
        tool=_enum(ToolName, entry.tool_name),
        decision_source=source,
        enforcement_layer=ENFORCEMENT_LAYERS[source] if source else None,
        is_armoriq_policy=bool(entry.is_armoriq_policy),
        jti=_text(entry.jti),
        ledger_entry_id=_text(payload.get("ledger_entry_id")),
        execution_status=_enum(ToolExecutionStatus, payload.get("execution_status")),
        tool_server_reached=_flag(payload.get("tool_server_reached")),
        validation_outcome=_text(payload.get("validation_outcome")),
        error_class=_text(payload.get("error_class")),
        outcome=_enum(DecisionOutcome, payload.get("outcome")),
        reason=_text(payload.get("reason")),
        delegation_id=_text(payload.get("delegation_id")),
        approval_state=_enum(ApprovalState, payload.get("approval_state")),
        delivery_channel=_enum(DeliveryChannel, payload.get("channel")),
        delivery_status=_delivery_status(entry),
        delivered_to_persona=_persona(delivered_to),
        tier_delivered=_enum(DataTier, payload.get("tier_delivered")),
        simulated=_simulated(payload),
    )


def _delivery_status(entry: Any) -> str | None:
    if entry.kind == "delivery":
        # The reader names these entries "delivery_<status>".
        return str(entry.event_type).removeprefix("delivery_") or None
    return _text(entry.payload.get("status") if isinstance(entry.payload, dict) else None)


def build_hold_view(hold: Any, *, console_url: str | None) -> ManagerHoldView:
    return ManagerHoldView(
        request_id=str(hold.request_id),
        plan_id=str(hold.plan_id),
        step_index=int(hold.step_index),
        tool=_enum(ToolName, hold.tool_name),
        delegation_id=_text(hold.delegation_id),
        # The reader returns unresolved 'pending' approvals only.
        approval_state=ApprovalState.PENDING,
        requester_persona=_persona(hold.requested_by_member_id),
        requested_at=hold.requested_at,
        armoriq_console_url=console_url,
        note=HOLD_NOTE,
    )


def build_unanswered_view(item: Any) -> ManagerUnansweredView:
    return ManagerUnansweredView(
        question_id=str(item.question_id),
        request_id=str(item.request_id),
        question=item.question,
        reason=_text(item.reason),
        status=str(item.status),
        requester_persona=_persona(item.requester_member_id),
        created_at=item.created_at,
    )


def build_rule_view(rule: Any) -> ManagerTopicRuleView:
    """The reader guarantees a parsed ``DataTier`` (unknown tiers fail closed)."""
    return ManagerTopicRuleView(
        rule_id=str(rule.id),
        topic=rule.topic,
        max_tier=rule.max_tier,
    )


# --- endpoints -----------------------------------------------------------------


@router.get(
    "/manager/requests",
    response_model=list[ManagerRequestSummary],
    summary="Recent requests (manager session only)",
)
def list_requests(
    request: Request,
    user: ManagerUser,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIST_LIMIT)] = 50,
) -> list[ManagerRequestSummary]:
    summaries = _call(_reader(request).recent_requests, _db(request), user, limit)
    return [build_summary_view(summary) for summary in summaries]


@router.get(
    "/manager/requests/{request_id}/timeline",
    response_model=list[ManagerTimelineEntry],
    summary="One request's audit timeline with ledger correlation",
)
def request_timeline(
    request_id: str, request: Request, user: ManagerUser
) -> list[ManagerTimelineEntry]:
    if _REQUEST_ID_RE.fullmatch(request_id) is None:
        # Unknown, malformed and never-recorded IDs answer the same 404.
        raise ApiError(404, "not_found", _NOT_FOUND)
    entries = _call(_reader(request).timeline, _db(request), request_id, user)
    if not entries:
        # Every recorded request has a request_received event, so an empty
        # timeline means no such request (never "no events yet").
        raise ApiError(404, "not_found", _NOT_FOUND)
    return [build_timeline_view(entry) for entry in entries]


@router.get(
    "/manager/holds",
    response_model=list[ManagerHoldView],
    summary="Pending approvals with console link (no approve action)",
)
def list_holds(request: Request, user: ManagerUser, settings: SettingsDep) -> list[ManagerHoldView]:
    holds = _call(_reader(request).pending_holds, _db(request), user)
    console_url = settings.armoriq_console_url or None
    return [build_hold_view(hold, console_url=console_url) for hold in holds]


@router.get(
    "/manager/unanswered",
    response_model=list[ManagerUnansweredView],
    summary="Unanswered-question queue (no protected snippets)",
)
def list_unanswered(
    request: Request, user: ManagerUser, only_open: bool = True
) -> list[ManagerUnansweredView]:
    items = _call(_reader(request).unanswered, _db(request), user, only_open)
    return [build_unanswered_view(item) for item in items]


@router.get(
    "/manager/topic-rules",
    response_model=ManagerTopicRulesView,
    summary="Topic map (retrieval configuration, not authority)",
)
def list_topic_rules(request: Request, user: ManagerUser) -> ManagerTopicRulesView:
    rules = _call(_topics(request).list_rules, _db(request), DEFAULT_WORKSPACE_ID, user)
    return ManagerTopicRulesView(rules=[build_rule_view(rule) for rule in rules], note=TOPIC_NOTE)


@router.put(
    "/manager/topic-rules",
    response_model=ManagerTopicRuleView,
    summary="Create or update one topic rule (manager session only)",
)
def upsert_topic_rule(
    body: ManagerTopicRuleUpdate, request: Request, user: ManagerUser
) -> ManagerTopicRuleView:
    topics = _topics(request)
    try:
        # Validation before the write: an unusable topic is a client error,
        # while any later store failure is a real service error (503).
        topic = topics.validate_topic(body.topic)
    except AuditStoreError:
        raise ApiError(422, "invalid_topic", _BAD_TOPIC) from None
    rule_id = _call(
        topics.upsert_rule,
        _db(request),
        actor=user,
        workspace_id=DEFAULT_WORKSPACE_ID,
        topic=topic,
        max_tier=body.max_tier,
    )
    return ManagerTopicRuleView(rule_id=str(rule_id), topic=topic, max_tier=body.max_tier)


# --- health (real probes only) -------------------------------------------------


def _probe_tool_server(base_url: str | None) -> bool:
    base = (base_url or "").rstrip("/")
    if not base:
        return False
    url = f"{base}/health"
    try:
        # Settings-controlled URL, never user input; short timeout so the
        # console page cannot hang on a dead tool server.
        with urlopen(url, timeout=_HEALTH_TIMEOUT_SECONDS) as response:  # noqa: S310
            return 200 <= int(getattr(response, "status", 0) or 0) < 300
    except Exception:  # a probe must never raise (HTTP/timeout/DNS/TLS)
        return False


def _probe_database(db: Any) -> bool:
    if db is None:
        return False
    try:
        with db.tx() as cursor:
            cursor.execute("SELECT 1")
        return True
    except Exception:  # a probe must never raise
        return False


def _probe_slack(app: Any) -> bool:
    runtime = getattr(app.state, "slack_runtime", None)
    is_alive = getattr(getattr(runtime, "thread", None), "is_alive", None)
    return bool(callable(is_alive) and is_alive())


def _connector(name: str, connected: bool) -> ManagerConnectorStatus:
    if connected:
        return ManagerConnectorStatus(name=name, status="connected", label=CONNECTED_LABEL)
    return ManagerConnectorStatus(name=name, status="unavailable", label=UNAVAILABLE_LABEL)


def _planned(name: str) -> ManagerConnectorStatus:
    return ManagerConnectorStatus(name=name, status="planned", label=PLANNED_LABEL)


@router.get(
    "/manager/health",
    response_model=ManagerHealthView,
    summary="Real connector checks (tool server, backend DB, Slack socket)",
)
def manager_health(
    request: Request, _user: ManagerUser, settings: SettingsDep
) -> ManagerHealthView:
    db = getattr(request.app.state, "audit_db", None)  # reported, not enforced: health
    return ManagerHealthView(
        service=settings.service_name,
        version=__version__,
        environment=settings.app_env,
        checked_at=datetime.now(UTC),
        connectors=[
            _connector("Tool server", _probe_tool_server(settings.toolserver_base_url)),
            _connector("Backend database", _probe_database(db)),
            _connector("Slack", _probe_slack(request.app)),
            _planned("Gmail"),
            _planned("Google Drive"),
            _planned("Google Meet"),
        ],
    )


__all__ = [
    "ManagerUser",
    "build_hold_view",
    "build_rule_view",
    "build_summary_view",
    "build_timeline_view",
    "build_unanswered_view",
    "require_manager",
    "router",
]
