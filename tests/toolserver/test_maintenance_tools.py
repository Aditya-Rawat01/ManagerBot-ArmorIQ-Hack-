"""Knowledge maintenance tool tests (P2-09).

Denial/argument tests run without a database (dev path); the offline
SQL-level tests drive the real dispatcher against `RecordingPool`, which
records every statement, its parameters and its transaction (CI-visible).
Behavior tests write permanent rows (team_notes, source_records,
knowledge_chunks, ledger) and need a LOCAL DATABASE_URL_TOOLSERVER with
PROXYLEAD_DB_WRITE_TESTS=1, and skip otherwise; the DSN is captured at
collection time and never logged. Tests that confirm a note (it is embedded)
or search request `real_model` (skipped unless the model is cached).

D-10: a saved note is always team-tier and always a draft, never chunked or
searchable; only Maya (manager role in the verified `_ctx`) can confirm or
supersede, bound to the exact record+version. D-09 rule 2 (clarified 17 Oct
2026): a confirmed note becomes searchable team-tier evidence (source
`note-<id>`); superseding it takes it out of search again.
"""

import inspect
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.contracts import TOOL_CATALOG, ToolKind, ToolName
from services.contracts.tool_args import KnowledgeMarkSupersededArgs
from services.toolserver import main, tools_memory
from services.toolserver.main import create_app
from services.toolserver.schemas import InvalidArguments

from .conftest import make_settings
from .test_tool_dispatch import (
    CTX_SECRET,
    FAKE_DSN,
    LIVE_DSN,
    NEEDS_DB,
    FakeLedger,
    call,
    dev_client,
    fake_ledger_client,
    live_client,
    live_write_dsn,
    make_ctx,
    result_text,
)

# The backend DSN reads Maya's metadata-only review queue (ADR-023). Same
# local-only, opt-in rule as LIVE_DSN; captured at collection time.
BACKEND_DSN = live_write_dsn(
    os.environ.get("DATABASE_URL_BACKEND", ""), os.environ.get("PROXYLEAD_DB_WRITE_TESTS")
)
NEEDS_BOTH_DSNS = pytest.mark.skipif(
    not (LIVE_DSN and BACKEND_DSN),
    reason="needs DATABASE_URL_TOOLSERVER + DATABASE_URL_BACKEND and PROXYLEAD_DB_WRITE_TESTS=1",
)

MAYA = {
    "app_role": "manager",
    "requester_member_id": "m-maya",
    "armoriq_member_email_ref": "PERSONA_MAYA_EMAIL",
}
SAM = {
    "app_role": "senior",
    "requester_member_id": "m-sam",
    "armoriq_member_email_ref": "PERSONA_SAM_EMAIL",
}
PRIYA = {
    "app_role": "junior",
    "requester_member_id": "m-priya",
    "armoriq_member_email_ref": "PERSONA_PRIYA_EMAIL",
}

SAVE = ToolName.KNOWLEDGE_SAVE_TEAM_NOTE
CONFIRM = ToolName.KNOWLEDGE_CONFIRM_MAYA_DECISION
SUPERSEDE = ToolName.KNOWLEDGE_MARK_SUPERSEDED

NOTE_COLUMNS = (
    "author_member_id",
    "review_status",
    "version",
    "tier",
    "content_hash",
    "approved_by_member_id",
    "approved_at",
    "superseded_by_id",
    "provenance",
)


def save_note(client, content: str, *, kind: str = "note", **ctx_overrides: object):
    ctx = make_ctx(tool=SAVE.value, **ctx_overrides)
    return call(
        client,
        {"content": content, "content_kind": kind, "_ctx": ctx},
        tool=SAVE.value,
    )


def ok_json(message: dict) -> dict:
    """The §4 body of a successful (isError False) tool result."""
    assert message["result"]["isError"] is False, message["result"]
    return json.loads(message["result"]["content"][0]["text"])


def confirm(
    client, record_id: str, version: int, *, persona: dict | None = None, **ctx_overrides: Any
) -> dict:
    ctx = make_ctx(tool=CONFIRM.value, **{**(persona or MAYA), **ctx_overrides})
    return call(
        client, {"record_id": record_id, "version": version, "_ctx": ctx}, tool=CONFIRM.value
    )


def supersede(
    client,
    record_id: str,
    version: int,
    by_record_id: str,
    *,
    reason: str = "replaced",
    persona: dict | None = None,
    **ctx_overrides: Any,
) -> dict:
    ctx = make_ctx(tool=SUPERSEDE.value, **{**(persona or MAYA), **ctx_overrides})
    return call(
        client,
        {
            "record_id": record_id,
            "version": version,
            "superseded_by_record_id": by_record_id,
            "reason": reason,
            "_ctx": ctx,
        },
        tool=SUPERSEDE.value,
    )


def note_row(record_id: str) -> dict:
    # S608 false positive: the column list is a module constant of names.
    query = (
        "SELECT author_member_id, review_status, version, tier, content_hash,"
        " approved_by_member_id, approved_at, superseded_by_id, provenance"
        " FROM public.team_notes WHERE id = %s"
    )
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(query, (int(record_id),))
        row = cur.fetchone()
    assert row is not None, f"note {record_id} does not exist"
    return dict(zip(NOTE_COLUMNS, row, strict=True))


def unique_word() -> str:
    """A fresh letters-only token: one lexeme for `websearch_to_tsquery`, and
    in no seed row, so it pins a test's own note in chunks and search."""
    return "zq" + "".join(chr(ord("a") + int(digit, 16)) for digit in uuid.uuid4().hex[:18])


def chunk_count(token: str) -> int:
    """Chunks whose text carries `token`. Chunk text is the title-prefixed
    note content (`"{title}\\n\\n{content}"`, the P2-03 layout), so a note
    whose content holds `token` is counted once it is chunked: 0 for a draft,
    >= 1 once Maya confirms it (this count can fail, unlike a title match)."""
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.knowledge_chunks WHERE chunk_text LIKE %s",
            (f"%{token}%",),
        )
        return int(cur.fetchone()[0])


SOURCE_COLUMNS = (
    "id",
    "source_type",
    "title",
    "author",
    "tier",
    "status",
    "content_hash",
    "provenance",
    "created_at",
)


def source_rows(source_id: str) -> list[dict]:
    """Every `source_records` row with `source_id` (any workspace)."""
    query = (
        "SELECT id, source_type, title, author, tier, status, content_hash, provenance,"
        " created_at FROM public.source_records WHERE source_id = %s"
    )
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(query, (source_id,))
        return [dict(zip(SOURCE_COLUMNS, row, strict=True)) for row in cur.fetchall()]


def source_chunk_rows(source_pk: int) -> list[tuple[str, str, str]]:
    """(tier, workspace_id, chunk_text) of a source's chunks, in id order."""
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tier, workspace_id, chunk_text FROM public.knowledge_chunks"
            " WHERE source_id = %s ORDER BY id",
            (source_pk,),
        )
        return list(cur.fetchall())


def search_team(client, query: str, persona: dict | None = None) -> list[dict]:
    """`knowledge_search_team` hits for `query` (Priya by default: team tier
    is everyone's evidence)."""
    ctx = make_ctx(tool=ToolName.KNOWLEDGE_SEARCH_TEAM.value, **(persona or PRIYA))
    message = call(
        client,
        {"query": query, "limit": 10, "_ctx": ctx},
        tool=ToolName.KNOWLEDGE_SEARCH_TEAM.value,
    )
    return ok_json(message)["hits"]


def get_source_team(client, source_ref: str) -> dict:
    ctx = make_ctx(tool=ToolName.KNOWLEDGE_GET_SOURCE_TEAM.value, **PRIYA)
    return call(
        client,
        {"source_ref": source_ref, "_ctx": ctx},
        tool=ToolName.KNOWLEDGE_GET_SOURCE_TEAM.value,
    )


def hits_for(hits: list[dict], source_id: str, token: str) -> list[dict]:
    """Hits that are the note's source or quote its token."""
    return [
        hit for hit in hits if hit["source"]["source_id"] == source_id or token in hit["snippet"]
    ]


# --- behavior (live, local database only) -----------------------------------


@NEEDS_DB
def test_note_saved_as_draft_with_ctx_author() -> None:
    """D-10: author/workspace from the verified `_ctx`; always draft, team
    tier, version 1; provenance carries the request; never chunked."""
    token = unique_word()
    content = f"p209-draft deploy checklist {token}"
    with live_client() as client:
        message = save_note(client, content, **SAM)
    body = ok_json(message)
    assert body["tool"] == SAVE.value
    assert body["review_status"] == "draft" and body["version"] == 1
    row = note_row(body["record_id"])
    assert row["author_member_id"] == "m-sam", "author is the _ctx requester"
    assert row["review_status"] == "draft" and row["version"] == 1
    assert row["tier"] == "team", "D-10: saved notes are always team-tier"
    assert row["approved_by_member_id"] is None
    assert row["provenance"]["request_id"] == "req-dispatch"
    assert len(row["content_hash"]) == 64
    # D-09 rule 2 / D-10: a draft is never chunked, so never searchable.
    assert chunk_count(token) == 0
    assert source_rows(f"note-{body['record_id']}") == []


@NEEDS_DB
def test_summary_kind_is_draft() -> None:
    """A summary is a draft too — same lifecycle, same queue."""
    with live_client() as client:
        message = save_note(
            client, f"p209-draft summary {uuid.uuid4().hex}", kind="summary", **MAYA
        )
    body = ok_json(message)
    assert body["review_status"] == "draft"
    row = note_row(body["record_id"])
    assert row["review_status"] == "draft", "even Maya's own save stays a draft"


@NEEDS_DB
def test_non_manager_cannot_confirm() -> None:
    """Sam and Priya get `out_of_scope` from the handler role check,
    independent of the ArmorIQ policy (step 9.2)."""
    with live_client() as client:
        saved = ok_json(save_note(client, f"p209-draft {uuid.uuid4().hex}", **MAYA))
        record_id, version = saved["record_id"], saved["version"]
        for persona in (SAM, PRIYA):
            message = call(
                client,
                {
                    "record_id": record_id,
                    "version": version,
                    "_ctx": make_ctx(tool=CONFIRM.value, **persona),
                },
                tool=CONFIRM.value,
            )
            assert result_text(message)["code"] == "out_of_scope", persona
        # The same role check guards supersession (a real replacement id so
        # the argument policy is not what rejects the call).
        message = call(
            client,
            {
                "record_id": record_id,
                "version": version,
                "superseded_by_record_id": str(int(record_id) + 1),
                "reason": "not allowed",
                "_ctx": make_ctx(tool=SUPERSEDE.value, **PRIYA),
            },
            tool=SUPERSEDE.value,
        )
        assert result_text(message)["code"] == "out_of_scope"
    assert note_row(record_id)["review_status"] == "draft"


@NEEDS_DB
def test_confirm_binds_exact_version(real_model: Any) -> None:
    """Stale version → `precondition_failed`; the exact current version
    succeeds once and flips draft → authoritative with Maya's provenance."""
    jti = f"jti-confirm-{uuid.uuid4().hex}"
    again_jti = f"jti-confirm-again-{uuid.uuid4().hex}"
    with live_client() as client:
        saved = ok_json(save_note(client, f"p209-draft {uuid.uuid4().hex}", **SAM))
        record_id, version = saved["record_id"], saved["version"]
        stale = call(
            client,
            {
                "record_id": record_id,
                "version": version + 1,
                "_ctx": make_ctx(tool=CONFIRM.value, **MAYA),
            },
            tool=CONFIRM.value,
        )
        assert result_text(stale)["code"] == "precondition_failed"
        ok = call(
            client,
            {
                "record_id": record_id,
                "version": version,
                "_ctx": make_ctx(tool=CONFIRM.value, jti=jti, **MAYA),
            },
            tool=CONFIRM.value,
        )
        body = ok_json(ok)
        assert body["review_status"] == "authoritative" and body["version"] == version
        row = note_row(record_id)
        assert row["review_status"] == "authoritative"
        assert row["approved_by_member_id"] == "m-maya", "approved_by is the manager _ctx"
        assert row["approved_at"] is not None
        # The same jti again is deduplicated by the replay guard: the note
        # is not re-confirmed and the response references the original row.
        replay = call(
            client,
            {
                "record_id": record_id,
                "version": version,
                "_ctx": make_ctx(tool=CONFIRM.value, jti=jti, **MAYA),
            },
            tool=CONFIRM.value,
        )
        replay_body = result_text(replay)
        assert replay_body["code"] == "replay"
        # A second confirm with a NEW jti fails the state precondition:
        # it succeeds exactly once.
        again = call(
            client,
            {
                "record_id": record_id,
                "version": version,
                "_ctx": make_ctx(tool=CONFIRM.value, jti=again_jti, **MAYA),
            },
            tool=CONFIRM.value,
        )
        assert result_text(again)["code"] == "precondition_failed"


@NEEDS_DB
def test_supersede_preserves_history(real_model: Any) -> None:
    """Both records remain; the old one gains superseded_by plus reason and
    request provenance; the replacement (the current authoritative record)
    is untouched."""
    maya_super = make_ctx(tool=SUPERSEDE.value, **MAYA)
    with live_client() as client:
        old = ok_json(save_note(client, f"p209-draft old {uuid.uuid4().hex}", **MAYA))
        new = ok_json(save_note(client, f"p209-draft new {uuid.uuid4().hex}", **MAYA))
        ok_json(confirm(client, new["record_id"], new["version"]))
        message = call(
            client,
            {
                "record_id": old["record_id"],
                "version": old["version"],
                "superseded_by_record_id": new["record_id"],
                "reason": "replaced by the current runbook",
                "_ctx": maya_super,
            },
            tool=SUPERSEDE.value,
        )
    body = ok_json(message)
    assert body["superseded_by_record_id"] == new["record_id"]
    assert body["review_status"] == "draft"
    old_row = note_row(old["record_id"])
    new_row = note_row(new["record_id"])
    assert old_row["superseded_by_id"] == int(new["record_id"]), "old row kept, link set"
    assert old_row["provenance"]["superseded_reason"] == "replaced by the current runbook"
    assert old_row["provenance"]["superseded_request_id"] == "req-dispatch"
    assert new_row["superseded_by_id"] is None, "replacement untouched"


def saved_authoritative(client, text: str) -> dict:
    """A note saved by Sam and confirmed by Maya (a valid replacement)."""
    saved = ok_json(save_note(client, f"{text} {uuid.uuid4().hex}", **SAM))
    ok_json(confirm(client, saved["record_id"], saved["version"]))
    return saved


@NEEDS_DB
def test_supersession_cycles_are_refused(real_model: Any) -> None:
    """Review I2: only the current record can replace another, so a 2-cycle
    (A->B then B->A) and a 3-cycle (A->B, B->C, then C->A) are refused."""
    with live_client() as client:
        a = saved_authoritative(client, "p209 cycle a")
        b = saved_authoritative(client, "p209 cycle b")
        c = saved_authoritative(client, "p209 cycle c")
        ok_json(supersede(client, a["record_id"], 1, b["record_id"]))
        two_cycle = supersede(client, b["record_id"], 1, a["record_id"])
        assert result_text(two_cycle)["code"] == "precondition_failed"
        ok_json(supersede(client, b["record_id"], 1, c["record_id"]))
        three_cycle = supersede(client, c["record_id"], 1, a["record_id"])
        assert result_text(three_cycle)["code"] == "precondition_failed"
    assert note_row(a["record_id"])["superseded_by_id"] == int(b["record_id"])
    assert note_row(b["record_id"])["superseded_by_id"] == int(c["record_id"])
    assert note_row(c["record_id"])["superseded_by_id"] is None, "chain head untouched"


@NEEDS_DB
def test_replacement_must_be_current_and_authoritative(real_model: Any) -> None:
    """Review I2: a draft, or an already-superseded record, cannot replace
    anything (`precondition_failed`, nothing written)."""
    with live_client() as client:
        target = ok_json(save_note(client, f"p209 target {uuid.uuid4().hex}", **SAM))
        draft = ok_json(save_note(client, f"p209 draft repl {uuid.uuid4().hex}", **SAM))
        old = saved_authoritative(client, "p209 old repl")
        head = saved_authoritative(client, "p209 head repl")
        ok_json(supersede(client, old["record_id"], 1, head["record_id"]))
        for replacement in (draft, old):
            refused = supersede(client, target["record_id"], 1, replacement["record_id"])
            assert result_text(refused)["code"] == "precondition_failed", replacement
    assert note_row(target["record_id"])["superseded_by_id"] is None


@NEEDS_DB
def test_alternate_spelling_cannot_self_supersede() -> None:
    """Review I2 (observed: row 92 superseded by "092"): every non-canonical
    spelling of the row's own id is `invalid_arguments`; the row is
    unchanged."""
    with live_client() as client:
        saved = ok_json(save_note(client, f"p209 self {uuid.uuid4().hex}", **SAM))
        record_id = saved["record_id"]
        for spelling in (f"0{record_id}", f" {record_id}", f"+{record_id}", f"{record_id}\n"):
            message = supersede(client, record_id, 1, spelling)
            assert result_text(message)["code"] == "invalid_arguments", repr(spelling)
    assert note_row(record_id)["superseded_by_id"] is None


@NEEDS_DB
def test_database_refuses_self_supersession() -> None:
    """Review I2 (0013 CHECK): even a direct toolserver-role UPDATE cannot
    make a note its own successor."""
    with live_client() as client:
        saved = ok_json(save_note(client, f"p209 check {uuid.uuid4().hex}", **SAM))
    with (
        psycopg.connect(LIVE_DSN) as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        cur.execute(
            "UPDATE public.team_notes SET superseded_by_id = id WHERE id = %s",
            (int(saved["record_id"]),),
        )
    assert note_row(saved["record_id"])["superseded_by_id"] is None


@NEEDS_DB
def test_supersede_binds_exact_version(real_model: Any) -> None:
    """Review I5 (live): a stale (or future) target version is
    `precondition_failed` and writes nothing; the exact version succeeds."""
    with live_client() as client:
        target = ok_json(save_note(client, f"p209 stale target {uuid.uuid4().hex}", **SAM))
        head = saved_authoritative(client, "p209 stale head")
        for wrong in (target["version"] + 1, target["version"] + 7):
            refused = supersede(client, target["record_id"], wrong, head["record_id"])
            assert result_text(refused)["code"] == "precondition_failed", wrong
        assert note_row(target["record_id"])["superseded_by_id"] is None
        ok_json(supersede(client, target["record_id"], target["version"], head["record_id"]))
    assert note_row(target["record_id"])["superseded_by_id"] == int(head["record_id"])


@NEEDS_DB
def test_other_workspace_cannot_confirm_or_supersede(real_model: Any) -> None:
    """Review I5 (live): Maya's `_ctx` for ANOTHER workspace cannot confirm
    or supersede this workspace's notes (`precondition_failed`, the same
    answer as a missing record). Both calls would succeed without the
    workspace filter: the draft is confirmable and the head is a valid
    replacement."""
    other = {"workspace_id": f"workspace-other-{uuid.uuid4().hex[:8]}"}
    with live_client() as client:
        draft = ok_json(save_note(client, f"p209 xws draft {uuid.uuid4().hex}", **SAM))
        head = saved_authoritative(client, "p209 xws head")
        refused = [
            confirm(client, draft["record_id"], 1, **other),
            supersede(client, draft["record_id"], 1, head["record_id"], **other),
        ]
    for message in refused:
        assert result_text(message)["code"] == "precondition_failed"
    row = note_row(draft["record_id"])
    assert row["review_status"] == "draft" and row["superseded_by_id"] is None
    assert row["approved_by_member_id"] is None
    assert note_row(head["record_id"])["superseded_by_id"] is None
    assert source_rows(f"note-{draft['record_id']}") == []


@NEEDS_DB
def test_superseded_draft_cannot_be_confirmed(real_model: Any) -> None:
    """Review I1: a superseded draft is no longer awaiting review. Confirming
    it is `precondition_failed`; it stays a superseded, unapproved draft."""
    with live_client() as client:
        old = ok_json(save_note(client, f"p209 old draft {uuid.uuid4().hex}", **SAM))
        new = ok_json(save_note(client, f"p209 replacement {uuid.uuid4().hex}", **MAYA))
        ok_json(confirm(client, new["record_id"], new["version"]))
        ok_json(supersede(client, old["record_id"], old["version"], new["record_id"]))
        refused = confirm(client, old["record_id"], old["version"])
    assert result_text(refused)["code"] == "precondition_failed"
    row = note_row(old["record_id"])
    assert row["review_status"] == "draft"
    assert row["superseded_by_id"] == int(new["record_id"])
    assert row["approved_by_member_id"] is None and row["approved_at"] is None


def review_queue_ids(record_ids: list[str]) -> set[int]:
    """Record IDs of `record_ids` listed in `draft_review_queue`, read as the
    backend role (the queue's only reader, ADR-023)."""
    with psycopg.connect(BACKEND_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT record_id FROM public.draft_review_queue WHERE record_id = ANY(%s)",
            ([int(record_id) for record_id in record_ids],),
        )
        return {int(row[0]) for row in cur.fetchall()}


@NEEDS_BOTH_DSNS
def test_review_queue_lists_only_current_drafts(real_model: Any) -> None:
    """Review I1 (0013 view): a draft is queued; once confirmed or
    superseded it leaves Maya's metadata-only queue."""
    with live_client() as client:
        old = ok_json(save_note(client, f"p209 queued {uuid.uuid4().hex}", **PRIYA))
        new = ok_json(save_note(client, f"p209 queued too {uuid.uuid4().hex}", **SAM))
        ids = [old["record_id"], new["record_id"]]
        assert review_queue_ids(ids) == {int(old["record_id"]), int(new["record_id"])}
        ok_json(confirm(client, new["record_id"], new["version"]))
        ok_json(supersede(client, old["record_id"], old["version"], new["record_id"]))
    assert review_queue_ids(ids) == set(), "confirmed and superseded drafts leave the queue"


@NEEDS_DB
def test_draft_is_absent_from_search(real_model: Any) -> None:
    """D-09 rules 1-2: a saved draft is no evidence — `knowledge_search_team`
    never returns it and `knowledge_get_source_team` cannot open it."""
    token = unique_word()
    with live_client() as client:
        saved = ok_json(save_note(client, f"Draft rumor about {token} rollout", **SAM))
        source_id = f"note-{saved['record_id']}"
        assert hits_for(search_team(client, token), source_id, token) == []
        assert result_text(get_source_team(client, source_id))["code"] == "not_found"
    assert chunk_count(token) == 0


@NEEDS_DB
def test_confirmed_note_is_searchable_until_superseded(real_model: Any) -> None:
    """D-09 rule 2 (VTCodeCraft, 17 Oct 2026): save -> absent from search ->
    Maya confirms -> found by `knowledge_search_team` (and `get_source`) as
    authoritative team-tier evidence with the seed chunk layout ->
    superseded -> absent again (D-09 rule 3), history kept."""
    token = unique_word()
    # A newline and a bidi override (U+202E) never reach the citation title.
    title = "Rollback\nrunbook" + chr(0x202E) + " v2"
    content = f"Rollback runbook: run {token} drills every sprint and page the on-call."
    with live_client() as client:
        saved = ok_json(
            call(
                client,
                {
                    "content": content,
                    "content_kind": "note",
                    "title": title,
                    "_ctx": make_ctx(tool=SAVE.value, **SAM),
                },
                tool=SAVE.value,
            )
        )
        record_id = saved["record_id"]
        source_id = f"note-{record_id}"
        assert hits_for(search_team(client, token), source_id, token) == []

        confirm_jti = f"jti-confirm-{uuid.uuid4().hex}"
        ok_json(confirm(client, record_id, 1, jti=confirm_jti))
        found = hits_for(search_team(client, token), source_id, token)
        assert [hit["source"]["source_id"] for hit in found] == [source_id]
        citation = found[0]["source"]
        assert citation["tier"] == "team" and citation["review_status"] == "authoritative"
        assert citation["version"] == 1 and citation["superseded_by"] is None
        assert citation["author"] == "m-sam", "author = the note's verified author"
        assert citation["title"] == "Rollback runbook v2"
        assert token in found[0]["snippet"]
        opened = ok_json(get_source_team(client, source_id))
        assert token in opened["excerpt"] and opened["source"]["source_id"] == source_id

        [row] = source_rows(source_id)
        assert (row["tier"], row["status"], row["source_type"]) == ("team", "active", "team_note")
        assert row["content_hash"] == note_row(record_id)["content_hash"]
        prov = row["provenance"]
        assert prov["origin"] == "team_note" and prov["note_id"] == record_id
        assert prov["approved_by_member_id"] == "m-maya" and prov["jti"] == confirm_jti
        assert prov["embed_model"] == "BAAI/bge-small-en-v1.5" and prov["embed_dim"] == 384
        assert row["created_at"] == note_row(record_id)["approved_at"]
        chunks = source_chunk_rows(row["id"])
        # P2-03 layout: `"{title}\n\n{content}"` in word windows (joined by
        # single spaces), so one window here.
        assert [text for _, _, text in chunks] == [f"Rollback runbook v2 {content}"]
        assert {(tier, ws) for tier, ws, _ in chunks} == {("team", "workspace-primary")}
        assert chunk_count(token) == 1

        replacement = saved_authoritative(client, "Rollback runbook v3")
        ok_json(supersede(client, record_id, 1, replacement["record_id"]))
        assert hits_for(search_team(client, token), source_id, token) == []
        assert result_text(get_source_team(client, source_id))["code"] == "not_found"
    [row] = source_rows(source_id)
    assert row["status"] == "superseded"
    assert row["provenance"]["superseded_by"] == f"note-{replacement['record_id']}"
    assert chunk_count(token) == 1, "history is kept: the chunks stay, search filters them"
    assert note_row(record_id)["review_status"] == "authoritative"


@NEEDS_DB
def test_replayed_confirm_creates_no_duplicate_source(real_model: Any) -> None:
    """A replayed confirm (same jti) is `replay`; a new jti is
    `precondition_failed`; either way exactly one source and one chunk set."""
    token = unique_word()
    jti = f"jti-confirm-{uuid.uuid4().hex}"
    with live_client() as client:
        saved = ok_json(save_note(client, f"Replay check {token}", **PRIYA))
        record_id = saved["record_id"]
        ok_json(confirm(client, record_id, 1, jti=jti))
        assert result_text(confirm(client, record_id, 1, jti=jti))["code"] == "replay"
        again = confirm(client, record_id, 1)
        assert result_text(again)["code"] == "precondition_failed"
    assert len(source_rows(f"note-{record_id}")) == 1
    assert chunk_count(token) == 1


@NEEDS_DB
def test_confirm_fails_closed_without_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """No startup-loaded model: confirm is `internal` (an error ledger row),
    the note stays a draft and no source or chunk is written. The request
    path never loads the model (`_load` here refuses, see conftest)."""
    from services.knowledge import embeddings

    monkeypatch.setattr(embeddings, "_model", None)
    token = unique_word()
    with live_client() as client:
        saved = ok_json(save_note(client, f"No model {token}", **SAM))
        message = confirm(client, saved["record_id"], 1)
    assert result_text(message)["code"] == "internal"
    assert note_row(saved["record_id"])["review_status"] == "draft"
    assert source_rows(f"note-{saved['record_id']}") == []
    assert chunk_count(token) == 0


# --- offline doubles: every statement, its parameters, its transaction -------

FIXED_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def note(pk: int, **values: Any) -> dict[str, Any]:
    """A scripted team_notes row (column -> value); defaults: a current v1
    draft by Sam in the default workspace."""
    row: dict[str, Any] = {
        "id": pk,
        "workspace_id": "workspace-primary",
        "title": None,
        "content": f"scripted note {pk} content",
        "content_kind": "note",
        "author_member_id": "m-sam",
        "version": 1,
        "review_status": "draft",
        "superseded_by_id": None,
        "approved_by_member_id": None,
        "approved_at": None,
    }
    row.update(values)
    return row


def _columns(clause: str) -> list[str]:
    return [name.strip() for name in clause.split(",")]


class RecordingPool:
    """`db.transaction`-compatible pool double (offline, CI-visible).

    Records every cursor statement as (normalized SQL, params, in_transaction)
    and every transaction boundary. SELECTs on `team_notes` /
    `source_records` answer from the scripted rows WITHOUT evaluating the
    WHERE clause (filters are asserted on the recorded SQL instead): rows
    whose `id` / `source_id` appears among the parameters are returned, with
    the columns the statement names. `... RETURNING` statements return what
    the real statement would."""

    def __init__(
        self,
        notes: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        *,
        source_conflict: bool = False,
        after_first_read: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.notes = {row["id"]: row for row in notes or []}
        self.sources = {row["source_id"]: row for row in sources or []}
        self.source_conflict = source_conflict
        # Column changes applied to scripted notes right after the first
        # team_notes SELECT (a row that changes between read and lock).
        self.after_first_read = after_first_read
        self.statements: list[tuple[str, tuple, bool]] = []
        self.events: list[str] = []
        self.in_transaction = False

    @contextmanager
    def connection(self) -> Iterator["_RecordingConn"]:
        yield _RecordingConn(self)

    def close(self) -> None:
        pass

    def matching(self, prefix: str) -> list[tuple[str, tuple, bool]]:
        return [entry for entry in self.statements if entry[0].startswith(prefix)]

    def writes(self) -> list[tuple[str, tuple, bool]]:
        verbs = ("INSERT", "UPDATE", "DELETE")
        return [entry for entry in self.statements if entry[0].startswith(verbs)]

    def respond(self, query: str, params: tuple) -> list[tuple]:
        if query.startswith("SELECT ") and " FROM public.team_notes " in query:
            columns = _columns(query[len("SELECT ") : query.index(" FROM public.team_notes ")])
            ids = [value for value in params if type(value) is int]
            rows = [
                tuple(row[name] for name in columns)
                for pk, row in sorted(self.notes.items())
                if pk in ids
            ]
            if self.after_first_read:
                for pk, changes in self.after_first_read.items():
                    self.notes[pk].update(changes)
                self.after_first_read = None
            return rows
        if query.startswith("SELECT ") and " FROM public.source_records " in query:
            columns = _columns(query[len("SELECT ") : query.index(" FROM public.source_records ")])
            return [
                tuple(row[name] for name in columns)
                for source_id, row in self.sources.items()
                if source_id in params
            ]
        if " RETURNING " not in query:
            return []
        returning = _columns(query.rsplit(" RETURNING ", 1)[1])
        if query.startswith("INSERT INTO public.team_notes"):
            return [(41, 1)]
        if query.startswith("INSERT INTO public.source_records"):
            return [] if self.source_conflict else [(901,)]
        if query.startswith("UPDATE public.team_notes"):
            pk = next(value for value in params if type(value) is int and value in self.notes)
            current = {**self.notes[pk], "approved_at": FIXED_NOW}
            return [tuple(current[name] for name in returning)]
        return []


class _RecordingConn:
    def __init__(self, pool: RecordingPool) -> None:
        self.pool = pool

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.pool.events.append("begin")
        self.pool.in_transaction = True
        try:
            yield
        except BaseException:
            self.pool.events.append("rollback")
            raise
        else:
            self.pool.events.append("commit")
        finally:
            self.pool.in_transaction = False

    def execute(self, query: Any, params: Any = None) -> None:
        # Only db.transaction's `SET LOCAL statement_timeout` (a Composed).
        self.pool.events.append("set_local")

    def cursor(self) -> "_RecordingCursor":
        return _RecordingCursor(self.pool)


class _RecordingCursor:
    def __init__(self, pool: RecordingPool) -> None:
        self.pool = pool
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple = ()) -> None:
        normalized = " ".join(str(query).split())
        values = tuple(params)
        self.pool.statements.append((normalized, values, self.pool.in_transaction))
        self.pool.events.append(normalized.split(" ", 1)[0])
        self._rows = self.pool.respond(normalized, values)
        self.rowcount = len(self._rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


def recording_client(
    monkeypatch: pytest.MonkeyPatch, pool: RecordingPool, ledger: FakeLedger | None = None
) -> TestClient:
    """The real app and guarded dispatcher, with `pool` as its database and an
    in-memory ledger."""
    ledger = ledger if ledger is not None else FakeLedger()
    monkeypatch.setattr(main.db, "create_pool", lambda dsn, **kwargs: pool)
    monkeypatch.setattr(main, "Ledger", lambda pool: ledger)
    app = create_app(make_settings(ctx_secret=CTX_SECRET, database_url_toolserver=FAKE_DSN))
    return TestClient(app, raise_server_exceptions=False)


def test_confirm_refuses_a_superseded_draft_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review I1 (CI-visible): the locked row's `superseded_by_id` is read and
    a superseded draft is `precondition_failed` with no write at all."""
    pool = RecordingPool([note(5, superseded_by_id=6), note(6, review_status="authoritative")])
    with recording_client(monkeypatch, pool) as client:
        message = confirm(client, "5", 1)
    assert result_text(message)["code"] == "precondition_failed"
    assert pool.writes() == []


FAKE_VECTOR = [0.25] * 384
FAKE_EMBED_TOOL = "BAAI/bge-small-en-v1.5+fastembed-test"


def install_embedder(monkeypatch: pytest.MonkeyPatch, pool: RecordingPool) -> list[list[str]]:
    """Loaded-model double: returns one fixed 384-dim vector per text and
    records each embed (and whether a transaction was open) in `pool.events`."""
    from services.knowledge import embeddings

    calls: list[list[str]] = []

    def embed_loaded(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        pool.events.append(f"embed(in_transaction={pool.in_transaction})")
        return [list(FAKE_VECTOR) for _ in texts]

    monkeypatch.setattr(embeddings, "embed_loaded", embed_loaded)
    monkeypatch.setattr(embeddings, "model_version", lambda: FAKE_EMBED_TOOL)
    return calls


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_confirm_ingests_the_note_in_one_transaction_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-09 rule 2 (CI-visible): read -> embed OUTSIDE any transaction ->
    one transaction that re-locks the row, flips it, and inserts the
    team-tier source `note-<id>` plus its chunks in the P2-03 layout."""
    content = "Rotate the pager every Monday at 09:00."
    pool = RecordingPool([note(5, title="Pager rota", content=content)])
    calls = install_embedder(monkeypatch, pool)
    jti = f"jti-confirm-{uuid.uuid4().hex}"
    with recording_client(monkeypatch, pool) as client:
        body = ok_json(confirm(client, "5", 1, jti=jti, requester_member_id="m-maya-ctx"))
    assert body == {
        "tool": CONFIRM.value,
        "record_id": "5",
        "version": 1,
        "review_status": "authoritative",
    }
    chunk = f"Pager rota {content}"  # chunk_text("Pager rota\n\n" + content): one window
    assert calls == [[chunk]]
    statement_events = [e for e in pool.events if e != "set_local"]
    assert statement_events == [
        "begin",
        "SELECT",
        "commit",
        "embed(in_transaction=False)",
        "begin",
        "SELECT",
        "UPDATE",
        "INSERT",
        "INSERT",
        "commit",
    ]
    lock_sql = pool.matching("SELECT")[1][0]
    assert lock_sql.endswith("FOR UPDATE")
    [(source_sql, source_params, source_tx)] = pool.matching("INSERT INTO public.source_records")
    assert "'team_note', %s, %s, '', 'team', 'active'" in source_sql
    assert "ON CONFLICT (workspace_id, source_id) DO NOTHING" in source_sql
    source_id, workspace, title, author, digest, provenance, created, updated = source_params
    assert (source_id, workspace, title, author) == (
        "note-5",
        "workspace-primary",
        "Pager rota",
        "m-sam",
    )
    assert digest == _sha(content) and created == updated == FIXED_NOW
    assert json.loads(provenance) == {
        "origin": "team_note",
        "note_id": "5",
        "content_kind": "note",
        "version": 1,
        "review_status": "authoritative",
        "superseded_by": None,
        "approved_by_member_id": "m-maya-ctx",
        "approved_at": FIXED_NOW.isoformat(),
        "request_id": "req-dispatch",
        "jti": jti,
        "embed_model": "BAAI/bge-small-en-v1.5",
        "embed_tool": FAKE_EMBED_TOOL,
        "embed_dim": 384,
        "chunk_words": 120,
        "chunk_overlap": 20,
    }
    [(chunk_sql, chunk_params, chunk_tx)] = pool.matching("INSERT INTO public.knowledge_chunks")
    assert "VALUES (%s, %s, 'team', %s, %s::vector, %s)" in chunk_sql
    assert chunk_params == (
        901,
        "workspace-primary",
        chunk,
        "[" + ",".join(["0.25"] * 384) + "]",
        _sha(f"note-5:{chunk}"),
    )
    assert source_tx and chunk_tx and all(tx for _, _, tx in pool.writes())


def test_confirm_without_the_model_writes_nothing_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: no startup-loaded model -> `internal` with an `error`
    ledger row, no write, and the request path never tries to load one."""
    from services.knowledge import embeddings

    loads: list[str] = []
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_load", lambda: loads.append("load"))
    pool = RecordingPool([note(5)])
    ledger = FakeLedger()
    with recording_client(monkeypatch, pool, ledger) as client:
        message = confirm(client, "5", 1)
    assert result_text(message)["code"] == "internal"
    assert pool.writes() == [] and loads == []
    [entry] = ledger.entries.values()
    assert (entry.execution_status.value, entry.error_class) == ("error", "EmbeddingUnavailable")


def test_confirm_never_duplicates_a_source_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `note-<id>` already exists the insert does nothing and the whole
    confirmation rolls back as `precondition_failed` (no chunks, no flip)."""
    pool = RecordingPool([note(5)], source_conflict=True)
    install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        message = confirm(client, "5", 1)
    assert result_text(message)["code"] == "precondition_failed"
    assert pool.matching("INSERT INTO public.knowledge_chunks") == []
    assert pool.events[-1] == "rollback"


def test_confirm_refuses_a_note_changed_after_embedding_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The locked re-read must equal what was embedded (a concurrent re-seed
    could rewrite a seeded note): otherwise `precondition_failed`, no write."""
    pool = RecordingPool([note(5)], after_first_read={5: {"content": "rewritten by a re-seed"}})
    install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        message = confirm(client, "5", 1)
    assert result_text(message)["code"] == "precondition_failed"
    assert pool.writes() == []


def test_unattributed_note_cannot_become_evidence_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD §8.1: a source carries its author; a legacy note without one is
    refused before anything is embedded."""
    pool = RecordingPool([note(5, author_member_id=None)])
    calls = install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        message = confirm(client, "5", 1)
    assert result_text(message)["code"] == "precondition_failed"
    assert calls == [] and pool.writes() == []


@pytest.mark.parametrize(("replacement_source", "expected"), [(True, "note-6"), (False, None)])
def test_supersede_takes_the_note_source_out_of_search_offline(
    monkeypatch: pytest.MonkeyPatch, replacement_source: bool, expected: str | None
) -> None:
    """D-09 rule 3: in the supersede transaction the old note's source turns
    `superseded`, `provenance.superseded_by` naming the replacement's active
    source when it has one."""
    sources = [{"source_id": "note-5"}]
    if replacement_source:
        sources.append({"source_id": "note-6"})
    pool = RecordingPool(
        [note(5, review_status="authoritative"), note(6, review_status="authoritative")], sources
    )
    with recording_client(monkeypatch, pool) as client:
        body = ok_json(supersede(client, "5", 1, "6", reason="v2 replaces v1"))
    assert body["superseded_by_record_id"] == "6"
    [(lookup_sql, lookup_params, _)] = pool.matching("SELECT source_id FROM public.source_records")
    assert "AND status = 'active'" in lookup_sql
    assert lookup_params == ("workspace-primary", "note-6")
    [(sql_text, params, in_tx)] = pool.matching("UPDATE public.source_records")
    assert "SET status = 'superseded'" in sql_text and in_tx
    merge, workspace, source_id = params
    assert (workspace, source_id) == ("workspace-primary", "note-5")
    assert json.loads(merge)["superseded_by"] == expected
    verbs = [e for e in pool.events if e not in ("set_local",)]
    assert verbs == ["begin", "SELECT", "UPDATE", "SELECT", "UPDATE", "commit"]


# --- denial and argument paths (no database needed) -------------------------


def test_non_manager_role_check_never_touches_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler raises `out_of_scope` before reading the pool: no SQL can
    run for a non-manager confirm."""
    ledger = FakeLedger()
    with fake_ledger_client(monkeypatch, ledger) as client:
        message = call(
            client,
            {"record_id": "1", "version": 1, "_ctx": make_ctx(tool=CONFIRM.value, **SAM)},
            tool=CONFIRM.value,
        )
    assert result_text(message)["code"] == "out_of_scope"
    assert ledger.entries and all(
        entry.execution_status.value == "rejected_by_tool_server"
        for entry in ledger.entries.values()
    )


def test_self_supersede_is_invalid_arguments() -> None:
    with dev_client() as client:
        message = call(
            client,
            {
                "record_id": "7",
                "version": 1,
                "superseded_by_record_id": "7",
                "reason": "loop",
                "_ctx": make_ctx(tool=SUPERSEDE.value, **MAYA),
            },
            tool=SUPERSEDE.value,
        )
    assert result_text(message)["code"] == "invalid_arguments"


# Spellings `int()` accepts (or that name no BIGINT row) but that are not the
# canonical record reference (review I2: "092" self-superseded row 92).
NON_CANONICAL_IDS = [
    "092",  # leading zero
    " 92",  # leading whitespace
    "92 ",  # trailing whitespace
    "92\n",  # trailing newline (`$` would accept it)
    "+92",  # sign
    "9_2",  # digit separator
    chr(0x0669) + chr(0x0662),  # Arabic-Indic digits nine, two (int() reads 92)
    chr(0xFF19) + chr(0xFF12),  # fullwidth digits nine, two (int() reads 92)
    "0",
    "-1",
    "1e3",
    "9223372036854775808",  # BIGINT max + 1
    "12345678901234567890",  # 20 digits
]


@pytest.mark.parametrize("spelling", NON_CANONICAL_IDS)
def test_non_canonical_record_ids_are_invalid_arguments(spelling: str) -> None:
    """Review I2: confirm and supersede accept only `^[1-9][0-9]{0,18}$`
    (and <= BIGINT max), in every id field; anything else is a recorded
    `invalid_arguments` at dispatch step 1.2, before any lookup."""
    with dev_client() as client:
        confirmed = confirm(client, spelling, 1)
        as_target = supersede(client, spelling, 1, "92")
        as_replacement = supersede(client, "92", 1, spelling)
    for message in (confirmed, as_target, as_replacement):
        assert result_text(message)["code"] == "invalid_arguments", repr(spelling)


@pytest.mark.parametrize(
    "spelling", ["092", " 92", "+92", "9_2", chr(0x0669) + chr(0x0662), chr(0xFF19) + chr(0xFF12)]
)
def test_self_supersede_cannot_hide_behind_another_spelling(spelling: str) -> None:
    """Review I2: the PARSED ids are compared — `int("092") == 92` — so a
    different spelling of the same row is refused, never executed."""
    with pytest.raises(InvalidArguments):
        tools_memory.check_supersede_arguments(
            KnowledgeMarkSupersededArgs(
                record_id="92", version=1, superseded_by_record_id=spelling, reason="loop"
            )
        )
    assert tools_memory.parse_record_id("9223372036854775807") == 2**63 - 1


# R-57: what a maintenance tool must never read or write. Table names are
# matched without their schema, so `FROM members`, `JOIN public.members` and
# `UPDATE plans` are all caught.
R57_FORBIDDEN = (
    "members",
    "plans",
    "plan_steps",
    "approval_requests",
    "topic_access_rules",
    "workspaces",
    "GRANT ",
    "REVOKE ",
    "ALTER ROLE",
    "SET ROLE",
    "SET SESSION AUTHORIZATION",
)
# Every function the three maintenance handlers run (directly or through a
# helper). A new helper must be added here to be scanned.
R57_FUNCTIONS = (
    "handle_knowledge_save_team_note",
    "handle_knowledge_confirm_maya_decision",
    "handle_knowledge_mark_superseded",
    "_require_manager",
    "parse_record_id",
    "_note_pk",
    "check_confirm_arguments",
    "check_supersede_arguments",
    "_insert_note",
    "_confirm_note",
    "_note_from_row",
    "_require_confirmable",
    "_embed_note_source",
    "_insert_note_source",
    "note_source_id",
    "note_source_title",
    "_supersede_note",
    "_supersede_note_source",
)


def _code_only(source: str) -> str:
    """Source without comments/docstrings' prose lines that merely mention
    a table (the SQL strings stay: they are what matters)."""
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


@pytest.mark.parametrize("tool", [SAVE, CONFIRM, SUPERSEDE])
def test_maintenance_tools_have_no_policy_or_identity_side_effects(tool: ToolName) -> None:
    """R-57: the maintenance tools are registered as MAINTENANCE and their
    handler is one of the scanned functions."""
    assert TOOL_CATALOG[tool].kind is ToolKind.MAINTENANCE
    assert tools_memory.PRODUCT_TOOLS[tool].handler.__name__ in R57_FUNCTIONS


@pytest.mark.parametrize("name", R57_FUNCTIONS)
def test_maintenance_helpers_never_touch_policy_or_identity(name: str) -> None:
    """R-57 (review I5): nothing the maintenance tools run — handlers AND
    the helpers that hold their SQL — reads or writes policy, permission,
    member, plan or workspace tables, or changes roles/grants; role and
    identity come only from the verified `_ctx`."""
    source = _code_only(inspect.getsource(getattr(tools_memory, name)))
    sql_words = source.replace("public.", "")
    for forbidden in R57_FORBIDDEN:
        if forbidden.endswith(" ") or forbidden.isupper():
            assert forbidden not in source, (name, forbidden)
            continue
        for verb in ("FROM ", "JOIN ", "INTO ", "UPDATE "):
            assert f"{verb}{forbidden}" not in sql_words, (name, verb + forbidden)


def test_maintenance_sql_lives_only_in_scanned_functions() -> None:
    """Every `public.` table the P2-09 section names sits inside a scanned
    function (no SQL constant or helper escapes the R-57 scan)."""
    module = inspect.getsource(tools_memory)
    start = module.index("# P2-09 — knowledge maintenance")
    end = module.index("class ProductTool")
    section = module[start:end]
    scanned = "".join(inspect.getsource(getattr(tools_memory, name)) for name in R57_FUNCTIONS)
    for table in set(re.findall(r"public\.[a-z_]+", section)):
        assert table in scanned, table
    statements = re.findall(r'"""\s*(?:SELECT|INSERT|UPDATE|DELETE)[^"]*"""', section)
    assert statements, "the section holds the maintenance SQL"
    for statement in statements:
        assert statement in scanned, statement[:60]


# --- I5: mutation-resistant offline checks (CI-visible) ----------------------


@pytest.mark.parametrize("persona", [SAM, PRIYA, {**MAYA, "app_role": "senior"}])
@pytest.mark.parametrize("tool", [CONFIRM, SUPERSEDE])
def test_role_check_runs_before_any_sql(
    monkeypatch: pytest.MonkeyPatch, tool: ToolName, persona: dict
) -> None:
    """Review I5: confirm AND supersede refuse every non-manager role (Sam,
    Priya, even Maya's member ID with a non-manager role: the ROLE decides,
    not the member ID) with a recorded `out_of_scope` before the pool is
    touched — the scripted records would otherwise make the call succeed.
    (`CtxEnvelope` itself refuses roles outside manager/senior/junior.)"""
    pool = RecordingPool(
        [note(5, review_status="authoritative"), note(6, review_status="authoritative")],
        [{"source_id": "note-5"}],
    )
    install_embedder(monkeypatch, pool)
    ledger = FakeLedger()
    with recording_client(monkeypatch, pool, ledger) as client:
        if tool is CONFIRM:
            pool.notes[5]["review_status"] = "draft"
            message = confirm(client, "5", 1, persona=persona)
        else:
            message = supersede(client, "5", 1, "6", persona=persona)
    assert result_text(message)["code"] == "out_of_scope"
    assert pool.statements == [] and pool.events == []
    assert [e.execution_status.value for e in ledger.entries.values()] == [
        "rejected_by_tool_server"
    ]


@pytest.mark.parametrize(("stored", "asked"), [(2, 1), (1, 2)])
def test_confirm_binds_the_exact_version_offline(
    monkeypatch: pytest.MonkeyPatch, stored: int, asked: int
) -> None:
    """Review I5: a version mismatch (stale or ahead) is
    `precondition_failed`: nothing embedded, nothing written."""
    pool = RecordingPool([note(5, version=stored)])
    calls = install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        message = confirm(client, "5", asked)
    assert result_text(message)["code"] == "precondition_failed"
    assert calls == [] and pool.writes() == []


@pytest.mark.parametrize(("stored", "asked"), [(2, 1), (1, 2)])
def test_supersede_binds_the_exact_version_offline(
    monkeypatch: pytest.MonkeyPatch, stored: int, asked: int
) -> None:
    """Review I5: supersede is bound to the target's exact version."""
    pool = RecordingPool([note(5, version=stored), note(6, review_status="authoritative")])
    with recording_client(monkeypatch, pool) as client:
        message = supersede(client, "5", asked, "6")
    assert result_text(message)["code"] == "precondition_failed"
    assert pool.writes() == []


def test_save_writes_a_team_draft_by_the_ctx_requester_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I5: the INSERT stores literal `'draft'`, version 1 and tier
    `'team'`; author and workspace are the verified `_ctx` values; nothing
    is chunked, embedded or made a source."""
    pool = RecordingPool()
    calls = install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        message = save_note(
            client,
            "Remember the freeze",
            requester_member_id="m-ctx-author",
            workspace_id="workspace-ctx",
            request_id="req-ctx",
        )
    body = ok_json(message)
    assert body["review_status"] == "draft" and body["record_id"] == "41"
    [(sql_text, params, in_tx)] = pool.statements
    assert sql_text.startswith("INSERT INTO public.team_notes")
    columns = sql_text[sql_text.index("(") + 1 : sql_text.index(")")]
    assert _columns(columns) == [
        "workspace_id",
        "author_member_id",
        "title",
        "content",
        "content_kind",
        "review_status",
        "version",
        "content_hash",
        "tier",
        "provenance",
    ]
    assert "VALUES (%s, %s, %s, %s, %s, 'draft', 1, %s, 'team', %s)" in sql_text
    assert "authoritative" not in sql_text
    workspace, author, title, content, kind, digest, provenance = params
    assert (workspace, author, title, content, kind) == (
        "workspace-ctx",
        "m-ctx-author",
        None,
        "Remember the freeze",
        "note",
    )
    assert digest == _sha("Remember the freeze") and in_tx
    assert json.loads(provenance)["request_id"] == "req-ctx"
    assert calls == []


def test_confirm_records_the_ctx_manager_as_approver_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I5: `approved_by_member_id` is the verified `_ctx` requester
    (never an argument, never a constant)."""
    pool = RecordingPool([note(5)])
    install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        ok_json(confirm(client, "5", 1, requester_member_id="m-ctx-approver"))
    [(sql_text, params, _)] = pool.matching("UPDATE public.team_notes")
    assert "SET review_status = 'authoritative', approved_by_member_id = %s" in sql_text
    assert params == ("m-ctx-approver", 5, "workspace-primary")
    [(_, source_params, _)] = pool.matching("INSERT INTO public.source_records")
    assert json.loads(source_params[5])["approved_by_member_id"] == "m-ctx-approver"


def test_every_maintenance_statement_is_scoped_to_the_ctx_workspace_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I5: the confirm read, the confirm lock, the supersede lock
    (target AND replacement) and every write filter on the verified `_ctx`
    workspace — a dropped filter or a wrong parameter fails here."""
    workspace = "workspace-ctx"
    notes = [
        note(5, workspace_id=workspace),
        note(6, workspace_id=workspace, review_status="authoritative"),
        note(7, workspace_id=workspace),
    ]
    pool = RecordingPool(notes, [{"source_id": "note-6"}])
    install_embedder(monkeypatch, pool)
    with recording_client(monkeypatch, pool) as client:
        ok_json(confirm(client, "5", 1, workspace_id=workspace))
        ok_json(supersede(client, "7", 1, "6", workspace_id=workspace))
    team_notes = [e for e in pool.statements if "public.team_notes" in e[0]]
    read, lock, flip, supersede_lock, link = team_notes
    assert read[0].endswith("WHERE id = %s AND workspace_id = %s")
    assert read[1] == (5, workspace)
    assert lock[0].endswith("WHERE id = %s AND workspace_id = %s FOR UPDATE")
    assert lock[1] == (5, workspace)
    assert flip[0].endswith("WHERE id = %s AND workspace_id = %s RETURNING version, approved_at")
    assert flip[1][1:] == (5, workspace)
    both = "WHERE id IN (%s, %s) AND workspace_id = %s ORDER BY id FOR UPDATE"
    assert supersede_lock[0].endswith(both)
    assert supersede_lock[1] == (7, 6, workspace)
    assert "WHERE id = %s AND workspace_id = %s RETURNING version, review_status" in link[0]
    assert link[1][2:] == (7, workspace)
    sources = [e for e in pool.statements if "public.source_records" in e[0]]
    assert len(sources) == 3  # confirm insert, replacement lookup, source update
    insert, lookup, update = sources
    assert insert[1][1] == workspace
    assert "WHERE workspace_id = %s AND source_id = %s" in lookup[0]
    assert lookup[1] == (workspace, "note-6")
    assert "WHERE workspace_id = %s AND source_id = %s" in update[0]
    assert update[1][1:] == (workspace, "note-7")
    [(_, chunk_params, _)] = pool.matching("INSERT INTO public.knowledge_chunks")
    assert chunk_params[1] == workspace


@pytest.mark.parametrize(
    "extra",
    [
        {"author_member_id": "m-maya"},
        {"author": "maya"},
        {"requester_member_id": "m-maya"},
        {"workspace_id": "workspace-other"},
        {"review_status": "authoritative"},
        {"tier": "confidential"},
        {"version": 2},
        {"approved_by_member_id": "m-maya"},
    ],
)
def test_save_rejects_author_and_authority_arguments(extra: dict) -> None:
    """Review I5 / R-57: the author (and workspace, review status, tier,
    version, approver) can never come from arguments — any such key is a
    recorded `invalid_arguments`, before the handler runs."""
    with dev_client() as client:
        message = call(
            client,
            {
                "content": "attempted attribution",
                "content_kind": "note",
                **extra,
                "_ctx": make_ctx(tool=SAVE.value, **PRIYA),
            },
            tool=SAVE.value,
        )
    assert result_text(message)["code"] == "invalid_arguments", extra


@pytest.mark.parametrize("extra", [{"approved": True}, {"approved_by_member_id": "m-maya"}])
def test_confirm_accepts_no_approval_text(extra: dict) -> None:
    """PRD §8.4: an "approved" value is never input; only record_id + version."""
    with dev_client() as client:
        ctx = make_ctx(tool=CONFIRM.value, **MAYA)
        message = call(
            client, {"record_id": "5", "version": 1, **extra, "_ctx": ctx}, tool=CONFIRM.value
        )
    assert result_text(message)["code"] == "invalid_arguments"
