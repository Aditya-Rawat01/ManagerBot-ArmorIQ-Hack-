"""Hybrid retrieval with hard-coded tier filters (P2-04 lexical + P2-05 vector,
Arch §5.4 layer 3).

Two fixed, parameterized statements, one per leg, share ONE filter clause
(`_FILTER_SQL`): workspace, exact tier, active status, and the D-09 rule (only
`provenance->>'review_status' = 'authoritative'`; missing/unknown excluded).
The source join also requires `s.workspace_id = c.workspace_id AND s.tier =
c.tier`, so a chunk can never surface through another workspace's or tier's
source (defense in depth). The `tier` argument is a constant supplied by the
calling tool handler (P2-06); no parameter controls sorting, columns, tables,
or the tier. Superseded records are excluded from search entirely (D-09
rule 3); explicit source-read tools (P2-08) reach them instead.

Legs: lexical = `websearch_to_tsquery` over the chunk `tsvector`, ranked by
`ts_rank_cd` (zero-relevance matches, e.g. exclusion-only queries such as
`-word`, are dropped); vector = HNSW cosine nearest neighbours of the query
embedding (`hnsw.ef_search` >= the candidate limit, strict-order iterative
scan so the filters cannot starve the leg; pgvector >= 0.8). Each leg returns
at most FTS_CANDIDATES / VECTOR_CANDIDATES chunks.

Ordering vs score (the threshold VALUE is D-23/E17's; this fixes the scale):

* ORDER: reciprocal rank fusion, the sum of 1/(RRF_K + rank) over the legs a
  chunk appears in. Chunks dedupe by id, then each source keeps its best-fused
  chunk; ties break by (source_id, chunk_id). `rank` is this order (1-based).
* SCORE: `KnowledgeSearchHit.score` is the cosine similarity
  `1 - (embedding <=> query_embedding)` of the returned chunk, computed in SQL
  for every candidate of BOTH legs. It is comparable across queries (the RRF
  sum is rank-only: a nonsense query fuses to the same values as a perfect
  match), in [-1, 1] (bge-small vectors are normalized and cluster high: on
  the seed corpus, on-topic top hits score ~0.70-0.81 and off-topic or
  nonsense queries ~0.45-0.60), specific to the embedding model, and NOT
  monotonic in `rank`. `min_score` (None = no threshold) drops chunks below
  it before source dedupe and the limit.

Snippets: the whole chunk when it fits SNIPPET_CHARS; otherwise, for lexical
matches, `ts_headline` fragments around the matched terms; otherwise the
leading text. Always at most SNIPPET_CHARS.

The query is embedded BEFORE the database transaction (`search_with_pool`),
with the model the tool server warmed at startup (never loaded here), so a
slow or failed embed never holds a pooled connection. Every failure —
embedding, pool, transaction, SQL, or row mapping — raises `RetrievalError`;
handlers fail closed on it, never "no results".
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any, NamedTuple

from services.contracts import DataTier, KnowledgeSearchHit, ReviewStatus, SourceCitation
from services.contracts.tool_args import MAX_QUERY_CHARS
from services.knowledge import embeddings
from services.toolserver import db

DEFAULT_LIMIT = 10
MAX_LIMIT = 25
SNIPPET_CHARS = 400
# Fusion depth per leg and RRF constant (PROPOSED, recorded here for E17).
FTS_CANDIDATES = 50
VECTOR_CANDIDATES = 50
RRF_K = 60
# HNSW candidate list per index scan; must cover the vector leg's LIMIT.
HNSW_EF_SEARCH = 100
if HNSW_EF_SEARCH < VECTOR_CANDIDATES:  # pragma: no cover - constant sanity check
    raise RuntimeError("HNSW_EF_SEARCH must be >= VECTOR_CANDIDATES")

# Fragments around matched terms; no highlight markup in the snippet text.
_HEADLINE_OPTIONS = 'MaxFragments=2,MaxWords=35,MinWords=15,StartSel="",StopSel=""'

# The ONE filter clause both legs use (workspace, exact tier, status, D-09).
_FILTER_SQL = """FROM public.knowledge_chunks AS c
    JOIN public.source_records AS s
        ON s.id = c.source_id
        AND s.workspace_id = c.workspace_id
        AND s.tier = c.tier
    WHERE c.workspace_id = %(workspace_id)s
    AND c.tier = %(tier)s
    AND s.status = 'active'
    AND s.provenance ->> 'review_status' = 'authoritative'"""

_COLUMNS_SQL = """SELECT c.id, c.chunk_text, s.source_id, s.title, s.author, s.tier,
    s.provenance, s.created_at,
    1 - (c.embedding <=> %(embedding)s::vector) AS similarity"""

_TSQUERY_SQL = "websearch_to_tsquery('english', %(query)s)"

_LEXICAL_SQL = f"""{_COLUMNS_SQL},
    ts_rank_cd(c.content_tsv, {_TSQUERY_SQL}) AS text_rank,
    ts_headline('english', c.chunk_text, {_TSQUERY_SQL}, '{_HEADLINE_OPTIONS}') AS headline
    {_FILTER_SQL}
    AND c.content_tsv @@ {_TSQUERY_SQL}
    AND ts_rank_cd(c.content_tsv, {_TSQUERY_SQL}) > 0
    ORDER BY text_rank DESC, c.id ASC
    LIMIT %(candidates)s"""

_VECTOR_SQL = f"""{_COLUMNS_SQL}
    {_FILTER_SQL}
    ORDER BY c.embedding <=> %(embedding)s::vector
    LIMIT %(candidates)s"""

# Transaction-local (SET LOCAL ends with the transaction); harmless when the
# planner does not use the HNSW index.
_HNSW_SETTINGS_SQL = (
    f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}",
    "SET LOCAL hnsw.iterative_scan = strict_order",
)


class RetrievalError(Exception):
    """Retrieval failed (embedding, pool, database, or row mapping).

    Handlers fail closed on this: a tool error, never an empty success.
    """


class Fused(NamedTuple):
    """One fused candidate: RRF orders, `similarity` is the reported score."""

    chunk_id: str
    source_id: str
    rrf: float
    similarity: float


def _clip(text: str) -> str:
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS]


def _snippet(text: str, headline: str | None = None) -> str:
    """Whole chunk if it fits, else matched-term fragments, else leading text."""
    if len(text) <= SNIPPET_CHARS:
        return text
    if headline and headline.strip():
        return _clip(headline.strip())
    return _clip(text)


def _citation(row: Mapping[str, Any]) -> SourceCitation:
    """Provenance from the row; a malformed record raises (never a default)."""
    provenance = row["provenance"]
    if not isinstance(provenance, Mapping):
        raise ValueError("provenance must be a JSON object")
    version = provenance["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("provenance.version must be an integer")
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return SourceCitation(
        source_id=row["source_id"],
        title=row["title"],
        tier=DataTier(row["tier"]),
        author=row["author"],
        created_at=created_at,
        version=version,
        review_status=ReviewStatus(provenance["review_status"]),
        superseded_by=provenance.get("superseded_by"),
        reference_url=None,
    )


def _check_request(tier: DataTier, workspace_id: str, query: str, min_score: float | None) -> bool:
    """Validate a search request. False = blank query (empty result, no I/O)."""
    if not isinstance(tier, DataTier):
        raise TypeError(f"tier must be a DataTier constant, got {tier!r}")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace_id is required")
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query exceeds {MAX_QUERY_CHARS} characters")
    if min_score is not None and (
        isinstance(min_score, bool)
        or not isinstance(min_score, int | float)
        or not math.isfinite(min_score)
    ):
        raise ValueError("min_score must be a finite number or None")
    return bool(query.strip())


def _bound_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector text form `[x,y,...]`, bound as a value and cast in SQL."""
    values = [float(value) for value in vector]
    if len(values) != embeddings.EMBED_DIMS:
        raise ValueError(f"query vector must have {embeddings.EMBED_DIMS} dims")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("query vector must be finite")
    return "[" + ",".join(repr(value) for value in values) + "]"


def embed_query(query: str) -> list[float]:
    """Embed a query with the model warmed at startup (never loads one).

    Raises RetrievalError when the model is unavailable (fail closed).
    """
    try:
        [vector] = embeddings.embed_loaded([query])
    except Exception as exc:
        raise RetrievalError("query embedding unavailable") from exc
    return vector


def fuse_candidates(
    fts_ids: Sequence[tuple[str, str]],
    vector_ids: Sequence[tuple[str, str]],
    *,
    similarity: Mapping[str, float],
    limit: int,
    min_score: float | None = None,
) -> list[Fused]:
    """Reciprocal rank fusion over chunk ids (pure; no database).

    Each list holds (chunk_id, source_id) in rank order. RRF sums
    1/(RRF_K + rank) per list (chunks dedupe naturally by id). Chunks whose
    `similarity` is below `min_score` are dropped, then each source keeps its
    best-fused chunk. Ties break by (source_id, chunk_id): deterministic.
    Returns at most `limit` candidates (none for `limit <= 0`).
    """
    if limit <= 0:
        return []
    fused: dict[str, list[Any]] = {}
    for ranked in (fts_ids, vector_ids):
        for rank, (chunk_id, source_id) in enumerate(ranked, 1):
            entry = fused.setdefault(chunk_id, [source_id, 0.0])
            entry[1] += 1.0 / (RRF_K + rank)
    candidates = [
        Fused(chunk_id, source_id, rrf, float(similarity[chunk_id]))
        for chunk_id, (source_id, rrf) in fused.items()
    ]
    if min_score is not None:
        candidates = [item for item in candidates if item.similarity >= min_score]
    candidates.sort(key=lambda item: (-item.rrf, item.source_id, item.chunk_id))
    best_per_source: dict[str, Fused] = {}
    for item in candidates:
        best_per_source.setdefault(item.source_id, item)
    return list(best_per_source.values())[:limit]


def _rows(cur: Any) -> list[dict[str, Any]]:
    names = [d.name for d in cur.description or []]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _to_hits(
    tier: DataTier,
    lexical: list[dict[str, Any]],
    vector: list[dict[str, Any]],
    *,
    limit: int,
    min_score: float | None,
) -> list[KnowledgeSearchHit]:
    by_id: dict[str, dict[str, Any]] = {}
    similarity: dict[str, float] = {}
    for row in lexical + vector:
        chunk_id = str(row["id"])
        if row["tier"] != tier.value:
            raise ValueError("row outside the requested tier")
        value = float(row["similarity"])
        if not math.isfinite(value):
            raise ValueError("non-finite similarity")
        by_id.setdefault(chunk_id, row)  # lexical row first: it carries the headline
        similarity[chunk_id] = value
    fused = fuse_candidates(
        [(str(row["id"]), row["source_id"]) for row in lexical],
        [(str(row["id"]), row["source_id"]) for row in vector],
        similarity=similarity,
        limit=limit,
        min_score=min_score,
    )
    hits = []
    for rank, item in enumerate(fused, 1):
        row = by_id[item.chunk_id]
        hits.append(
            KnowledgeSearchHit(
                source=_citation(row),
                chunk_id=item.chunk_id,
                snippet=_snippet(row["chunk_text"], row.get("headline")),
                score=item.similarity,
                rank=rank,
            )
        )
    return hits


def search(
    conn: Any,
    *,
    tier: DataTier,
    workspace_id: str,
    query: str,
    query_vector: Sequence[float],
    limit: int = DEFAULT_LIMIT,
    min_score: float | None = None,
) -> list[KnowledgeSearchHit]:
    """Hybrid FTS + vector search at exactly one tier (P2-05 extends P2-04).

    Call it inside `db.transaction(...)` (its `SET LOCAL`s need a transaction;
    both legs then read one snapshot) — `search_with_pool` does both. The
    caller embeds the query first (`embed_query`), outside the transaction.
    Raises TypeError/ValueError on a bad request, RetrievalError on failure.
    """
    if not _check_request(tier, workspace_id, query, min_score):
        return []
    bounded = _bound_limit(limit)
    params = {
        "workspace_id": workspace_id,
        "tier": tier.value,
        "query": query,
        "embedding": _vector_literal(query_vector),
    }
    try:
        with conn.cursor() as cur:
            for statement in _HNSW_SETTINGS_SQL:
                cur.execute(statement)
            cur.execute(_LEXICAL_SQL, {**params, "candidates": FTS_CANDIDATES})
            lexical = _rows(cur)
            cur.execute(_VECTOR_SQL, {**params, "candidates": VECTOR_CANDIDATES})
            vector = _rows(cur)
        return _to_hits(tier, lexical, vector, limit=bounded, min_score=min_score)
    except Exception as exc:
        raise RetrievalError("hybrid retrieval failed") from exc


def search_with_pool(
    pool: Any,
    *,
    tier: DataTier,
    workspace_id: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
    min_score: float | None = None,
) -> list[KnowledgeSearchHit]:
    """P2-06 handlers' entry point: embed, then one read transaction.

    The query is embedded before a connection is checked out. Pool waits
    (`PoolTimeout`), `SET LOCAL`, commit, and every other failure surface as
    RetrievalError.
    """
    if not _check_request(tier, workspace_id, query, min_score):
        return []
    query_vector = embed_query(query)
    try:
        with db.transaction(pool) as conn:
            return search(
                conn,
                tier=tier,
                workspace_id=workspace_id,
                query=query,
                query_vector=query_vector,
                limit=limit,
                min_score=min_score,
            )
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError("retrieval transaction failed") from exc
