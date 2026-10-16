"""Mutation-resistant filter tests for `knowledge_get_source_*` and
`read_source_*` (P2-08 review, D-09/D-11).

Each server-side filter has a test that fails when the filter is removed.
Live fixtures are inserted by the toolserver role inside ONE transaction that
always rolls back (`rolled_back()`), and handlers run on that same connection
(`SharedConnection`), so nothing persists. The toolserver role cannot create
workspaces, so cross-workspace cases query the seeded rows under another
workspace id. Where a live fixture is impossible (a second workspace, the
company-data base table: no runtime role writes it), the fixed SQL text is
asserted offline. Live tests need a LOCAL DATABASE_URL_TOOLSERVER (seeded)
plus PROXYLEAD_DB_WRITE_TESTS=1, and skip otherwise.
"""

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import anyio

from services.contracts import KnowledgeGetSourceTeamArgs
from services.toolserver import tools_memory

from .test_tool_dispatch import LIVE_DSN, NEEDS_DB

WS = "workspace-primary"


class SharedConnection:
    """Pool stand-in that hands out one connection: the handler runs inside
    the test's rolled-back transaction (`db.transaction` nests a savepoint)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        yield self._conn


@contextmanager
def rolled_back() -> Iterator[Any]:
    import psycopg

    with psycopg.connect(LIVE_DSN) as conn, conn.transaction(force_rollback=True):
        yield conn


def insert_source(
    conn: Any,
    *,
    source_id: str,
    tier: str,
    chunks: list[tuple[str, str]],
    status: str = "active",
    review_status: str = "authoritative",
) -> None:
    """One source_records row plus its chunks (`(chunk tier, text)` pairs)."""
    provenance: dict[str, Any] = {
        "version": 1,
        "review_status": review_status,
        "recorded_at": "2026-10-01",
        "superseded_by": "x" if status == "superseded" else None,
    }
    with conn.cursor() as cur:
        cur.execute("SELECT embedding::text FROM public.knowledge_chunks LIMIT 1")
        [embedding] = cur.fetchone()
        cur.execute(
            "INSERT INTO public.source_records (source_id, workspace_id, title, author,"
            " tier, status, content_hash, provenance)"
            " VALUES (%s, %s, %s, 'probe', %s, %s, %s, %s::jsonb) RETURNING id",
            (
                source_id,
                WS,
                f"probe {source_id}",
                tier,
                status,
                hashlib.sha256(source_id.encode()).hexdigest(),
                json.dumps(provenance),
            ),
        )
        [pk] = cur.fetchone()
        for chunk_tier, text in chunks:
            cur.execute(
                "INSERT INTO public.knowledge_chunks (source_id, workspace_id, tier,"
                " chunk_text, embedding, content_hash)"
                " VALUES (%s, %s, %s, %s, %s::vector, %s)",
                (
                    pk,
                    WS,
                    chunk_tier,
                    text,
                    embedding,
                    hashlib.sha256(f"{source_id}:{text}".encode()).hexdigest(),
                ),
            )


def get_source_team(conn: Any, source_ref: str, workspace_id: str = WS) -> Any:
    """Run the real team handler on `conn`; the result model, or None on
    not_found."""
    verified = SimpleNamespace(ctx=SimpleNamespace(workspace_id=workspace_id))
    args = KnowledgeGetSourceTeamArgs(source_ref=source_ref)
    runtime = SimpleNamespace(pool=SharedConnection(conn))
    try:
        return anyio.run(tools_memory.handle_knowledge_get_source_team, verified, args, runtime)
    except tools_memory.NotFound:
        return None


def _normalized(sql_text: str) -> str:
    return " ".join(sql_text.split())


# --- I3: the excerpt chunk must share the source's workspace and tier ----------


def test_excerpt_subquery_requires_chunk_workspace_and_tier() -> None:
    """Offline: a second workspace cannot be created by the toolserver role,
    so the workspace half of the chunk join is asserted on the fixed SQL."""
    sql_text = _normalized(tools_memory._GET_SOURCE_SQL)
    assert "c.source_id = s.id AND c.workspace_id = s.workspace_id AND c.tier = s.tier" in (
        sql_text
    )


@NEEDS_DB
def test_excerpt_never_comes_from_a_chunk_of_another_tier() -> None:
    tag = uuid.uuid4().hex[:10]
    secret = f"zq restricted text {tag}"
    control_text = f"zq team text {tag}"
    with rolled_back() as conn:
        insert_source(
            conn,
            source_id=f"fx-mixed-{tag}",
            tier="team",
            chunks=[("restricted", secret)],
        )
        insert_source(
            conn,
            source_id=f"fx-team-{tag}",
            tier="team",
            chunks=[("team", control_text)],
        )
        mixed = get_source_team(conn, f"fx-mixed-{tag}")
        control = get_source_team(conn, f"fx-team-{tag}")
    assert mixed is not None, "the team source itself is visible"
    assert secret not in mixed.excerpt
    assert mixed.excerpt == ""
    assert control is not None and control.excerpt == control_text
    assert fixture_rows_left(tag) == 0


def fixture_rows_left(tag: str) -> int:
    """Rows a test inserted that survived (must be 0: always rolled back)."""
    import psycopg

    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.source_records WHERE source_id LIKE %s",
            (f"fx-%-{tag}",),
        )
        [count] = cur.fetchone()
    return count
