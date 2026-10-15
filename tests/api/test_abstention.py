"""Abstention tests (fake LLM only — never live evidence)."""

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from apps.api.responder import ModelDraft, respond
from services.contracts import DataTier, ReviewStatus
from tests.fakes.responder import make_ctx as _ctx
from tests.fakes.responder import make_item as _item
from tests.fakes.responder import never_called as _never_called
from tests.fakes.responder import scripted as _scripted


def _respond(evidence: list[Any], min_score: float | None = 0.5, **kwargs: Any) -> Any:
    kwargs.setdefault("generate", _never_called)
    return asyncio.run(respond(_ctx(), "what?", evidence, min_score, **kwargs))


def test_no_evidence_abstains() -> None:
    out = _respond([])
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"
    assert out.citations == []


def test_below_threshold_abstains_without_generation() -> None:
    out = _respond([_item("s1", score=0.2)])
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"


def test_unscored_evidence_abstains() -> None:
    out = _respond([_item("s1", score=None)])
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"


def test_conflict_abstains() -> None:
    evidence = [
        _item("s-hosted", title="Self-hosted runners", refutes=("s-managed",)),
        _item("s-managed", title="Hosted runners", refutes=("s-hosted",)),
    ]
    out = _respond(evidence)
    assert out.abstained is True
    assert out.abstain_reason == "conflict"
    assert out.citations == []


def test_conflict_with_a_below_threshold_record_still_abstains() -> None:
    """Finding 3: a contradicting record is not ignored for scoring low."""
    evidence = [
        _item("s-hosted", title="Hosted runners", score=0.9),
        _item("s-self", title="Self-hosted runners", score=0.2, refutes=("s-hosted",)),
    ]
    out = _respond(evidence)
    assert out.abstained is True
    assert out.abstain_reason == "conflict"


def test_conflict_needs_two_confirmed_current_records() -> None:
    """A draft or a superseded record cannot manufacture a conflict."""
    draft = ModelDraft(answer_text="Hosted runners [s-hosted].", citations=["s-hosted"])
    evidence = [
        _item("s-hosted", title="Hosted runners"),
        _item("s-draft", review_status=ReviewStatus.DRAFT, refutes=("s-hosted",)),
        _item("s-old", superseded_by="s-hosted", refutes=("s-hosted",)),
    ]
    out = _respond(evidence, generate=_scripted(draft))
    assert out.abstained is False
    assert out.citations == ["s-hosted"]


def test_superseded_only_abstains() -> None:
    evidence = [_item("v1", title="Checklist v1", superseded_by="v2")]
    out = _respond(evidence)
    assert out.abstained is True
    assert out.abstain_reason == "superseded"
    assert out.citations == []
    # Abstention carries no content, so nothing is presented as current.


def test_supersession_comes_from_superseded_by_only() -> None:
    """Finding 8: no free-form status field ("ACTIVE" used to read as superseded)."""
    with pytest.raises(ValidationError):
        _item("s1", status="ACTIVE")
    draft = ModelDraft(answer_text="Current [s1].", citations=["s1"])
    out = _respond([_item("s1", superseded_by=None)], generate=_scripted(draft))
    assert out.abstained is False


def test_review_status_has_no_silent_default() -> None:
    from apps.api.responder import EvidenceItem

    with pytest.raises(ValidationError):
        EvidenceItem(source_id="s1", title="t", tier=DataTier.TEAM, text="x", score=0.9)


def test_draft_is_never_grounding_evidence() -> None:
    def generate(system: str, block: str, question: str) -> ModelDraft:
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
    out = _respond(evidence, generate=generate)
    assert out.abstained is False
    assert out.citations == ["s-real"]


def test_draft_only_abstains_with_label() -> None:
    out = _respond([_item("s-draft", title="Draft rumor", review_status=ReviewStatus.DRAFT)])
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"
    assert out.citations == []
    assert "nconfirmed" in out.answer_text  # drafts labeled, never cited


# --- threshold authority (D-23) -------------------------------------------------


def test_unset_min_score_fails_closed_with_distinct_reason() -> None:
    out = _respond([_item("s1", score=0.99)], None)
    assert out.abstained is True
    assert out.abstain_reason == "threshold_unset"
    assert out.citations == []
    assert "MIN_SCORE" in out.answer_text


def test_non_finite_min_score_counts_as_unset() -> None:
    out = _respond([_item("s1", score=0.99)], float("nan"))
    assert out.abstain_reason == "threshold_unset"


def test_stricter_result_threshold_wins() -> None:
    """Configured 0.5, search result says 0.8: a 0.7 hit is below threshold."""
    out = _respond([_item("s1", score=0.7, result_min_score=0.8)], 0.5)
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"


def test_stricter_configured_threshold_wins() -> None:
    out = _respond([_item("s1", score=0.7, result_min_score=0.3)], 0.8)
    assert out.abstained is True
    assert out.abstain_reason == "no_evidence"


def test_result_threshold_applies_when_none_is_configured() -> None:
    draft = ModelDraft(answer_text="Freeze [s1].", citations=["s1"])
    out = _respond([_item("s1", score=0.7, result_min_score=0.6)], None, generate=_scripted(draft))
    assert out.abstained is False
    assert out.citations == ["s1"]


def test_effective_min_score_is_the_max() -> None:
    from apps.api.responder import effective_min_score

    items = [_item("a", result_min_score=0.4), _item("b", result_min_score=0.6), _item("c")]
    assert effective_min_score(0.5, items) == 0.6
    assert effective_min_score(0.7, items) == 0.7
    assert effective_min_score(None, [_item("c")]) is None


def test_settings_min_score_drives_the_responder() -> None:
    """P3-04 passes `settings.min_score` (MIN_SCORE) as the configured value."""
    from tests.api.conftest import make_settings

    unset = make_settings()
    assert _respond([_item("s1")], unset.min_score).abstain_reason == "threshold_unset"
    strict = make_settings(min_score="0.95")
    assert _respond([_item("s1", score=0.9)], strict.min_score).abstain_reason == "no_evidence"


# --- failure and wording ----------------------------------------------------------


def test_llm_error_fails_closed() -> None:
    def broken(system: str, block: str, question: str) -> ModelDraft:
        raise RuntimeError("provider down")

    out = _respond([_item("s1")], generate=broken)
    assert out.abstained is True
    assert out.abstain_reason == "error"  # P3-04 maps this to ERROR, not the queue
    assert out.citations == []
    assert "retry" in out.answer_text.lower()


def test_missing_generator_fails_closed_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.api import responder

    monkeypatch.setattr(responder, "_default_generator", lambda: None)
    out = asyncio.run(respond(_ctx(), "what?", [_item("s1")], 0.5))
    assert out.abstain_reason == "error"


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ([_item("v1", superseded_by="v2")], "superseded"),
        ([_item("a", refutes=("b",)), _item("b")], "conflict"),
    ],
)
def test_abstention_text_names_no_hard_coded_person(evidence: list[Any], reason: str) -> None:
    default = _respond(evidence)
    assert default.abstain_reason == reason
    assert "Maya" not in default.answer_text
    assert "your manager" in default.answer_text
    named = _respond(evidence, manager_name="Alex")
    assert "Ask Alex" in named.answer_text
