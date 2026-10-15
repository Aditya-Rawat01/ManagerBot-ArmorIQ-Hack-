"""Offline responder test helpers (P3-03; reusable by P3-04 / P3-09).

Builders for a trusted `RequestContext`, evidence items, P2-02 result models,
and scripted generators. Nothing here touches the network, an SDK, or a live
model: generators return a fixed `ModelDraft`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from apps.api.responder import EvidenceItem, ModelDraft
from services.contracts import (
    AppRole,
    AuthenticatedUser,
    DataTier,
    EntryPoint,
    KnowledgeSearchHit,
    KnowledgeSearchResult,
    PersonaKey,
    RequestContext,
    ReviewStatus,
    SourceCitation,
    ToolName,
)

Generator = Callable[[str, str, str], ModelDraft]

_SEARCH_TOOL = {
    DataTier.TEAM: ToolName.KNOWLEDGE_SEARCH_TEAM,
    DataTier.CONFIDENTIAL: ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
    DataTier.RESTRICTED: ToolName.KNOWLEDGE_SEARCH_RESTRICTED,
}


def make_ctx(request_id: str = "r1") -> RequestContext:
    """A trusted request context for a synthetic member (no real identity)."""
    return RequestContext(
        request_id=request_id,
        trace_id="t1",
        workspace_id="workspace-primary",
        requester=AuthenticatedUser(
            member_id="member-sam",
            persona=PersonaKey.SAM,
            display_name="Sam",
            app_role=AppRole.SENIOR,
            masked_email="s***@example.com",
        ),
        entry_point=EntryPoint.WEB,
        received_at=datetime.now(UTC),
    )


def make_item(
    source_id: str,
    title: str = "Record",
    tier: DataTier = DataTier.TEAM,
    text: str = "supporting text",
    score: float | None = 0.9,
    review_status: ReviewStatus = ReviewStatus.AUTHORITATIVE,
    **extra: Any,
) -> EvidenceItem:
    """A search-origin evidence item; authoritative and current by default."""
    fields: dict[str, Any] = {
        "source_id": source_id,
        "title": title,
        "tier": tier,
        "text": text,
        "score": score,
        "review_status": review_status,
    }
    fields.update(extra)
    return EvidenceItem(**fields)


def make_citation(
    source_id: str,
    title: str = "Record",
    tier: DataTier = DataTier.TEAM,
    review_status: ReviewStatus = ReviewStatus.AUTHORITATIVE,
    **extra: Any,
) -> SourceCitation:
    """Synthetic `SourceCitation` provenance (version 1, current by default)."""
    fields: dict[str, Any] = {
        "source_id": source_id,
        "title": title,
        "tier": tier,
        "author": "maya",
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        "version": 1,
        "review_status": review_status,
    }
    fields.update(extra)
    return SourceCitation(**fields)


def make_search_result(
    *hits: tuple[SourceCitation, str, float],
    tier: DataTier = DataTier.TEAM,
    min_score: float | None = None,
) -> KnowledgeSearchResult:
    """A `KnowledgeSearchResult` from (citation, snippet, score) triples."""
    return KnowledgeSearchResult(
        tool=_SEARCH_TOOL[tier],
        tier=tier,
        query="synthetic query",
        hits=[
            KnowledgeSearchHit(
                source=source,
                chunk_id=f"{source.source_id}#{rank}",
                snippet=snippet,
                score=score,
                rank=rank,
            )
            for rank, (source, snippet, score) in enumerate(hits, start=1)
        ],
        min_score=min_score,
    )


def scripted(draft: ModelDraft, calls: list[tuple[str, str, str]] | None = None) -> Generator:
    """A generator that records its (system, evidence, question) inputs."""

    def generate(system: str, evidence_block: str, question_block: str) -> ModelDraft:
        if calls is not None:
            calls.append((system, evidence_block, question_block))
        return draft

    return generate


def never_called(system: str, evidence_block: str, question_block: str) -> ModelDraft:
    """Generator for paths that must abstain before any model call."""
    raise AssertionError("generation must not run on this path")
