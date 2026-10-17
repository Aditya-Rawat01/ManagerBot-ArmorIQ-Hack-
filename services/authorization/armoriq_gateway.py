"""ArmorIQ gateway: the ONLY module allowed to import ``armoriq_sdk``.

P1-02. Every other backend module talks to ArmorIQ through
:class:`ArmorIQGateway`; probes and ``scripts/`` call this gateway instead of
the SDK (ADR-018, enforced by ``tests/api/test_guardrails.py``).

Source-verified against ``armoriq-sdk==0.6.10`` (see ``docs/SECURITY.md``).
Live behavior is confirmed or corrected only by E1-E4; the decision mapping in
this module is labeled provisional where the evidence is still open.

Fail-closed contract (PRD §0.6): any exception, timeout, or unverifiable state
becomes ``block``/``unverified`` with a truthful ``DecisionSource``. Never
``allow``. ``armoriq_sdk_plan_check``, ``tool_server_rule``,
``application_rule``, and ``unknown`` are never reported as ArmorIQ policy
decisions (ADR-026).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import armoriq_sdk
from armoriq_sdk import ArmorIQClient, InvokeOptions
from armoriq_sdk import exceptions as sdk_exceptions
from armoriq_sdk.observability import ObservabilityConfig
from armoriq_sdk.session import SessionOptions

from services.authorization.ctx_envelope import CtxEnvelope, sign_ctx
from services.contracts import (
    TOOL_CATALOG,
    ApprovalState,
    ApprovalStatus,
    ArmorIQDecision,
    AuthenticatedUser,
    DecisionSource,
    PlanProposal,
    RequestContext,
    ToolExecutionStatus,
    ToolInvocationResult,
    ToolName,
)

logger = logging.getLogger(__name__)

SDK_VERSION = armoriq_sdk.VERSION

# Short `_ctx` window chosen by the gateway; P1-04 owns envelope semantics and
# validation. Always well within the intent-token lifetime.
CTX_TTL_S = 300

# Tool-result shape (SHARED_INTERFACES §4, emitted by P2-06): the payload is the
# MCP CallToolResult. Success: `isError: false`, ledger reference in
# `_meta.ledger_entry_id`. Rejection: `isError: true`, text block
# `{"code", "message"}`, optional `_meta.ledger_entry_id` (the call's own row) or
# `_meta.original_entry_id` (replay). Top-level `ledger_entry_id`/`error_code`
# keys are still accepted (earlier provisional shape).
#
# `not_found` (D-11, P2-08) is the one `isError` code that is NOT a rejection:
# the authorized lookup executed and found nothing visible, with its own
# accepted/executed ledger row in `_meta.ledger_entry_id`. It maps to an
# EXECUTED invocation with no result (no evidence) and keeps the ArmorIQ
# decision; it is never turned into a `tool_server_rule` block. Without a
# ledger reference it cannot be proven executed and becomes `internal`.
_LEDGER_KEYS = ("ledger_entry_id", "ledgerEntryId")
NOT_FOUND_CODE = "not_found"
_TOOL_SERVER_CODES = frozenset(
    {
        "unauthenticated",
        "invalid_context",
        "context_expired",
        "replay",
        "invalid_arguments",
        "out_of_scope",
        "precondition_failed",
        NOT_FOUND_CODE,
        "internal",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class PlanHandle:
    """One ArmorIQ plan for one request (PRD §7 step 5).

    ``decision`` is set only when plan creation or minting failed; callers must
    check it before invoking steps. ``armoriq_plan_id`` is populated only when
    the SDK exposes it (E1 confirms).
    """

    request_id: str
    plan_id: str
    member_id: str
    step_count: int
    token_expires_at: datetime | None
    armoriq_plan_id: str | None = None
    decision: ArmorIQDecision | None = None
    _token: Any = field(default=None, repr=False, compare=False)
    _email: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class StepOutcome:
    """Result of one planned step: authorization, approval, and execution."""

    decision: ArmorIQDecision
    approval: ApprovalStatus | None = None
    invocation: ToolInvocationResult | None = None


# P1-04 owns the `_ctx` contract. The default signer below adapts the gateway's
# fields to `CtxEnvelope` and calls the real `sign_ctx`; tests may inject a fake
# behind this narrow protocol. This module never redefines the envelope.
CtxSigner = Callable[[Mapping[str, Any], str], str]

# P3-04: told about each SDK hold (a ``pending`` ApprovalStatus) while the SDK
# is still waiting, so the orchestrator can record the HOLD and DM Maya
# before the console decision. Called on the SDK worker thread.
HoldObserver = Callable[[ApprovalStatus], None]


# ArmorIQ's delegation (approval) API rejects amounts below 0.01, but
# armoriq-sdk 0.6.10 ``invoke_with_policy`` sends ``policy_context.amount or 0``
# for tools without an amount field (all of ours), so every HOLD failed with
# "Action held for approval (delegation request failed)" (E3, 18 Oct 2026).
# The SDK's own session path clamps to 0.01 (session.py); do the same here.
MIN_DELEGATION_AMOUNT = 0.01


def _clamp_delegation_amount(client: Any) -> None:
    """Wrap ``client.create_delegation_request`` once so amount >= 0.01.

    Only the approval request's amount changes; the decision, tool, plan and
    arguments are untouched, and a HOLD still waits for the console decision.
    """
    original = getattr(client, "create_delegation_request", None)
    if original is None or getattr(original, "_proxylead_clamped", False):
        return

    def _clamped(params: Any, *args: Any, **kwargs: Any) -> Any:
        amount = getattr(params, "amount", None)
        if not isinstance(amount, int | float) or amount < MIN_DELEGATION_AMOUNT:
            if hasattr(params, "model_copy"):
                params = params.model_copy(update={"amount": MIN_DELEGATION_AMOUNT})
            else:
                params.amount = MIN_DELEGATION_AMOUNT
        return original(params, *args, **kwargs)

    _clamped._proxylead_clamped = True  # type: ignore[attr-defined]
    try:
        client.create_delegation_request = _clamped
    except (AttributeError, TypeError):  # read-only fake/stub: leave it alone
        pass


def sign_ctx_fields(fields: Mapping[str, Any], secret: str) -> str:
    """Default signer: P1-04 ``sign_ctx`` (``iat``/``exp`` as epoch seconds)."""
    values = dict(fields)
    for key in ("iat", "exp"):
        if isinstance(values[key], datetime):
            values[key] = values[key].timestamp()
    return sign_ctx(CtxEnvelope(**values), secret)


def _secret_value(secret: Any) -> str | None:
    if secret is None:
        return None
    get_value = getattr(secret, "get_secret_value", None)
    value = get_value() if callable(get_value) else secret
    return value or None


class _MemberClients:
    """One SDK client plus one async lock per member identity (R-12)."""

    def __init__(self, client_factory: Callable[[str], Any]) -> None:
        self._factory = client_factory
        self._clients: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = threading.Lock()

    def get(self, member_id: str) -> tuple[Any, asyncio.Lock]:
        with self._guard:
            if member_id not in self._clients:
                self._clients[member_id] = self._factory(member_id)
                self._locks[member_id] = asyncio.Lock()
            return self._clients[member_id], self._locks[member_id]

    def __len__(self) -> int:
        with self._guard:
            return len(self._clients)


class ArmorIQGateway:
    """Single ArmorIQ entry point for the backend (P1-02).

    ``settings`` is the API ``Settings`` object (or anything exposing the same
    attribute names). ``client_factory`` and ``ctx_signer`` are seams for
    offline tests; ``ctx_signer`` defaults to P1-04's ``sign_ctx``.
    """

    def __init__(
        self,
        settings: Any,
        *,
        client_factory: Callable[[str], Any] | None = None,
        ctx_signer: CtxSigner | None = None,
    ) -> None:
        self._settings = settings
        self._ctx_signer: CtxSigner = ctx_signer or sign_ctx_fields
        self._token_validity_s = int(getattr(settings, "token_validity_s", 1200))
        self._approval_timeout_ms = int(getattr(settings, "approval_timeout_ms", 900_000))
        self._agent_id = str(getattr(settings, "armoriq_agent_id", "proxylead"))
        self._observability = getattr(settings, "armoriq_observability", False) is True
        self._member_clients = _MemberClients(client_factory or self._build_client)

    # -- identity ---------------------------------------------------------

    def member_email(self, requester: AuthenticatedUser) -> str | None:
        """Trusted member email for one requester, or ``None`` when unmapped.

        Reads only the server-side persona→email mapping (P1-03 owns the
        resolver; the gateway never trusts message text or request bodies).
        """
        persona = requester.persona.value
        attr = f"persona_{persona}_email"
        return _secret_value(getattr(self._settings, attr, None))

    def _email_ref(self, requester: AuthenticatedUser) -> str:
        return f"PERSONA_{requester.persona.value.upper()}_EMAIL"

    def _build_client(self, member_id: str) -> Any:
        key = _secret_value(getattr(self._settings, "armoriq_api_key", None))
        if not key:
            raise sdk_exceptions.ConfigurationException("ARMORIQ_API_KEY is not configured")
        client = ArmorIQClient(api_key=key, agent_id=self._agent_id)
        _clamp_delegation_amount(client)
        return client

    # -- plans ------------------------------------------------------------

    async def start_plan(self, ctx: RequestContext, plan: PlanProposal) -> PlanHandle:
        """Create one ArmorIQ plan and mint once for this request.

        Never raises: failures are returned as a ``PlanHandle`` carrying a
        ``block``/``unverified`` decision.
        """
        requester = ctx.requester
        email = self.member_email(requester)
        if email is None:
            return self._failed_handle(
                ctx,
                plan,
                DecisionSource.APPLICATION_RULE,
                "unverified",
                "no mapped ArmorIQ email for this requester",
            )
        try:
            client, lock = self._member_clients.get(requester.member_id)
        except sdk_exceptions.ArmorIQException as exc:
            return self._failed_handle(
                ctx,
                plan,
                DecisionSource.APPLICATION_RULE,
                "unverified",
                f"client unavailable: {type(exc).__name__}",
            )
        except Exception as exc:  # fail closed on anything unexpected
            logger.warning(
                "gateway start_plan client error request_id=%s err=%s",
                ctx.request_id,
                type(exc).__name__,
            )
            return self._failed_handle(
                ctx,
                plan,
                DecisionSource.UNKNOWN,
                "unverified",
                "client construction failed",
            )
        async with lock:
            try:
                token = await asyncio.to_thread(
                    self._mint_token, client, email, plan, ctx.request_id
                )
            except sdk_exceptions.PolicyBlockedException as exc:
                return self._failed_handle(
                    ctx,
                    plan,
                    DecisionSource.ARMORIQ_POLICY_DECISION,
                    "block",
                    (exc.reason or str(exc) or "mint blocked by policy")[:300],
                    enforcement_action=exc.enforcement_action,
                )
            except (
                sdk_exceptions.InvalidTokenException,
                sdk_exceptions.RevokedTokenError,
            ) as exc:
                return self._failed_handle(
                    ctx,
                    plan,
                    DecisionSource.ARMORIQ_ISSUANCE,
                    "unverified",
                    f"mint failed at ArmorIQ: {type(exc).__name__}",
                )
            except sdk_exceptions.ArmorIQException as exc:
                logger.warning(
                    "gateway start_plan sdk error request_id=%s err=%s",
                    ctx.request_id,
                    type(exc).__name__,
                )
                return self._failed_handle(
                    ctx,
                    plan,
                    DecisionSource.UNKNOWN,
                    "unverified",
                    f"mint failed: {type(exc).__name__}",
                )
            except Exception as exc:
                logger.warning(
                    "gateway start_plan error request_id=%s err=%s",
                    ctx.request_id,
                    type(exc).__name__,
                )
                return self._failed_handle(
                    ctx,
                    plan,
                    DecisionSource.UNKNOWN,
                    "unverified",
                    "mint failed with an unexpected error",
                )
        plan_id = _new_id("p")
        armoriq_plan_id = getattr(token, "plan_id", None) or None
        expires_at = self._token_expiry(token)
        logger.info(
            "gateway plan minted request_id=%s trace_id=%s plan_id=%s "
            "steps=%d outcome=allow source=armoriq_issuance",
            ctx.request_id,
            ctx.trace_id,
            plan_id,
            len(plan.steps),
        )
        return PlanHandle(
            request_id=ctx.request_id,
            plan_id=plan_id,
            member_id=requester.member_id,
            step_count=len(plan.steps),
            token_expires_at=expires_at,
            armoriq_plan_id=armoriq_plan_id,
            decision=None,
            _token=token,
            _email=email,
        )

    def _session_options(self) -> SessionOptions:
        """Options for the per-mint session.

        The SDK's observability shipper is on by default and uploads spans
        (plan goal text included) to ArmorIQ from a background thread, with a
        blocking flush on ``close()``. An explicit ``enabled=False`` turns it
        off whatever ``ARMORIQ_OBSERVABILITY`` says in the process
        environment. ``None`` (opt-in) leaves the decision to the SDK.
        """
        return SessionOptions(
            mode="proxy",
            validity_seconds=self._token_validity_s,
            llm="proxylead",
            observability=None if self._observability else ObservabilityConfig(enabled=False),
        )

    def _mint_token(self, client: Any, email: str, plan: PlanProposal, request_id: str) -> Any:
        """Blocking SDK section: one session, one plan, one mint (worker thread)."""
        scoped = client.for_user(email)
        session = scoped.start_session(self._session_options())
        try:
            # The SDK plan builder needs `<mcp>__<action>` names (a plan can
            # span both endpoints, so no single default MCP fits). It splits
            # them back into each step's (mcp, action); `invoke` then finds
            # the step by that action.
            tool_calls = [
                {"name": f"{step.mcp.value}__{step.tool.value}", "args": dict(step.arguments)}
                for step in plan.steps
            ]
            return session.start_plan(tool_calls, goal=plan.summary[:500])
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("gateway session close failed request_id=%s", request_id)

    # -- steps ------------------------------------------------------------

    async def invoke_step(
        self,
        handle: PlanHandle,
        step_index: int,
        ctx: RequestContext,
        plan: PlanProposal,
        *,
        on_hold: HoldObserver | None = None,
    ) -> StepOutcome:
        """Invoke one planned step under the handle's plan and token.

        The step's arguments are read from ``plan`` (the validated, stored
        plan): ArmorIQ binds tool names, not argument values (E2), so callers
        must pass the exact plan object they validated and minted.

        ``on_hold`` (P3-04) is called once per SDK hold, with a ``pending``
        :class:`ApprovalStatus`, while the SDK is still waiting for the
        console decision. It runs on the SDK worker thread: it must be quick,
        thread-safe and never raise (an exception is logged by class and
        ignored; it can never turn a HOLD into an approval). The returned
        :class:`StepOutcome` still carries the resolved decision.
        """
        if handle.decision is not None:
            return StepOutcome(decision=handle.decision)
        if step_index < 0 or step_index >= handle.step_count:
            return StepOutcome(
                decision=self._decision(
                    "block",
                    DecisionSource.APPLICATION_RULE,
                    "step index outside the minted plan",
                    handle,
                )
            )
        if handle.member_id != ctx.requester.member_id:
            return StepOutcome(
                decision=self._decision(
                    "block",
                    DecisionSource.APPLICATION_RULE,
                    "plan handle does not belong to this requester",
                    handle,
                )
            )
        token = handle._token
        if token is None or self._token_expired(token):
            return StepOutcome(
                decision=self._decision(
                    "unverified",
                    DecisionSource.UNKNOWN,
                    "intent token missing or expired; retry with a fresh request",
                    handle,
                )
            )
        step = plan.steps[step_index]
        jti = uuid.uuid4().hex
        signed_ctx = self._sign_step_ctx(handle, ctx, plan, step, jti)
        if signed_ctx is None:
            return StepOutcome(
                decision=self._decision(
                    "unverified",
                    DecisionSource.UNKNOWN,
                    "signed context unavailable; P1-04 signer not merged",
                    handle,
                )
            )
        arguments = dict(step.arguments)
        arguments["_ctx"] = signed_ctx
        try:
            client, lock = self._member_clients.get(handle.member_id)
        except Exception:
            return StepOutcome(
                decision=self._decision(
                    "unverified", DecisionSource.UNKNOWN, "client unavailable", handle
                )
            )
        observer = self._hold_observer(handle, ctx, step, on_hold) if on_hold else None
        async with lock:
            try:
                result, holds = await asyncio.to_thread(
                    self._invoke_policy,
                    client,
                    step,
                    token,
                    arguments,
                    handle._email or "",
                    jti,
                    observer,
                )
            except sdk_exceptions.PolicyBlockedException as exc:
                return self._blocked(handle, ctx, step, jti, exc)
            except sdk_exceptions.PolicyHoldException as exc:
                return await self._resolve_hold(handle, ctx, step, jti, exc)
            except sdk_exceptions.IntentMismatchException:
                logger.warning(
                    "gateway plan mismatch request_id=%s step=%d jti=%s",
                    ctx.request_id,
                    step_index,
                    jti,
                )
                return StepOutcome(
                    decision=self._decision(
                        "block",
                        DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
                        "planned step rejected by the SDK plan check",
                        handle,
                    )
                )
            except sdk_exceptions.DelegationException as exc:
                return self._resolve_delegation_error(handle, ctx, step, jti, exc)
            except (
                sdk_exceptions.InvalidTokenException,
                sdk_exceptions.RevokedTokenError,
            ):
                return StepOutcome(
                    decision=self._decision(
                        "unverified",
                        DecisionSource.UNKNOWN,
                        "intent token rejected during invocation",
                        handle,
                    )
                )
            except sdk_exceptions.ArmorIQException as exc:
                logger.warning(
                    "gateway invoke sdk error request_id=%s step=%d jti=%s err=%s",
                    ctx.request_id,
                    step_index,
                    jti,
                    type(exc).__name__,
                )
                return StepOutcome(
                    decision=self._decision(
                        "unverified",
                        DecisionSource.UNKNOWN,
                        f"invocation failed: {type(exc).__name__}",
                        handle,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "gateway invoke error request_id=%s step=%d jti=%s err=%s",
                    ctx.request_id,
                    step_index,
                    jti,
                    type(exc).__name__,
                )
                return StepOutcome(
                    decision=self._decision(
                        "unverified",
                        DecisionSource.UNKNOWN,
                        "invocation failed with an unexpected error",
                        handle,
                    )
                )
        return self._map_success(handle, ctx, step, jti, result, holds)

    def _hold_observer(
        self, handle: PlanHandle, ctx: RequestContext, step: Any, on_hold: HoldObserver
    ) -> Callable[[Any, datetime], None]:
        """Adapt the SDK's ``HoldInfo`` to a pending ``ApprovalStatus`` for ``on_hold``.

        Never raises: a shape that does not validate, or an observer error, is
        logged by exception class and dropped (the hold itself is unaffected).
        """

        def _observe(hold: Any, observed_at: datetime) -> None:
            try:
                approval = ApprovalStatus(
                    request_id=ctx.request_id,
                    plan_id=handle.plan_id,
                    step_index=step.step_index,
                    tool=step.tool,
                    state=ApprovalState.PENDING,
                    delegation_id=getattr(hold, "delegation_id", None) or None,
                    requested_at=observed_at,
                )
                on_hold(approval)
            except Exception as exc:
                logger.warning(
                    "gateway hold observer failed request_id=%s step=%d err=%s",
                    ctx.request_id,
                    step.step_index,
                    type(exc).__name__,
                )

        return _observe

    def _invoke_policy(
        self,
        client: Any,
        step: Any,
        token: Any,
        arguments: dict[str, Any],
        email: str,
        jti: str,
        observer: Callable[[Any, datetime], None] | None = None,
    ) -> tuple[Any, list[tuple[Any, datetime]]]:
        """Blocking SDK section (worker thread, never the event loop).

        Returns ``(result, holds)`` where ``holds`` records every ``on_hold``
        callback firing as ``(HoldInfo, observed_at)``. The list is local to
        this call, so concurrent requests never share hold state.
        ``observer`` (from :meth:`_hold_observer`) learns of each hold while
        the SDK is still waiting.
        """
        seen: list[tuple[Any, datetime]] = []

        def _on_hold(hold: Any) -> None:
            observed_at = _now()
            seen.append((hold, observed_at))
            logger.info(
                "gateway hold mcp=%s tool=%s jti=%s delegation_id=%s",
                step.mcp.value,
                step.tool.value,
                jti,
                getattr(hold, "delegation_id", None),
            )
            if observer is not None:
                observer(hold, observed_at)

        result = client.invoke_with_policy(
            step.mcp.value,
            step.tool.value,
            token,
            params=arguments,
            options=InvokeOptions(
                wait_for_approval=True,
                delegation_timeout_ms=self._approval_timeout_ms,
                on_hold=_on_hold,
                user_email=email,
            ),
        )
        return result, seen

    async def attempt_off_plan(
        self, handle: PlanHandle, tool: ToolName, arguments: Mapping[str, Any], ctx: RequestContext
    ) -> ArmorIQDecision:
        """Route a non-planned tool request through the SDK plan-check path.

        Never executes. Returns ``BLOCK`` + ``armoriq_sdk_plan_check`` only when
        the SDK raises its mismatch exception before any network call;
        everything else is labeled per the evidence (``unknown`` until E2).
        """
        if handle.decision is not None:
            return handle.decision
        token = handle._token
        if token is None or self._token_expired(token):
            return self._decision(
                "unverified", DecisionSource.UNKNOWN, "no live plan to check against", handle
            )
        try:
            client, lock = self._member_clients.get(handle.member_id)
        except Exception:
            return self._decision(
                "unverified", DecisionSource.UNKNOWN, "client unavailable", handle
            )
        mcp = TOOL_CATALOG[tool].endpoint.value
        async with lock:
            try:
                await asyncio.to_thread(
                    client.invoke, mcp, tool.value, token, dict(arguments), None, handle._email
                )
            except sdk_exceptions.IntentMismatchException:
                logger.info(
                    "gateway off-plan blocked request_id=%s tool=%s source=armoriq_sdk_plan_check",
                    ctx.request_id,
                    tool.value,
                )
                return self._decision(
                    "block",
                    DecisionSource.ARMORIQ_SDK_PLAN_CHECK,
                    "off-plan attempt rejected by the SDK plan check; never executed",
                    handle,
                )
            except sdk_exceptions.ArmorIQException as exc:
                logger.warning(
                    "gateway off-plan sdk error request_id=%s tool=%s err=%s",
                    ctx.request_id,
                    tool.value,
                    type(exc).__name__,
                )
                return self._decision(
                    "unverified",
                    DecisionSource.UNKNOWN,
                    f"off-plan check inconclusive: {type(exc).__name__}; never executed",
                    handle,
                )
            except Exception as exc:
                logger.warning(
                    "gateway off-plan error request_id=%s tool=%s err=%s",
                    ctx.request_id,
                    tool.value,
                    type(exc).__name__,
                )
                return self._decision(
                    "unverified",
                    DecisionSource.UNKNOWN,
                    "off-plan check inconclusive; never executed",
                    handle,
                )
        # The proxy accepted an off-plan call: alarming, never ALLOW, E2 investigates.
        logger.warning(
            "gateway off-plan reached proxy request_id=%s tool=%s", ctx.request_id, tool.value
        )
        return self._decision(
            "block",
            DecisionSource.UNKNOWN,
            "off-plan call was not stopped client-side; blocked without execution pending E2",
            handle,
        )

    # -- HOLD -------------------------------------------------------------

    async def _resolve_hold(
        self, handle: PlanHandle, ctx: RequestContext, step: Any, jti: str, exc: Any
    ) -> StepOutcome:
        """Map a hold that surfaced as ``PolicyHoldException``.

        Reaching this handler means the SDK did not wait (or delegation
        creation failed): approved resumes return through the success path.
        """
        message = str(exc) or ""
        delegation_ctx = getattr(exc, "delegation_context", None) or {}
        metadata = getattr(exc, "metadata", None) or {}
        delegation_id = (
            delegation_ctx.get("delegationId")
            or delegation_ctx.get("delegation_id")
            or metadata.get("delegationId")
        )
        lowered = message.lower()
        if "reject" in lowered:
            state: str = "rejected"
        elif "expir" in lowered:
            state = "expired"
        elif "timeout" in lowered or "timed out" in lowered:
            state = "timeout"
        else:
            logger.info(
                "gateway hold unresolved request_id=%s step=%d jti=%s",
                ctx.request_id,
                step.step_index,
                jti,
            )
            invocation = ToolInvocationResult(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                jti=jti,
                status=ToolExecutionStatus.NOT_REACHED,
                tool_server_reached=False,
            )
            return StepOutcome(
                decision=self._decision(
                    "unverified",
                    DecisionSource.UNKNOWN,
                    "hold did not resolve to a known state; no execution, no disclosure",
                    handle,
                    delegation_id=delegation_id,
                ),
                invocation=invocation,
            )
        return self._hold_terminal(handle, ctx, step, jti, state, delegation_id)

    def _resolve_delegation_error(
        self, handle: PlanHandle, ctx: RequestContext, step: Any, jti: str, exc: Any
    ) -> StepOutcome:
        """Map terminal HOLD states that surface as ``DelegationException``.

        Verified SDK contract (``client.py:1761-1788``): the wait loop polls by
        delegation ID; ``approved`` resumes via retry (success path);
        ``rejected``/``expired`` raise ``DelegationException("Delegation
        {status}", delegation_id=...)``; deadline expiry raises
        ``DelegationException("Delegation approval timed out after Ns",
        delegation_id=...)``. Status words are matched only in these verified
        shapes — anything else fails closed as ``unverified``/``unknown``.
        """
        delegation_id = getattr(exc, "delegation_id", None)
        lowered = (str(exc) or "").lower()
        if "delegation rejected" in lowered:
            state: str = "rejected"
        elif "delegation expired" in lowered:
            state = "expired"
        elif "delegation" in lowered and "timed out" in lowered:
            state = "timeout"
        else:
            logger.warning(
                "gateway delegation error request_id=%s step=%d jti=%s err=%s",
                ctx.request_id,
                step.step_index,
                jti,
                type(exc).__name__,
            )
            return StepOutcome(
                decision=self._decision(
                    "unverified",
                    DecisionSource.UNKNOWN,
                    f"delegation error: {type(exc).__name__}; no execution, no disclosure",
                    handle,
                    delegation_id=delegation_id,
                )
            )
        return self._hold_terminal(handle, ctx, step, jti, state, delegation_id)

    def _hold_terminal(
        self,
        handle: PlanHandle,
        ctx: RequestContext,
        step: Any,
        jti: str,
        state: str,
        delegation_id: str | None,
    ) -> StepOutcome:
        """Terminal hold record: rejected/expired deny, timeout is unverifiable.

        Never executes, never discloses. ``ApprovalStatus`` is recorded for all
        three states whenever the shape validates; a validation failure drops
        the record (never the fail-closed decision).
        """
        states = {
            "rejected": (ApprovalState.REJECTED, "block", DecisionSource.ARMORIQ_POLICY_DECISION),
            "expired": (ApprovalState.EXPIRED, "block", DecisionSource.ARMORIQ_POLICY_DECISION),
            "timeout": (ApprovalState.TIMEOUT, "unverified", DecisionSource.UNKNOWN),
        }
        approval_state, outcome, source = states[state]
        approval: ApprovalStatus | None = None
        try:
            approval = ApprovalStatus(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                state=approval_state,
                delegation_id=delegation_id,
                requested_at=_now(),
                resolved_at=_now(),
            )
        except Exception:
            approval = None
        logger.info(
            "gateway hold resolved request_id=%s step=%d jti=%s state=%s",
            ctx.request_id,
            step.step_index,
            jti,
            state,
        )
        invocation = ToolInvocationResult(
            request_id=ctx.request_id,
            plan_id=handle.plan_id,
            step_index=step.step_index,
            tool=step.tool,
            jti=jti,
            status=ToolExecutionStatus.NOT_REACHED,
            tool_server_reached=False,
        )
        return StepOutcome(
            decision=self._decision(
                outcome,  # type: ignore[arg-type]
                source,
                f"hold {state}: no execution, no disclosure",
                handle,
                delegation_id=delegation_id,
            ),
            approval=approval,
            invocation=invocation,
        )

    # -- mapping helpers --------------------------------------------------

    def _map_success(
        self,
        handle: PlanHandle,
        ctx: RequestContext,
        step: Any,
        jti: str,
        result: Any,
        holds: list[tuple[Any, datetime]],
    ) -> StepOutcome:
        payload = getattr(result, "result", None)
        ledger_id = self._ledger_ref(payload)
        error_code = self._error_code(payload, tool_error=self._is_tool_error(result, payload))
        if error_code == NOT_FOUND_CODE:
            if ledger_id is not None:
                return self._map_not_found(handle, ctx, step, jti, ledger_id, holds)
            # An empty lookup without its ledger row is unprovable: fail closed.
            error_code = "internal"
        if error_code is not None:
            # A tool-server rejection is never an ALLOW (isError is honoured
            # even without a parsable code; `not_found` is handled above). The
            # ledger reference, if any, is the call's own rejection row or, on
            # replay only, the original entry.
            original = (
                self._meta_ref(payload, "original_entry_id") if error_code == "replay" else None
            )
            invocation = ToolInvocationResult(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                jti=jti,
                status=ToolExecutionStatus.REJECTED_BY_TOOL_SERVER,
                tool_server_reached=True,
                ledger_entry_id=ledger_id or original,
                error_code=error_code,
            )
            return StepOutcome(
                decision=self._decision(
                    "block",
                    DecisionSource.TOOL_SERVER_RULE,
                    f"tool server rejected the call: {error_code}",
                    handle,
                ),
                invocation=invocation,
            )
        if ledger_id is not None:
            invocation = ToolInvocationResult(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                jti=jti,
                status=ToolExecutionStatus.EXECUTED,
                tool_server_reached=True,
                ledger_entry_id=ledger_id,
                result=payload,
            )
            status = "allow"
        else:
            # No ledger reference surfaced (provisional until P2-06/P2-07 define
            # the correlation shape via ICR): success is recorded without
            # claiming ledger-backed execution.
            invocation = ToolInvocationResult(
                request_id=ctx.request_id,
                plan_id=handle.plan_id,
                step_index=step.step_index,
                tool=step.tool,
                jti=jti,
                status=ToolExecutionStatus.UNKNOWN,
                tool_server_reached=True,
                result=payload,
            )
            status = "allow"
        approval = self._approved_hold(ctx, handle, step, holds)
        logger.info(
            "gateway step ok request_id=%s step=%d jti=%s ledger=%s",
            ctx.request_id,
            step.step_index,
            jti,
            ledger_id,
        )
        return StepOutcome(
            decision=self._decision(
                status,  # type: ignore[arg-type]
                DecisionSource.ARMORIQ_POLICY_DECISION,
                "policy allowed the planned call (provisional until E1/E3)",
                handle,
            ),
            approval=approval,
            invocation=invocation,
        )

    def _map_not_found(
        self,
        handle: PlanHandle,
        ctx: RequestContext,
        step: Any,
        jti: str,
        ledger_id: str,
        holds: list[tuple[Any, datetime]],
    ) -> StepOutcome:
        """`not_found`: an executed, ledger-backed lookup that returned nothing.

        The ArmorIQ decision stands (the call was authorized and reached the
        tool server); the invocation is EXECUTED with ``result=None`` so no
        evidence can be built from it, and ``error_code`` says why.
        """
        invocation = ToolInvocationResult(
            request_id=ctx.request_id,
            plan_id=handle.plan_id,
            step_index=step.step_index,
            tool=step.tool,
            jti=jti,
            status=ToolExecutionStatus.EXECUTED,
            tool_server_reached=True,
            ledger_entry_id=ledger_id,
            result=None,
            error_code=NOT_FOUND_CODE,
        )
        logger.info(
            "gateway step not_found request_id=%s step=%d jti=%s ledger=%s",
            ctx.request_id,
            step.step_index,
            jti,
            ledger_id,
        )
        return StepOutcome(
            decision=self._decision(
                "allow",
                DecisionSource.ARMORIQ_POLICY_DECISION,
                "policy allowed the planned call (provisional until E1/E3); "
                "the lookup found no visible source",
                handle,
            ),
            approval=self._approved_hold(ctx, handle, step, holds),
            invocation=invocation,
        )

    @staticmethod
    def _approved_hold(
        ctx: RequestContext,
        handle: PlanHandle,
        step: Any,
        holds: list[tuple[Any, datetime]],
    ) -> ApprovalStatus | None:
        """Approval record for a success that followed an SDK hold.

        Evidence rule: only when the SDK fired ``on_hold`` with a
        ``delegation_id`` and then returned success — its wait loop retries
        only after ``status == "approved"`` for that delegation
        (``client.py:1761-1777``). ``requested_at`` is when the hold callback
        fired. Without a delegation ID no record is produced (insufficient
        evidence, documented limitation).
        """
        for hold, fired_at in reversed(holds):
            delegation_id = getattr(hold, "delegation_id", None)
            if delegation_id:
                try:
                    return ApprovalStatus(
                        request_id=ctx.request_id,
                        plan_id=handle.plan_id,
                        step_index=step.step_index,
                        tool=step.tool,
                        state=ApprovalState.APPROVED,
                        delegation_id=delegation_id,
                        requested_at=fired_at,
                        resolved_at=_now(),
                    )
                except Exception:
                    return None
        return None

    def _blocked(
        self, handle: PlanHandle, ctx: RequestContext, step: Any, jti: str, exc: Any
    ) -> StepOutcome:
        reason = (getattr(exc, "reason", None) or str(exc) or "blocked by policy")[:300]
        logger.info(
            "gateway step blocked request_id=%s step=%d jti=%s",
            ctx.request_id,
            step.step_index,
            jti,
        )
        invocation = ToolInvocationResult(
            request_id=ctx.request_id,
            plan_id=handle.plan_id,
            step_index=step.step_index,
            tool=step.tool,
            jti=jti,
            status=ToolExecutionStatus.NOT_REACHED,
            tool_server_reached=False,
        )
        return StepOutcome(
            decision=self._decision(
                "block",
                DecisionSource.ARMORIQ_POLICY_DECISION,
                reason,
                handle,
                enforcement_action=getattr(exc, "enforcement_action", None),
            ),
            invocation=invocation,
        )

    def _decision(
        self, outcome: str, source: DecisionSource, reason: str, handle: PlanHandle, **extra: Any
    ) -> ArmorIQDecision:
        armoriq_plan_id = extra.pop("armoriq_plan_id", handle.armoriq_plan_id)
        return ArmorIQDecision(
            outcome=outcome,  # type: ignore[arg-type]
            source=source,
            reason=reason[:300] or None,
            armoriq_plan_id=armoriq_plan_id,
            delegation_id=extra.pop("delegation_id", None),
            decided_at=_now(),
            **extra,
        )

    def _failed_handle(
        self,
        ctx: RequestContext,
        plan: PlanProposal,
        source: DecisionSource,
        outcome: str,
        reason: str,
        **extra: Any,
    ) -> PlanHandle:
        logger.info(
            "gateway plan failed request_id=%s outcome=%s source=%s",
            ctx.request_id,
            outcome,
            source.value,
        )
        return PlanHandle(
            request_id=ctx.request_id,
            plan_id=_new_id("p"),
            member_id=ctx.requester.member_id,
            step_count=len(plan.steps),
            token_expires_at=None,
            armoriq_plan_id=None,
            decision=ArmorIQDecision(
                outcome=outcome,  # type: ignore[arg-type]
                source=source,
                reason=reason[:300],
                decided_at=_now(),
                **extra,
            ),
        )

    # -- small utilities --------------------------------------------------

    def _sign_step_ctx(
        self, handle: PlanHandle, ctx: RequestContext, plan: PlanProposal, step: Any, jti: str
    ) -> str | None:
        secret = _secret_value(getattr(self._settings, "ctx_secret", None))
        if not secret:
            logger.warning("gateway ctx unavailable request_id=%s", ctx.request_id)
            return None
        now = _now()
        fields: dict[str, Any] = {
            "request_id": ctx.request_id,
            "trace_id": ctx.trace_id,
            "workspace_id": ctx.workspace_id,
            "requester_member_id": ctx.requester.member_id,
            # ADR-020: the env-key reference, never the raw email.
            "armoriq_member_email_ref": self._email_ref(ctx.requester),
            "app_role": ctx.requester.app_role.value,
            "plan_id": handle.plan_id,
            "step_index": step.step_index,
            "tool": step.tool.value,
            "aud": step.mcp.value,
            "iat": now,
            "exp": now + timedelta(seconds=CTX_TTL_S),
            "jti": jti,
        }
        try:
            return self._ctx_signer(fields, secret)
        except Exception as exc:
            logger.warning(
                "gateway ctx sign failed request_id=%s err=%s", ctx.request_id, type(exc).__name__
            )
            return None

    @staticmethod
    def _meta_ref(payload: Any, key: str) -> str | None:
        meta = payload.get("_meta") if isinstance(payload, Mapping) else None
        value = meta.get(key) if isinstance(meta, Mapping) else None
        return value if isinstance(value, str) and value else None

    @classmethod
    def _ledger_ref(cls, payload: Any) -> str | None:
        if isinstance(payload, Mapping):
            for key in _LEDGER_KEYS:
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        return cls._meta_ref(payload, "ledger_entry_id")

    @staticmethod
    def _is_tool_error(result: Any, payload: Any) -> bool:
        """MCP `isError` on the payload, or the SDK's own error status for it."""
        if isinstance(payload, Mapping) and (payload.get("isError") or payload.get("is_error")):
            return True
        return getattr(result, "status", None) == "error"

    @staticmethod
    def _error_code(payload: Any, *, tool_error: bool = False) -> str | None:
        if isinstance(payload, Mapping):
            code = payload.get("error_code", payload.get("errorCode"))
            if isinstance(code, str) and code:
                return code
        if not tool_error:
            return None
        # §4 body in the first text block; anything else is `internal`.
        content = payload.get("content") if isinstance(payload, Mapping) else None
        first = content[0] if isinstance(content, list) and content else None
        text = first.get("text") if isinstance(first, Mapping) else None
        try:
            body = json.loads(text) if isinstance(text, str) else None
        except (ValueError, RecursionError):
            body = None
        code = body.get("code") if isinstance(body, Mapping) else None
        return code if isinstance(code, str) and code in _TOOL_SERVER_CODES else "internal"

    @staticmethod
    def _token_expiry(token: Any) -> datetime | None:
        expires_at = getattr(token, "expires_at", None)
        if isinstance(expires_at, datetime):
            return expires_at
        if isinstance(expires_at, (int, float)):
            return datetime.fromtimestamp(expires_at, tz=UTC)
        return None

    @staticmethod
    def _token_expired(token: Any) -> bool:
        is_expired = getattr(token, "is_expired", None)
        if callable(is_expired):
            try:
                return bool(is_expired())
            except Exception:
                return True
        if isinstance(is_expired, bool):
            return is_expired
        return False
