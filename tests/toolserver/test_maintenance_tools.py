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
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.contracts import TOOL_CATALOG, ToolKind, ToolName
from services.toolserver import main, tools_memory
from services.toolserver.main import create_app

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
    ) -> None:
        self.notes = {row["id"]: row for row in notes or []}
        self.sources = {row["source_id"]: row for row in sources or []}
        self.source_conflict = source_conflict
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
            return [
                tuple(row[name] for name in columns)
                for pk, row in sorted(self.notes.items())
                if pk in ids
            ]
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
