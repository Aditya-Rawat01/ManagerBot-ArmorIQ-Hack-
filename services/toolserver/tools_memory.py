"""`memory-mcp` product tools (P2-06; module name per ADR-027).

Each entry in PRODUCT_TOOLS pairs a tool name with its handler. Handlers run
inside the guarded dispatcher (`servers.py`), never directly: they receive the
verified call and the validated P2-02 arguments, and return the P2-02 result
model. P2-10 reuses this pattern for `workspace-mcp` (it owns
`tools_workspace.py`, not this file).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

import anyio.to_thread
from psycopg import sql
from pydantic import BaseModel

from services.contracts import (
    AppRole,
    ConfirmMayaDecisionResult,
    DataTier,
    KnowledgeGetSourceConfidentialResult,
    KnowledgeGetSourceRestrictedResult,
    KnowledgeGetSourceTeamResult,
    KnowledgeSearchResult,
    KnowledgeSearchTeamArgs,
    MarkSupersededResult,
    ReadSourceConfidentialResult,
    ReadSourceRestrictedResult,
    ReadSourceTeamResult,
    ReviewStatus,
    SaveTeamNoteResult,
    SourceCitation,
    SourceRow,
    ToolName,
)
from services.contracts.tool_args import MAX_EXCERPT_CHARS
from services.knowledge import retrieval
from services.toolserver import db
from services.toolserver.schemas import (
    InvalidArguments,
    NotFound,
    OutOfScope,
    PreconditionFailed,
)
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


# One fixed, parameterized re-check (D-11): verified-ctx workspace, the
# handler's exact tier literal, active, authoritative (D-09). The excerpt
# subquery requires the chunk's own workspace and tier to equal the source's,
# so a mis-tiered chunk can never leak into another tier's excerpt.
_GET_SOURCE_SQL = """SELECT s.source_id, s.title, s.author, s.tier, s.created_at,
       s.provenance,
       (SELECT c.chunk_text FROM public.knowledge_chunks AS c
         WHERE c.source_id = s.id
           AND c.workspace_id = s.workspace_id
           AND c.tier = s.tier
         ORDER BY c.id LIMIT 1) AS excerpt
FROM public.source_records AS s
WHERE s.workspace_id = %s AND s.source_id = %s AND s.tier = %s
  AND s.status = 'active'
  AND s.provenance ->> 'review_status' = 'authoritative'"""


def _fetch_source(pool: Any, workspace_id: str, source_ref: str, tier: DataTier) -> dict | None:
    """Single re-check query (workspace + exact tier + active + authoritative);
    None when missing, wrong-tier, or inactive — the cases share one response."""
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(_GET_SOURCE_SQL, (workspace_id, source_ref, tier.value))
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


# The one approved view per tier (D-11, final names). `read_source_*` derives
# its view from the handler's tier literal, never from the arguments.
_VIEW_BY_TIER: dict[DataTier, str] = {
    DataTier.TEAM: "approved_company_data_team",
    DataTier.CONFIDENTIAL: "approved_company_data_confidential",
    DataTier.RESTRICTED: "approved_company_data_restricted",
}


def _like_literal(text: str) -> str:
    """Escape LIKE metacharacters for a literal substring match."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def check_read_source_arguments(args: Any, *, tier: DataTier) -> None:
    """Trusted-code argument policy for `read_source_*` (dispatch step 1.2).

    - `view` must be the tool's own tier view (`_VIEW_BY_TIER[tier]`). The
      P2-02 Literal already enforces it; this keeps the rule if a model ever
      widens, and the SQL uses the tier's view regardless.
    - `include_superseded=True` is rejected (`invalid_arguments`, fail
      closed): `SourceRow` has no structured status field, so a superseded
      row would reach the responder as current evidence (D-09 rule 3). Rows
      are always `status = 'active'` until an ICR adds a status field to
      `SourceRow`.
    """
    if args.view != _VIEW_BY_TIER[tier]:
        raise InvalidArguments("view is not this tool's approved view")
    if args.include_superseded:
        raise InvalidArguments("include_superseded is not supported")


def _read_view_rows(
    pool: Any,
    tier: DataTier,
    workspace_id: str,
    author: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Fixed per-tier SQL: the tier's allowlisted view, the verified-ctx
    workspace, the exact tier again (defense in depth over the view's own
    filter), current rows only (D-09 rule 3)."""
    table = sql.Identifier("public", _VIEW_BY_TIER[tier])
    fragments = [
        sql.SQL("workspace_id = %s"),
        sql.SQL("tier = %s"),
        sql.SQL("status = 'active'"),
    ]
    params: list[Any] = [workspace_id, tier.value]
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
    # Never fails through dispatch (it ran at step 1.2); repeated so a direct
    # handler call fails closed too.
    check_read_source_arguments(args, tier=tier)
    rows, total = await anyio.to_thread.run_sync(
        _read_view_rows,
        runtime.pool,
        tier,
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
        view=_VIEW_BY_TIER[tier],
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


# ---------------------------------------------------------------------------
# P2-09 — knowledge maintenance: draft notes, Maya's confirmation, supersession
#
# D-10 (resolved 16 Oct 2026): a saved note is ALWAYS team-tier and ALWAYS a
# draft; it is never chunked or embedded (D-09 rule 2 — drafts are never
# searchable evidence, so step 9.1's ingestion is deliberately DEFERRED: no
# `knowledge_chunks` row is ever written for a draft). Drafts are visible only
# in Maya's metadata-only review queue (`draft_review_queue`, ADR-023) until
# she confirms them.
#
# Author, workspace and provenance come from the verified `_ctx`
# (SHARED_INTERFACES §5), never from arguments; no tool here touches policy,
# permissions or identity (R-57). These tools are tier-less, so the member
# tier ceiling passes them (step 1.3): the Maya/manager role check therefore
# lives in the handlers, before any SQL runs.
#
# Unmet state (record missing, wrong workspace, stale version, already
# confirmed/superseded, supersede target missing) is one uniform
# `precondition_failed` rejection, recorded never executed: a mutation that
# did nothing must not be reported as an execution, and one code never
# discloses which condition failed.
# ---------------------------------------------------------------------------


def _require_manager(verified: VerifiedCall, tool: ToolName) -> None:
    """Maya-only maintenance (task 9.2/9.3; R-46/R-56): `app_role` read from
    the verified `_ctx`, independent of the ArmorIQ policy. Unknown roles
    fail closed."""
    try:
        role = AppRole(verified.ctx.app_role)
    except ValueError:
        role = AppRole.JUNIOR  # anything unparsable: not a manager
    if role is not AppRole.MANAGER:
        raise OutOfScope(tool.value)


def _note_pk(record_id: str) -> int:
    """`record_id` is the string form of the `team_notes` primary key."""
    try:
        pk = int(record_id)
    except ValueError:
        raise PreconditionFailed(f"no record {record_id!r}") from None
    if pk < 1:
        raise PreconditionFailed(f"no record {record_id!r}")
    return pk


def _insert_note(
    pool: Any,
    *,
    workspace_id: str,
    author_member_id: str,
    content: str,
    content_kind: str,
    title: str | None,
    provenance: dict[str, Any],
) -> tuple[int, int]:
    """One draft row: `review_status='draft'`, `version=1`, `tier='team'`
    (D-10, CHECK-enforced by migration 0013). No chunk/embedding — drafts
    are never searchable (D-09 rule 2)."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO public.team_notes
               (workspace_id, author_member_id, title, content, content_kind,
                review_status, version, content_hash, tier, provenance)
               VALUES (%s, %s, %s, %s, %s, 'draft', 1, %s, 'team', %s)
               RETURNING id, version""",
            (
                workspace_id,
                author_member_id,
                title,
                content,
                content_kind,
                digest,
                json.dumps(provenance, sort_keys=True),
            ),
        )
        row = cur.fetchone()
    if row is None:  # unreachable: INSERT ... RETURNING yields a row
        raise PreconditionFailed("note was not stored")
    return int(row[0]), int(row[1])


async def handle_knowledge_save_team_note(
    verified: VerifiedCall, args: Any, runtime: Any
) -> SaveTeamNoteResult:
    """Save a note/summary as a draft attributed to the verified requester.

    Always `draft` + `team` tier + `version=1`; author and workspace from
    `_ctx` (the P2-02 model accepts no author argument — R-57). Idempotency
    per `jti` is the dispatcher's replay guard: a repeat with the same `jti`
    is replayed, never re-inserted.
    """
    if runtime.pool is None:
        raise _NoDatabase("save note needs a database pool")
    ctx = verified.ctx
    pk, version = await anyio.to_thread.run_sync(
        partial(
            _insert_note,
            runtime.pool,
            workspace_id=ctx.workspace_id,
            author_member_id=ctx.requester_member_id,
            content=args.content,
            content_kind=args.content_kind,
            title=args.title,
            provenance={
                "request_id": ctx.request_id,
                "jti": ctx.jti,
                "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
    )
    return SaveTeamNoteResult(
        tool=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE,
        record_id=str(pk),
        version=version,
        review_status="draft",
    )


def _confirm_note(
    pool: Any,
    *,
    workspace_id: str,
    record_id: str,
    version: int,
    approved_by: str,
) -> tuple[int, int]:
    """Confirm exactly `record_id` + `version`: lock the row, check the
    binding, then mark that version authoritative with Maya's provenance.
    Only a CURRENT draft can be confirmed: a superseded draft stays
    superseded (review I1)."""
    pk = _note_pk(record_id)
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT version, review_status, superseded_by_id FROM public.team_notes
               WHERE id = %s AND workspace_id = %s
               FOR UPDATE""",
            (pk, workspace_id),
        )
        row = cur.fetchone()
        if row is None:
            raise PreconditionFailed(f"no record {pk}")
        if int(row[0]) != version:
            raise PreconditionFailed(f"record {pk} is version {row[0]}, not {version}")
        if row[1] != "draft":
            raise PreconditionFailed(f"record {pk} is already {row[1]}")
        if row[2] is not None:
            raise PreconditionFailed(f"record {pk} is superseded")
        cur.execute(
            """UPDATE public.team_notes
               SET review_status = 'authoritative',
                   approved_by_member_id = %s,
                   approved_at = now(),
                   updated_at = now()
               WHERE id = %s AND workspace_id = %s
               RETURNING version""",
            (approved_by, pk, workspace_id),
        )
        updated = cur.fetchone()
    if updated is None:  # unreachable: row locked above
        raise PreconditionFailed(f"no record {pk}")
    return int(updated[0]), version


async def handle_knowledge_confirm_maya_decision(
    verified: VerifiedCall, args: Any, runtime: Any
) -> ConfirmMayaDecisionResult:
    """Confirm the exact draft record/version as authoritative (R-56).

    Manager role only (`out_of_scope` for Sam/Priya), bound to the exact
    `record_id` + `version` (`precondition_failed` otherwise). `approved_by`
    is Maya's member ID from `_ctx`; an "approved" text is never accepted
    as input (the P2-02 model has only record_id + version). Repeating with
    the same `jti` is deduplicated by the replay guard.
    """
    _require_manager(verified, ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION)
    if runtime.pool is None:
        raise _NoDatabase("confirm needs a database pool")
    ctx = verified.ctx
    version, _ = await anyio.to_thread.run_sync(
        partial(
            _confirm_note,
            runtime.pool,
            workspace_id=ctx.workspace_id,
            record_id=args.record_id,
            version=args.version,
            approved_by=ctx.requester_member_id,
        )
    )
    return ConfirmMayaDecisionResult(
        tool=ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION,
        record_id=args.record_id,
        version=version,
        review_status="authoritative",
    )


def check_supersede_arguments(args: Any) -> None:
    """Trusted-code argument policy (dispatch step 1.2): a record cannot
    supersede itself."""
    if args.record_id == args.superseded_by_record_id:
        raise InvalidArguments("a record cannot supersede itself")


def _supersede_note(
    pool: Any,
    *,
    workspace_id: str,
    record_id: str,
    version: int,
    superseded_by_record_id: str,
    reason: str,
    provenance: dict[str, Any],
) -> tuple[int, int, str]:
    """Mark exactly `record_id` + `version` superseded by another record.
    Both rows are kept: only `superseded_by_id` and a provenance merge are
    written — history is never deleted or overwritten."""
    pk = _note_pk(record_id)
    by_pk = _note_pk(superseded_by_record_id)
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT version, review_status, superseded_by_id
               FROM public.team_notes
               WHERE id = %s AND workspace_id = %s
               FOR UPDATE""",
            (pk, workspace_id),
        )
        row = cur.fetchone()
        if row is None:
            raise PreconditionFailed(f"no record {pk}")
        if int(row[0]) != version:
            raise PreconditionFailed(f"record {pk} is version {row[0]}, not {version}")
        if row[2] is not None:
            raise PreconditionFailed(f"record {pk} is already superseded")
        cur.execute(
            """SELECT 1 FROM public.team_notes WHERE id = %s AND workspace_id = %s""",
            (by_pk, workspace_id),
        )
        if cur.fetchone() is None:
            raise PreconditionFailed(f"no replacement record {by_pk}")
        cur.execute(
            """UPDATE public.team_notes
               SET superseded_by_id = %s,
                   provenance = provenance || %s::jsonb,
                   updated_at = now()
               WHERE id = %s AND workspace_id = %s
               RETURNING version, review_status""",
            (by_pk, json.dumps(provenance, sort_keys=True), pk, workspace_id),
        )
        updated = cur.fetchone()
    if updated is None:  # unreachable: row locked above
        raise PreconditionFailed(f"no record {pk}")
    return int(updated[0]), version, str(updated[1])


async def handle_knowledge_mark_superseded(
    verified: VerifiedCall, args: Any, runtime: Any
) -> MarkSupersededResult:
    """Mark the exact record/version superseded by another record (R-56).

    Manager role only, version-bound, and history-preserving: both records
    remain; the old one gains `superseded_by` plus reason/request provenance.
    """
    _require_manager(verified, ToolName.KNOWLEDGE_MARK_SUPERSEDED)
    if runtime.pool is None:
        raise _NoDatabase("mark superseded needs a database pool")
    check_supersede_arguments(args)
    ctx = verified.ctx
    _, version, review_status = await anyio.to_thread.run_sync(
        partial(
            _supersede_note,
            runtime.pool,
            workspace_id=ctx.workspace_id,
            record_id=args.record_id,
            version=args.version,
            superseded_by_record_id=args.superseded_by_record_id,
            reason=args.reason,
            provenance={
                "superseded_reason": args.reason,
                "superseded_request_id": ctx.request_id,
                "superseded_jti": ctx.jti,
                "superseded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
    )
    return MarkSupersededResult(
        tool=ToolName.KNOWLEDGE_MARK_SUPERSEDED,
        record_id=args.record_id,
        version=version,
        review_status=ReviewStatus(review_status),
        superseded_by_record_id=args.superseded_by_record_id,
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
        check_arguments=partial(check_read_source_arguments, tier=DataTier.TEAM),
    ),
    ToolName.READ_SOURCE_CONFIDENTIAL: ProductTool(
        name=ToolName.READ_SOURCE_CONFIDENTIAL,
        description=(
            "Read current rows from the approved confidential-tier company-data view. "
            "include_superseded=true is rejected."
        ),
        handler=handle_read_source_confidential,
        check_arguments=partial(check_read_source_arguments, tier=DataTier.CONFIDENTIAL),
    ),
    ToolName.READ_SOURCE_RESTRICTED: ProductTool(
        name=ToolName.READ_SOURCE_RESTRICTED,
        description=(
            "Read current rows from the approved restricted-tier company-data view. "
            "include_superseded=true is rejected."
        ),
        handler=handle_read_source_restricted,
        check_arguments=partial(check_read_source_arguments, tier=DataTier.RESTRICTED),
    ),
    ToolName.KNOWLEDGE_SAVE_TEAM_NOTE: ProductTool(
        name=ToolName.KNOWLEDGE_SAVE_TEAM_NOTE,
        description=(
            "Save a team note or summary as a draft attributed to the verified "
            "requester (team tier, never searchable until Maya confirms it)."
        ),
        handler=handle_knowledge_save_team_note,
    ),
    ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION: ProductTool(
        name=ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION,
        description=(
            "Manager only: confirm the exact draft record and version as Maya's "
            "authoritative decision (R-46, R-56)."
        ),
        handler=handle_knowledge_confirm_maya_decision,
    ),
    ToolName.KNOWLEDGE_MARK_SUPERSEDED: ProductTool(
        name=ToolName.KNOWLEDGE_MARK_SUPERSEDED,
        description=(
            "Manager only: mark the exact record and version superseded by another "
            "record; both records and their history are preserved (R-56)."
        ),
        handler=handle_knowledge_mark_superseded,
        check_arguments=check_supersede_arguments,
    ),
}
