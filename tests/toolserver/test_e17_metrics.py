"""E17 metric and threshold-selection functions (P3-08, #30; CP-6).

Offline: toy score lists with known answers, no database, no model. The
harness's I/O (tool server, responder) is the experiment itself
(`scripts/eval_e17_grounding.py`, recorded in docs/EXPERIMENTS.md § E17).
The eval set in `seed/eval/` is validated against the seed corpus here too.
"""

import json
from pathlib import Path

import pytest

from scripts import eval_e17_grounding as e17
from scripts.eval_e17_grounding import Hit, ScoredCase

REPO_ROOT = Path(__file__).resolve().parents[2]


def case(
    case_id: str,
    scores: dict[str, float],
    *,
    answerable: bool,
    expected: tuple[str, ...] = (),
    split: str = "train",
    case_type: str | None = None,
) -> ScoredCase:
    kind = case_type or ("answerable" if answerable else "unrelated")
    return ScoredCase(
        case_id=case_id,
        split=split,
        case_type=kind,
        answerable=answerable,
        expected_source_ids=frozenset(expected),
        hits=tuple(
            Hit(source_id=source, tier="team", score=score, rank=rank)
            for rank, (source, score) in enumerate(scores.items(), 1)
        ),
    )


def answerable(case_id: str, score: float, **kw: object) -> ScoredCase:
    return case(case_id, {"src-a": score}, answerable=True, expected=("src-a",), **kw)


def unanswerable(case_id: str, score: float, **kw: object) -> ScoredCase:
    return case(case_id, {"src-x": score}, answerable=False, **kw)


# --- per-case outcome -----------------------------------------------------------------


def test_outcomes_of_answerable_cases() -> None:
    both = case("a", {"src-a": 0.8, "src-b": 0.7}, answerable=True, expected=("src-a",))
    assert e17.outcome(both, 0.75) == e17.CORRECT_ACCEPT
    assert e17.outcome(both, 0.85) == e17.FALSE_ABSTAIN
    # Accepted, but only through a source that is not an expected one.
    wrong = case("b", {"src-a": 0.6, "src-b": 0.9}, answerable=True, expected=("src-a",))
    assert e17.outcome(wrong, 0.7) == e17.MISGROUNDED_ACCEPT
    assert e17.outcome(wrong, 0.5) == e17.CORRECT_ACCEPT


def test_outcomes_of_unanswerable_cases() -> None:
    item = unanswerable("u", 0.62)
    assert e17.outcome(item, 0.60) == e17.FALSE_ACCEPT
    assert e17.outcome(item, 0.65) == e17.CORRECT_ABSTAIN


def test_score_equal_to_threshold_is_accepted() -> None:
    """Same comparison as the responder (`score >= threshold`)."""
    assert e17.outcome(answerable("a", 0.7), 0.7) == e17.CORRECT_ACCEPT
    assert e17.outcome(unanswerable("u", 0.7), 0.7) == e17.FALSE_ACCEPT
    assert e17.outcome(unanswerable("u", 0.7), 0.71) == e17.CORRECT_ABSTAIN


def test_no_hits_always_abstains() -> None:
    empty_answerable = case("a", {}, answerable=True, expected=("src-a",))
    empty_unanswerable = case("u", {}, answerable=False)
    assert e17.max_score(()) is None
    assert not e17.accepts((), 0.0)
    assert e17.outcome(empty_answerable, -1.0) == e17.FALSE_ABSTAIN
    assert e17.outcome(empty_unanswerable, -1.0) == e17.CORRECT_ABSTAIN


def test_max_score_is_the_best_score_not_rank_one() -> None:
    """Cosine score is not monotonic in RRF rank (P2-05)."""
    item = case("a", {"first": 0.55, "second": 0.81}, answerable=False)
    assert item.hits[0].rank == 1
    assert e17.max_score(item.hits) == 0.81


# --- aggregate metrics ----------------------------------------------------------------


def test_evaluate_counts_and_lists_ids_sorted() -> None:
    cases = [
        answerable("a2", 0.80),
        answerable("a1", 0.60),  # false abstain at 0.7
        unanswerable("u2", 0.75),  # false accept at 0.7
        unanswerable("u1", 0.50),
    ]
    metrics = e17.evaluate(cases, 0.7)
    assert (metrics.total, metrics.answerable, metrics.unanswerable) == (4, 2, 2)
    assert metrics.false_accepts == ("u2",)
    assert metrics.false_abstains == ("a1",)
    assert metrics.misgrounded == ()
    assert metrics.cost == 2 * 1 + 1
    assert metrics.errors == 2


def test_evaluate_empty_set_is_all_zero() -> None:
    metrics = e17.evaluate([], 0.5)
    assert (metrics.total, metrics.answerable, metrics.unanswerable, metrics.cost) == (0, 0, 0, 0)


def test_conflict_cases_are_not_threshold_cases() -> None:
    conflict = case("c", {"src-a": 0.9}, answerable=False, case_type="conflict")
    assert e17.calibration_cases([conflict]) == []
    assert e17.evaluate([conflict], 0.5).total == 0


# --- threshold selection (train only) -------------------------------------------------

GRID = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)


def test_separable_train_set_picks_the_upper_median_of_the_gap() -> None:
    train = [answerable("a1", 0.80), answerable("a2", 0.85), unanswerable("u1", 0.60)]
    # Zero cost for 0.65, 0.70, 0.75, 0.80 (0.80 still accepts a1: ties accept).
    assert [t for t in GRID if e17.evaluate(train, t).cost == 0] == [0.65, 0.70, 0.75, 0.80]
    assert e17.choose_threshold(train, GRID) == 0.75


def test_single_minimum_is_chosen() -> None:
    train = [answerable("a1", 0.70), unanswerable("u1", 0.60)]
    assert e17.choose_threshold(train, (0.60, 0.65, 0.75)) == 0.65


def test_false_accepts_cost_double() -> None:
    """Overlap: abstaining on the answerable case (cost 1) beats accepting the
    unanswerable one (cost 2): grounding fails closed."""
    train = [answerable("a1", 0.70), unanswerable("u1", 0.75)]
    assert e17.evaluate(train, 0.70).cost == 2
    assert e17.evaluate(train, 0.80).cost == 1
    assert e17.choose_threshold(train, (0.60, 0.70, 0.80)) == 0.80


def test_grid_order_and_duplicates_do_not_matter() -> None:
    train = [answerable("a1", 0.80), unanswerable("u1", 0.60)]
    assert e17.choose_threshold(train, (0.9, 0.7, 0.5, 0.7, 0.65)) == e17.choose_threshold(
        train, (0.5, 0.65, 0.7, 0.9)
    )


def test_choose_threshold_refuses_held_out_cases() -> None:
    train = [answerable("a1", 0.80), unanswerable("u1", 0.60)]
    with pytest.raises(ValueError, match="only train"):
        e17.choose_threshold([*train, answerable("h1", 0.70, split="heldout")], GRID)


def test_choose_threshold_needs_data_and_a_grid() -> None:
    with pytest.raises(ValueError, match="no train cases"):
        e17.choose_threshold([], GRID)
    conflict_only = [case("c", {"src-a": 0.9}, answerable=False, case_type="conflict")]
    with pytest.raises(ValueError, match="no train cases"):
        e17.choose_threshold(conflict_only, GRID)
    with pytest.raises(ValueError, match="empty candidate grid"):
        e17.choose_threshold([answerable("a1", 0.8)], ())


def test_held_out_data_cannot_influence_the_threshold() -> None:
    train = [
        answerable("a1", 0.78),
        answerable("a2", 0.83),
        unanswerable("u1", 0.58),
        unanswerable("u2", 0.62),
    ]
    benign = [answerable("h1", 0.80, split="heldout"), unanswerable("h2", 0.50, split="heldout")]
    hostile = [
        # Scores that would pull a held-out-aware choice far up or down.
        *(unanswerable(f"hu{i}", 0.95, split="heldout") for i in range(20)),
        *(answerable(f"ha{i}", 0.31, split="heldout") for i in range(20)),
    ]
    first = e17.calibrate([*train, *benign], GRID)
    second = e17.calibrate([*train, *hostile], GRID)
    alone = e17.choose_threshold(train, GRID)
    assert first.threshold == second.threshold == alone
    assert first.train == second.train
    # The held-out metrics do change: they are measured, never fitted.
    assert first.heldout.errors == 0
    assert len(second.heldout.false_accepts) == 20
    assert len(second.heldout.false_abstains) == 20


def test_calibrate_sweeps_every_grid_value_for_both_splits() -> None:
    cases = [answerable("a1", 0.8), unanswerable("u1", 0.6), answerable("h1", 0.7, split="heldout")]
    result = e17.calibrate(cases, GRID)
    assert [train.threshold for train, _ in result.sweep] == list(GRID)
    assert all(train.threshold == heldout.threshold for train, heldout in result.sweep)
    assert result.heldout.threshold == result.threshold


def test_calibrate_rejects_unknown_splits() -> None:
    with pytest.raises(ValueError, match="unknown split"):
        e17.calibrate([answerable("a1", 0.8), answerable("x", 0.8, split="test")], GRID)


def test_default_grid_is_fixed_and_score_independent() -> None:
    assert e17.GRID[0] == 0.30
    assert e17.GRID[-1] == 0.95
    assert len(e17.GRID) == 66
    assert all(round(b - a, 2) == 0.01 for a, b in zip(e17.GRID, e17.GRID[1:], strict=False))


# --- the eval set in seed/eval/ -------------------------------------------------------


def _seed_sources() -> dict[str, dict]:
    sources = {}
    for path in (REPO_ROOT / "seed" / "sources").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        sources[record["source_id"]] = record
    return sources


def test_eval_set_is_valid_and_split_is_fixed() -> None:
    cases = e17.load_cases()
    by_split = {split: [c for c in cases if c["split"] == split] for split in e17.SPLITS}
    assert len(by_split["train"]) == 27
    assert len(by_split["heldout"]) == 20
    assert len({c["id"] for c in cases}) == len(cases)
    assert all(c["id"].startswith("tr-") for c in by_split["train"])
    assert all(c["id"].startswith("ho-") for c in by_split["heldout"])


def test_eval_set_covers_every_prd_case_type_in_both_splits() -> None:
    cases = e17.load_cases()
    for split in e17.SPLITS:
        types = {c["case_type"] for c in cases if c["split"] == split}
        assert types == e17.CASE_TYPES, split
        personas = {
            c["persona"] for c in cases if c["split"] == split and c["case_type"] == "escalation"
        }
        assert personas == {"maya", "sam", "priya"}, split


def test_eval_labels_match_the_seed_corpus() -> None:
    """Expected sources exist, are searchable, and are visible to the persona
    through one of the tiers the case searches; labels never name the
    superseded record as an answer."""
    sources = _seed_sources()
    for item in e17.load_cases():
        ceiling = e17.TIER_RANK[e17.PERSONAS[item["persona"]].ceiling]
        for source_id in item["expected_source_ids"]:
            record = sources[source_id]
            assert record["status"] == "active", item["id"]
            assert record["tier"] in item["tiers"], item["id"]
            if item["expected_answerable"]:
                assert e17.TIER_RANK[record["tier"]] <= ceiling, item["id"]
    assert e17.superseded_seed_ids() == frozenset({"team-release-checklist-v1"})


def test_eval_cases_are_validated_strictly() -> None:
    good = json.loads((REPO_ROOT / "seed" / "eval" / "e17_train.json").read_text("utf-8"))[0]
    assert e17.validate_case(good, "x") == []
    assert e17.validate_case({**good, "case": "label"}, "x")  # unknown key
    assert e17.validate_case({**good, "persona": "eve"}, "x")
    assert e17.validate_case({**good, "tiers": ["team", "team"]}, "x")
    assert e17.validate_case({**good, "expected_abstain_reason": "no_evidence"}, "x")
    assert e17.validate_case({**good, "expected_answerable": False}, "x")  # needs a reason
    assert e17.validate_case({**good, "case_type": "conflict"}, "x")
    assert e17.validate_case([], "x")


def test_load_cases_rejects_duplicates(tmp_path: Path) -> None:
    good = json.loads((REPO_ROOT / "seed" / "eval" / "e17_train.json").read_text("utf-8"))[0]
    for name in e17.SPLIT_FILES.values():
        (tmp_path / name).write_text(json.dumps([good]), encoding="utf-8")
    with pytest.raises(e17.EvalSetError, match="duplicate"):
        e17.load_cases(tmp_path)
