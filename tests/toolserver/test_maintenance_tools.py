"""Knowledge maintenance tool tests (P2-09).

Denial/argument tests run without a database (dev path). Behavior tests
write permanent rows (team_notes + ledger) and need a LOCAL
DATABASE_URL_TOOLSERVER with PROXYLEAD_DB_WRITE_TESTS=1, and skip
otherwise; the DSN is captured at collection time and never logged.

D-10 (resolved 16 Oct 2026): a saved note is always team-tier, always a
draft, never chunked or searchable, and only Maya (manager role in the
verified `_ctx`) can confirm or supersede, bound to the exact record+version.
"""

import inspect
import json
import uuid

import psycopg
import pytest

from services.contracts import TOOL_CATALOG, ToolKind, ToolName
from services.toolserver import tools_memory

from .test_tool_dispatch import (
    LIVE_DSN,
    NEEDS_DB,
    FakeLedger,
    call,
    dev_client,
    fake_ledger_client,
    live_client,
    make_ctx,
    result_text,
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


def chunk_count() -> int:
    """Chunks created by these tests: drafts must never be chunked, so the
    seeded corpus is excluded by a title pattern no seed row uses."""
    with psycopg.connect(LIVE_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.knowledge_chunks AS c"
            " JOIN public.source_records AS s ON c.source_id = s.id"
            " WHERE s.title LIKE 'p209-draft-%'"
        )
        return int(cur.fetchone()[0])


# --- behavior (live, local database only) -----------------------------------


@NEEDS_DB
def test_note_saved_as_draft_with_ctx_author() -> None:
    """D-10: author/workspace from the verified `_ctx`; always draft, team
    tier, version 1; provenance carries the request; never chunked."""
    content = f"p209-draft deploy checklist {uuid.uuid4().hex}"
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
    assert chunk_count() == 0


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
def test_confirm_binds_exact_version() -> None:
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
def test_supersede_preserves_history() -> None:
    """Both records remain; the old one gains superseded_by plus reason and
    request provenance; the replacement is untouched."""
    maya_super = make_ctx(tool=SUPERSEDE.value, **MAYA)
    with live_client() as client:
        old = ok_json(save_note(client, f"p209-draft old {uuid.uuid4().hex}", **MAYA))
        new = ok_json(save_note(client, f"p209-draft new {uuid.uuid4().hex}", **MAYA))
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


@pytest.mark.parametrize("tool", [SAVE, CONFIRM, SUPERSEDE])
def test_maintenance_tools_have_no_policy_or_identity_side_effects(tool: ToolName) -> None:
    """R-57: nothing in a maintenance handler reads or writes policy,
    permission, member, or plan tables — role and identity come only from
    the verified `_ctx`."""
    assert TOOL_CATALOG[tool].kind is ToolKind.MAINTENANCE
    handler = tools_memory.PRODUCT_TOOLS[tool].handler
    source = inspect.getsource(handler)
    for forbidden in (
        "FROM public.members",
        "FROM public.plans",
        "FROM public.approval_requests",
        "GRANT",
        "REVOKE",
    ):
        assert forbidden not in source, (tool.value, forbidden)
