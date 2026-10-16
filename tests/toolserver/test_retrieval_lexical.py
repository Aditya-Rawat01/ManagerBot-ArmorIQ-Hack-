"""Lexical retrieval behavior tests (P2-04). Live tests need a LOCAL
DATABASE_URL_TOOLSERVER (see test_tier_filters); DB-failure and edge tests
are pure. DSNs are captured at collection time and never logged.
"""

import os
from urllib.parse import urlparse

import pytest

from services.contracts import DEFAULT_WORKSPACE_ID, DataTier
from services.knowledge import retrieval
from services.knowledge.retrieval import RetrievalError

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


def _search(tier: DataTier, query: str, limit: int = 10):
    import psycopg

    with psycopg.connect(LIVE_DSN) as conn:
        return retrieval.search(conn, tier=tier, workspace_id=WS, query=query, limit=limit)


@NEEDS_DB
def test_ranking_stable_quokka_first() -> None:
    first = [hit.source.source_id for hit in _search(DataTier.TEAM, "quokka freeze")]
    second = [hit.source.source_id for hit in _search(DataTier.TEAM, "quokka freeze")]
    assert first == second and first[0] == "team-friday-deploy-freeze"


@NEEDS_DB
def test_superseded_excluded_v2_returned() -> None:
    ids = [hit.source.source_id for hit in _search(DataTier.TEAM, "release checklist")]
    assert "team-release-checklist-v2" in ids
    assert "team-release-checklist-v1" not in ids


@NEEDS_DB
def test_snippet_truncation() -> None:
    hits = _search(DataTier.CONFIDENTIAL, "rotation compliance operations meeting")
    assert hits, "expected the multi-chunk rotation record"
    assert all(len(hit.snippet) <= retrieval.SNIPPET_CHARS for hit in hits)
    assert all(hit.rank >= 1 for hit in hits)


def test_snippet_truncates_long_chunks() -> None:
    long_text = "word " * 500
    assert len(retrieval._snippet(long_text)) == retrieval.SNIPPET_CHARS
    assert retrieval._snippet("short") == "short"


@NEEDS_DB
def test_limit_bounded() -> None:
    hits = _search(DataTier.TEAM, "team", limit=1000)
    assert len(hits) <= retrieval.MAX_LIMIT


def test_db_error_raises_typed_error() -> None:
    class BrokenConn:
        def cursor(self):
            raise OSError("connection lost")

    with pytest.raises(RetrievalError):
        retrieval.search(BrokenConn(), tier=DataTier.TEAM, workspace_id=WS, query="quokka")


def test_blank_query_returns_empty_without_touching_db() -> None:
    assert retrieval.search(None, tier=DataTier.TEAM, workspace_id=WS, query="   ") == []
    assert retrieval.search(None, tier=DataTier.TEAM, workspace_id="", query="quokka") == []
