"""Evidence adapter tests: P2-02 result models -> `EvidenceItem` (offline only)."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from apps.api import responder
from apps.api.responder import ModelDraft, respond
from services.contracts import (
    TOOL_RESULT_MODELS,
    ConfirmMayaDecisionResult,
    DataTier,
    ExternalMessageResult,
    FollowupTaskResult,
    KnowledgeGetSourceConfidentialResult,
    KnowledgeGetSourceRestrictedResult,
    KnowledgeGetSourceTeamResult,
    MarkSupersededResult,
    PersonaKey,
    ReadSourceConfidentialResult,
    ReadSourceRestrictedResult,
    ReadSourceTeamResult,
    ReviewStatus,
    SaveTeamNoteResult,
    SourceRow,
    ToolName,
)
from tests.fakes.responder import (
    make_citation,
    make_ctx,
    make_search_result,
    never_called,
    scripted,
)

_GET_SOURCE = {
    DataTier.TEAM: (ToolName.KNOWLEDGE_GET_SOURCE_TEAM, KnowledgeGetSourceTeamResult),
    DataTier.CONFIDENTIAL: (
        ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
        KnowledgeGetSourceConfidentialResult,
    ),
    DataTier.RESTRICTED: (
        ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED,
        KnowledgeGetSourceRestrictedResult,
    ),
}
_READ_SOURCE = {
    DataTier.TEAM: (ToolName.READ_SOURCE_TEAM, ReadSourceTeamResult),
    DataTier.CONFIDENTIAL: (ToolName.READ_SOURCE_CONFIDENTIAL, ReadSourceConfidentialResult),
    DataTier.RESTRICTED: (ToolName.READ_SOURCE_RESTRICTED, ReadSourceRestrictedResult),
}


def _get_source(source_id: str, excerpt: str = "record text", **citation: Any) -> BaseModel:
    source = make_citation(source_id, **citation)
    tool, model = _GET_SOURCE[source.tier]
    return model(tool=tool, tier=source.tier, source=source, excerpt=excerpt)


def _read_source(*rows: tuple[str, str, str], tier: DataTier = DataTier.TEAM) -> BaseModel:
    tool, model = _READ_SOURCE[tier]
    return model(
        tool=tool,
        tier=tier,
        view=f"approved_company_data_{tier.value}",
        rows=[
            SourceRow(source_id=sid, title=title, snippet=snippet) for sid, title, snippet in rows
        ],
        row_count=len(rows),
    )


def _answer(evidence: list[Any], cite: str, min_score: float | None = 0.5) -> Any:
    draft = ModelDraft(answer_text=f"Answer [{cite}].", citations=[cite])
    return asyncio.run(respond(make_ctx(), "q?", evidence, min_score, generate=scripted(draft)))


def test_search_hits_keep_tier_ids_provenance_and_threshold() -> None:
    old = make_citation(
        "conf-plan-v1", "Plan", DataTier.CONFIDENTIAL, version=1, superseded_by="conf-plan-v2"
    )
    new = make_citation("conf-plan-v2", "Plan", DataTier.CONFIDENTIAL, version=2, author="sam")
    result = make_search_result(
        (old, "old text", 0.4), (new, "new text", 0.8), tier=DataTier.CONFIDENTIAL, min_score=0.6
    )
    first, second = responder.evidence_from_results([result])
    assert (first.source_id, first.tier, first.version) == (
        "conf-plan-v1",
        DataTier.CONFIDENTIAL,
        1,
    )
    assert first.superseded_by == "conf-plan-v2"
    assert (second.source_id, second.version, second.author) == ("conf-plan-v2", 2, "sam")
    assert second.superseded_by is None
    assert second.review_status is ReviewStatus.AUTHORITATIVE
    assert (second.origin, second.score, second.result_min_score) == ("search", 0.8, 0.6)
    assert second.text == "new text"


def test_threshold_applies_to_search_hits_only() -> None:
    evidence = responder.evidence_from_results(
        [
            make_search_result((make_citation("s-hit"), "hit text", 0.9)),
            _get_source("s-get"),
            _read_source(("row-1", "Project status", "On track")),
        ]
    )
    # 0.99 excludes the 0.9 search hit, never the explicitly requested sources.
    assert _answer(evidence, "s-get", 0.99).citations == ["s-get"]
    assert _answer(evidence, "row-1", 0.99).citations == ["row-1"]
    withheld = _answer(evidence, "s-hit", 0.99)
    assert withheld.abstained is True and withheld.abstain_reason == "withheld"


def test_read_source_rows_use_the_documented_default_not_draft() -> None:
    (row,) = responder.evidence_from_results(
        [_read_source(("row-9", "Budget", "Within plan"), tier=DataTier.RESTRICTED)]
    )
    assert row.review_status is ReviewStatus.AUTHORITATIVE  # approved view, not a draft
    assert row.superseded_by is None
    assert (row.source_id, row.title, row.tier) == ("row-9", "Budget", DataTier.RESTRICTED)
    assert (row.origin, row.score, row.result_min_score) == ("read_source", None, None)


def test_get_source_draft_stays_a_draft() -> None:
    (item,) = responder.evidence_from_results(
        [_get_source("s-note", review_status=ReviewStatus.DRAFT)]
    )
    assert item.review_status is ReviewStatus.DRAFT
    assert item.origin == "get_source" and item.score is None
    out = asyncio.run(respond(make_ctx(), "q?", [item], 0.5, generate=never_called))
    assert out.abstain_reason == "no_evidence"
    assert "nconfirmed" in out.answer_text


def test_empty_snippet_or_excerpt_does_not_crash_or_ground() -> None:
    evidence = responder.evidence_from_results(
        [
            make_search_result((make_citation("s-empty"), "", 0.9)),
            _get_source("s-blank", excerpt="   "),
            _read_source(("row-empty", "Status", "")),
        ]
    )
    assert [item.source_id for item in evidence] == ["s-empty", "s-blank", "row-empty"]
    out = asyncio.run(respond(make_ctx(), "q?", evidence, 0.5, generate=never_called))
    assert out.abstained is True and out.abstain_reason == "no_evidence"


def test_superseded_citation_is_never_current() -> None:
    evidence = responder.evidence_from_results(
        [make_search_result((make_citation("v1", superseded_by="v2"), "old rule", 0.9))]
    )
    out = asyncio.run(respond(make_ctx(), "q?", evidence, 0.5, generate=never_called))
    assert out.abstain_reason == "superseded"


def test_refutes_links_reach_conflict_detection() -> None:
    result = make_search_result(
        (make_citation("team-ci-runner-hosted", "Hosted CI runners"), "stay hosted", 0.9),
        (make_citation("team-ci-runner-selfhosted", "Self-hosted CI runners"), "move", 0.3),
    )
    evidence = responder.evidence_from_results(
        [result], refutes={"team-ci-runner-selfhosted": ["team-ci-runner-hosted"]}
    )
    assert evidence[1].refutes == ("team-ci-runner-hosted",)
    out = asyncio.run(respond(make_ctx(), "q?", evidence, 0.5, generate=never_called))
    assert out.abstain_reason == "conflict"


def _sample_results() -> dict[ToolName, BaseModel]:
    samples: dict[ToolName, BaseModel] = {}
    for tier in DataTier:
        search = make_search_result((make_citation(f"{tier}-s", tier=tier), "x", 0.9), tier=tier)
        samples[search.tool] = search
        get = _get_source(f"{tier}-g", tier=tier)
        samples[get.tool] = get  # type: ignore[attr-defined]
        read = _read_source((f"{tier}-r", "Row", "x"), tier=tier)
        samples[read.tool] = read  # type: ignore[attr-defined]
    tool = ToolName
    samples[tool.KNOWLEDGE_SAVE_TEAM_NOTE] = SaveTeamNoteResult(
        tool=tool.KNOWLEDGE_SAVE_TEAM_NOTE, record_id="n1", version=1
    )
    samples[tool.KNOWLEDGE_CONFIRM_MAYA_DECISION] = ConfirmMayaDecisionResult(
        tool=tool.KNOWLEDGE_CONFIRM_MAYA_DECISION, record_id="d1", version=1
    )
    samples[tool.KNOWLEDGE_MARK_SUPERSEDED] = MarkSupersededResult(
        tool=tool.KNOWLEDGE_MARK_SUPERSEDED,
        record_id="d1",
        version=1,
        review_status=ReviewStatus.AUTHORITATIVE,
        superseded_by_record_id="d2",
    )
    samples[tool.CREATE_FOLLOWUP_TASK] = FollowupTaskResult(
        tool=tool.CREATE_FOLLOWUP_TASK,
        task_id="t1",
        title="Follow up",
        owner=PersonaKey.SAM,
        priority="low",
    )
    samples[tool.SEND_EXTERNAL_MESSAGE] = ExternalMessageResult(
        tool=tool.SEND_EXTERNAL_MESSAGE, reason="honeypot"
    )
    return samples


def test_every_p2_02_result_model_is_mapped_or_skipped() -> None:
    samples = _sample_results()
    assert set(samples) == set(TOOL_RESULT_MODELS)  # a new result model must be handled here
    for tool, sample in samples.items():
        assert isinstance(sample, TOOL_RESULT_MODELS[tool])
    evidence = responder.evidence_from_results(samples.values())
    by_origin: dict[str, set[DataTier]] = {}
    for item in evidence:
        by_origin.setdefault(item.origin, set()).add(item.tier)
    assert by_origin == {
        origin: set(DataTier) for origin in ("search", "get_source", "read_source")
    }
    assert len(evidence) == 9  # write-side results carry no evidence


@pytest.mark.parametrize("bogus", [{"hits": []}, ModelDraft(answer_text="x", citations=[])])
def test_unknown_result_objects_are_rejected(bogus: object) -> None:
    with pytest.raises(TypeError):
        responder.evidence_from_results([bogus])  # type: ignore[list-item]
