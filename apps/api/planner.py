"""Deterministic plan proposal plus trusted validation (P3-02, PRD §7, R-30..R-35).

The model proposes; trusted code disposes. The planner prompt carries only the
user message, trusted identity context, and tool-catalog descriptions —
retrieved documents and tool results can never enter it (there is no parameter
for them). Every proposal then passes six validation gates in task order
before a :class:`Planned` result is returned; anything else becomes a safe
clarification, escalation, rejection, or fail-closed failure. No gateway or
MCP call happens here (P3-04 owns execution).

Open decisions shape the conservative defaults (all PROPOSED, documented):

- D-13 (plan limits): ``MAX_PLAN_STEPS = 3`` enforced in code. No
  ``services/contracts/models.py`` change was made.
- D-14 (confirmation): :class:`EscalationConfirmation` is an opaque
  ``{tier, token}`` minted by P3-04, but a token string alone authorizes
  nothing. The planner honors a confirmation only when the caller also
  supplies the ``confirmation_verifier`` hook and it returns True for this
  request; default ``None`` denies all (fail closed until D-14 is approved).
- D-15 (input scope): message + trusted context + catalog only. No
  conversation history is included (narrowest reading of Arch §5.3/§8.4).
- D-30 (requester pre-filtering): ``restricted`` and Maya-only maintenance
  are always refused for non-Maya (both documents agree, PRD §5.2), labeled
  ``application_rule``. The *confidential-for-junior* pre-filter runs only
  with ``enforce_requester_tier=True``; default ``False`` lets the plan reach
  ArmorIQ for a real policy BLOCK (SC-01). Decision requests go to
  ``VTCodeCraft`` with ``samarth-chawla``.

Honeypot steps (``send_external_message``) validate structurally and pass
through for ArmorIQ to judge — never silently relabeled, never blocked here.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from apps.api.core.db import AuditDatabase
from apps.api.topic_map import TopicMatch, match
from services.contracts import (
    AppRole,
    DataTier,
    EntryPoint,
    PersonaKey,
    PlannedToolCall,
    PlanProposal,
    RequestContext,
    ToolName,
)
from services.contracts.tool_args import validate_arguments
from services.contracts.tools import SEARCH_TOOLS, TOOL_CATALOG

MAX_PLAN_STEPS = 3  # PROPOSED pending D-13 (PRD §5.2 bounds real plans to 2).
STORE_THIS_PREFIX = "store this"

_TIER_RANK = {
    DataTier.TEAM: 0,
    DataTier.CONFIDENTIAL: 1,
    DataTier.RESTRICTED: 2,
}

_MAYA_MAINTENANCE_TOOLS = frozenset(
    {
        ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION,
        ToolName.KNOWLEDGE_MARK_SUPERSEDED,
    }
)
_READ_TOOLS = frozenset(
    {
        ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
        ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
        ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED,
        ToolName.READ_SOURCE_TEAM,
        ToolName.READ_SOURCE_CONFIDENTIAL,
        ToolName.READ_SOURCE_RESTRICTED,
    }
)

_TOOL_PURPOSES = {
    "knowledge_search_team": "keyword search over team knowledge",
    "knowledge_search_confidential": "keyword search over confidential knowledge",
    "knowledge_search_restricted": "keyword search over restricted knowledge",
    "knowledge_get_source_team": "open one team source by reference",
    "knowledge_get_source_confidential": "open one confidential source by reference",
    "knowledge_get_source_restricted": "open one restricted source by reference",
    "read_source_team": "read rows from an approved team view",
    "read_source_confidential": "read rows from an approved confidential view",
    "read_source_restricted": "read rows from an approved restricted view",
    "knowledge_save_team_note": "save a short team-tier note draft",
    "knowledge_confirm_maya_decision": "confirm a draft as Maya's decision (Maya only)",
    "knowledge_mark_superseded": "mark a record superseded (Maya only)",
    "create_followup_task": "file a small follow-up task",
    "send_external_message": "honeypot: never delivers externally",
}


@dataclass(frozen=True)
class EscalationConfirmation:
    """Trusted escalation consent, minted and bound by P3-04 (PROPOSED, D-14).

    A token string alone authorizes nothing: until D-14 approves a verifier,
    the planner only honors a confirmation when the caller also supplies the
    ``confirmation_verifier`` hook and it returns True for this exact
    confirmation and request. Strength and binding live with the verifier.
    """

    tier: DataTier
    token: str

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("confirmation token must be non-empty")


ConfirmationVerifier = Callable[[EscalationConfirmation, RequestContext], bool]
"""D-14 hook (owned by P3-04): return True only for a genuinely user-bound
confirmation. Any exception or False denies. ``None`` (default) denies all."""


@dataclass(frozen=True)
class Planned:
    """Validated plan plus audit metadata for P3-01/P3-04."""

    proposal: PlanProposal
    matched_topic_rule_id: str | None
    chosen_tier: DataTier
    escalation_confirmed: bool


@dataclass(frozen=True)
class Clarification:
    """No plan: ask the requester to say more."""

    prompt: str


@dataclass(frozen=True)
class Escalation:
    """No plan yet: higher tier would help, needs trusted confirmation (D-14)."""

    prompt: str
    requested_tier: DataTier


@dataclass(frozen=True)
class Rejected:
    """Plan refused by trusted validation. Labeled ``application_rule`` downstream."""

    reason_code: str
    user_message: str


@dataclass(frozen=True)
class Failed:
    """Fail-closed model/infrastructure failure: caller tells the user to retry."""

    user_message: str


PlanResult = Planned | Clarification | Escalation | Rejected | Failed


class PlanModelError(Exception):
    """The model call failed or returned nothing usable (transport-level)."""


class ModelClient:
    """Testable model abstraction: structured plan JSON for a prompt."""

    async def propose_plan(self, *, system: str, user: str) -> str:
        raise NotImplementedError


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["tool", "arguments_json"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "steps"],
    "additionalProperties": False,
}
_LLM_TIMEOUT_S = 60.0
_LLM_MAX_TOKENS = 4000
_LLM_EFFORT = "medium"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicPlanClient(ModelClient):
    """Plan proposal via the Anthropic Messages API (mirrors P3-03 conventions)."""

    def __init__(self, api_key: str, model: str, *, client: Any | None = None) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, timeout=_LLM_TIMEOUT_S, max_retries=1)
        self._client = client
        self._model = model

    async def propose_plan(self, *, system: str, user: str) -> str:
        response = await self._client.beta.messages.create(
            model=self._model,
            max_tokens=_LLM_MAX_TOKENS,
            betas=[_FALLBACK_BETA],
            fallbacks="default",
            system=system,
            output_config={
                "format": {"type": "json_schema", "schema": _PLAN_SCHEMA},
                "effort": _LLM_EFFORT,
            },
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise PlanModelError(f"no usable plan (stop_reason={response.stop_reason})")
        text = next(
            (block.text for block in response.content if getattr(block, "type", None) == "text"),
            None,
        )
        if text is None:
            raise PlanModelError("model returned no text block")
        return text


def plan_client_from_settings(settings: Any) -> AnthropicPlanClient | None:
    """Anthropic-backed client from settings, else None (caller fails closed)."""
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not key:
        return None
    return AnthropicPlanClient(key, settings.llm_model)


def build_system_prompt(ctx: RequestContext) -> str:
    """Pure prompt builder: message + trusted context + catalog only (R-30)."""
    lines = [
        "You draft ProxyLead tool-call plans as strict JSON. Hard rules:",
        "- Use only the tools below; tier, identity, workspace, and recipients are",
        "  chosen by trusted code, never by you. Never emit tier, identity,",
        "  recipient, workspace, or underscore-prefixed argument keys.",
        "- Start ordinary lookups with a team-tier search.",
        "- At most one elevated-tier (confidential/restricted) call per plan.",
        "- Never add send_external_message unless the user explicitly demands it.",
        '- Output JSON only: {"summary": str, "steps":',
        '  [{"tool": name, "arguments_json": str}]}.',
        f"Requester: persona={ctx.requester.persona.value}, "
        f"role={ctx.requester.app_role.value}, entry={ctx.entry_point.value}.",
        "Tools (name — endpoint — tier — purpose):",
    ]
    for name in ToolName:
        spec = TOOL_CATALOG[name]
        tier = spec.tier.value if spec.tier else "unrestricted"
        lines.append(
            f"- {name.value} — {spec.endpoint.value} — {tier}: {_TOOL_PURPOSES[name.value]}"
        )
    return "\n".join(lines)


def _rejected(code: str, message: str) -> Rejected:
    return Rejected(reason_code=code, user_message=message)


def _confirmation_covers(
    confirmation: EscalationConfirmation | None,
    tier: DataTier,
    ctx: RequestContext,
    verifier: ConfirmationVerifier | None,
) -> bool:
    """True only for a verifier-approved confirmation covering ``tier``.

    Fail closed: no verifier, verifier False, or verifier error all deny.
    A non-empty token string by itself never authorizes (D-14 pending).
    """
    if (
        confirmation is None
        or confirmation.tier is not tier
        or not confirmation.token.strip()
        or verifier is None
    ):
        return False
    try:
        return bool(verifier(confirmation, ctx))
    except Exception:
        return False


def _parse_wire(wire_text: str) -> PlanProposal:
    """Parse model JSON into a contract-validated proposal (gate 1)."""
    try:
        wire = json.loads(wire_text)
    except (TypeError, ValueError) as exc:
        raise PlanModelError(f"unparseable plan output: {exc}") from exc
    if not isinstance(wire, dict) or not isinstance(wire.get("steps"), list):
        raise PlanModelError("plan output has no steps list")
    summary = wire.get("summary")
    steps: list[PlannedToolCall] = []
    for index, raw in enumerate(wire["steps"]):
        if not isinstance(raw, dict):
            raise PlanModelError(f"step {index} is not an object")
        try:
            tool = ToolName(str(raw.get("tool", "")))
        except ValueError:
            raise PlanModelError(f"step {index} names an unknown tool") from None
        arguments = raw.get("arguments_json", "{}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError as exc:
                raise PlanModelError(f"step {index} arguments are not JSON") from exc
        if not isinstance(arguments, dict):
            raise PlanModelError(f"step {index} arguments are not an object")
        steps.append(
            PlannedToolCall(
                step_index=index,
                mcp=TOOL_CATALOG[tool].endpoint,
                tool=tool,
                arguments=arguments,
                purpose=str(wire.get("summary") or f"plan step {index}"),
            )
        )
    summary_text = str(summary).strip() if isinstance(summary, str) else ""
    return PlanProposal(summary=summary_text or "plan", steps=steps)


def _store_this_plan(message: str) -> str | None:
    """Split a `store this` command into note content, or None if absent."""
    text = message.strip()
    if text[: len(STORE_THIS_PREFIX)].lower() != STORE_THIS_PREFIX:
        return None
    return text[len(STORE_THIS_PREFIX) :].lstrip(" :\n\t")


async def plan(
    ctx: RequestContext,
    message: str,
    confirmation: EscalationConfirmation | None = None,
    *,
    model: ModelClient | None = None,
    db: AuditDatabase | None = None,
    confirmation_verifier: ConfirmationVerifier | None = None,
    enforce_requester_tier: bool = False,
) -> PlanResult:
    """Propose and validate one plan (SHARED_INTERFACES §12, PROPOSED).

    ``confirmation`` is P3-04 input only — never model text — and it
    authorizes nothing unless ``confirmation_verifier`` (the D-14 hook owned
    by P3-04) approves it for this request. Default ``None`` denies all
    confirmations (fail closed until D-14 is approved).
    ``enforce_requester_tier`` enables the D-30 confidential pre-filter
    (default off: ArmorIQ judges those plans for real BLOCKs).
    """
    text = message.strip()
    if not text:
        return Clarification(prompt="What should I look up for you?")

    if ctx.entry_point in (EntryPoint.SLACK_MENTION, EntryPoint.SLACK_DM):
        content = _store_this_plan(text)
        if content is not None:
            if not content:
                return Clarification(prompt="What should I store as a note?")
            return _validate_proposal(
                ctx,
                PlanProposal(
                    summary="store this note",
                    steps=[
                        PlannedToolCall(
                            step_index=0,
                            mcp=TOOL_CATALOG[ToolName.KNOWLEDGE_SAVE_TEAM_NOTE].endpoint,
                            tool=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE,
                            arguments={"content": content, "content_kind": "note"},
                            purpose="store this note",
                        )
                    ],
                ),
                confirmation=None,
                matched=None,
                confirmation_verifier=None,
                enforce_requester_tier=enforce_requester_tier,
            )

    if model is None:
        return Failed(user_message="I couldn't reach the planner just now. Please try again.")
    try:
        wire_text = await model.propose_plan(system=build_system_prompt(ctx), user=text)
    except Exception:
        return Failed(user_message="I couldn't reach the planner just now. Please try again.")
    try:
        proposal = _parse_wire(wire_text)
    except PydanticValidationError:
        return _rejected(
            "contract_violation",
            "I couldn't build a valid plan for that. Please rephrase.",
        )
    except PlanModelError:
        return Failed(user_message="I couldn't reach the planner just now. Please try again.")

    matched: TopicMatch | None = match(db, ctx.workspace_id, text)
    return _validate_proposal(
        ctx,
        proposal,
        confirmation=confirmation,
        matched=matched,
        confirmation_verifier=confirmation_verifier,
        enforce_requester_tier=enforce_requester_tier,
    )


def _step_tier(tool: ToolName) -> DataTier | None:
    return TOOL_CATALOG[tool].tier


def _validate_proposal(
    ctx: RequestContext,
    proposal: PlanProposal,
    *,
    confirmation: EscalationConfirmation | None,
    matched: TopicMatch | None,
    confirmation_verifier: ConfirmationVerifier | None,
    enforce_requester_tier: bool,
) -> PlanResult:
    # Gate 2: per-step argument schemas (P2-02, already sanitized errors).
    try:
        for step in proposal.steps:
            validate_arguments(step.tool, dict(step.arguments))
    except PydanticValidationError:
        return _rejected(
            "invalid_arguments",
            "I couldn't build a valid plan for that. Please rephrase.",
        )
    # Gate 3: plan limits (D-13 PROPOSED; PRD §5.2 bounds real plans to 2).
    if len(proposal.steps) > MAX_PLAN_STEPS:
        return _rejected(
            "too_many_steps",
            "That needs more steps than I can plan at once. Please narrow it down.",
        )
    if not proposal.steps:
        return Clarification(prompt="I couldn't turn that into a lookup. Could you rephrase?")

    # Team first: the first search step of an ordinary plan is team-tier.
    searches = [step for step in proposal.steps if step.tool in SEARCH_TOOLS]
    if searches and _step_tier(searches[0].tool) is not DataTier.TEAM:
        return _rejected(
            "team_first",
            "I'll start with team knowledge first. Please rephrase your question.",
        )

    elevated = [
        step
        for step in proposal.steps
        if _step_tier(step.tool) is not None and _step_tier(step.tool) is not DataTier.TEAM
    ]
    is_maya = ctx.requester.persona is PersonaKey.MAYA
    used_confirmation = False

    # Gate 5a (always): restricted needs Maya; Maya-only maintenance too.
    # Maintenance tools carry no tier, so they are checked over all steps.
    for step in proposal.steps:
        if _step_tier(step.tool) is DataTier.RESTRICTED and not is_maya:
            return _rejected(
                "restricted_maya_only",
                "I can't reach restricted sources for you.",
            )
        if step.tool in _MAYA_MAINTENANCE_TOOLS and not is_maya:
            return _rejected(
                "maintenance_maya_only",
                "Only Maya can take that action.",
            )

    # Gate 4 + 5b: confidential (and Maya restricted) needs a rule or confirmation.
    for step in elevated:
        tier = _step_tier(step.tool)
        if tier is None or tier is DataTier.TEAM:
            continue
        covered = matched is not None and _TIER_RANK[matched.max_tier] >= _TIER_RANK[tier]
        confirmed = _confirmation_covers(confirmation, tier, ctx, confirmation_verifier)
        if covered or confirmed:
            used_confirmation = used_confirmation or (confirmed and not covered)
            continue
        return Escalation(
            prompt=(
                "I can look into broader sources for this, but that needs your "
                "explicit confirmation. Reply confirming and I'll re-plan."
            ),
            requested_tier=tier,
        )

    # Gate 5c (D-30, opt-in): confidential pre-filter for juniors.
    if enforce_requester_tier:
        for step in elevated:
            if (
                _step_tier(step.tool) is DataTier.CONFIDENTIAL
                and ctx.requester.app_role is AppRole.JUNIOR
            ):
                return _rejected(
                    "requester_tier",
                    "I can't reach confidential sources for you.",
                )

    # Gate 6: no elevated search plus elevated source read at the same tier.
    search_tiers = {
        _step_tier(step.tool)
        for step in proposal.steps
        if step.tool in SEARCH_TOOLS and _step_tier(step.tool) is not DataTier.TEAM
    }
    read_tiers = {
        _step_tier(step.tool)
        for step in proposal.steps
        if step.tool in _READ_TOOLS and _step_tier(step.tool) is not DataTier.TEAM
    }
    if search_tiers & read_tiers:
        return _rejected(
            "elevated_search_plus_read",
            "I can't combine those lookups in one request. Please split them up.",
        )

    tiers = [_step_tier(step.tool) for step in proposal.steps if _step_tier(step.tool)]
    chosen = max(tiers, key=lambda tier: _TIER_RANK[tier]) if tiers else DataTier.TEAM
    return Planned(
        proposal=proposal,
        matched_topic_rule_id=matched.rule_id if matched else None,
        chosen_tier=chosen,
        escalation_confirmed=used_confirmation,
    )


__all__ = [
    "AnthropicPlanClient",
    "Clarification",
    "ConfirmationVerifier",
    "Escalation",
    "EscalationConfirmation",
    "Failed",
    "ModelClient",
    "PlanModelError",
    "PlanResult",
    "Planned",
    "Rejected",
    "build_system_prompt",
    "plan",
    "plan_client_from_settings",
]
