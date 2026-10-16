"""Vector + RRF fusion tests (P2-05 and its review follow-ups).

Offline tests use fake connections/pools and a fake `TextEmbedding` (no model,
no database, no download). Live tests need a LOCAL DATABASE_URL_TOOLSERVER
with seeded data and the cached model (`real_model`), and skip otherwise; the
D-09 write test also needs PROXYLEAD_DB_WRITE_TESTS=1 (it inserts rows inside
one transaction and always rolls back). DSNs are captured at collection time
and never logged.
"""

import hashlib
import json
import os
import sys
import threading
import time
import types
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, NamedTuple
from urllib.parse import urlparse

import pytest

from services.contracts import DEFAULT_WORKSPACE_ID, DataTier
from services.knowledge import embeddings, retrieval
from services.knowledge.retrieval import RRF_K, RetrievalError, fuse_candidates

from .conftest import OneShotPool
from .test_tool_dispatch import live_write_dsn

_RAW_DSN = os.environ.get("DATABASE_URL_TOOLSERVER", "")
_HOST = urlparse(_RAW_DSN).hostname or "" if _RAW_DSN else ""
_WRITE_TESTS = os.environ.get("PROXYLEAD_DB_WRITE_TESTS") == "1"
LIVE_DSN = (
    _RAW_DSN
    if _RAW_DSN
    and ("localhost" in _HOST or "127.0.0.1" in _HOST or _WRITE_TESTS)
    and "neon" not in _HOST
    else ""
)
WRITE_DSN = live_write_dsn(_RAW_DSN, os.environ.get("PROXYLEAD_DB_WRITE_TESTS"))

NEEDS_DB = pytest.mark.skipif(not LIVE_DSN, reason="needs a local DATABASE_URL_TOOLSERVER")
NEEDS_WRITE_DB = pytest.mark.skipif(
    not WRITE_DSN,
    reason="needs DATABASE_URL_TOOLSERVER and PROXYLEAD_DB_WRITE_TESTS=1 (local only)",
)

WS = DEFAULT_WORKSPACE_ID
DIMS = embeddings.EMBED_DIMS
UNIT = [1.0] + [0.0] * (DIMS - 1)


def _search(tier: DataTier, query: str, **kwargs: Any):
    """The handlers' path: embed (model warmed by `real_model`), then one
    transaction on a fresh connection."""
    return retrieval.search_with_pool(
        OneShotPool(LIVE_DSN), tier=tier, workspace_id=WS, query=query, **kwargs
    )


# --- fakes ---------------------------------------------------------------------------


class _Column(NamedTuple):
    name: str


def make_row(
    chunk_id: int,
    source_id: str,
    similarity: float,
    *,
    tier: str = "team",
    text: str = "chunk text",
    provenance: dict[str, Any] | None = None,
    headline: str | None = None,
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "chunk_text": text,
        "source_id": source_id,
        "title": f"Title {source_id}",
        "author": "Maya",
        "tier": tier,
        "provenance": provenance
        if provenance is not None
        else {"version": 1, "review_status": "authoritative", "superseded_by": None},
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "similarity": similarity,
        "text_rank": 0.5,
        "headline": headline,
    }


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self.description: list[_Column] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.log.append(("execute", sql, params))
        if sql == retrieval._LEXICAL_SQL:
            rows = self._conn.lexical
        elif sql == retrieval._VECTOR_SQL:
            rows = self._conn.vector
        else:
            self.description, self._rows = None, []
            return
        names = list(rows[0]) if rows else ["id"]
        self.description = [_Column(name) for name in names]
        self._rows = [tuple(row[name] for name in names) for row in rows]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeConn:
    """Enough of a psycopg connection for `search` and `db.transaction`."""

    def __init__(
        self,
        lexical: list[dict[str, Any]] | None = None,
        vector: list[dict[str, Any]] | None = None,
        *,
        log: list[Any] | None = None,
        fail_set_local: bool = False,
        fail_commit: bool = False,
    ) -> None:
        self.lexical = lexical or []
        self.vector = vector or []
        self.log = log if log is not None else []
        self.fail_set_local = fail_set_local
        self.fail_commit = fail_commit

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, sql: Any, params: Any = None) -> None:
        self.log.append(("conn.execute", str(sql), params))
        if self.fail_set_local:
            raise RuntimeError("SET LOCAL failed")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.log.append(("begin",))
        yield
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.log.append(("commit",))


class FakePool:
    def __init__(self, conn: FakeConn | None = None, *, error: Exception | None = None) -> None:
        self.conn = conn or FakeConn()
        self.error = error
        self.checkouts = 0

    @contextmanager
    def connection(self) -> Iterator[FakeConn]:
        self.checkouts += 1
        self.conn.log.append(("checkout",))
        if self.error is not None:
            raise self.error
        yield self.conn


class FakeTextEmbedding:
    """Stands in for `fastembed.TextEmbedding`: records builds, measures how
    many embeds run at once, and returns fixed 384-dim vectors."""

    builds: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        type(self).builds.append({"model_name": model_name, **kwargs})
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.log: list[Any] | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if self.log is not None:
            self.log.append(("embed", list(texts)))
        time.sleep(0.02)
        with self._guard:
            self.active -= 1
        return [list(UNIT) for _ in texts]


@pytest.fixture
def fake_text_embedding(monkeypatch: pytest.MonkeyPatch) -> type[FakeTextEmbedding]:
    """A fake `fastembed` module (nothing real is imported or downloaded) and
    no model loaded in this process for the duration of the test."""
    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = FakeTextEmbedding  # type: ignore[attr-defined]
    fake_module.__version__ = "0.0-test"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)
    monkeypatch.setattr(FakeTextEmbedding, "builds", [])
    monkeypatch.setattr(embeddings, "_model", None)
    return FakeTextEmbedding


@pytest.fixture
def loaded_fake_model(fake_text_embedding: type[FakeTextEmbedding]) -> FakeTextEmbedding:
    embeddings.warm()
    model = embeddings._model
    assert isinstance(model, FakeTextEmbedding)
    return model


# --- RRF math (pure) -------------------------------------------------------------------


def test_rrf_math_on_fixed_lists() -> None:
    fts = [("c1", "s1"), ("c2", "s2")]
    vec = [("c2", "s2"), ("c3", "s3")]
    sims = {"c1": 0.9, "c2": 0.5, "c3": 0.7}
    out = fuse_candidates(fts, vec, similarity=sims, limit=10)
    # c2 in both lists outranks c1/c3; c1 (fts #1) beats c3 (vec #2).
    assert [item.chunk_id for item in out] == ["c2", "c1", "c3"]
    rrf = {item.chunk_id: item.rrf for item in out}
    assert rrf["c2"] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert rrf["c1"] == pytest.approx(1 / (RRF_K + 1))
    # The reported score is the similarity, not the (rank-only) RRF sum.
    assert {item.chunk_id: item.similarity for item in out} == sims
    # Deterministic across runs.
    assert fuse_candidates(fts, vec, similarity=sims, limit=10) == out


def test_dedupe_by_chunk_then_source() -> None:
    fts = [("c1", "s1"), ("c2", "s1")]
    vec = [("c1", "s1")]
    out = fuse_candidates(fts, vec, similarity={"c1": 0.4, "c2": 0.8}, limit=10)
    assert [(item.chunk_id, item.source_id) for item in out] == [("c1", "s1")]
    assert fuse_candidates([], [], similarity={}, limit=10) == []


def test_limit_and_min_score() -> None:
    ranked = [(f"c{i}", f"s{i}") for i in range(1, 6)]
    sims = {f"c{i}": i / 10 for i in range(1, 6)}  # c1 0.1 ... c5 0.5
    assert len(fuse_candidates(ranked, [], similarity=sims, limit=3)) == 3
    assert fuse_candidates(ranked, [], similarity=sims, limit=0) == []
    assert fuse_candidates(ranked, [], similarity=sims, limit=-1) == []
    # min_score filters on similarity; order stays RRF (c3 before c4 before c5).
    kept = fuse_candidates(ranked, [], similarity=sims, limit=10, min_score=0.3)
    assert [item.chunk_id for item in kept] == ["c3", "c4", "c5"]
    assert fuse_candidates(ranked, [], similarity=sims, limit=10, min_score=0.51) == []


def test_min_score_drops_a_weak_best_chunk_but_keeps_its_source() -> None:
    # s1's best-fused chunk is below threshold; its other chunk qualifies.
    fts = [("c1", "s1"), ("c2", "s1")]
    out = fuse_candidates(fts, [], similarity={"c1": 0.2, "c2": 0.8}, limit=5, min_score=0.5)
    assert [(item.chunk_id, item.similarity) for item in out] == [("c2", 0.8)]


# --- score = cosine similarity; ordering = RRF (offline search) ------------------------


def test_hit_score_is_cosine_similarity_and_rank_is_rrf_order() -> None:
    lexical = [make_row(1, "s-lex", 0.41), make_row(2, "s-both", 0.83)]
    vector = [make_row(2, "s-both", 0.83), make_row(3, "s-vec", 0.62)]
    conn = FakeConn(lexical, vector)
    hits = retrieval.search(
        conn, tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=UNIT, limit=10
    )
    assert [(h.source.source_id, h.rank, h.score) for h in hits] == [
        ("s-both", 1, 0.83),
        ("s-lex", 2, 0.41),  # RRF puts the lexical #1 above vector #2...
        ("s-vec", 3, 0.62),  # ...even though its similarity is lower.
    ]
    gated = retrieval.search(
        FakeConn(lexical, vector),
        tier=DataTier.TEAM,
        workspace_id=WS,
        query="q",
        query_vector=UNIT,
        min_score=0.6,
    )
    assert [h.source.source_id for h in gated] == ["s-both", "s-vec"]


def test_bound_params_and_limits_resist_mutation() -> None:
    rows = [make_row(i, f"s{i:02d}", 0.5) for i in range(1, 41)]
    for limit, expected in ((10**9, retrieval.MAX_LIMIT), (0, 1), (-5, 1), (3, 3)):
        conn = FakeConn(rows, [])
        hits = retrieval.search(
            conn, tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=UNIT, limit=limit
        )
        assert len(hits) == expected, limit
    executed = [entry for entry in conn.log if entry[0] == "execute"]
    statements = [sql for _, sql, _ in executed]
    assert statements == [
        *retrieval._HNSW_SETTINGS_SQL,
        retrieval._LEXICAL_SQL,
        retrieval._VECTOR_SQL,
    ]
    lexical_params, vector_params = executed[-2][2], executed[-1][2]
    for params, candidates in (
        (lexical_params, retrieval.FTS_CANDIDATES),
        (vector_params, retrieval.VECTOR_CANDIDATES),
    ):
        assert params["workspace_id"] == WS
        assert params["tier"] == "team"
        assert params["query"] == "q"
        assert params["candidates"] == candidates
        assert params["embedding"] == "[" + ",".join(repr(v) for v in UNIT) + "]"


def test_vector_is_bound_as_text_not_registered_per_search() -> None:
    conn = FakeConn([make_row(1, "s1", 0.5)], [])
    retrieval.search(conn, tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=UNIT)
    assert "%(embedding)s::vector" in retrieval._LEXICAL_SQL
    assert "%(embedding)s::vector" in retrieval._VECTOR_SQL
    assert "register_vector" not in Path(retrieval.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "vector", [[0.1] * (DIMS - 1), [float("nan")] + [0.0] * (DIMS - 1), [float("inf")] * DIMS]
)
def test_malformed_query_vector_rejected(vector: list[float]) -> None:
    with pytest.raises(ValueError):
        retrieval.search(
            FakeConn(), tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=vector
        )


# --- typed errors (P2-04 carry-over A, H) --------------------------------------------


@pytest.mark.parametrize(
    "provenance",
    [
        {"version": "v2", "review_status": "authoritative"},
        {"version": True, "review_status": "authoritative"},
        {"review_status": "authoritative"},  # version missing: never silently 1
        {"version": 1},  # review_status missing
        {"version": 1, "review_status": "pending"},
    ],
)
def test_row_mapping_errors_are_retrieval_errors(provenance: dict[str, Any]) -> None:
    conn = FakeConn([make_row(1, "s1", 0.5, provenance=provenance)], [])
    with pytest.raises(RetrievalError):
        retrieval.search(conn, tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=UNIT)


def test_foreign_tier_row_fails_closed() -> None:
    conn = FakeConn([], [make_row(1, "s1", 0.5, tier="confidential")])
    with pytest.raises(RetrievalError):
        retrieval.search(conn, tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=UNIT)


def test_citation_uses_row_created_at() -> None:
    row = make_row(
        1,
        "s1",
        0.5,
        provenance={"version": 2, "review_status": "authoritative", "recorded_at": "2020-01-01"},
    )
    [hit] = retrieval.search(
        FakeConn([row], []), tier=DataTier.TEAM, workspace_id=WS, query="q", query_vector=UNIT
    )
    assert hit.source.created_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert hit.source.version == 2


@pytest.mark.parametrize(
    "pool",
    [
        pytest.param(lambda: FakePool(error=_pool_timeout()), id="pool-timeout"),
        pytest.param(lambda: FakePool(FakeConn(fail_set_local=True)), id="set-local"),
        pytest.param(lambda: FakePool(FakeConn(fail_commit=True)), id="commit"),
    ],
)
def test_pool_and_transaction_failures_are_retrieval_errors(
    pool: Any, loaded_fake_model: FakeTextEmbedding
) -> None:
    with pytest.raises(RetrievalError) as excinfo:
        retrieval.search_with_pool(pool(), tier=DataTier.TEAM, workspace_id=WS, query="quokka")
    assert excinfo.value.__cause__ is not None


def _pool_timeout() -> Exception:
    from psycopg_pool import PoolTimeout

    return PoolTimeout("couldn't get a connection after 5.00 sec")


# --- request validation (P2-04 carry-over E) -------------------------------------------


def test_request_validation(loaded_fake_model: FakeTextEmbedding) -> None:
    from services.contracts.tool_args import MAX_QUERY_CHARS

    pool = FakePool()
    calls: list[tuple[Any, Any, dict[str, Any]]] = [
        (retrieval.search_with_pool, pool, {}),
        (retrieval.search, FakeConn(), {"query_vector": UNIT}),
    ]
    for call, target, extra in calls:
        for workspace_id, query, min_score in (
            ("", "quokka", None),
            ("  ", "quokka", None),
            (WS, "q" * (MAX_QUERY_CHARS + 1), None),
            (WS, "q", float("nan")),
        ):
            with pytest.raises(ValueError):
                call(
                    target,
                    tier=DataTier.TEAM,
                    workspace_id=workspace_id,
                    query=query,
                    min_score=min_score,
                    **extra,
                )
        assert call(target, tier=DataTier.TEAM, workspace_id=WS, query="   ", **extra) == []
        longest = "q" * MAX_QUERY_CHARS
        assert call(target, tier=DataTier.TEAM, workspace_id=WS, query=longest, **extra) == []
    assert pool.checkouts == 1  # only the in-bounds query reached the pool


# --- model: single load, threads, serialized embeds, never loaded on requests ---------


def test_single_model_load_with_one_thread(fake_text_embedding: type[FakeTextEmbedding]) -> None:
    embeddings.warm()
    embeddings.warm()
    embeddings.embed(["hello"])
    embeddings.embed_loaded(["again"])
    assert fake_text_embedding.builds == [
        {"model_name": embeddings.MODEL_ID, "threads": embeddings.EMBED_THREADS}
    ]
    assert embeddings.EMBED_THREADS == 1


def test_concurrent_embeds_are_serialized(loaded_fake_model: FakeTextEmbedding) -> None:
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            embeddings.embed(["concurrent"])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert loaded_fake_model.max_active == 1


def test_request_path_never_loads_the_model(
    fake_text_embedding: type[FakeTextEmbedding],
) -> None:
    pool = FakePool()
    with pytest.raises(RetrievalError) as excinfo:
        retrieval.search_with_pool(pool, tier=DataTier.TEAM, workspace_id=WS, query="quokka")
    assert isinstance(excinfo.value.__cause__, embeddings.EmbeddingUnavailable)
    assert fake_text_embedding.builds == []  # no lazy load, no download
    assert pool.checkouts == 0  # failed before any connection was taken


def test_query_embedded_before_the_transaction(loaded_fake_model: FakeTextEmbedding) -> None:
    log: list[Any] = []
    loaded_fake_model.log = log
    pool = FakePool(FakeConn([make_row(1, "s1", 0.9)], [], log=log))
    hits = retrieval.search_with_pool(pool, tier=DataTier.TEAM, workspace_id=WS, query="quokka")
    assert [hit.source.source_id for hit in hits] == ["s1"]
    assert log[0] == ("embed", ["quokka"])
    assert log[1] == ("checkout",)
    assert not any(entry[0] == "embed" for entry in log[1:])


# --- SQL text (D-09 filters on both legs, P2-04 carry-over C/D) ------------------------


def test_both_legs_share_one_filter_clause() -> None:
    flt = retrieval._FILTER_SQL
    for predicate in (
        "s.id = c.source_id",
        "s.workspace_id = c.workspace_id",
        "s.tier = c.tier",
        "c.workspace_id = %(workspace_id)s",
        "c.tier = %(tier)s",
        "s.status = 'active'",
        "s.provenance ->> 'review_status' = 'authoritative'",
    ):
        assert predicate in flt, predicate
    for sql in (retrieval._LEXICAL_SQL, retrieval._VECTOR_SQL):
        assert sql.count(flt) == 1
        assert sql.count("WHERE") == 1  # no second, weaker WHERE
        assert "%s" not in sql  # named, bound values only
    assert "ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', %(query)s)) > 0" in (
        retrieval._LEXICAL_SQL
    )
    assert "ts_headline(" in retrieval._LEXICAL_SQL
    assert retrieval.HNSW_EF_SEARCH >= retrieval.VECTOR_CANDIDATES


# --- live: seeded corpus -------------------------------------------------------------


@NEEDS_DB
def test_tier_isolation_on_fused_path(real_model: Any) -> None:
    team = _search(DataTier.TEAM, "quokka Friday deploy freeze")
    assert team and team[0].source.source_id == "team-friday-deploy-freeze"
    assert all(hit.source.tier is DataTier.TEAM for hit in team)
    # The vector leg always returns same-tier candidates: isolation means no
    # FOREIGN-tier rows, not empty results (emptiness is min_score's job).
    for query, tier in (
        ("runway review", DataTier.TEAM),
        ("workforce planning", DataTier.CONFIDENTIAL),
    ):
        for hit in _search(tier, query):
            assert hit.source.tier is tier
    assert "conf-runway-review" not in [
        hit.source.source_id for hit in _search(DataTier.TEAM, "runway review")
    ]


@NEEDS_DB
def test_scores_are_similarities_and_superseded_excluded(real_model: Any) -> None:
    hits = _search(DataTier.TEAM, "release checklist")
    ids = [hit.source.source_id for hit in hits]
    assert "team-release-checklist-v2" in ids
    assert "team-release-checklist-v1" not in ids  # superseded stays excluded
    assert all(-1.0 <= hit.score <= 1.0 for hit in hits)
    assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))
    assert _search(DataTier.TEAM, "release checklist", min_score=1.01) == []


@NEEDS_DB
def test_semantic_match_scores_above_nonsense(real_model: Any) -> None:
    """The score is calibratable: a real (vector-only) match scores clearly
    above a nonsense query, so one `min_score` separates them."""
    semantic = _search(DataTier.TEAM, "when do we stop merging code on Fridays")
    nonsense = _search(DataTier.TEAM, "purple xylophone nebula spatula")
    assert semantic and nonsense  # the vector leg always returns neighbours
    top = max(hit.score for hit in semantic)
    noise = max(hit.score for hit in nonsense)
    assert top > noise + 0.1, (top, noise)
    cut = (top + noise) / 2
    kept = _search(DataTier.TEAM, "when do we stop merging code on Fridays", min_score=cut)
    assert kept and all(hit.score >= cut for hit in kept)
    assert _search(DataTier.TEAM, "purple xylophone nebula spatula", min_score=cut) == []


@NEEDS_DB
def test_vector_only_match_surfaces_semantically(real_model: Any) -> None:
    hits = _search(DataTier.TEAM, "when do we stop merging code on Fridays")
    assert hits[0].source.source_id == "team-friday-deploy-freeze"


@NEEDS_DB
def test_snippet_contains_the_matched_terms(real_model: Any) -> None:
    hits = _search(DataTier.CONFIDENTIAL, "expired keys gateway latency")
    [hit] = [h for h in hits if h.source.source_id == "conf-api-key-rotation"]
    snippet = hit.snippet.lower()
    assert "expired keys" in snippet and "gateway latency" in snippet
    assert len(hit.snippet) <= retrieval.SNIPPET_CHARS
    assert "<b>" not in hit.snippet


@NEEDS_DB
def test_exclusion_only_query_has_no_lexical_hits() -> None:
    """`-zzzzz` matches every chunk with ts_rank_cd = 0: no lexical evidence."""
    import psycopg

    params = {
        "workspace_id": WS,
        "tier": "team",
        "query": "-zzzzz",
        "embedding": retrieval._vector_literal(UNIT),
        "candidates": retrieval.FTS_CANDIDATES,
    }
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(retrieval._LEXICAL_SQL, params)
        assert cur.fetchall() == []


@NEEDS_DB
def test_unknown_workspace_returns_nothing(real_model: Any) -> None:
    assert (
        retrieval.search_with_pool(
            OneShotPool(LIVE_DSN), tier=DataTier.TEAM, workspace_id="ws-nonexistent", query="quokka"
        )
        == []
    )


# --- live: D-09 filters on both legs (rows inserted, always rolled back) -------------


def _fetch_legs(cur: Any, params: dict[str, Any]) -> tuple[set[str], set[str]]:
    cur.execute(retrieval._LEXICAL_SQL, {**params, "candidates": retrieval.FTS_CANDIDATES})
    lexical = {row[2] for row in cur.fetchall()}
    cur.execute(retrieval._VECTOR_SQL, {**params, "candidates": retrieval.VECTOR_CANDIDATES})
    vector = {row[2] for row in cur.fetchall()}
    return lexical, vector


@NEEDS_WRITE_DB
def test_d09_filters_hold_on_both_legs() -> None:
    import psycopg

    tag = uuid.uuid4().hex[:10]
    cases: dict[str, tuple[str, str, str, dict[str, Any]]] = {
        # case: (source tier, chunk tier, status, provenance)
        "control": ("team", "team", "active", {"version": 1, "review_status": "authoritative"}),
        "draft": ("team", "team", "active", {"version": 1, "review_status": "draft"}),
        "missing": ("team", "team", "active", {"version": 1}),
        "unknown": ("team", "team", "active", {"version": 1, "review_status": "pending"}),
        "superseded": (
            "team",
            "team",
            "superseded",
            {"version": 1, "review_status": "authoritative", "superseded_by": "x"},
        ),
        # A team-tier chunk hanging off a confidential source (join check, C).
        "tiermix": (
            "confidential",
            "team",
            "active",
            {"version": 1, "review_status": "authoritative"},
        ),
    }
    tokens = {case: f"zqx{case}{tag}" for case in cases}
    source_ids = {case: f"fx-d09-{case}-{tag}" for case in cases}
    query = " or ".join(tokens.values())

    with (
        psycopg.connect(WRITE_DSN) as conn,
        conn.transaction(force_rollback=True),
        conn.cursor() as cur,
    ):
        cur.execute("SELECT embedding::text FROM public.knowledge_chunks LIMIT 1")
        [embedding_text] = cur.fetchone()
        embedding = [float(v) for v in embedding_text.strip("[]").split(",")]
        for case, (source_tier, chunk_tier, status, provenance) in cases.items():
            text = f"D-09 probe {tokens[case]} release checklist deploy freeze"
            digest = hashlib.sha256(text.encode()).hexdigest()
            cur.execute(
                "INSERT INTO public.source_records (source_id, workspace_id, title,"
                " author, tier, status, content_hash, provenance)"
                " VALUES (%s, %s, %s, 'probe', %s, %s, %s, %s::jsonb) RETURNING id",
                (
                    source_ids[case],
                    WS,
                    f"probe {case}",
                    source_tier,
                    status,
                    digest,
                    json.dumps(provenance),
                ),
            )
            [pk] = cur.fetchone()
            cur.execute(
                "INSERT INTO public.knowledge_chunks (source_id, workspace_id, tier,"
                " chunk_text, embedding, content_hash) VALUES (%s, %s, %s, %s, %s::vector, %s)",
                (pk, WS, chunk_tier, text, embedding_text, digest),
            )
        probe_ids = set(source_ids.values())
        control = {source_ids["control"]}

        # Each leg on its own: every probe matches lexically and has
        # similarity 1.0, so only the filters can keep them out.
        params = {
            "workspace_id": WS,
            "tier": "team",
            "query": query,
            "embedding": retrieval._vector_literal(embedding),
        }
        for statement in retrieval._HNSW_SETTINGS_SQL:
            cur.execute(statement)
        lexical, vector = _fetch_legs(cur, params)
        assert lexical & probe_ids == control
        assert vector & probe_ids == control
        # Another workspace sees none of them, on either leg.
        other = {**params, "workspace_id": f"ws-fx-other-{tag}"}
        assert _fetch_legs(cur, other) == (set(), set())

        # The fused search on the same connection and transaction.
        hits = retrieval.search(
            conn,
            tier=DataTier.TEAM,
            workspace_id=WS,
            query=query,
            query_vector=embedding,
            limit=retrieval.MAX_LIMIT,
        )
        assert {hit.source.source_id for hit in hits} & probe_ids == control
        assert all(hit.source.tier is DataTier.TEAM for hit in hits)
        assert (
            retrieval.search(
                conn,
                tier=DataTier.TEAM,
                workspace_id=f"ws-fx-other-{tag}",
                query=query,
                query_vector=embedding,
            )
            == []
        )
    with psycopg.connect(WRITE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.source_records WHERE source_id LIKE %s",
            (f"fx-d09-%-{tag}",),
        )
        assert cur.fetchone() == (0,)  # rolled back: no permanent rows
