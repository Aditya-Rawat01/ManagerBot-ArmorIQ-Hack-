"""Vector + RRF fusion tests (P2-05). RRF math is pure (no model, no DB);
live tests need a LOCAL DATABASE_URL_TOOLSERVER with seeded data and skip
otherwise. DSNs are captured at collection time and never logged.
"""

import os
from urllib.parse import urlparse

import pytest

from services.contracts import DEFAULT_WORKSPACE_ID, DataTier
from services.knowledge import retrieval
from services.knowledge.retrieval import RRF_K, fuse_candidates

_RAW_DSN = os.environ.get("DATABASE_URL_TOOLSERVER", "")
_HOST = urlparse(_RAW_DSN).hostname or "" if _RAW_DSN else ""
LIVE_DSN = (
    _RAW_DSN
    if _RAW_DSN
    and ("localhost" in _HOST or "127.0.0.1" in _HOST or os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1")
    and "neon" not in _HOST
    else ""
)

NEEDS_DB = pytest.mark.skipif(not LIVE_DSN, reason="needs a local DATABASE_URL_TOOLSERVER")

WS = DEFAULT_WORKSPACE_ID


def _search(tier: DataTier, query: str, **kwargs: object):
    import psycopg

    with psycopg.connect(LIVE_DSN) as conn:
        return retrieval.search(conn, tier=tier, workspace_id=WS, query=query, **kwargs)


def test_rrf_math_on_fixed_lists() -> None:
    fts = [("c1", "s1"), ("c2", "s2")]
    vec = [("c2", "s2"), ("c3", "s3")]
    out = fuse_candidates(fts, vec, limit=10)
    # c2 in both lists outranks c1/c3; c1 (fts #1) beats c3 (vec #2).
    assert [chunk for chunk, _, _ in out] == ["c2", "c1", "c3"]
    scores = {chunk: score for chunk, _, score in out}
    assert scores["c2"] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert scores["c1"] == pytest.approx(1 / (RRF_K + 1))
    # Deterministic across runs.
    assert fuse_candidates(fts, vec, limit=10) == out


def test_dedupe_by_chunk_then_source() -> None:
    fts = [("c1", "s1"), ("c2", "s1")]
    vec = [("c1", "s1")]
    out = fuse_candidates(fts, vec, limit=10)
    assert [(chunk, source) for chunk, source, _ in out] == [("c1", "s1")]
    assert fuse_candidates([], [], limit=10) == []


def test_limit_and_min_score() -> None:
    ranked = [(f"c{i}", f"s{i}") for i in range(1, 6)]
    assert len(fuse_candidates(ranked, [], limit=3)) == 3
    top_score = fuse_candidates(ranked, [], limit=10)[0][2]
    assert fuse_candidates(ranked, [], limit=10, min_score=top_score + 1.0) == []
    assert len(fuse_candidates(ranked, [], limit=10, min_score=0.0)) == 5


def test_single_model_load(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastembed

    from services.knowledge import embeddings

    builds: list[str] = []
    real = fastembed.TextEmbedding

    def counting(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        builds.append("built")
        return real(*args, **kwargs)

    monkeypatch.setattr(fastembed, "TextEmbedding", counting)
    monkeypatch.setattr(embeddings, "_model", None)
    try:
        embeddings.warm()
        embeddings.warm()
        embeddings.embed(["hello"])
    finally:
        monkeypatch.setattr(embeddings, "_model", None)
    assert builds == ["built"]


@NEEDS_DB
def test_tier_isolation_on_fused_path() -> None:
    team = _search(DataTier.TEAM, "quokka Friday deploy freeze")
    assert team and team[0].source.source_id == "team-friday-deploy-freeze"
    assert all(hit.source.tier is DataTier.TEAM for hit in team)
    # The vector leg always returns same-tier candidates (no MIN_SCORE until
    # E17): isolation means no FOREIGN-tier rows, not empty results.
    for query, tier in (("runway review", DataTier.TEAM), ("workforce planning", DataTier.CONFIDENTIAL)):
        for hit in _search(tier, query):
            assert hit.source.tier is tier
    assert "conf-runway-review" not in [
        hit.source.source_id for hit in _search(DataTier.TEAM, "runway review")
    ]


@NEEDS_DB
def test_fused_scores_reported_without_threshold() -> None:
    hits = _search(DataTier.TEAM, "release checklist")
    ids = [hit.source.source_id for hit in hits]
    assert "team-release-checklist-v2" in ids
    assert "team-release-checklist-v1" not in ids  # superseded stays excluded
    assert all(hit.score > 0 for hit in hits)
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))
    gated = _search(DataTier.TEAM, "release checklist", min_score=10.0)
    assert gated == []


@NEEDS_DB
def test_vector_only_match_surfaces_semantically() -> None:
    hits = _search(DataTier.TEAM, "when do we stop merging code on Fridays")
    assert "team-friday-deploy-freeze" in [hit.source.source_id for hit in hits]
