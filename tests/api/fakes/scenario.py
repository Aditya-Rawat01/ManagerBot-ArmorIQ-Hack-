"""Scenario helpers: fake settings, trusted contexts, results, and a wired harness.

Everything is synthetic: ``*.example.test`` member emails, ``U-<persona>``
Slack IDs, obviously fake secrets, and an unroutable DSN that nothing here
ever opens (the harness's database is P3-02's ``FakeTopicDb``, which only
answers topic-rule reads; audit writes go to :class:`RecordingAudit`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apps.api.core.config import Settings
from apps.api.identity import build_member_table, mask_email
from apps.api.orchestrator import (
    ConfirmationOffer,
    ConfirmationStore,
    Orchestrator,
    RequestOptions,
)
from apps.api.planner import ModelClient
from services.contracts import (
    DEFAULT_WORKSPACE_ID,
    AuthenticatedUser,
    DataTier,
    EntryPoint,
    KnowledgeSearchResult,
    PersonaKey,
    RequestContext,
    ReviewStatus,
    ToolName,
)
from tests.api.conftest import make_settings
from tests.fakes.planner import FakeTopicDb
from tests.fakes.responder import make_citation, make_search_result

from .audit import RecordingAudit
from .delivery import FakeDelivery
from .gateway import ScriptedGateway, StepScript
from .llm import DraftRecorder, FakeModelClient, draft

FAKE_EMAILS = {
    PersonaKey.MAYA: "maya@example.test",
    PersonaKey.SAM: "sam@example.test",
    PersonaKey.PRIYA: "priya@example.test",
}
SLACK_IDS = {
    PersonaKey.MAYA: "U-maya",
    PersonaKey.SAM: "U-sam",
    PersonaKey.PRIYA: "U-priya",
}
FAKE_SECRETS = {
    "ctx_secret": "ctx-offline-fake-secret-0000",
    "armoriq_api_key": "armoriq-offline-fake-key-0000",
    "gemini_api_key": "gemini-offline-fake-key-0000",
    "database_url_backend": "postgresql://fake:fake@127.0.0.1:9/fake",
}
DEFAULT_MIN_SCORE = 0.5
THREAD_TS = "1700000000.0001"


def orchestrator_settings(**overrides: object) -> Settings:
    """Settings with every orchestrator setting filled with obvious fakes."""
    values: dict[str, object] = {
        "persona_maya_email": FAKE_EMAILS[PersonaKey.MAYA],
        "persona_sam_email": FAKE_EMAILS[PersonaKey.SAM],
        "persona_priya_email": FAKE_EMAILS[PersonaKey.PRIYA],
        "slack_user_maya_id": SLACK_IDS[PersonaKey.MAYA],
        "slack_user_sam_id": SLACK_IDS[PersonaKey.SAM],
        "slack_user_priya_id": SLACK_IDS[PersonaKey.PRIYA],
        "min_score": DEFAULT_MIN_SCORE,
        **FAKE_SECRETS,
    }
    values.update(overrides)
    return make_settings(**values)


def request_ctx(
    persona: PersonaKey = PersonaKey.SAM,
    entry: EntryPoint = EntryPoint.SLACK_MENTION,
    *,
    settings: Settings | None = None,
    channel: str | None = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    member_id: str | None = None,
) -> RequestContext:
    """A trusted context for a configured member (what P3-05 / P4-01 build)."""
    record = build_member_table(settings or orchestrator_settings())[persona]
    user = AuthenticatedUser(
        member_id=member_id or record.member_id,
        persona=persona,
        display_name=record.display_name,
        app_role=record.app_role,
        masked_email=mask_email(record.email),
        slack_user_id=record.slack_user_id,
    )
    refs: dict[str, Any] = {}
    if entry is not EntryPoint.WEB:
        default_channel = "C1" if entry is EntryPoint.SLACK_MENTION else f"D-{persona.value}"
        refs = {
            "slack_team_id": "T1",
            "slack_channel_id": channel or default_channel,
            "slack_thread_ts": THREAD_TS,
            "slack_event_id": f"Ev{uuid.uuid4().hex[:10]}",
        }
    return RequestContext(
        request_id=f"entry-{uuid.uuid4().hex[:8]}",
        trace_id=f"entry-trace-{uuid.uuid4().hex[:8]}",
        workspace_id=workspace_id,
        requester=user,
        entry_point=entry,
        received_at=datetime.now(UTC),
        **refs,
    )


Hit = tuple[str, str, str, float]  # (source_id, title, snippet, score)


def search(
    tier: DataTier,
    *hits: Hit,
    review_status: ReviewStatus = ReviewStatus.AUTHORITATIVE,
) -> KnowledgeSearchResult:
    """A P2-02 search result of ``tier`` with the given hits."""
    return make_search_result(
        *[
            (make_citation(source_id, title, tier, review_status), snippet, score)
            for source_id, title, snippet, score in hits
        ],
        tier=tier,
    )


TEAM_HIT: Hit = ("team-atlas-budget", "Atlas budget review", "The Atlas budget is fixed.", 0.9)
CONF_SECRET = "CONFIDENTIAL-SNIPPET-Atlas-cut-plan-7731"
CONF_TITLE = "Atlas SECRET cut plan"
CONF_HIT: Hit = ("conf-atlas-cut", CONF_TITLE, f"Do not share: {CONF_SECRET}.", 0.92)


@dataclass
class Harness:
    """An orchestrator wired to offline doubles (P3-04; reusable by P3-09)."""

    orchestrator: Orchestrator
    gateway: ScriptedGateway
    audit: RecordingAudit
    delivery: FakeDelivery
    model: ModelClient
    generator: DraftRecorder
    settings: Settings
    db: FakeTopicDb
    presented: list[tuple[RequestContext, ConfirmationOffer]] = field(default_factory=list)

    async def ask(
        self, ctx: RequestContext, message: str, options: RequestOptions | None = None
    ) -> str:
        request_id = await self.orchestrator.handle(ctx, message, options)
        await self.orchestrator.wait_idle(10)
        return request_id

    async def confirm(self, ctx: RequestContext, token: str, accept: bool = True) -> str:
        request_id = await self.orchestrator.confirm(ctx, token, accept)
        await self.orchestrator.wait_idle(10)
        return request_id

    def delivered_to(self, request_id: str) -> list[str]:
        return [
            item.answer.answer_text
            for item in self.delivery.deliveries
            if item.ctx.request_id == request_id
        ]


def build_harness(
    *,
    plans: Sequence[str | Exception] = (),
    steps: Mapping[ToolName, StepScript] | None = None,
    gateway: ScriptedGateway | None = None,
    answer: Any = None,
    topic_rules: Sequence[tuple[str, str, str]] = (),
    audit: RecordingAudit | None = None,
    delivery: FakeDelivery | None = None,
    settings: Settings | None = None,
    model: ModelClient | None = None,
    confirmations: ConfirmationStore | None = None,
    present: bool = True,
    **orchestrator_kwargs: Any,
) -> Harness:
    """Wire an :class:`Orchestrator` from offline doubles.

    ``plans``: canned planner JSON (``wire(...)``) in call order. ``steps``:
    per-tool gateway scripts. ``answer``: a ``ModelDraft`` (or exception)
    for the responder. ``topic_rules``: ``(rule_id, topic, max_tier)`` rows.
    """
    settings = settings or orchestrator_settings()
    audit = audit or RecordingAudit()
    delivery = delivery or FakeDelivery(audit)
    gateway = gateway or ScriptedGateway(steps)
    generator = DraftRecorder(answer if answer is not None else draft("No answer scripted."))
    db = FakeTopicDb(topic_rules)
    orchestrator = Orchestrator(
        settings=settings,
        gateway=gateway,
        db=db,
        plan_model=model or FakeModelClient(plans),
        members=build_member_table(settings),
        generate=generator,
        audit=audit,
        deliver_fn=delivery.deliver,
        notify_hold_fn=delivery.notify_hold,
        confirmations=confirmations,
        **orchestrator_kwargs,
    )
    harness = Harness(
        orchestrator=orchestrator,
        gateway=gateway,
        audit=audit,
        delivery=delivery,
        model=orchestrator._plan_model,
        generator=generator,
        settings=settings,
        db=db,
    )
    if present:

        def _present(ctx: RequestContext, offer: ConfirmationOffer) -> None:
            harness.presented.append((ctx, offer))

        for entry in EntryPoint:
            orchestrator.set_confirmation_presenter(entry, _present)
    return harness


__all__ = [
    "CONF_HIT",
    "CONF_SECRET",
    "CONF_TITLE",
    "DEFAULT_MIN_SCORE",
    "FAKE_EMAILS",
    "FAKE_SECRETS",
    "SLACK_IDS",
    "TEAM_HIT",
    "Harness",
    "build_harness",
    "orchestrator_settings",
    "request_ctx",
    "search",
]
