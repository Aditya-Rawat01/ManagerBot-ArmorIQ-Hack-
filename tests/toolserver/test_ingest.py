"""Ingestion tests (P2-03). DB-touching tests use an in-memory fake of the
knowledge tables (offline, no database); the embedding test needs the cached
FastEmbed model and skips otherwise (no download in CI). The live upgrade path
(origin/main rows -> keyed rows) is proven against a local PostgreSQL in the
PR, not here.
"""

import ast
import copy
import importlib
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest

import services.knowledge as knowledge_pkg
from services.contracts import DEFAULT_WORKSPACE_ID
from services.knowledge import ingest
from services.knowledge.embeddings import EMBED_DIMS, MODEL_ID

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = REPO_ROOT / "seed"
WS = DEFAULT_WORKSPACE_ID
FAKE_MODEL_VERSION = f"{MODEL_ID}+fastembed-test"
EMAILS = {
    "PERSONA_MAYA_EMAIL": "maya@example.test",
    "PERSONA_SAM_EMAIL": "sam@example.test",
    "PERSONA_PRIYA_EMAIL": "priya@example.test",
}


def _corpus_files() -> list[Path]:
    """Files the ingest reads (seed/eval/, owned by P3-08, is never ingested)."""
    names = ("decisions.json", "guidance.json", "notes.json", "company_projects.json")
    return sorted((SEED_DIR / "sources").glob("*.json")) + [SEED_DIR / name for name in names]


_SOURCE_COLS = (
    "source_type",
    "title",
    "author",
    "reference",
    "tier",
    "status",
    "content_hash",
    "provenance",
    "created_at",
    "updated_at",
)
_DECISION_COLS = (
    "workspace_id",
    "seed_key",
    "statement",
    "rationale",
    "project",
    "owner_member_id",
    "authoritative",
    "source_id",
    "version",
)
_GUIDANCE_COLS = (
    "workspace_id",
    "seed_key",
    "guidance_text",
    "scope",
    "authoritative",
    "approved_by_member_id",
    "provenance",
)
_NOTE_COLS = (
    "workspace_id",
    "seed_key",
    "author_member_id",
    "title",
    "content",
    "content_kind",
    "review_status",
    "version",
    "content_hash",
)
_PROJECT_COLS = (
    "workspace_id",
    "project_key",
    "project_name",
    "status",
    "tier",
    "owner",
    "summary",
    "details",
    "budget",
    "internal_notes",
)
_MEMBER_COLS = (
    "id",
    "workspace_id",
    "persona",
    "display_name",
    "app_role",
    "masked_email",
    "slack_user_id",
    "armoriq_member_email_ref",
)


class FakeDB:
    """In-memory stand-in for the tables the ingest writes. It models the
    semantics the SQL relies on (keyed upserts with IS DISTINCT FROM, COALESCE
    on member FKs, one-time legacy claims, unique chunk hashes) and rejects
    any statement it does not know, so new SQL must be modelled here."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.workspaces: set[str] = set()
        self.members: dict[str, dict[str, Any]] = {}
        self.sources: dict[int, dict[str, Any]] = {}
        self.chunks: dict[int, dict[str, Any]] = {}
        self.decisions: dict[int, dict[str, Any]] = {}
        self.guidance: dict[int, dict[str, Any]] = {}
        self.notes: dict[int, dict[str, Any]] = {}
        # company_projects keyed like the SQL: (workspace_id, project_key).
        self.projects: dict[tuple[str, str], dict[str, Any]] = {}
        self.statements: list[tuple[str, tuple]] = []
        self.events: list[str] = []
        self.embedded: list[str] = []
        self.commits = 0
        self.model_version = FAKE_MODEL_VERSION

    def tables(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "workspaces": self.workspaces,
                "members": self.members,
                "sources": self.sources,
                "chunks": self.chunks,
                "decisions": self.decisions,
                "guidance": self.guidance,
                "notes": self.notes,
                "projects": self.projects,
            }
        )

    def source(self, source_id: str) -> tuple[int, dict[str, Any]]:
        matches = [
            (pk, row)
            for pk, row in self.sources.items()
            if row["workspace_id"] == WS and row["source_id"] == source_id
        ]
        assert len(matches) == 1, f"{len(matches)} rows for {source_id}"
        return matches[0]

    def chunks_of(self, source_pk: int) -> list[dict[str, Any]]:
        return [row for row in self.chunks.values() if row["source_id"] == source_pk]

    def keyed(self, table: dict[int, dict[str, Any]], key: str) -> dict[str, Any]:
        matches = [
            row for row in table.values() if row["workspace_id"] == WS and row["seed_key"] == key
        ]
        assert len(matches) == 1, f"{len(matches)} rows for {key}"
        return matches[0]


class FakeCursor:
    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self._one: tuple | None = None
        self._all: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def fetchone(self) -> tuple | None:
        return self._one

    def fetchall(self) -> list[tuple]:
        return self._all

    def _claim(self, table: dict[int, dict[str, Any]], column: str, params: tuple) -> None:
        key, ws, identity, ws_again, key_again = params
        if any(
            r["workspace_id"] == ws_again and r["seed_key"] == key_again for r in table.values()
        ):
            return
        candidates = sorted(
            pk
            for pk, r in table.items()
            if r["workspace_id"] == ws and r["seed_key"] is None and r[column] == identity
        )
        if candidates:
            table[candidates[0]]["seed_key"] = key
            self.rowcount = 1

    def _upsert(
        self,
        table: dict[int, dict[str, Any]],
        new: dict[str, Any],
        coalesce: tuple[str, ...],
        insert_only: tuple[str, ...] = (),
    ) -> None:
        current = [
            row
            for row in table.values()
            if row["workspace_id"] == new["workspace_id"] and row["seed_key"] == new["seed_key"]
        ]
        if not current:
            table[next(self.db.ids)] = {**new, "superseded_by_id": None}
            self.rowcount = 1
            return
        row = current[0]
        for column in coalesce:
            if new[column] is None:
                new[column] = row[column]
        changes = {k: v for k, v in new.items() if k not in insert_only and row.get(k) != v}
        if changes:
            row.update(changes)
            self.rowcount = 1

    def execute(self, query: str, params: tuple = ()) -> None:
        db = self.db
        q = " ".join(query.split())
        p = tuple(params)
        db.statements.append((q, p))
        self._one, self._all, self.rowcount = None, [], 0
        if q.startswith("SELECT pg_advisory_xact_lock("):
            db.events.append(f"lock:{p[0]}")
        elif q.startswith("INSERT INTO public.workspaces"):
            db.workspaces.add(p[0])
        elif q.startswith("INSERT INTO public.members"):
            new = dict(zip(_MEMBER_COLS, p, strict=True))
            old = db.members.get(new["id"])
            if old is None:
                db.members[new["id"]] = {**new, "active": True}
                self.rowcount = 1
            else:
                new["slack_user_id"] = new["slack_user_id"] or old["slack_user_id"]
                if any(old[k] != v for k, v in new.items()):
                    old.update(new)
                    self.rowcount = 1
        elif q.startswith("SELECT id FROM public.members"):
            row = db.members.get(p[0])
            self._one = (row["id"],) if row and row["workspace_id"] == p[1] else None
        elif q.startswith("SELECT id, provenance->>'ingest_digest'"):
            for pk, row in db.sources.items():
                if (row["workspace_id"], row["source_id"]) == p:
                    prov = row["provenance"]
                    self._one = (pk, prov.get("ingest_digest"), prov.get("embed_tool"))
        elif q.startswith("UPDATE public.source_records"):
            row = dict(zip(_SOURCE_COLS, p[:10], strict=True))
            row["provenance"] = json.loads(row["provenance"])
            db.sources[p[10]].update(row)
            self.rowcount = 1
        elif q.startswith("INSERT INTO public.source_records"):
            row = dict(zip((*_SOURCE_COLS, "source_id", "workspace_id"), p, strict=True))
            row["provenance"] = json.loads(row["provenance"])
            assert not any(
                (r["workspace_id"], r["source_id"]) == (row["workspace_id"], row["source_id"])
                for r in db.sources.values()
            ), "unique violation: source_records (workspace_id, source_id)"
            pk = next(db.ids)
            db.sources[pk] = row
            self._one = (pk,)
        elif q.startswith("SELECT content_hash FROM public.knowledge_chunks"):
            self._all = [(r["content_hash"],) for r in db.chunks_of(p[0])]
        elif q.startswith("UPDATE public.knowledge_chunks"):
            for row in db.chunks_of(p[2]):
                if (row["tier"], row["workspace_id"]) != (p[0], p[1]):
                    row["tier"], row["workspace_id"] = p[0], p[1]
                    self.rowcount += 1
        elif q.startswith("DELETE FROM public.knowledge_chunks"):
            for pk in [pk for pk, r in db.chunks.items() if r["source_id"] == p[0]]:
                del db.chunks[pk]
                self.rowcount += 1
        elif q.startswith("INSERT INTO public.knowledge_chunks"):
            assert not any(
                (r["workspace_id"], r["content_hash"]) == (p[1], p[5]) for r in db.chunks.values()
            ), "unique violation: knowledge_chunks (workspace_id, content_hash)"
            db.chunks[next(db.ids)] = {
                "source_id": p[0],
                "workspace_id": p[1],
                "tier": p[2],
                "chunk_text": p[3],
                "content_hash": p[5],
            }
            self.rowcount = 1
        elif q.startswith("UPDATE public.decisions SET seed_key"):
            self._claim(db.decisions, "statement", p)
        elif q.startswith("UPDATE public.manager_guidance SET seed_key"):
            self._claim(db.guidance, "guidance_text", p)
        elif q.startswith("UPDATE public.team_notes SET seed_key"):
            self._claim(db.notes, "content_hash", p)
        elif q.startswith("INSERT INTO public.decisions"):
            new = dict(zip(_DECISION_COLS, p, strict=True))
            self._upsert(db.decisions, new, coalesce=("owner_member_id",))
        elif q.startswith("UPDATE public.decisions AS old SET superseded_by_id"):
            old = [r for r in db.decisions.values() if (r["workspace_id"], r["seed_key"]) == p[:2]]
            new = [
                pk for pk, r in db.decisions.items() if (r["workspace_id"], r["seed_key"]) == p[2:]
            ]
            if old and new and old[0]["superseded_by_id"] != new[0]:
                old[0]["superseded_by_id"] = new[0]
                self.rowcount = 1
        elif q.startswith("INSERT INTO public.manager_guidance"):
            new = dict(zip(_GUIDANCE_COLS, p, strict=True))
            new["provenance"] = json.loads(new["provenance"])
            self._upsert(db.guidance, new, coalesce=("approved_by_member_id",))
        elif q.startswith("INSERT INTO public.team_notes"):
            new = dict(zip(_NOTE_COLS, p, strict=True))
            self._upsert(
                db.notes,
                new,
                coalesce=("author_member_id",),
                insert_only=("review_status", "version"),
            )
        elif q.startswith("INSERT INTO public.company_projects"):
            assert "ON CONFLICT (workspace_id, project_key) DO UPDATE" in q
            assert "IS DISTINCT FROM" in q
            assert WS in db.workspaces, "FK violation: company_projects.workspace_id"
            new = dict(zip(_PROJECT_COLS, p, strict=True))
            key = (new["workspace_id"], new["project_key"])
            if db.projects.get(key) != new:
                db.projects[key] = new
                self.rowcount = 1
        else:
            raise AssertionError(f"SQL not modelled by the fake: {q[:100]}")


class FakeConn:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        self.db.commits += 1


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    db = FakeDB()

    def connect(dsn: str) -> FakeConn:
        db.events.append("connect")
        return FakeConn(db)

    def embed(texts: list[str]) -> list[list[float]]:
        db.embedded.extend(texts)
        return [[0.0] * EMBED_DIMS for _ in texts]

    monkeypatch.setattr(ingest, "_connect", connect)
    monkeypatch.setattr(ingest, "embed", embed)
    monkeypatch.setattr(ingest, "model_version", lambda: db.model_version)
    monkeypatch.setattr(ingest, "warm", lambda: db.events.append("warm"))
    return db


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """A private copy of seed/ that a test may edit."""
    target = tmp_path / "seed"
    shutil.copytree(SEED_DIR, target)
    return target


def _edit_json(path: Path, change: Callable[[Any], None]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _recorded(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), time(0), tzinfo=UTC)


def _all_zero(stats: dict[str, int]) -> bool:
    return bool(stats) and all(value == 0 for value in stats.values())


# --- pure helpers -------------------------------------------------------------


def test_chunking_deterministic() -> None:
    text = " ".join(f"word{i}" for i in range(300))
    first, second = ingest.chunk_text(text), ingest.chunk_text(text)
    assert first == second and len(first) == 3
    assert len(first[0].split()) == 120
    assert first[0].split()[-20:] == first[1].split()[:20]
    assert ingest.chunk_text("") == []


def test_chunking_rejects_overlap_not_smaller_than_window() -> None:
    # overlap >= words would never advance (infinite loop on long text).
    for words, overlap in ((5, 5), (5, 6), (0, 0), (5, -1)):
        with pytest.raises(ValueError):
            ingest.chunk_text("a b c", words=words, overlap=overlap)


def test_content_hash_stable() -> None:
    assert ingest.content_hash("abc") == ingest.content_hash("abc")
    assert ingest.content_hash("abc") != ingest.content_hash("abd")
    assert len(ingest.content_hash("abc")) == 64


def test_mask_email_matches_backend_rule() -> None:
    # Same vectors as tests/api/test_identity.py (apps.api.identity.mask_email).
    assert ingest._mask_email("sam@example.com") == "s***@e***.com"
    assert ingest._mask_email("maya@example.com") == "m***@e***.com"
    assert ingest._mask_email("not-an-email") == "***"
    assert ingest._mask_email("") == "***"


GOOD_SOURCE = {
    "source_id": "s",
    "title": "t",
    "tier": "team",
    "status": "active",
    "review_status": "authoritative",
    "version": 1,
    "superseded_by": None,
    "author": "sam",
    "recorded_at": "2026-09-05",
    "content": "c",
}


def _without(name: str) -> dict[str, Any]:
    return {k: v for k, v in GOOD_SOURCE.items() if k != name}


def test_metadata_complete() -> None:
    validate = ingest.validate_source_record
    assert validate({"source_id": "x"}) != []
    assert validate(GOOD_SOURCE) == []
    assert validate({**GOOD_SOURCE, "status": "superseded", "superseded_by": "s2"}) == []
    assert validate({**GOOD_SOURCE, "provenance": {"escalation_topic": "api"}}) == []
    broken_records = (
        {**GOOD_SOURCE, "tier": "gold"},
        {**GOOD_SOURCE, "status": "archived"},
        _without("version"),
        _without("review_status"),
        _without("recorded_at"),
        _without("superseded_by"),
        {**GOOD_SOURCE, "review_status": "approved"},
        {**GOOD_SOURCE, "version": 0},
        {**GOOD_SOURCE, "version": True},
        {**GOOD_SOURCE, "version": "1"},
        {**GOOD_SOURCE, "recorded_at": "last week"},
        {**GOOD_SOURCE, "author": "Jane Doe"},
        {**GOOD_SOURCE, "status": "superseded"},
        {**GOOD_SOURCE, "superseded_by": "s2"},
        {**GOOD_SOURCE, "status": "superseded", "superseded_by": "s"},
        {**GOOD_SOURCE, "case": "answerable-team"},
        {**GOOD_SOURCE, "provenance": {"case": "x"}},
        {**GOOD_SOURCE, "provenance": {"superseded_by": "s2"}},
        {**GOOD_SOURCE, "provenance": {"ingest_digest": "0" * 64}},
    )
    for broken in broken_records:
        assert validate(broken) != [], broken


def test_seed_files_validate_with_full_metadata() -> None:
    corpus = ingest.load_corpus(SEED_DIR)
    assert len(corpus.sources) == len(list((SEED_DIR / "sources").glob("*.json")))
    for path in sorted((SEED_DIR / "sources").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert ingest.validate_source_record(data) == [], path.name
        for name in ("version", "review_status", "superseded_by", "recorded_at"):
            assert name in data, f"{path.name}: {name} must be explicit"
    by_id = {data["source_id"]: data for data in corpus.sources}
    assert by_id["team-release-checklist-v1"]["superseded_by"] == "team-release-checklist-v2"
    assert by_id["team-release-checklist-v1"]["status"] == "superseded"
    assert all(item.get("key") for item in corpus.decisions + corpus.guidance + corpus.notes)


def test_seed_carries_no_eval_labels() -> None:
    # Case labels live in seed/README.md (case map), never in ingested records.
    for path in _corpus_files():
        text = path.read_text(encoding="utf-8")
        assert '"case"' not in text, f"{path.relative_to(REPO_ROOT)} carries an eval label"


def test_corpus_rejects_unknown_references(corpus_dir: Path) -> None:
    def typo_source(items: list[dict[str, Any]]) -> None:
        items[0]["source"] = "team-friday-deploy-freze"

    _edit_json(corpus_dir / "decisions.json", typo_source)
    with pytest.raises(ValueError, match="unknown source"):
        ingest.load_corpus(corpus_dir)

    shutil.copy(SEED_DIR / "decisions.json", corpus_dir / "decisions.json")

    def typo_supersedes(items: list[dict[str, Any]]) -> None:
        items[-1]["supersedes"] = "decision-release-checklist-v0"

    _edit_json(corpus_dir / "decisions.json", typo_supersedes)
    with pytest.raises(ValueError, match="supersedes"):
        ingest.load_corpus(corpus_dir)


def test_decision_source_typo_raises_before_any_write(fake: FakeDB, corpus_dir: Path) -> None:
    def typo(items: list[dict[str, Any]]) -> None:
        items[0]["source"] = "team-friday-deploy-freze"

    _edit_json(corpus_dir / "decisions.json", typo)
    with pytest.raises(ValueError, match="unknown source"):
        ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert "connect" not in fake.events
    assert fake.decisions == {}


# --- run(): transaction, lock, idempotency -------------------------------------


def test_one_locked_transaction_after_model_warmup(fake: FakeDB) -> None:
    assert int.from_bytes(b"PLSEED01", "big") == ingest.INGEST_LOCK_KEY
    assert 0 < ingest.INGEST_LOCK_KEY < 2**63
    ingest.run(SEED_DIR, "fake-dsn", EMAILS)
    assert fake.events[:3] == ["warm", "connect", f"lock:{ingest.INGEST_LOCK_KEY}"]
    assert fake.statements[0][0].startswith("SELECT pg_advisory_xact_lock(")
    assert fake.commits == 1


def test_fresh_ingest_then_rerun_changes_nothing(fake: FakeDB) -> None:
    first = ingest.run(SEED_DIR, "fake-dsn", EMAILS)
    corpus = ingest.load_corpus(SEED_DIR)
    assert first["sources"] == len(corpus.sources)
    assert first["decisions"] == len(corpus.decisions) == len(fake.decisions)
    assert first["guidance"] == len(corpus.guidance) == len(fake.guidance)
    assert first["notes"] == len(corpus.notes) == len(fake.notes)
    assert first["members"] == 3 and first["links"] == 1 and first["claimed"] == 0
    assert first["chunks"] == len(fake.chunks) == len(fake.embedded)
    assert first["projects"] == len(corpus.projects) == len(fake.projects) == 7
    assert {row["tier"] for row in fake.projects.values()} == {"team", "confidential", "restricted"}
    for data in corpus.sources:
        pk, row = fake.source(data["source_id"])
        assert row["created_at"] == row["updated_at"] == _recorded(data["recorded_at"])
        for name in ("version", "review_status", "superseded_by", "recorded_at"):
            assert row["provenance"][name] == data[name]
        assert "case" not in row["provenance"]
        assert row["provenance"]["embed_tool"] == FAKE_MODEL_VERSION
        assert {chunk["tier"] for chunk in fake.chunks_of(pk)} == {data["tier"]}
    v1 = fake.keyed(fake.decisions, "decision-release-checklist-v1")
    v2_pk = next(
        pk for pk, r in fake.decisions.items() if r["seed_key"] == "decision-release-checklist-v2"
    )
    assert v1["superseded_by_id"] == v2_pk

    before = fake.tables()
    embedded = len(fake.embedded)
    second = ingest.run(SEED_DIR, "fake-dsn", EMAILS)
    assert _all_zero(second), second
    assert fake.tables() == before
    assert len(fake.embedded) == embedded


def test_metadata_only_change_updates_rows_without_reembedding(
    fake: FakeDB, corpus_dir: Path
) -> None:
    ingest.run(corpus_dir, "fake-dsn", EMAILS)
    pk, _ = fake.source("team-office-snacks")
    chunk_hashes = {chunk["content_hash"] for chunk in fake.chunks_of(pk)}
    embedded = len(fake.embedded)

    def reclassify(data: dict[str, Any]) -> None:
        data["tier"] = "confidential"
        data["reference"] = "team-misc-2026-10"
        data["recorded_at"] = "2026-09-26"

    _edit_json(corpus_dir / "sources" / "team-office-snacks.json", reclassify)
    stats = ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert stats["sources"] == 1 and stats["chunks"] == 0, stats
    assert len(fake.embedded) == embedded, "metadata change must not re-embed"
    _, row = fake.source("team-office-snacks")
    assert row["tier"] == "confidential" and row["reference"] == "team-misc-2026-10"
    assert row["created_at"] == _recorded("2026-09-26")
    chunks = fake.chunks_of(pk)
    assert {chunk["content_hash"] for chunk in chunks} == chunk_hashes
    assert {chunk["tier"] for chunk in chunks} == {"confidential"}
    assert _all_zero(ingest.run(corpus_dir, "fake-dsn", EMAILS))


def test_text_or_model_change_reembeds(fake: FakeDB, corpus_dir: Path) -> None:
    ingest.run(corpus_dir, "fake-dsn", EMAILS)
    pk, _ = fake.source("team-standup-format")
    old_hashes = {chunk["content_hash"] for chunk in fake.chunks_of(pk)}

    def rewrite(data: dict[str, Any]) -> None:
        data["content"] += " Fridays have no standup at all."

    _edit_json(corpus_dir / "sources" / "team-standup-format.json", rewrite)
    stats = ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert stats["sources"] == 1 and stats["chunks"] == len(fake.chunks_of(pk)) >= 1
    assert not old_hashes & {chunk["content_hash"] for chunk in fake.chunks_of(pk)}
    assert "Fridays have no standup at all." in fake.embedded[-1]

    total = len(fake.chunks)
    fake.model_version = f"{MODEL_ID}+fastembed-next"
    stats = ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert stats["sources"] == len(fake.sources) and stats["chunks"] == total
    assert all(r["provenance"]["embed_tool"] == fake.model_version for r in fake.sources.values())
    assert _all_zero(ingest.run(corpus_dir, "fake-dsn", EMAILS))


def test_items_upsert_in_place_by_key(fake: FakeDB, corpus_dir: Path) -> None:
    ingest.run(corpus_dir, "fake-dsn", EMAILS)

    def edit_decision(items: list[dict[str, Any]]) -> None:
        items[1]["statement"] = "Stay on hosted CI runners until the November review."
        items[1]["project"] = "platform"

    def edit_guidance(items: list[dict[str, Any]]) -> None:
        items[0]["guidance_text"] = "Prefer boring technology for CI tooling."

    def edit_note(items: list[dict[str, Any]]) -> None:
        items[1]["content"] = "Idea: a two-page postmortem template. Draft only."

    _edit_json(corpus_dir / "decisions.json", edit_decision)
    _edit_json(corpus_dir / "guidance.json", edit_guidance)
    _edit_json(corpus_dir / "notes.json", edit_note)
    stats = ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert (stats["decisions"], stats["guidance"], stats["notes"]) == (1, 1, 1), stats
    assert (len(fake.decisions), len(fake.guidance), len(fake.notes)) == (5, 2, 2)
    decision = fake.keyed(fake.decisions, "decision-ci-hosted-runners")
    assert decision["statement"].endswith("November review.")
    assert decision["project"] == "platform"
    assert fake.keyed(fake.guidance, "guidance-boring-ci-release-tooling")[
        "guidance_text"
    ].endswith("CI tooling.")
    note = fake.keyed(fake.notes, "note-postmortem-template-idea")
    assert note["content_hash"] == ingest.content_hash(note["content"])

    # The supersession link is re-applied every run.
    fake.keyed(fake.decisions, "decision-release-checklist-v1")["superseded_by_id"] = None
    assert ingest.run(corpus_dir, "fake-dsn", EMAILS)["links"] == 1
    assert _all_zero(ingest.run(corpus_dir, "fake-dsn", EMAILS))


def test_projects_upsert_in_place_by_key(fake: FakeDB, corpus_dir: Path) -> None:
    """0011 rows come from seed/company_projects.json (never the migration):
    keyed by (workspace_id, project_key), rewritten only when a column differs."""
    ingest.run(corpus_dir, "fake-dsn", EMAILS)
    statements = [q for q, _ in fake.statements]
    project_inserts = [
        i for i, q in enumerate(statements) if q.startswith("INSERT INTO public.company_projects")
    ]
    workspace_insert = statements.index(
        next(q for q in statements if q.startswith("INSERT INTO public.workspaces"))
    )
    assert len(project_inserts) == 7
    assert workspace_insert < project_inserts[0], "the workspace FK target must exist first"

    def edit(items: list[dict[str, Any]]) -> None:
        items[0]["summary"] = "Atlas rolls out in four stages."

    _edit_json(corpus_dir / "company_projects.json", edit)
    stats = ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert stats["projects"] == 1 and len(fake.projects) == 7, stats
    assert fake.projects[(WS, "proj-atlas")]["summary"] == "Atlas rolls out in four stages."
    assert _all_zero(ingest.run(corpus_dir, "fake-dsn", EMAILS))


def test_project_rows_are_validated_before_any_write(fake: FakeDB, corpus_dir: Path) -> None:
    corpus = ingest.load_corpus(SEED_DIR)
    assert all(ingest.validate_project(item) == [] for item in corpus.projects)
    good = dict(corpus.projects[0])
    broken = [
        {k: v for k, v in good.items() if k != "summary"},
        {**good, "tier": "secret"},
        {**good, "status": "draft"},
        {**good, "owner": "someone-else"},
        {**good, "project_key": "k" * 129},
        {**good, "project_name": "n" * 201},
        {**good, "budget": 5},
        {**good, "case": "label"},
        "not an object",
    ]
    for item in broken:
        assert ingest.validate_project(item) != [], item

    def duplicate(items: list[dict[str, Any]]) -> None:
        items.append(dict(items[0]))

    _edit_json(corpus_dir / "company_projects.json", duplicate)
    with pytest.raises(ValueError, match="duplicate project_key"):
        ingest.run(corpus_dir, "fake-dsn", EMAILS)
    assert "connect" not in fake.events and fake.projects == {}


def test_reseed_never_resets_a_confirmed_note(fake: FakeDB) -> None:
    # review_status/version are insert-only: after insert the draft lifecycle
    # belongs to knowledge_confirm_maya_decision (P2-09, R-56).
    ingest.run(SEED_DIR, "fake-dsn", EMAILS)
    upsert = next(q for q, _ in fake.statements if q.startswith("INSERT INTO public.team_notes"))
    update_clause = upsert.split("DO UPDATE SET", 1)[1]
    assert "review_status" not in update_clause and "version" not in update_clause
    note = fake.keyed(fake.notes, "note-postmortem-template-idea")
    note["review_status"], note["version"] = "authoritative", 2  # P2-09 confirmation
    assert _all_zero(ingest.run(SEED_DIR, "fake-dsn", EMAILS))
    assert (note["review_status"], note["version"]) == ("authoritative", 2)


def _populate_like_main(db: FakeDB) -> None:
    """Rows as origin/main's ingest (PR #59) left them: no seed keys, eval
    `case` labels and `superseded_by`/`supersedes` in source provenance,
    created_at = ingest time, members masked from the same env."""
    ingest_time = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    corpus = ingest.load_corpus(SEED_DIR)
    db.workspaces.add(WS)
    for persona, ref in (("maya", "MAYA"), ("sam", "SAM"), ("priya", "PRIYA")):
        db.members[f"m-{persona}"] = {
            "id": f"m-{persona}",
            "workspace_id": WS,
            "persona": persona,
            "display_name": persona.capitalize(),
            "app_role": {"maya": "manager", "sam": "senior", "priya": "junior"}[persona],
            "masked_email": f"{persona[0]}***@e***.test",
            "slack_user_id": None,
            "armoriq_member_email_ref": f"PERSONA_{ref}_EMAIL",
            "active": True,
        }
    source_pks = {}
    for data in corpus.sources:
        provenance = {
            **data.get("provenance", {}),
            "embed_model": MODEL_ID,
            "embed_tool": FAKE_MODEL_VERSION,
            "embed_dim": EMBED_DIMS,
            "case": "legacy-eval-label",
            "recorded_at": data["recorded_at"],
        }
        if data["superseded_by"]:
            provenance["superseded_by"] = data["superseded_by"]
        if data["source_id"] == "team-release-checklist-v2":
            provenance["supersedes"] = "team-release-checklist-v1"
        pk = next(db.ids)
        source_pks[data["source_id"]] = pk
        db.sources[pk] = {
            "source_id": data["source_id"],
            "workspace_id": WS,
            "source_type": data["source_type"],
            "title": data["title"],
            "author": data["author"],
            "reference": data["reference"],
            "tier": data["tier"],
            "status": data["status"],
            "content_hash": ingest.content_hash(data["content"]),
            "provenance": provenance,
            "created_at": ingest_time,
            "updated_at": ingest_time,
        }
        for text in ingest.chunk_text(f"{data['title']}\n\n{data['content']}"):
            db.chunks[next(db.ids)] = {
                "source_id": pk,
                "workspace_id": WS,
                "tier": data["tier"],
                "chunk_text": text,
                "content_hash": ingest.content_hash(f"{data['source_id']}:{text}"),
            }
    decision_pks = {}
    for item in corpus.decisions:
        pk = next(db.ids)
        decision_pks[item["key"]] = pk
        db.decisions[pk] = {
            "workspace_id": WS,
            "seed_key": None,
            "statement": item["statement"],
            "rationale": item["rationale"],
            "project": item["project"],
            "owner_member_id": f"m-{item['owner']}",
            "authoritative": item["authoritative"],
            "source_id": source_pks[item["source"]],
            "version": item["version"],
            "superseded_by_id": None,
        }
    db.decisions[decision_pks["decision-release-checklist-v1"]]["superseded_by_id"] = decision_pks[
        "decision-release-checklist-v2"
    ]
    for item in corpus.guidance:
        db.guidance[next(db.ids)] = {
            "workspace_id": WS,
            "seed_key": None,
            "guidance_text": item["guidance_text"],
            "scope": item["scope"],
            "authoritative": item["authoritative"],
            "approved_by_member_id": f"m-{item['approved_by']}",
            "provenance": {"scope": item["scope"]},
            "superseded_by_id": None,
        }
    for item in corpus.notes:
        db.notes[next(db.ids)] = {
            "workspace_id": WS,
            "seed_key": None,
            "author_member_id": f"m-{item['author']}",
            "title": item["title"],
            "content": item["content"],
            "content_kind": item["kind"],
            "review_status": item["review_status"],
            "version": item["version"],
            "content_hash": ingest.content_hash(item["content"]),
            "superseded_by_id": None,
        }


def test_upgrades_rows_written_by_the_previous_ingest(fake: FakeDB) -> None:
    _populate_like_main(fake)
    counts = {name: len(rows) for name, rows in fake.tables().items()}
    stats = ingest.run(SEED_DIR, "fake-dsn", EMAILS)
    assert stats["sources"] == 15 and stats["chunks"] == 0, stats
    assert stats["claimed"] == 9, stats
    assert stats["members"] == stats["decisions"] == stats["guidance"] == stats["notes"] == 0
    # main's ingest wrote no company_projects (0011 is new): all of them load once.
    assert stats["projects"] == len(ingest.load_corpus(SEED_DIR).projects) == 7, stats
    counts["projects"] = stats["projects"]
    assert fake.embedded == [], "unchanged text must keep its vectors"
    assert {name: len(rows) for name, rows in fake.tables().items()} == counts
    corpus = ingest.load_corpus(SEED_DIR)
    for data in corpus.sources:
        pk, row = fake.source(data["source_id"])
        assert "case" not in row["provenance"] and "supersedes" not in row["provenance"]
        assert row["provenance"]["superseded_by"] == data["superseded_by"]
        assert row["provenance"]["review_status"] == data["review_status"]
        assert row["created_at"] == row["updated_at"] == _recorded(data["recorded_at"])
        assert {chunk["tier"] for chunk in fake.chunks_of(pk)} == {row["tier"]}
    for table in (fake.decisions, fake.guidance, fake.notes):
        assert all(row["seed_key"] for row in table.values())
    assert _all_zero(ingest.run(SEED_DIR, "fake-dsn", EMAILS))


# --- members and foreign keys ---------------------------------------------------


def test_members_from_env_store_only_masked_values(
    fake: FakeDB, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {key: value for key, value in EMAILS.items() if key != "PERSONA_PRIYA_EMAIL"}
    env["SLACK_USER_SAM_ID"] = "U0SAMTEST"
    fake.workspaces.add(WS)
    fake.members["m-sam"] = {
        "id": "m-sam",
        "workspace_id": WS,
        "persona": "sam",
        "display_name": "Sam",
        "app_role": "senior",
        "masked_email": "stale",
        "slack_user_id": None,
        "armoriq_member_email_ref": "PERSONA_SAM_EMAIL",
        "active": True,
    }
    stats = ingest.run(SEED_DIR, "fake-dsn", env)
    assert stats["members"] == 2  # maya inserted, sam refreshed in place
    assert fake.members["m-sam"]["masked_email"] == "s***@e***.test"
    assert fake.members["m-sam"]["slack_user_id"] == "U0SAMTEST"
    assert "m-priya" not in fake.members
    assert {m["armoriq_member_email_ref"] for m in fake.members.values()} == {
        "PERSONA_MAYA_EMAIL",
        "PERSONA_SAM_EMAIL",
    }
    err = capsys.readouterr().err
    assert "PERSONA_PRIYA_EMAIL" in err and "@" not in err
    text_params = [
        value for _, params in fake.statements for value in params if isinstance(value, str)
    ]
    for raw in env.values():
        if "@" in raw:
            assert not any(raw in value for value in text_params), "raw email reached SQL"
    owners = {row["seed_key"]: row["owner_member_id"] for row in fake.decisions.values()}
    assert owners["decision-friday-deploy-freeze"] == "m-maya"
    assert owners["decision-ci-hosted-runners"] == "m-sam"
    assert owners["decision-ci-selfhosted-runners"] is None  # priya: honest unknown
    # Env without the Slack ID keeps the stored one; nothing else changes.
    env.pop("SLACK_USER_SAM_ID")
    assert _all_zero(ingest.run(SEED_DIR, "fake-dsn", env))
    assert fake.members["m-sam"]["slack_user_id"] == "U0SAMTEST"


def test_owner_adoption_is_workspace_scoped(fake: FakeDB) -> None:
    corpus = ingest.load_corpus(SEED_DIR)
    statement = corpus.decisions[0]["statement"]
    fake.decisions[999] = {
        "workspace_id": "workspace-other",
        "seed_key": None,
        "statement": statement,
        "rationale": "",
        "project": "",
        "owner_member_id": None,
        "authoritative": False,
        "source_id": None,
        "version": 1,
        "superseded_by_id": None,
    }
    ingest.run(SEED_DIR, "fake-dsn", {})
    seeded = [row for row in fake.decisions.values() if row["workspace_id"] == WS]
    assert len(seeded) == 5 and all(row["owner_member_id"] is None for row in seeded)
    stats = ingest.run(SEED_DIR, "fake-dsn", EMAILS)
    assert stats["members"] == 3 and stats["decisions"] == 5
    assert all(row["owner_member_id"] for row in seeded)
    assert fake.decisions[999]["owner_member_id"] is None
    assert fake.decisions[999]["seed_key"] is None
    # A later run without emails keeps the adopted owners.
    assert _all_zero(ingest.run(SEED_DIR, "fake-dsn", {}))
    assert all(row["owner_member_id"] for row in seeded)


# --- embeddings -------------------------------------------------------------------


def _model_cached() -> bool:
    """True when the FastEmbed model is in FastEmbed's cache directory:
    FASTEMBED_CACHE_PATH, else `<tempdir>/fastembed_cache` (FastEmbed's rule)."""
    base = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
        tempfile.gettempdir(), "fastembed_cache"
    )
    return any(Path(base).glob("models--*bge-small-en-v1.5*"))


def test_model_cache_probe_uses_fastembed_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))
    assert not _model_cached()
    (tmp_path / "models--qdrant--bge-small-en-v1.5-onnx-q").mkdir()
    assert _model_cached()


@pytest.mark.slow
@pytest.mark.skipif(not _model_cached(), reason="FastEmbed model not cached (no download in tests)")
def test_embeddings_return_384_dims(real_model: object) -> None:
    from services.knowledge.embeddings import embed, model_version

    vectors = embed(["quokka Friday deploy freeze", "unrelated snack poll"])
    assert len(vectors) == 2
    assert all(len(vector) == EMBED_DIMS for vector in vectors)
    assert MODEL_ID in model_version()


def test_knowledge_modules_import_without_fastembed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The API CI job (`.[api,dev]`) has no fastembed: importing these modules
    # (constants, pure helpers) must not need it; only loading the model does.
    monkeypatch.setitem(sys.modules, "fastembed", None)
    for name in ("embeddings", "ingest"):
        monkeypatch.delitem(sys.modules, f"services.knowledge.{name}", raising=False)
        monkeypatch.setattr(knowledge_pkg, name, getattr(knowledge_pkg, name), raising=False)
    embeddings = importlib.import_module("services.knowledge.embeddings")
    fresh = importlib.import_module("services.knowledge.ingest")
    assert embeddings.EMBED_DIMS == 384
    assert fresh.chunk_text("a b c") == ["a b c"]
    with pytest.raises(ImportError):
        embeddings.model_version()


# --- security and corpus hygiene ------------------------------------------------


def test_ingest_reads_only_the_migrate_dsn() -> None:
    source = Path(ingest.__file__).read_text(encoding="utf-8")
    # Connects with the migration-role DSN only; runtime DSNs are read solely
    # to refuse a migration DSN equal to one of them.
    assert '_MIGRATE_ENV = "DATABASE_URL_MIGRATE"' in source
    assert 'os.environ.get("DATABASE_URL_BACKEND"' not in source
    assert 'os.environ.get("DATABASE_URL_TOOLSERVER"' not in source
    assert source.count("_connect(") == 2  # definition + the single call in run()
    for path in (
        REPO_ROOT / "apps" / "api" / "core" / "config.py",
        REPO_ROOT / "services" / "toolserver" / "config.py",
    ):
        assert "database_url_migrate" not in path.read_text(encoding="utf-8"), (
            f"MIGRATE DSN in {path.relative_to(REPO_ROOT)}"
        )


def test_cli_refuses_a_runtime_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_password = "not-a-real-password-1111"
    for runtime_key in ("DATABASE_URL_BACKEND", "DATABASE_URL_TOOLSERVER"):
        dsn = f"postgresql://runtime_login:{fake_password}@db.invalid/proxylead"
        monkeypatch.setenv("DATABASE_URL_MIGRATE", dsn)
        monkeypatch.delenv("DATABASE_URL_BACKEND", raising=False)
        monkeypatch.delenv("DATABASE_URL_TOOLSERVER", raising=False)
        monkeypatch.setenv(runtime_key, dsn)
        connected: list[str] = []
        monkeypatch.setattr(ingest, "_connect", connected.append)
        monkeypatch.setattr(ingest, "model_version", lambda: FAKE_MODEL_VERSION)
        monkeypatch.setattr(ingest, "warm", lambda: None, raising=False)
        code = ingest.main(["--source", str(SEED_DIR)])
        captured = capsys.readouterr()
        assert code == 2 and connected == []
        assert fake_password not in captured.out + captured.err
        assert runtime_key not in captured.err  # generic: names neither runtime DSN


def test_cli_never_prints_the_dsn_on_database_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed DSN makes libpq echo it (password included); the CLI must not."""
    fake_password = "not-a-real-password-0000"
    monkeypatch.setenv(
        "DATABASE_URL_MIGRATE", f"'postgresql://owner:{fake_password}@nowhere.invalid/db'"
    )
    monkeypatch.setattr(ingest, "model_version", lambda: FAKE_MODEL_VERSION)
    monkeypatch.setattr(ingest, "warm", lambda: None, raising=False)
    code = ingest.main(["--source", str(SEED_DIR)])
    captured = capsys.readouterr()
    assert code == 1
    assert fake_password not in captured.out + captured.err
    assert "database error" in captured.err


def test_seed_has_no_emails_or_tokens() -> None:
    pattern = re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|ak_(live|test)_[A-Za-z0-9]{6,}"
        r"|xox[bpa]-[0-9]|xapp-[0-9]"
    )
    files = sorted(path for path in SEED_DIR.rglob("*") if path.is_file())
    assert files, "seed/ is empty"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{index}: {line.strip()[:80]}"
        for path in files
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], f"PII/tokens in seed: {offenders}"
    tree = ast.parse(Path(ingest.__file__).read_text(encoding="utf-8"))
    assert "Settings" not in {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}, (
        "ingest must not use runtime Settings (migration role only)"
    )


def test_e1_probe_record_is_intact() -> None:
    corpus = ingest.load_corpus(SEED_DIR)
    record = {data["source_id"]: data for data in corpus.sources}["team-friday-deploy-freeze"]
    assert (record["tier"], record["status"], record["review_status"]) == (
        "team",
        "active",
        "authoritative",
    )
    assert "quokka friday deploy freeze" in record["content"].lower()
    assert WS == "workspace-primary"
    holders = [
        path.relative_to(SEED_DIR).as_posix()
        for path in _corpus_files()
        if "quokka" in path.read_text(encoding="utf-8").lower()
    ]
    assert holders == ["sources/team-friday-deploy-freeze.json"]
