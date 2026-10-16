"""`memory-mcp` product tools (P2-06; module name per ADR-027).

Each entry in PRODUCT_TOOLS pairs a tool name with its handler. Handlers run
inside the guarded dispatcher (`servers.py`), never directly: they receive the
verified call and the validated P2-02 arguments, and return the P2-02 result
model. P2-10 reuses this pattern for `workspace-mcp` (it owns
`tools_workspace.py`, not this file).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio.to_thread
from pydantic import BaseModel

from services.contracts import (
    DataTier,
    KnowledgeGetSourceConfidentialResult,
    KnowledgeGetSourceRestrictedResult,
    KnowledgeGetSourceTeamResult,
    KnowledgeSearchResult,
    KnowledgeSearchTeamArgs,
    ReadSourceConfidentialResult,
    ReadSourceRestrictedResult,
    ReadSourceTeamResult,
    SourceCitation,
    SourceRow,
    ToolName,
)
from services.contracts.tool_args import MAX_EXCERPT_CHARS
from services.knowledge import retrieval
from services.toolserver import db
from services.toolserver.schemas import InvalidArguments, NotFound
from services.toolserver.security import VerifiedCall


class _NoDatabase(Exception):
    """No database pool is configured; product tools fail closed."""


async def handle_knowledge_search_team(
    verified: VerifiedCall,
    args: KnowledgeSearchTeamArgs,
    runtime: Any,
) -> KnowledgeSearchResult:
    """Run team-tier search. `DataTier.TEAM` is a literal constant (R-22)."""
    if runtime.pool is None:
        raise _NoDatabase("knowledge search needs a database pool")
    hits = await anyio.to_thread.run_sync(
        partial(
            retrieval.search_with_pool,
            runtime.pool,
            tier=DataTier.TEAM,
            workspace_id=verified.ctx.workspace_id,
            query=args.query,
            limit=args.limit,
        )
    )
    return KnowledgeSearchResult(
        tool=ToolName.KNOWLEDGE_SEARCH_TEAM,
        tier=DataTier.TEAM,
        query=args.query,
        hits=hits,
        min_score=None,
    )


async def _search_at_tier(
    verified: VerifiedCall, args: Any, runtime: Any, tool: ToolName, tier: DataTier
) -> KnowledgeSearchResult:
    """Shared search body; each public handler passes its tier literal (R-22)."""
    if runtime.pool is None:
        raise _NoDatabase("knowledge search needs a database pool")
    hits = await anyio.to_thread.run_sync(
        partial(
            retrieval.search_with_pool,
            runtime.pool,
            tier=tier,
            workspace_id=verified.ctx.workspace_id,
            query=args.query,
            limit=args.limit,
        )
    )
    return KnowledgeSearchResult(tool=tool, tier=tier, query=args.query, hits=hits, min_score=None)


async def handle_knowledge_search_confidential(
    verified: VerifiedCall, args: Any, runtime: Any
) -> KnowledgeSearchResult:
    """Run confidential-tier search (`DataTier.CONFIDENTIAL` literal)."""
    return await _search_at_tier(
        verified, args, runtime, ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL, DataTier.CONFIDENTIAL
    )


async def handle_knowledge_search_restricted(
    verified: VerifiedCall, args: Any, runtime: Any
) -> KnowledgeSearchResult:
    """Run restricted-tier search (`DataTier.RESTRICTED` literal)."""
    return await _search_at_tier(
        verified, args, runtime, ToolName.KNOWLEDGE_SEARCH_RESTRICTED, DataTier.RESTRICTED
    )


def _citation_from_source_row(row: dict[str, Any]) -> SourceCitation:
    """Build a citation from a source_records row (mirrors retrieval._citation:
    recorded synthetic date preferred, stored timestamp as fallback)."""
    from datetime import UTC, datetime

    from services.contracts import ReviewStatus

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


def _fetch_source(pool: Any, workspace_id: str, source_ref: str, tier: DataTier) -> dict | None:
    """Single re-check query (workspace + exact tier + active + authoritative);
    None when missing, wrong-tier, or inactive — the cases share one response."""
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT s.source_id, s.title, s.author, s.tier, s.created_at,
                      s.provenance,
                      (SELECT c.chunk_text FROM public.knowledge_chunks AS c
                        WHERE c.source_id = s.id ORDER BY c.id LIMIT 1) AS excerpt
               FROM public.source_records AS s
               WHERE s.workspace_id = %s AND s.source_id = %s AND s.tier = %s
                 AND s.status = 'active'
                 AND s.provenance ->> 'review_status' = 'authoritative'""",
            (workspace_id, source_ref, tier.value),
        )
        row = cur.fetchone()
        if row is None:
            return None
        names = [d.name for d in cur.description or []]
        return dict(zip(names, row, strict=True))


async def _get_source(
    verified: VerifiedCall,
    args: Any,
    runtime: Any,
    tool: ToolName,
    tier: DataTier,
    result_model: Any,
) -> Any:
    if runtime.pool is None:
        raise _NoDatabase("source read needs a database pool")
    row = await anyio.to_thread.run_sync(
        _fetch_source, runtime.pool, verified.ctx.workspace_id, args.source_ref, tier
    )
    if row is None:
        raise NotFound(f"no visible source for {tool.value}")
    excerpt = (row["excerpt"] or "")[:MAX_EXCERPT_CHARS]
    return result_model(
        tool=tool, tier=tier, source=_citation_from_source_row(row), excerpt=excerpt
    )


async def handle_knowledge_get_source_team(verified: VerifiedCall, args: Any, runtime: Any) -> Any:
    """Read one team-tier source (D-11: `source_ref` is the source_id)."""
    return await _get_source(
        verified,
        args,
        runtime,
        ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
        DataTier.TEAM,
        KnowledgeGetSourceTeamResult,
    )


async def handle_knowledge_get_source_confidential(
    verified: VerifiedCall, args: Any, runtime: Any
) -> Any:
    """Read one confidential-tier source."""
    return await _get_source(
        verified,
        args,
        runtime,
        ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
        DataTier.CONFIDENTIAL,
        KnowledgeGetSourceConfidentialResult,
    )


async def handle_knowledge_get_source_restricted(
    verified: VerifiedCall, args: Any, runtime: Any
) -> Any:
    """Read one restricted-tier source."""
    return await _get_source(
        verified,
        args,
        runtime,
        ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED,
        DataTier.RESTRICTED,
        KnowledgeGetSourceRestrictedResult,
    )


_VIEW_NAMES: tuple[str, ...] = (
    "approved_company_data_team",
    "approved_company_data_confidential",
    "approved_company_data_restricted",
)


def _like_literal(text: str) -> str:
    """Escape LIKE metacharacters for a literal substring match."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def check_read_source_arguments(args: Any) -> None:
    """Trusted-code argument policy for `read_source_*` (dispatch step 1.2).

    `include_superseded=True` is rejected (`invalid_arguments`, fail closed):
    `SourceRow` has no structured status field, so a superseded row would
    reach the responder as current evidence (D-09 rule 3). Rows are always
    `status = 'active'` until an ICR adds a status field to `SourceRow`.
    """
    if args.include_superseded:
        raise InvalidArguments("include_superseded is not supported")


def _read_view_rows(
    pool: Any,
    view: str,
    workspace_id: str,
    author: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Fixed per-view SQL (view name from the allowlist constant only).
    Current rows only: superseded rows are never returned (D-09 rule 3)."""
    from psycopg import sql

    if view not in _VIEW_NAMES:
        # Unreachable: P2-02 Literals admit only the three allowlisted names.
        raise ValueError(f"unknown approved view: {view}")
    table = sql.Identifier("public", view)
    fragments = [sql.SQL("workspace_id = %s"), sql.SQL("status = 'active'")]
    params: list[Any] = [workspace_id]
    if author:
        # Escape LIKE metacharacters: the filter is a literal substring match.
        fragments.append(sql.SQL("owner ILIKE %s ESCAPE '\\'"))
        params.append(_like_literal(author))
    where = sql.SQL(" AND ").join(fragments)
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE ").format(table) + where,
            tuple(params),
        )
        row = cur.fetchone()
        total = row[0] if row else 0
        cur.execute(
            sql.SQL(
                "SELECT project_key, project_name, body, owner, tier, status FROM {} WHERE "
            ).format(table)
            + where
            + sql.SQL(" ORDER BY project_name LIMIT %s"),
            (*params, limit),
        )
        names = [d.name for d in cur.description or []]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    return rows, total


def _snippet(body: str) -> str:
    """Character cap for a returned row (P2-02 `SourceRow.snippet`)."""
    return body[:MAX_EXCERPT_CHARS]


async def _read_source(
    verified: VerifiedCall,
    args: Any,
    runtime: Any,
    tool: ToolName,
    tier: DataTier,
    result_model: Any,
) -> Any:
    if runtime.pool is None:
        raise _NoDatabase("source read needs a database pool")
    if args.include_superseded:
        # Never reached through dispatch (check_read_source_arguments); kept
        # so a direct handler call fails closed too.
        raise InvalidArguments("include_superseded is not supported")
    rows, total = await anyio.to_thread.run_sync(
        _read_view_rows,
        runtime.pool,
        args.view,
        verified.ctx.workspace_id,
        args.author,
        args.limit,
    )
    built = [
        SourceRow(
            source_id=row["project_key"],
            title=row["project_name"],
            snippet=_snippet(row["body"]),
        )
        for row in rows
    ]
    return result_model(
        tool=tool,
        tier=tier,
        view=args.view,
        rows=built,
        row_count=total,
        truncated=len(built) < total,
    )


async def handle_read_source_team(verified: VerifiedCall, args: Any, runtime: Any) -> Any:
    """Read the approved team-tier view."""
    return await _read_source(
        verified,
        args,
        runtime,
        ToolName.READ_SOURCE_TEAM,
        DataTier.TEAM,
        ReadSourceTeamResult,
    )


async def handle_read_source_confidential(verified: VerifiedCall, args: Any, runtime: Any) -> Any:
    """Read the approved confidential-tier view."""
    return await _read_source(
        verified,
        args,
        runtime,
        ToolName.READ_SOURCE_CONFIDENTIAL,
        DataTier.CONFIDENTIAL,
        ReadSourceConfidentialResult,
    )


async def handle_read_source_restricted(verified: VerifiedCall, args: Any, runtime: Any) -> Any:
    """Read the approved restricted-tier view."""
    return await _read_source(
        verified,
        args,
        runtime,
        ToolName.READ_SOURCE_RESTRICTED,
        DataTier.RESTRICTED,
        ReadSourceRestrictedResult,
    )


@dataclass(frozen=True)
class ProductTool:
    name: ToolName
    description: str
    handler: Callable[..., Awaitable[BaseModel]]
    # Trusted-code argument policy beyond the P2-02 schema. The dispatcher
    # runs it right after schema validation (step 1.2, before the ceiling and
    # the handler); InvalidArguments -> recorded `invalid_arguments`.
    check_arguments: Callable[[Any], None] | None = None


PRODUCT_TOOLS: dict[ToolName, ProductTool] = {
    ToolName.KNOWLEDGE_SEARCH_TEAM: ProductTool(
        name=ToolName.KNOWLEDGE_SEARCH_TEAM,
        description=(
            "Search team-tier knowledge the requester is allowed to see. "
            "Returns matching sources with citations."
        ),
        handler=handle_knowledge_search_team,
    ),
    ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL: ProductTool(
        name=ToolName.KNOWLEDGE_SEARCH_CONFIDENTIAL,
        description=(
            "Search confidential-tier knowledge the requester is cleared for. "
            "Member tier ceiling enforced server-side."
        ),
        handler=handle_knowledge_search_confidential,
    ),
    ToolName.KNOWLEDGE_SEARCH_RESTRICTED: ProductTool(
        name=ToolName.KNOWLEDGE_SEARCH_RESTRICTED,
        description=("Search restricted-tier knowledge. Maya-only via the tier ceiling."),
        handler=handle_knowledge_search_restricted,
    ),
    ToolName.KNOWLEDGE_GET_SOURCE_TEAM: ProductTool(
        name=ToolName.KNOWLEDGE_GET_SOURCE_TEAM,
        description="Read one team-tier source by its server-issued reference.",
        handler=handle_knowledge_get_source_team,
    ),
    ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL: ProductTool(
        name=ToolName.KNOWLEDGE_GET_SOURCE_CONFIDENTIAL,
        description="Read one confidential-tier source by its server-issued reference.",
        handler=handle_knowledge_get_source_confidential,
    ),
    ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED: ProductTool(
        name=ToolName.KNOWLEDGE_GET_SOURCE_RESTRICTED,
        description="Read one restricted-tier source by its server-issued reference.",
        handler=handle_knowledge_get_source_restricted,
    ),
    ToolName.READ_SOURCE_TEAM: ProductTool(
        name=ToolName.READ_SOURCE_TEAM,
        description=(
            "Read current rows from the approved team-tier company-data view. "
            "include_superseded=true is rejected."
        ),
        handler=handle_read_source_team,
        check_arguments=check_read_source_arguments,
    ),
    ToolName.READ_SOURCE_CONFIDENTIAL: ProductTool(
        name=ToolName.READ_SOURCE_CONFIDENTIAL,
        description=(
            "Read current rows from the approved confidential-tier company-data view. "
            "include_superseded=true is rejected."
        ),
        handler=handle_read_source_confidential,
        check_arguments=check_read_source_arguments,
    ),
    ToolName.READ_SOURCE_RESTRICTED: ProductTool(
        name=ToolName.READ_SOURCE_RESTRICTED,
        description=(
            "Read current rows from the approved restricted-tier company-data view. "
            "include_superseded=true is rejected."
        ),
        handler=handle_read_source_restricted,
        check_arguments=check_read_source_arguments,
    ),
}
