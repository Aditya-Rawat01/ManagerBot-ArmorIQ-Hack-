"""Tier-isolation tests (P2-04). Live tests need DATABASE_URL_TOOLSERVER
pointing at a LOCAL database (shared Neon is never used from pytest); they
skip otherwise, so CI stays green. DSNs are captured at collection time and
never logged.
"""

import inspect
import os
import re
from typing import Any
from urllib.parse import urlparse

import pytest

from services.contracts import DEFAULT_WORKSPACE_ID, DataTier
from services.knowledge import retrieval

from .conftest import OneShotPool

_RAW_DSN = os.environ.get("DATABASE_URL_TOOLSERVER", "")
_HOST = urlparse(_RAW_DSN).hostname or "" if _RAW_DSN else ""
LIVE_DSN = (
    _RAW_DSN
    if _RAW_DSN
    and (
        "localhost" in _HOST
        or "127.0.0.1" in _HOST
        or os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
    )
    and "neon" not in _HOST
    else ""
)

NEEDS_DB = pytest.mark.skipif(not LIVE_DSN, reason="needs a local DATABASE_URL_TOOLSERVER")

WS = DEFAULT_WORKSPACE_ID


def _search(tier: DataTier, query: str):
    """The handlers' path (embed, then one transaction); needs `real_model`."""
    return retrieval.search_with_pool(
        OneShotPool(LIVE_DSN), tier=tier, workspace_id=WS, query=query
    )


@NEEDS_DB
def test_exact_tier_match_per_tier(real_model: Any) -> None:
    # Fused path (P2-05): the E1 record ranks first; vector-similar same-tier
    # rows may follow. Isolation means every hit is the requested tier.
    team = _search(DataTier.TEAM, "quokka Friday deploy freeze")
    assert team and team[0].source.source_id == "team-friday-deploy-freeze"
    assert all(hit.source.tier is DataTier.TEAM for hit in team)
    conf = _search(DataTier.CONFIDENTIAL, "runway review")
    assert "conf-runway-review" in [hit.source.source_id for hit in conf]
    assert all(hit.source.tier is DataTier.CONFIDENTIAL for hit in conf)
    restr = _search(DataTier.RESTRICTED, "workforce planning")
    assert "restr-workforce-planning" in [hit.source.source_id for hit in restr]
    assert all(hit.source.tier is DataTier.RESTRICTED for hit in restr)


@NEEDS_DB
def test_higher_tier_text_invisible_at_lower_tier(real_model: Any) -> None:
    # No foreign-tier ROWS leak, on either leg, for matching or nonsense
    # queries alike. (Emptiness itself is E17's MIN_SCORE job: the vector leg
    # returns nearest same-tier neighbors for any query.)
    cases = [
        (DataTier.TEAM, "runway review"),
        (DataTier.TEAM, "workforce planning"),
        (DataTier.TEAM, "quokka"),
        (DataTier.CONFIDENTIAL, "workforce planning"),
        (DataTier.CONFIDENTIAL, "quokka"),
        (DataTier.RESTRICTED, "quokka Friday deploy freeze"),
    ]
    for tier, query in cases:
        for hit in _search(tier, query):
            assert hit.source.tier is tier, (tier, query, hit.source.source_id)
    team_ids = [hit.source.source_id for hit in _search(DataTier.TEAM, "runway review")]
    assert "conf-runway-review" not in team_ids
    assert "restr-workforce-planning" not in team_ids


@NEEDS_DB
def test_restricted_text_never_in_team_results(real_model: Any) -> None:
    for query in ("quokka", "freeze", "runway", "api", "checklist", "rotation", "snacks"):
        for hit in _search(DataTier.TEAM, query):
            assert hit.source.tier is DataTier.TEAM


def test_no_tier_argument_path() -> None:
    """Tier is a DataTier constant: a caller-controlled string is rejected."""
    params = list(inspect.signature(retrieval.search).parameters)
    assert params == ["conn", "tier", "workspace_id", "query", "query_vector", "limit", "min_score"]
    params = list(inspect.signature(retrieval.search_with_pool).parameters)
    assert params == ["pool", "tier", "workspace_id", "query", "limit", "min_score"]
    with pytest.raises(TypeError):
        retrieval.search(None, tier="team", workspace_id=WS, query="q", query_vector=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        retrieval.search_with_pool(None, tier="team", workspace_id=WS, query="q")  # type: ignore[arg-type]


def test_sql_text_is_constant() -> None:
    """Values only, all named and bound; the tier is never text in the SQL."""
    allowed = {"workspace_id", "tier", "query", "embedding", "candidates"}
    for sql in (retrieval._LEXICAL_SQL, retrieval._VECTOR_SQL):
        assert set(re.findall(r"%\((\w+)\)s", sql)) <= allowed
        assert "%s" not in sql
        assert "c.tier = %(tier)s" in sql
        assert "FORMAT" not in sql.upper()
        for tier in DataTier:
            assert f"'{tier.value}'" not in sql
    assert "ORDER BY text_rank DESC, c.id ASC" in retrieval._LEXICAL_SQL
    assert "ORDER BY c.embedding <=> %(embedding)s::vector" in retrieval._VECTOR_SQL
