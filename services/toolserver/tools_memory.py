"""`memory-mcp` product tools (P2-06; module name per ADR-027).

Each entry in PRODUCT_TOOLS pairs a tool name with its handler. Handlers run
inside the guarded dispatcher (`servers.py`), never directly: they receive the
verified call and the validated P2-02 arguments, and return the P2-02 result
model. P2-10 reuses this pattern for `workspace-mcp` (it owns
`tools_workspace.py`, not this file).
"""

from __future__ import annotations

import json
import re
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
from services.contracts.tool_args import MAX_EXCERPT_CHARS, MAX_TITLE_CHARS
from services.knowledge import embeddings, ingest, retrieval
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
# D-10 (pending samarth-chawla's visible confirmation on #24): a saved note is
# ALWAYS team-tier and ALWAYS a draft; a draft is never chunked or embedded,
# so it is never searchable evidence (D-09 rules 1-3). Drafts are visible
# only in Maya's metadata-only review queue (`draft_review_queue`, ADR-023,
# current drafts only since 0013) until she confirms them.
#
# D-09 rule 2 (clarified by VTCodeCraft on #24, 17 Oct 2026): Maya's
# confirmation makes the note authoritative AND searchable evidence. In the
# same transaction as the flip to `authoritative`, the note is ingested as
# the team-tier source `note-<id>` (`source_records` + `knowledge_chunks`)
# with exactly the P2-03 seed layout (title-prefixed 120/20-word windows,
# `ingest.chunk_hash`, the startup-loaded bge-small model), so retrieval
# treats it like any authoritative seed source. Superseding a note marks its
# source `status='superseded'` (with `provenance.superseded_by`) in the same
# transaction, which drops it out of search and `get_source` (D-09 rule 3).
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


#: `record_id` grammar (review I2): the canonical decimal form of a positive
#: BIGINT `team_notes` primary key — ASCII digits only, no sign, no leading
#: zero, no whitespace, no `_` separators. Python's `int()` accepts all of
#: those (and non-ASCII digits), which let `"092"` slip past a raw-string
#: self-supersede comparison while naming row 92.
_RECORD_ID_RE = re.compile(r"[1-9][0-9]{0,18}")
_BIGINT_MAX = 2**63 - 1


def parse_record_id(record_id: Any) -> int:
    """The `team_notes` primary key named by `record_id`, or InvalidArguments.

    `fullmatch` (never `match`/`$`, which accept a trailing newline) against
    an ASCII-only class (`[0-9]`, never `\\d`, which matches any Unicode
    digit). The format alone is checked: no lookup, so nothing about any
    record is disclosed.
    """
    if not isinstance(record_id, str) or _RECORD_ID_RE.fullmatch(record_id) is None:
        raise InvalidArguments("record_id is not a record reference")
    pk = int(record_id)
    if pk > _BIGINT_MAX:
        raise InvalidArguments("record_id is not a record reference")
    return pk


def _note_pk(record_id: str) -> int:
    """`record_id` is the canonical string form of the `team_notes` primary
    key. Dispatch already rejected any other form at step 1.2; repeated here
    so a direct handler call fails closed too."""
    return parse_record_id(record_id)


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
    are never searchable (D-09 rules 1-2); ingestion happens only when Maya
    confirms the note (`_confirm_note`)."""
    digest = ingest.content_hash(content)
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


def note_source_id(pk: int) -> str:
    """`source_records.source_id` of a confirmed note: `note-<team_notes.id>`
    (prefix reserved by the seed ingest, which never touches these rows)."""
    return f"{ingest.RUNTIME_NOTE_SOURCE_PREFIX}{pk}"


def note_source_title(title: str | None, content_kind: str, pk: int) -> str:
    """Citation title of a confirmed note: the note's own title with control
    and format characters (newlines, bidi overrides, zero-width marks) turned
    into spaces, whitespace runs collapsed, capped at MAX_TITLE_CHARS. A
    note without a usable title gets a fixed label, never text derived from
    its content."""
    printable = "".join(ch if ch.isprintable() else " " for ch in title or "")
    cleaned = " ".join(printable.split())[:MAX_TITLE_CHARS].strip()
    if cleaned:
        return cleaned
    return f"Team {'summary' if content_kind == 'summary' else 'note'} {pk}"


@dataclass(frozen=True)
class _Note:
    """The `team_notes` columns confirmation reads, in the order both
    `_confirm_note` SELECTs name them."""

    title: str | None
    content: str
    content_kind: str
    author_member_id: str | None
    version: int
    review_status: str
    superseded_by_id: int | None


def _note_from_row(row: Any) -> _Note | None:
    if row is None:
        return None
    title, content, content_kind, author, version, review_status, superseded_by = row
    return _Note(
        title=title,
        content=content,
        content_kind=content_kind,
        author_member_id=author,
        version=int(version),
        review_status=review_status,
        superseded_by_id=superseded_by,
    )


def _require_confirmable(note: _Note | None, pk: int, version: int) -> _Note:
    """A CURRENT, attributed draft at exactly `version` (review I1: a
    superseded draft stays superseded), else the uniform
    `precondition_failed`."""
    if note is None:
        raise PreconditionFailed(f"no record {pk}")
    if note.version != version:
        raise PreconditionFailed(f"record {pk} is version {note.version}, not {version}")
    if note.review_status != "draft":
        raise PreconditionFailed(f"record {pk} is already {note.review_status}")
    if note.superseded_by_id is not None:
        raise PreconditionFailed(f"record {pk} is superseded")
    if not note.author_member_id:
        # PRD §8.1: evidence carries its author; an unattributed (legacy
        # seed) note cannot become a source.
        raise PreconditionFailed(f"record {pk} has no author")
    return note


@dataclass(frozen=True)
class _NoteSource:
    """A confirmable note, chunked and embedded (outside any transaction)."""

    source_id: str
    title: str
    content_hash: str
    # (chunk_text, knowledge_chunks.content_hash, pgvector literal)
    chunks: tuple[tuple[str, str, str], ...]
    embed_tool: str


def _embed_note_source(pk: int, note: _Note) -> _NoteSource:
    """Chunk and embed a note exactly like a P2-03 seed source.

    Request-path rule (P2-05): `embeddings.embed_loaded` uses the model the
    tool server warmed at startup and NEVER loads or downloads one; when it
    is not ready it raises `EmbeddingUnavailable`, the dispatcher answers
    `internal`, and nothing has been written (fail closed). Runs before the
    write transaction opens, so a slow embed never holds a pooled
    connection or the row lock.
    """
    source_id = note_source_id(pk)
    title = note_source_title(note.title, note.content_kind, pk)
    texts = ingest.source_chunks(title, note.content)
    vectors = embeddings.embed_loaded(texts)
    if len(vectors) != len(texts):  # pragma: no cover - embed contract
        raise ValueError("one vector per chunk expected")
    return _NoteSource(
        source_id=source_id,
        title=title,
        content_hash=ingest.content_hash(note.content),
        chunks=tuple(
            (text, ingest.chunk_hash(source_id, text), ingest.vector_literal(vector))
            for text, vector in zip(texts, vectors, strict=True)
        ),
        embed_tool=embeddings.model_version(),
    )


def _insert_note_source(
    cur: Any,
    *,
    workspace_id: str,
    note: _Note,
    source: _NoteSource,
    provenance: dict[str, Any],
    created_at: datetime,
) -> None:
    """The confirmed note as a team-tier, active, authoritative source plus
    its chunks (chunk tier = source tier). `(workspace_id, source_id)` is
    unique: if `note-<id>` somehow exists already, nothing is inserted and
    the whole confirmation rolls back (`precondition_failed`) — never a
    duplicate or a half-written source."""
    cur.execute(
        """INSERT INTO public.source_records
           (source_id, workspace_id, source_type, title, author, reference,
            tier, status, content_hash, provenance, created_at, updated_at)
           VALUES (%s, %s, 'team_note', %s, %s, '', 'team', 'active', %s, %s::jsonb, %s, %s)
           ON CONFLICT (workspace_id, source_id) DO NOTHING
           RETURNING id""",
        (
            source.source_id,
            workspace_id,
            source.title,
            note.author_member_id,
            source.content_hash,
            json.dumps(provenance, sort_keys=True),
            created_at,
            created_at,
        ),
    )
    row = cur.fetchone()
    if row is None:
        raise PreconditionFailed(f"source {source.source_id} already exists")
    for text, digest, vector in source.chunks:
        cur.execute(
            """INSERT INTO public.knowledge_chunks
               (source_id, workspace_id, tier, chunk_text, embedding, content_hash)
               VALUES (%s, %s, 'team', %s, %s::vector, %s)""",
            (int(row[0]), workspace_id, text, vector, digest),
        )


def _confirm_note(
    pool: Any,
    *,
    workspace_id: str,
    record_id: str,
    version: int,
    approved_by: str,
    request_id: str,
    jti: str,
) -> tuple[int, int]:
    """Confirm exactly `record_id` + `version` and ingest it as evidence.

    1. Short read (no lock) of the note, with the uniform precondition
       check, so a refused call never embeds anything.
    2. Chunk + embed OUTSIDE any transaction (`_embed_note_source`).
    3. ONE write transaction: re-lock the row (`FOR UPDATE`), re-check every
       precondition and that the note is unchanged since step 1 (notes are
       immutable at runtime; this guards against a concurrent re-seed), flip
       it to `authoritative` with Maya's `approved_by` / `approved_at`, and
       insert its source + chunks. Any failure rolls all of it back.
    """
    pk = _note_pk(record_id)
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT title, content, content_kind, author_member_id, version,
                      review_status, superseded_by_id
               FROM public.team_notes
               WHERE id = %s AND workspace_id = %s""",
            (pk, workspace_id),
        )
        note = _require_confirmable(_note_from_row(cur.fetchone()), pk, version)
    source = _embed_note_source(pk, note)
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT title, content, content_kind, author_member_id, version,
                      review_status, superseded_by_id
               FROM public.team_notes
               WHERE id = %s AND workspace_id = %s
               FOR UPDATE""",
            (pk, workspace_id),
        )
        locked = _require_confirmable(_note_from_row(cur.fetchone()), pk, version)
        if locked != note:
            raise PreconditionFailed(f"record {pk} changed while being confirmed")
        cur.execute(
            """UPDATE public.team_notes
               SET review_status = 'authoritative',
                   approved_by_member_id = %s,
                   approved_at = now(),
                   updated_at = now()
               WHERE id = %s AND workspace_id = %s
               RETURNING version, approved_at""",
            (approved_by, pk, workspace_id),
        )
        updated = cur.fetchone()
        if updated is None:  # unreachable: row locked above
            raise PreconditionFailed(f"no record {pk}")
        confirmed_version, approved_at = int(updated[0]), updated[1]
        _insert_note_source(
            cur,
            workspace_id=workspace_id,
            note=locked,
            source=source,
            provenance={
                "origin": "team_note",
                "note_id": str(pk),
                "content_kind": locked.content_kind,
                "version": confirmed_version,
                "review_status": ReviewStatus.AUTHORITATIVE.value,
                "superseded_by": None,
                "approved_by_member_id": approved_by,
                "approved_at": approved_at.isoformat(),
                "request_id": request_id,
                "jti": jti,
                "embed_model": embeddings.MODEL_ID,
                "embed_tool": source.embed_tool,
                "embed_dim": embeddings.EMBED_DIMS,
                "chunk_words": ingest.CHUNK_WORDS,
                "chunk_overlap": ingest.CHUNK_OVERLAP_WORDS,
            },
            created_at=approved_at,
        )
    return pk, confirmed_version


def check_confirm_arguments(args: Any) -> None:
    """Trusted-code argument policy (dispatch step 1.2): `record_id` must be
    a canonical record reference (review I2)."""
    parse_record_id(args.record_id)


async def handle_knowledge_confirm_maya_decision(
    verified: VerifiedCall, args: Any, runtime: Any
) -> ConfirmMayaDecisionResult:
    """Confirm the exact draft record/version as authoritative (R-56).

    Manager role only (`out_of_scope` for Sam/Priya), bound to the exact
    `record_id` + `version` (`precondition_failed` otherwise). `approved_by`
    is Maya's member ID from `_ctx`; an "approved" text is never accepted
    as input (the P2-02 model has only record_id + version). Repeating with
    the same `jti` is deduplicated by the replay guard; a new `jti` finds the
    note authoritative (`precondition_failed`), so the source `note-<id>` is
    written exactly once. On success the note is searchable team-tier
    evidence (D-09 rule 2); without the startup-loaded embedding model the
    call is `internal` and nothing changes.
    """
    _require_manager(verified, ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION)
    if runtime.pool is None:
        raise _NoDatabase("confirm needs a database pool")
    ctx = verified.ctx
    pk, version = await anyio.to_thread.run_sync(
        partial(
            _confirm_note,
            runtime.pool,
            workspace_id=ctx.workspace_id,
            record_id=args.record_id,
            version=args.version,
            approved_by=ctx.requester_member_id,
            request_id=ctx.request_id,
            jti=ctx.jti,
        )
    )
    return ConfirmMayaDecisionResult(
        tool=ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION,
        record_id=str(pk),
        version=version,
        review_status="authoritative",
    )


def check_supersede_arguments(args: Any) -> None:
    """Trusted-code argument policy (dispatch step 1.2): both ids must be
    canonical record references, and a record cannot supersede itself — the
    PARSED ids are compared, never the raw strings (review I2)."""
    if parse_record_id(args.record_id) == parse_record_id(args.superseded_by_record_id):
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
) -> tuple[int, int, int, str]:
    """Mark exactly `record_id` + `version` superseded by another record.
    Both rows are kept: only `superseded_by_id` and a provenance merge are
    written — history is never deleted or overwritten.

    Both rows are locked in one statement, in primary-key order (two
    concurrent supersessions of the same pair cannot deadlock, and the
    second one re-reads the first one's committed link). The replacement
    must be the CURRENT authoritative record: authoritative (a draft is
    never the replacement of anything) and not itself superseded. Only
    chain heads can be linked, so no cycle can ever form (review I2): a new
    link X -> Y needs Y to have no successor, so Y's chain is just Y, and
    Y != X (argument policy + the 0013 CHECK)."""
    pk = _note_pk(record_id)
    by_pk = _note_pk(superseded_by_record_id)
    if pk == by_pk:  # unreachable through dispatch (step 1.2)
        raise InvalidArguments("a record cannot supersede itself")
    with db.transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, version, review_status, superseded_by_id
               FROM public.team_notes
               WHERE id IN (%s, %s) AND workspace_id = %s
               ORDER BY id
               FOR UPDATE""",
            (pk, by_pk, workspace_id),
        )
        rows = {int(row[0]): row for row in cur.fetchall()}
        target, replacement = rows.get(pk), rows.get(by_pk)
        if target is None:
            raise PreconditionFailed(f"no record {pk}")
        if int(target[1]) != version:
            raise PreconditionFailed(f"record {pk} is version {target[1]}, not {version}")
        if target[3] is not None:
            raise PreconditionFailed(f"record {pk} is already superseded")
        if replacement is None:
            raise PreconditionFailed(f"no replacement record {by_pk}")
        if replacement[2] != "authoritative":
            raise PreconditionFailed(f"replacement {by_pk} is not authoritative")
        if replacement[3] is not None:
            raise PreconditionFailed(f"replacement {by_pk} is itself superseded")
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
        _supersede_note_source(
            cur, workspace_id=workspace_id, pk=pk, by_pk=by_pk, provenance=provenance
        )
    return pk, by_pk, int(updated[0]), str(updated[1])


def _supersede_note_source(
    cur: Any, *, workspace_id: str, pk: int, by_pk: int, provenance: dict[str, Any]
) -> None:
    """D-09 rule 3 for confirmed notes: the superseded note's source (if it
    was ever confirmed and ingested) becomes `status='superseded'`, so it
    drops out of search and `get_source`; `provenance.superseded_by` names
    the replacement's active source (`note-<by_pk>`) when it has one, else
    null (e.g. an authoritative note seeded before D-09 rule 2's ingestion).
    Same transaction as the `team_notes` link; nothing is deleted."""
    cur.execute(
        """SELECT source_id FROM public.source_records
           WHERE workspace_id = %s AND source_id = %s AND status = 'active'""",
        (workspace_id, note_source_id(by_pk)),
    )
    replacement = cur.fetchone()
    merge = {
        "superseded_by": replacement[0] if replacement is not None else None,
        "superseded_at": provenance["superseded_at"],
        "superseded_request_id": provenance["superseded_request_id"],
        "superseded_jti": provenance["superseded_jti"],
    }
    cur.execute(
        """UPDATE public.source_records
           SET status = 'superseded',
               provenance = provenance || %s::jsonb,
               updated_at = now()
           WHERE workspace_id = %s AND source_id = %s""",
        (json.dumps(merge, sort_keys=True), workspace_id, note_source_id(pk)),
    )


async def handle_knowledge_mark_superseded(
    verified: VerifiedCall, args: Any, runtime: Any
) -> MarkSupersededResult:
    """Mark the exact record/version superseded by the current authoritative
    record (R-56).

    Manager role only, version-bound, and history-preserving: both records
    remain; the old one gains `superseded_by` plus reason/request provenance,
    and its search source (if confirmed) leaves search (D-09 rule 3).
    """
    _require_manager(verified, ToolName.KNOWLEDGE_MARK_SUPERSEDED)
    if runtime.pool is None:
        raise _NoDatabase("mark superseded needs a database pool")
    check_supersede_arguments(args)
    ctx = verified.ctx
    pk, by_pk, version, review_status = await anyio.to_thread.run_sync(
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
        record_id=str(pk),
        version=version,
        review_status=ReviewStatus(review_status),
        superseded_by_record_id=str(by_pk),
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
        check_arguments=check_confirm_arguments,
    ),
    ToolName.KNOWLEDGE_MARK_SUPERSEDED: ProductTool(
        name=ToolName.KNOWLEDGE_MARK_SUPERSEDED,
        description=(
            "Manager only: mark the exact record and version superseded by the current "
            "authoritative record; both records and their history are preserved (R-56)."
        ),
        handler=handle_knowledge_mark_superseded,
        check_arguments=check_supersede_arguments,
    ),
}
