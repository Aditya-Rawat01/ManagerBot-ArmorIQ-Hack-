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


def search(
    conn: Any,
    *,
    tier: DataTier,
    workspace_id: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
) -> list[KnowledgeSearchHit]:
    """Full-text search at exactly one tier. Raises RetrievalError on DB failure."""
    if not isinstance(tier, DataTier):
        raise TypeError(f"tier must be a DataTier constant, got {tier!r}")
    if not workspace_id or not query.strip():
        return []
    bounded = max(1, min(int(limit), MAX_LIMIT))
    try:
        with conn.cursor() as cur:
            cur.execute(_SEARCH_SQL, (query, workspace_id, tier.value, query, bounded))
            names = [d.name for d in cur.description or []]
            rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    except Exception as exc:
        raise RetrievalError("lexical retrieval failed") from exc
    hits = []
    for rank, row in enumerate(rows, 1):
        hits.append(
            KnowledgeSearchHit(
                source=_citation(row),
                chunk_id=str(row["id"]),
                snippet=_snippet(row["chunk_text"]),
                score=float(row["score"]),
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
) -> list[KnowledgeSearchHit]:
    """Pool convenience wrapper (P2-06 handlers): one read transaction per call."""
    with db.transaction(pool) as conn:
        return search(conn, tier=tier, workspace_id=workspace_id, query=query, limit=limit)
