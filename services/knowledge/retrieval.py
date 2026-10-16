"""Lexical retrieval with hard-coded tier filters (P2-04, Arch §5.4 layer 3).

One fixed, parameterized SQL statement: full-text search over the chunk
`tsvector` with workspace, exact-tier, active-status, and D-09 authoritativeness
filters baked in. The `tier` argument is a constant supplied by the calling
tool handler (P2-06); no parameter controls sorting, columns, tables, or the
tier. Superseded records are excluded from search entirely (D-09 rule 3);
explicit source-read tools (P2-08) reach them instead.

Score semantics are UNRESOLVED (D-23/E17): `score` is the raw `ts_rank_cd`
value for P2-05's RRF fusion to consume. No MIN_SCORE threshold is applied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.contracts import DataTier, KnowledgeSearchHit, ReviewStatus, SourceCitation
from services.toolserver import db

DEFAULT_LIMIT = 10
MAX_LIMIT = 25
SNIPPET_CHARS = 400
# Fusion depth per method and RRF constant (PROPOSED, recorded here for E17).
FTS_CANDIDATES = 50
VECTOR_CANDIDATES = 50
RRF_K = 60

_SEARCH_SQL = """SELECT c.id, c.chunk_text, s.source_id, s.title, s.author, s.tier,
    s.provenance, s.created_at,
    ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', %s)) AS score
    FROM public.knowledge_chunks AS c
    JOIN public.source_records AS s ON s.id = c.source_id
    WHERE c.workspace_id = %s AND c.tier = %s
    AND s.status = 'active'
    AND s.provenance ->> 'review_status' = 'authoritative'
    AND c.content_tsv @@ websearch_to_tsquery('english', %s)
    ORDER BY score DESC, c.id ASC
    LIMIT %s"""

_VECTOR_SQL = """SELECT c.id, c.chunk_text, s.source_id, s.title, s.author, s.tier,
    s.provenance, s.created_at
    FROM public.knowledge_chunks AS c
    JOIN public.source_records AS s ON s.id = c.source_id
    WHERE c.workspace_id = %s AND c.tier = %s
    AND s.status = 'active'
    AND s.provenance ->> 'review_status' = 'authoritative'
    ORDER BY c.embedding <=> %s::vector
    LIMIT %s"""


class RetrievalError(Exception):
    """Retrieval failed (database error, timeout). Handlers fail closed on this."""


def _snippet(text: str) -> str:
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS]


def _citation(row: dict[str, Any]) -> SourceCitation:
    provenance = row["provenance"] or {}
    recorded = provenance.get("recorded_at") or ""
    try:
        created_at = datetime.fromisoformat(f"{recorded}T00:00:00+00:00")
    except (ValueError, TypeError):
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
    return SourceCitation(
        source_id=row["source_id"],
        title=row["title"],
        tier=DataTier(row["tier"]),
        author=row["author"],
        created_at=created_at,
        version=int(provenance.get("version", 1)),
        review_status=ReviewStatus(provenance.get("review_status", "authoritative")),
        superseded_by=provenance.get("superseded_by"),
        reference_url=None,
    )


def fuse_candidates(
    fts_ids: list[tuple[str, str]],
    vector_ids: list[tuple[str, str]],
    *,
    limit: int,
    min_score: float | None = None,
) -> list[tuple[str, str, float]]:
    """Reciprocal rank fusion over chunk ids (pure; no database).

    Each list holds (chunk_id, source_id) in rank order. Scores sum
    1/(RRF_K + rank) per list; chunks dedupe naturally by id, then sources
    keep their best chunk. Ties break by (source_id, chunk_id): deterministic.
    Returns (chunk_id, source_id, fused_score) cut to `limit`, or fewer when
    `min_score` filters.
    """
    fused: dict[str, list[Any]] = {}
    for ranked in (fts_ids, vector_ids):
        for rank, (chunk_id, source_id) in enumerate(ranked, 1):
            entry = fused.setdefault(chunk_id, [source_id, 0.0])
            entry[1] += 1.0 / (RRF_K + rank)
    ordered = sorted(fused.items(), key=lambda item: (-item[1][1], item[1][0], item[0]))
    best_per_source: dict[str, tuple[str, float]] = {}
    for chunk_id, (source_id, score) in ordered:
        if source_id not in best_per_source:
            best_per_source[source_id] = (chunk_id, score)
    ranked_sources = sorted(
        best_per_source.items(), key=lambda item: (-item[1][1], item[0], item[1][0])
    )
    out = [(chunk_id, source_id, score) for source_id, (chunk_id, score) in ranked_sources]
    if min_score is not None:
        out = [row for row in out if row[2] >= min_score]
    return out[: max(1, limit)]


def search(
    conn: Any,
    *,
    tier: DataTier,
    workspace_id: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
    min_score: float | None = None,
) -> list[KnowledgeSearchHit]:
    """Hybrid FTS + vector search at exactly one tier (P2-05 extends P2-04).

    Fuses FTS_CANDIDATES lexical hits with VECTOR_CANDIDATES cosine hits by
    RRF, dedupes by chunk then source, cuts to `limit`. `score` is the fused
    RRF sum (scale UNRESOLVED, D-23/E17). `min_score` filters fused scores;
    None (default) disables thresholding. Raises RetrievalError on DB or
    embedding failure.
    """
    if not isinstance(tier, DataTier):
        raise TypeError(f"tier must be a DataTier constant, got {tier!r}")
    if not workspace_id or not query.strip():
        return []
    bounded = max(1, min(int(limit), MAX_LIMIT))
    try:
        from pgvector.psycopg import register_vector

        from services.knowledge.embeddings import embed

        query_vector = embed([query])[0]
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(_SEARCH_SQL, (query, workspace_id, tier.value, query, FTS_CANDIDATES))
            names = [d.name for d in cur.description or []]
            fts_rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
            cur.execute(_VECTOR_SQL, (workspace_id, tier.value, query_vector, VECTOR_CANDIDATES))
            names = [d.name for d in cur.description or []]
            vec_rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    except Exception as exc:
        raise RetrievalError("hybrid retrieval failed") from exc
    by_id = {str(row["id"]): row for row in fts_rows + vec_rows}
    fused = fuse_candidates(
        [(str(row["id"]), row["source_id"]) for row in fts_rows],
        [(str(row["id"]), row["source_id"]) for row in vec_rows],
        limit=bounded,
        min_score=min_score,
    )
    hits = []
    for rank, (chunk_id, _source_id, score) in enumerate(fused, 1):
        row = by_id[chunk_id]
        hits.append(
            KnowledgeSearchHit(
                source=_citation(row),
                chunk_id=chunk_id,
                snippet=_snippet(row["chunk_text"]),
                score=score,
                rank=rank,
            )
        )
    return hits


def search_with_pool(
    pool: Any,
    *,
    tier: DataTier,
    workspace_id: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
    min_score: float | None = None,
) -> list[KnowledgeSearchHit]:
    """Pool convenience wrapper (P2-06 handlers): one read transaction per call."""
    with db.transaction(pool) as conn:
        return search(
            conn,
            tier=tier,
            workspace_id=workspace_id,
            query=query,
            limit=limit,
            min_score=min_score,
        )
