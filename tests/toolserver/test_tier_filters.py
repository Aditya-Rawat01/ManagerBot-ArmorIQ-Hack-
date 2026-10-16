"""Tier-isolation tests (P2-04). Live tests need DATABASE_URL_TOOLSERVER
pointing at a LOCAL database (shared Neon is never used from pytest); they
skip otherwise, so CI stays green. DSNs are captured at collection time and
never logged.
"""

import inspect
import os
from urllib.parse import urlparse

import pytest

from services.contracts import DEFAULT_WORKSPACE_ID, DataTier
from services.knowledge import retrieval

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
    import psycopg

    with psycopg.connect(LIVE_DSN) as conn:
        return retrieval.search(conn, tier=tier, workspace_id=WS, query=query)


@NEEDS_DB
def test_exact_tier_match_per_tier() -> None:
    team = _search(DataTier.TEAM, "quokka Friday deploy freeze")
    assert [hit.source.source_id for hit in team] == ["team-friday-deploy-freeze"]
    conf = _search(DataTier.CONFIDENTIAL, "runway review")
    assert "conf-runway-review" in [hit.source.source_id for hit in conf]
    restr = _search(DataTier.RESTRICTED, "workforce planning")
    assert "restr-workforce-planning" in [hit.source.source_id for hit in restr]


@NEEDS_DB
def test_higher_tier_text_invisible_at_lower_tier() -> None:
    assert _search(DataTier.TEAM, "runway review") == []
    assert _search(DataTier.TEAM, "workforce planning") == []
    assert _search(DataTier.CONFIDENTIAL, "workforce planning") == []
    assert _search(DataTier.CONFIDENTIAL, "quokka") == []


@NEEDS_DB
def test_restricted_text_never_in_team_results() -> None:
    for query in ("quokka", "freeze", "runway", "api", "checklist", "rotation", "snacks"):
        for hit in _search(DataTier.TEAM, query):
            assert hit.source.tier is DataTier.TEAM


def test_no_tier_argument_path() -> None:
    """Tier is a DataTier constant: a caller-controlled string is rejected."""
    params = list(inspect.signature(retrieval.search).parameters)
    assert params == ["conn", "tier", "workspace_id", "query", "limit"]
    with pytest.raises(TypeError):
        retrieval.search(None, tier="team", workspace_id=WS, query="q")  # type: ignore[arg-type]


def test_sql_text_is_constant() -> None:
    sql = retrieval._SEARCH_SQL
    assert sql.count("%s") == 5  # query, workspace, tier, query, limit: values only
    assert "c.tier = %s" in sql and "ORDER BY score DESC" in sql
    assert "FORMAT" not in sql.upper()
