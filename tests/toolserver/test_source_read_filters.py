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

from services.contracts import DataTier, KnowledgeGetSourceTeamArgs, ReadSourceTeamArgs
from services.contracts.tool_args import MAX_EXCERPT_CHARS
from services.toolserver import tools_memory

from .conftest import OneShotPool
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


# --- I4: get_source filters (D-09 authoritative, workspace, tier, truncation) ---


@NEEDS_DB
def test_get_source_hides_drafts_and_non_authoritative_sources() -> None:
    """D-09: only `review_status = 'authoritative'` sources are readable;
    draft, unknown, and superseded sources answer not_found like a miss."""
    tag = uuid.uuid4().hex[:10]
    cases: dict[str, dict[str, str]] = {
        "control": {"review_status": "authoritative"},
        "draft": {"review_status": "draft"},
        "unknown": {"review_status": "pending"},
        "superseded": {"review_status": "authoritative", "status": "superseded"},
    }
    with rolled_back() as conn:
        for case, extra in cases.items():
            insert_source(
                conn,
                source_id=f"fx-d09-{case}-{tag}",
                tier="team",
                chunks=[("team", f"zq {case} text {tag}")],
                **extra,
            )
        found = {case: get_source_team(conn, f"fx-d09-{case}-{tag}") for case in cases}
    assert found["control"] is not None
    assert found["control"].source.review_status.value == "authoritative"
    assert {case for case, result in found.items() if result is not None} == {"control"}
    assert fixture_rows_left(tag) == 0


@NEEDS_DB
def test_get_source_is_workspace_scoped() -> None:
    """A seeded team source is invisible under any other workspace id (the
    workspace comes from the verified `_ctx`, never from arguments)."""
    import psycopg

    with psycopg.connect(LIVE_DSN) as conn:
        assert get_source_team(conn, "team-friday-deploy-freeze") is not None
        other = f"ws-fx-other-{uuid.uuid4().hex[:8]}"
        assert get_source_team(conn, "team-friday-deploy-freeze", workspace_id=other) is None


@NEEDS_DB
def test_get_source_excerpt_is_truncated_to_the_contract_limit() -> None:
    tag = uuid.uuid4().hex[:10]
    long_text = f"zq long excerpt {tag} " * 400  # well over 2000 chars
    with rolled_back() as conn:
        insert_source(conn, source_id=f"fx-long-{tag}", tier="team", chunks=[("team", long_text)])
        result = get_source_team(conn, f"fx-long-{tag}")
    assert len(long_text) > MAX_EXCERPT_CHARS
    assert result is not None
    assert result.excerpt == long_text[:MAX_EXCERPT_CHARS]
    assert fixture_rows_left(tag) == 0


# --- I4: read_source filters (workspace, LIKE escaping, truncation) -------------


def read_source_team(pool: Any, workspace_id: str = WS, **arguments: Any) -> Any:
    """Run the real team read handler on `pool`; returns the result model."""
    verified = SimpleNamespace(ctx=SimpleNamespace(workspace_id=workspace_id))
    args = ReadSourceTeamArgs(view="approved_company_data_team", limit=50, **arguments)
    runtime = SimpleNamespace(pool=pool)
    return anyio.run(tools_memory.handle_read_source_team, verified, args, runtime)


@NEEDS_DB
def test_read_source_is_workspace_scoped() -> None:
    pool = OneShotPool(LIVE_DSN)
    own = read_source_team(pool)
    assert {row.source_id for row in own.rows} >= {"proj-atlas", "proj-beacon"}
    other = read_source_team(pool, workspace_id=f"ws-fx-other-{uuid.uuid4().hex[:8]}")
    assert other.rows == [] and other.row_count == 0


@NEEDS_DB
def test_author_filter_is_a_literal_substring_match() -> None:
    """`%` and `_` in `author` match themselves only. Seeded owners are
    `sam`/`priya`/`maya`: unescaped, each probe below would match rows."""
    pool = OneShotPool(LIVE_DSN)
    assert {row.source_id for row in read_source_team(pool, author="sam").rows} >= {"proj-atlas"}
    for probe in ("%", "_", "s_m", "%a%"):
        result = read_source_team(pool, author=probe)
        assert (result.rows, result.row_count) == ([], 0), probe


@NEEDS_DB
def test_like_literal_matches_metacharacters_literally_in_postgres() -> None:
    """The escaped pattern under the handler's exact `ILIKE ... ESCAPE`."""
    import psycopg

    backslash = chr(92)
    cases = [
        ("50%_off", "50%_OFF", True),
        ("50%_off", "50xyoff", False),
        ("a_b", "a_b", True),
        ("a_b", "axb", False),
        (f"back{backslash}slash", f"back{backslash}slash", True),
        (f"back{backslash}slash", "backslash", False),
    ]
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        for author, owner, expected in cases:
            # Same clause text as the handler: ESCAPE '\' (one backslash).
            cur.execute(
                "SELECT %s ILIKE %s ESCAPE '\\'", (owner, tools_memory._like_literal(author))
            )
            assert cur.fetchone()[0] is expected, (author, owner)


def test_like_literal_escapes_every_metacharacter() -> None:
    backslash = chr(92)
    raw = f"50%_a{backslash}b"
    expected = f"%50{backslash}%{backslash}_a{backslash}{backslash}b%"
    assert tools_memory._like_literal(raw) == expected


def test_read_source_snippet_is_truncated(monkeypatch: Any) -> None:
    """Offline: no runtime role writes the company-data base table, so a long
    body is injected below the SQL layer; the row builder must cap it."""
    long_body = "x" * (MAX_EXCERPT_CHARS + 500)

    def fake_rows(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        row = {
            "project_key": "proj-long",
            "project_name": "Long",
            "body": long_body,
            "owner": "sam",
            "tier": "team",
            "status": "active",
        }
        return [row], 1

    monkeypatch.setattr(tools_memory, "_read_view_rows", fake_rows)
    result = read_source_team(object())
    assert [len(row.snippet) for row in result.rows] == [MAX_EXCERPT_CHARS]


# --- I4: the Sam cell (senior, confidential tool, restricted id) ---------------


@NEEDS_DB
def test_sam_cannot_read_a_restricted_source_through_the_confidential_tool() -> None:
    """Sam clears `knowledge_get_source_confidential`, but a restricted
    source id through it is a byte-identical not_found (never the record,
    never `internal`); his own tier's source still reads."""
    from .test_elevated_tier_tools import GET_SOURCE_TOOLS, member_ctx
    from .test_tool_dispatch import call, live_client, result_text

    tool = GET_SOURCE_TOOLS[DataTier.CONFIDENTIAL]
    with live_client() as client:
        replies = {
            name: call(client, {"source_ref": ref, "_ctx": member_ctx("sam", tool)}, tool=tool)
            for name, ref in (
                ("restricted", "restr-workforce-planning"),
                ("missing", f"no-such-source-{uuid.uuid4().hex[:8]}"),
                ("own", "conf-runway-review"),
            )
        }
    restricted, missing, own = replies["restricted"], replies["missing"], replies["own"]
    assert result_text(restricted) == {"code": "not_found", "message": "No matching source found."}
    assert restricted["result"]["content"] == missing["result"]["content"]
    assert "workforce" not in json.dumps(restricted).lower()
    assert own["result"]["isError"] is False
    body = json.loads(own["result"]["content"][0]["text"])
    assert (body["source"]["source_id"], body["tier"]) == ("conf-runway-review", "confidential")
