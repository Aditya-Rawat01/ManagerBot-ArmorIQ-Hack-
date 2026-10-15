"""Abstention tests (fake LLM only — never live evidence)."""

import asyncio
from types import SimpleNamespace
from typing import Any

from apps.api.responder import EvidenceItem, ModelDraft, respond
from services.contracts import DataTier, ReviewStatus


def _item(
    source_id: str,
    title: str = "Record",
    tier: DataTier = DataTier.TEAM,
    text: str = "supporting text",
    score: float | None = 0.9,
    review_status: ReviewStatus = ReviewStatus.AUTHORITATIVE,
    **extra: Any,
) -> EvidenceItem:
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


def _ctx() -> Any:
    return SimpleNamespace(request_id="r1")


def test_no_evidence_abstains() -> None:
    out = asyncio.run(respond(_ctx(), "what?", [], 0.5, generate=_never_called))
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"
    assert out.citations == []


def _never_called(system: str, block: str, message: str) -> ModelDraft:
    raise AssertionError("generation must not run without qualifying evidence")


def test_below_threshold_abstains_without_generation() -> None:
    out = asyncio.run(
        respond(_ctx(), "what?", [_item("s1", score=0.2)], 0.5, generate=_never_called)
    )
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"


def test_unscored_evidence_abstains() -> None:
    out = asyncio.run(
        respond(_ctx(), "what?", [_item("s1", score=None)], 0.5, generate=_never_called)
    )
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"


def test_conflict_abstains() -> None:
    evidence = [
        _item("s-hosted", title="Self-hosted runners", refutes=("s-managed",)),
        _item("s-managed", title="Hosted runners", refutes=("s-hosted",)),
    ]
    out = asyncio.run(respond(_ctx(), "which?", evidence, 0.5, generate=_never_called))
    assert out.abstained is True
    assert out.abstain_reason == "conflict"
    assert out.citations == []


def test_superseded_only_abstains() -> None:
    evidence = [_item("v1", title="Checklist v1", status="superseded", superseded_by="v2")]
    out = asyncio.run(respond(_ctx(), "checklist?", evidence, 0.5, generate=_never_called))
    assert out.abstained is True
    assert out.abstain_reason == "superseded"
    assert out.citations == []
    # Abstention carries no content, so nothing is presented as current.


def test_draft_is_never_grounding_evidence() -> None:
    def generate(system: str, block: str, message: str) -> ModelDraft:
        assert "Draft rumor" not in block  # drafts never reach the model
        return ModelDraft(answer_text="Confirmed [s-real].", citations=["s-real"])

    evidence = [
        _item(
            "s-draft",
            title="Draft rumor",
            text="Draft rumor content",
            review_status=ReviewStatus.DRAFT,
        ),
        _item("s-real", title="Confirmed", text="Confirmed content"),
    ]
    out = asyncio.run(respond(_ctx(), "what?", evidence, 0.5, generate=generate))
    assert out.abstained is False
    assert out.citations == ["s-real"]


def test_draft_only_abstains_with_label() -> None:
    out = asyncio.run(
        respond(
            _ctx(),
            "what?",
            [_item("s-draft", title="Draft rumor", review_status=ReviewStatus.DRAFT)],
            0.5,
            generate=_never_called,
        )
    )
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"
    assert out.citations == []
    assert "nconfirmed" in out.answer_text  # drafts labeled, never cited


def test_unset_min_score_fails_closed() -> None:
    out = asyncio.run(
        respond(_ctx(), "what?", [_item("s1", score=0.99)], None, generate=_never_called)
    )
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"
    assert out.citations == []


def test_llm_error_fails_closed() -> None:
    def broken(system: str, block: str, message: str) -> ModelDraft:
        raise RuntimeError("provider down")

    out = asyncio.run(respond(_ctx(), "what?", [_item("s1")], 0.5, generate=broken))
    assert out.abstained is True
    assert out.abstain_reason == "error"
    assert out.citations == []
    assert "retry" in out.answer_text.lower()
