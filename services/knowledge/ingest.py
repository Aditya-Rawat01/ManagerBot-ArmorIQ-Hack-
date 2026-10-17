"""Seed ingestion (P2-03): corpus files in `seed/` into the knowledge tables.

Operator only, repository root:

    DATABASE_URL_MIGRATE='<owner DSN>' python -m services.knowledge.ingest --source seed/

Connects ONLY with `DATABASE_URL_MIGRATE` (the migration/seed role) and refuses
to run when that DSN equals a runtime service DSN. The corpus is validated and
the embedding model loaded BEFORE any connection opens; the whole run is then
one transaction, serialized by `pg_advisory_xact_lock(INGEST_LOCK_KEY)`.

Idempotent upserts on stable keys — re-running changes nothing:

* sources: `(workspace_id, source_id)`. `provenance.ingest_digest` is the
  sha256 of the canonical JSON of every ingested field plus the model version
  and chunking parameters; an equal digest skips the record. A metadata-only
  change updates the source row and the tier/workspace copied onto its chunks;
  chunks are re-embedded only when their text or the embedding model changes.
* decisions / guidance / notes: `(workspace_id, seed_key)` (migration 0010),
  from each item's `key`. Rows written before keys existed are claimed once by
  their old exact-text identity, never duplicated. Supersession links are
  re-applied every run.
* company projects (`seed/company_projects.json`, the rows behind the P2-08
  approved views, migration 0011): `(workspace_id, project_key)`, updated in
  place only when a column differs (counted in the `projects` stat).
* workspace / members: from env (D-20/ADR-021). No raw emails are stored —
  only masked display values plus the env-key reference.
* runtime note sources (`note-<team_notes.id>`, written by the tool server
  when Maya confirms a note, D-09 rule 2): never deleted, rewritten or
  re-chunked. Only the source_ids listed in `seed/sources/` are touched, and
  the `note-` prefix is reserved (a seed source using it is a CorpusError).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import psycopg

from services.contracts import (
    DEFAULT_WORKSPACE_ID,
    AppRole,
    DataTier,
    PersonaKey,
    ReviewStatus,
)
from services.contracts.tool_args import MAX_REF_CHARS, MAX_TITLE_CHARS
from services.knowledge.embeddings import EMBED_DIMS, MODEL_ID, embed, model_version, warm

CHUNK_WORDS = 120
CHUNK_OVERLAP_WORDS = 20

#: Part of every source digest: bump when the stored layout changes so the next
#: run rewrites every source once (2 = P2-03 follow-up layout).
INGEST_LAYOUT_VERSION = 2

#: `pg_advisory_xact_lock` key that serializes concurrent ingests: ASCII
#: "PLSEED01" read as a big-endian signed 64-bit integer (0x504C534545443031).
#: Fixed forever — anything else that wants to exclude the seed job uses it.
INGEST_LOCK_KEY = 5786091178244190257

_MIGRATE_ENV = "DATABASE_URL_MIGRATE"
# Read ONLY to refuse a migration DSN that equals a runtime DSN; never used to connect.
_RUNTIME_DSNS = ("DATABASE_URL_BACKEND", "DATABASE_URL_TOOLSERVER")

_MEMBER_ENV: tuple[tuple[PersonaKey, str, AppRole, str, str], ...] = (
    (PersonaKey.MAYA, "Maya", AppRole.MANAGER, "PERSONA_MAYA_EMAIL", "SLACK_USER_MAYA_ID"),
    (PersonaKey.SAM, "Sam", AppRole.SENIOR, "PERSONA_SAM_EMAIL", "SLACK_USER_SAM_ID"),
    (PersonaKey.PRIYA, "Priya", AppRole.JUNIOR, "PERSONA_PRIYA_EMAIL", "SLACK_USER_PRIYA_ID"),
)

_TIERS = frozenset(tier.value for tier in DataTier)
_REVIEW_STATUSES = frozenset(status.value for status in ReviewStatus)
_PERSONAS = frozenset(persona.value for persona in PersonaKey)
# Literals of source_records.status (0003 CHECK) and team_notes.content_kind
# (0003 CHECK, `KnowledgeSaveTeamNoteArgs.content_kind`); no shared enum exists.
_SOURCE_STATUSES = frozenset({"active", "superseded"})
_NOTE_KINDS = frozenset({"note", "summary"})

_SOURCE_REQUIRED = (
    "source_id",
    "title",
    "tier",
    "status",
    "review_status",
    "version",
    "author",
    "recorded_at",
    "content",
)
_SOURCE_FIELDS = frozenset(
    {*_SOURCE_REQUIRED, "superseded_by", "source_type", "reference", "provenance"}
)
# Written by the ingest itself (or stale keys from earlier layouts): a seed
# file's `provenance` extras may not set them. `case` is an evaluation label
# (seed/README.md case map) and is never stored.
_RESERVED_PROVENANCE = frozenset(
    {
        "recorded_at",
        "version",
        "review_status",
        "superseded_by",
        "supersedes",
        "embed_model",
        "embed_tool",
        "embed_dim",
        "chunk_words",
        "chunk_overlap",
        "ingest_digest",
        "case",
    }
)

_DECISION_FIELDS = frozenset(
    {
        "key",
        "statement",
        "rationale",
        "project",
        "owner",
        "authoritative",
        "source",
        "version",
        "supersedes",
    }
)
_GUIDANCE_FIELDS = frozenset({"key", "guidance_text", "scope", "authoritative", "approved_by"})
_NOTE_FIELDS = frozenset({"key", "title", "content", "kind", "author", "review_status", "version"})
# company_projects rows (0011). Every column is explicit text; `project_key` /
# `project_name` must fit the `SourceRow` (`source_id` / `title`) they become
# in `read_source_*` results.
_PROJECT_REQUIRED = ("project_key", "project_name", "tier", "status", "owner", "summary")
_PROJECT_TEXT = ("details", "budget", "internal_notes")
_PROJECT_FIELDS = frozenset({*_PROJECT_REQUIRED, *_PROJECT_TEXT})
_PROJECT_LIMITS = {"project_key": MAX_REF_CHARS, "project_name": MAX_TITLE_CHARS}


class CorpusError(ValueError):
    """The seed corpus is invalid; nothing was written."""


def _mask_email(address: str) -> str:
    """First-char masking (`s***@e***.com`). Never echoes raw input on failure.

    Deliberately a local copy of `apps.api.identity.mask_email` (keep them in
    sync): importing the backend module would pull runtime `Settings` into
    this migration-role tool.
    """
    try:
        local, _, domain = address.partition("@")
        labels = domain.split(".")
        if not local or len(labels) < 2 or not all(labels):
            return "***"
        masked_labels = [f"{label[0]}***" for label in labels[:-1]] + [labels[-1]]
        return f"{local[0]}***@{'.'.join(masked_labels)}"
    except Exception:
        return "***"


#: `source_id` prefix reserved for team notes Maya confirmed at runtime
#: (`knowledge_confirm_maya_decision`, P2-09, D-09 rule 2): `note-<team_notes.id>`.
#: The seed corpus may not use it (`validate_source_record`), and the ingest
#: only ever reads, updates or re-chunks the source_ids its own files list —
#: it never deletes or rewrites any other source — so a re-seed leaves every
#: runtime note source and its chunks exactly as the tool server wrote them.
RUNTIME_NOTE_SOURCE_PREFIX = "note-"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_chunks(title: str, content: str) -> list[str]:
    """The chunk texts of one source: the title-prefixed text
    (`"{title}\\n\\n{content}"`) in `CHUNK_WORDS` windows sharing
    `CHUNK_OVERLAP_WORDS`. A repeated window (only possible for repetitive
    text) is kept once: its chunk hash would collide anyway. Shared by the
    seed ingest and the tool server's confirmed-note ingestion, so both
    write byte-identical chunk layouts."""
    return list(dict.fromkeys(chunk_text(f"{title}\n\n{content}")))


def chunk_hash(source_id: str, text: str) -> str:
    """`knowledge_chunks.content_hash` (unique per workspace)."""
    return content_hash(f"{source_id}:{text}")


def vector_literal(vector: Any) -> str:
    """pgvector text form `[x,y,...]`, bound as a value and cast in SQL."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def chunk_text(
    text: str, *, words: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS
) -> list[str]:
    """Deterministic word windows: `words` per chunk, `overlap` shared."""
    if words < 1 or overlap < 0 or overlap >= words:
        raise ValueError(f"need words >= 1 and 0 <= overlap < words, got {words}/{overlap}")
    tokens = text.split()
    if not tokens:
        return []
    chunks = []
    start = 0
    while start < len(tokens):
        chunks.append(" ".join(tokens[start : start + words]))
        if start + words >= len(tokens):
            break
        start += words - overlap
    return chunks


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_source_record(data: Mapping[str, Any]) -> list[str]:
    """Problems with one source record (PRD §8.1 metadata). Empty means complete.

    Every record carries source, author, timestamp (`recorded_at`), tier,
    review status, version, and supersession metadata; the content hash is
    computed at ingest.
    """
    if not isinstance(data, Mapping):
        return ["record(object)"]
    problems = [name for name in _SOURCE_REQUIRED if data.get(name) in (None, "")]
    if data.get("tier") not in _TIERS:
        problems.append("tier(valid)")
    if data.get("status") not in _SOURCE_STATUSES:
        problems.append("status(valid)")
    if data.get("review_status") not in _REVIEW_STATUSES:
        problems.append("review_status(valid)")
    if not _is_positive_int(data.get("version")):
        problems.append("version(valid)")
    if not _is_iso_date(data.get("recorded_at")):
        problems.append("recorded_at(valid)")
    if data.get("author") not in _PERSONAS:
        problems.append("author(persona)")
    for name in ("source_id", "title", "content", "source_type", "reference"):
        if name in data and not isinstance(data[name], str):
            problems.append(f"{name}(text)")
    if isinstance(data.get("source_id"), str) and data["source_id"].startswith(
        RUNTIME_NOTE_SOURCE_PREFIX
    ):
        problems.append(f"source_id(reserved prefix {RUNTIME_NOTE_SOURCE_PREFIX!r})")
    if "superseded_by" not in data:
        problems.append("superseded_by")
    else:
        target = data["superseded_by"]
        superseded = data.get("status") == "superseded"
        linked = isinstance(target, str) and target != "" and target != data.get("source_id")
        if superseded != linked or (target is not None and not linked):
            problems.append("superseded_by(consistent with status)")
    provenance = data.get("provenance", {})
    if not isinstance(provenance, Mapping):
        problems.append("provenance(object)")
    elif reserved := sorted(_RESERVED_PROVENANCE & set(provenance)):
        problems.append(f"provenance(reserved: {', '.join(reserved)})")
    if unknown := sorted(set(data) - _SOURCE_FIELDS):
        problems.append(f"unknown({', '.join(unknown)})")
    return problems


def _item_problems(
    item: Any, fields: frozenset[str], required: tuple[str, ...], personas: tuple[str, ...]
) -> list[str]:
    if not isinstance(item, Mapping):
        return ["item(object)"]
    problems = [name for name in required if not isinstance(item.get(name), str) or not item[name]]
    for name in personas:
        if item.get(name) is not None and item[name] not in _PERSONAS:
            problems.append(f"{name}(persona)")
    if "authoritative" in item and not isinstance(item["authoritative"], bool):
        problems.append("authoritative(bool)")
    if "version" in item and not _is_positive_int(item["version"]):
        problems.append("version(valid)")
    if unknown := sorted(set(item) - fields):
        problems.append(f"unknown({', '.join(unknown)})")
    return problems


def validate_project(item: Any) -> list[str]:
    """Problems with one `company_projects` row (0011). Empty means valid."""
    if not isinstance(item, Mapping):
        return ["item(object)"]
    problems = [
        name for name in _PROJECT_REQUIRED if not isinstance(item.get(name), str) or not item[name]
    ]
    problems += [name for name in _PROJECT_TEXT if not isinstance(item.get(name, ""), str)]
    for name, limit in _PROJECT_LIMITS.items():
        if isinstance(item.get(name), str) and len(item[name]) > limit:
            problems.append(f"{name}(<= {limit} chars)")
    if item.get("tier") not in _TIERS:
        problems.append("tier(valid)")
    if item.get("status") not in _SOURCE_STATUSES:
        problems.append("status(valid)")
    if item.get("owner") not in _PERSONAS:
        problems.append("owner(persona)")
    if unknown := sorted(set(item) - _PROJECT_FIELDS):
        problems.append(f"unknown({', '.join(unknown)})")
    return problems


@dataclass
class Corpus:
    """A validated seed corpus (nothing touches the database before this exists)."""

    sources: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    guidance: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items = _load_json(path)
    if not isinstance(items, list):
        raise CorpusError(f"{path.name}: expected a JSON list")
    return items


def _check_keys(items: list[dict[str, Any]], label: str) -> set[str]:
    keys: set[str] = set()
    for item in items:
        if item["key"] in keys:
            raise CorpusError(f"{label}: duplicate key {item['key']!r}")
        keys.add(item["key"])
    return keys


def load_corpus(source_dir: str | os.PathLike[str]) -> Corpus:
    """Read and validate every seed file. Raises CorpusError on any defect."""
    root = Path(source_dir)
    corpus = Corpus()
    source_ids: set[str] = set()
    for path in sorted((root / "sources").glob("*.json")):
        data = _load_json(path)
        if problems := validate_source_record(data):
            raise CorpusError(f"sources/{path.name}: invalid record: {problems}")
        if data["source_id"] in source_ids:
            raise CorpusError(f"sources/{path.name}: duplicate source_id {data['source_id']!r}")
        source_ids.add(data["source_id"])
        corpus.sources.append(data)
    for data in corpus.sources:
        if data["superseded_by"] is not None and data["superseded_by"] not in source_ids:
            raise CorpusError(
                f"source {data['source_id']!r}: superseded_by names unknown source "
                f"{data['superseded_by']!r}"
            )

    corpus.decisions = _load_items(root / "decisions.json")
    for item in corpus.decisions:
        if problems := _item_problems(item, _DECISION_FIELDS, ("key", "statement"), ("owner",)):
            raise CorpusError(f"decisions.json: invalid item {item.get('key')!r}: {problems}")
        if item.get("source") is not None and item["source"] not in source_ids:
            raise CorpusError(f"decision {item['key']!r}: unknown source {item['source']!r}")
    decision_keys = _check_keys(corpus.decisions, "decisions.json")
    for item in corpus.decisions:
        target = item.get("supersedes")
        if target is not None and (target not in decision_keys or target == item["key"]):
            raise CorpusError(f"decision {item['key']!r}: unknown supersedes key {target!r}")

    corpus.guidance = _load_items(root / "guidance.json")
    for item in corpus.guidance:
        problems = _item_problems(
            item, _GUIDANCE_FIELDS, ("key", "guidance_text"), ("approved_by",)
        )
        if problems:
            raise CorpusError(f"guidance.json: invalid item {item.get('key')!r}: {problems}")
    _check_keys(corpus.guidance, "guidance.json")

    corpus.notes = _load_items(root / "notes.json")
    for item in corpus.notes:
        problems = _item_problems(item, _NOTE_FIELDS, ("key", "content"), ("author",))
        if item.get("kind", "note") not in _NOTE_KINDS:
            problems.append("kind(valid)")
        if item.get("review_status", ReviewStatus.DRAFT.value) not in _REVIEW_STATUSES:
            problems.append("review_status(valid)")
        if problems:
            raise CorpusError(f"notes.json: invalid item {item.get('key')!r}: {problems}")
    _check_keys(corpus.notes, "notes.json")

    corpus.projects = _load_items(root / "company_projects.json")
    project_keys: set[str] = set()
    for item in corpus.projects:
        if problems := validate_project(item):
            raise CorpusError(
                f"company_projects.json: invalid item {_label(item, 'project_key')!r}: {problems}"
            )
        if item["project_key"] in project_keys:
            raise CorpusError(
                f"company_projects.json: duplicate project_key {item['project_key']!r}"
            )
        project_keys.add(item["project_key"])
    return corpus


def _label(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else None


def source_digest(data: Mapping[str, Any], version: str) -> str:
    """sha256 over every ingested field + model version + chunking + layout."""
    payload = {
        "record": dict(data),
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "model_version": version,
        "chunk_words": CHUNK_WORDS,
        "chunk_overlap": CHUNK_OVERLAP_WORDS,
        "layout": INGEST_LAYOUT_VERSION,
    }
    return content_hash(_canonical_json(payload))


def _recorded_ts(value: str) -> datetime:
    """Synthetic `recorded_at` date -> midnight UTC (created_at / updated_at)."""
    return datetime.combine(date.fromisoformat(value), time(0), tzinfo=UTC)


def _source_provenance(data: Mapping[str, Any], version: str, digest: str) -> dict[str, Any]:
    provenance = dict(data.get("provenance") or {})
    provenance.update(
        {
            "recorded_at": data["recorded_at"],
            "version": data["version"],
            "review_status": data["review_status"],
            "superseded_by": data["superseded_by"],
            "embed_model": MODEL_ID,
            "embed_tool": version,
            "embed_dim": EMBED_DIMS,
            "chunk_words": CHUNK_WORDS,
            "chunk_overlap": CHUNK_OVERLAP_WORDS,
            "ingest_digest": digest,
        }
    )
    return provenance


def _connect(dsn: str) -> Any:
    return psycopg.connect(dsn)


def _upsert_workspace_and_members(
    cur: Any, env: Mapping[str, str], stats: dict[str, int]
) -> dict[str, str]:
    """Workspace + member rows from env. Returns persona -> member id.

    Personas without a configured email are not written (a warning names the
    env key only); an existing row for them is still used for foreign keys.
    """
    cur.execute(
        "INSERT INTO public.workspaces (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (DEFAULT_WORKSPACE_ID, "ProxyLead demo workspace"),
    )
    member_ids: dict[str, str] = {}
    for persona, display, role, email_key, slack_key in _MEMBER_ENV:
        member_id = f"m-{persona.value}"
        email = (env.get(email_key) or "").strip()
        if email:
            cur.execute(
                """INSERT INTO public.members AS m
                   (id, workspace_id, persona, display_name, app_role, masked_email,
                    slack_user_id, armoriq_member_email_ref, active)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                   ON CONFLICT (id) DO UPDATE SET
                     workspace_id = EXCLUDED.workspace_id,
                     persona = EXCLUDED.persona,
                     display_name = EXCLUDED.display_name,
                     app_role = EXCLUDED.app_role,
                     masked_email = EXCLUDED.masked_email,
                     slack_user_id = COALESCE(EXCLUDED.slack_user_id, m.slack_user_id),
                     armoriq_member_email_ref = EXCLUDED.armoriq_member_email_ref
                   WHERE (m.workspace_id, m.persona, m.display_name, m.app_role,
                          m.masked_email, m.slack_user_id, m.armoriq_member_email_ref)
                     IS DISTINCT FROM
                         (EXCLUDED.workspace_id, EXCLUDED.persona, EXCLUDED.display_name,
                          EXCLUDED.app_role, EXCLUDED.masked_email,
                          COALESCE(EXCLUDED.slack_user_id, m.slack_user_id),
                          EXCLUDED.armoriq_member_email_ref)""",
                (
                    member_id,
                    DEFAULT_WORKSPACE_ID,
                    persona.value,
                    display,
                    role.value,
                    _mask_email(email),
                    (env.get(slack_key) or "").strip() or None,
                    email_key,
                ),
            )
            stats["members"] += cur.rowcount
        else:
            print(
                f"warning: {email_key} is not set; member {member_id} not created or refreshed",
                file=sys.stderr,
            )
        cur.execute(
            "SELECT id FROM public.members WHERE id = %s AND workspace_id = %s",
            (member_id, DEFAULT_WORKSPACE_ID),
        )
        row = cur.fetchone()
        if row is not None:
            member_ids[persona.value] = row[0]
    return member_ids


def _upsert_source(cur: Any, data: Mapping[str, Any], version: str, stats: dict[str, int]) -> int:
    """Insert or refresh one source + its chunks. Returns the source PK."""
    source_id = str(data["source_id"])
    digest = source_digest(data, version)
    cur.execute(
        """SELECT id, provenance->>'ingest_digest', provenance->>'embed_tool'
           FROM public.source_records WHERE workspace_id = %s AND source_id = %s""",
        (DEFAULT_WORKSPACE_ID, source_id),
    )
    existing = cur.fetchone()
    if existing is not None and existing[1] == digest:
        return int(existing[0])

    content = str(data["content"])
    recorded = _recorded_ts(data["recorded_at"])
    values = (
        data.get("source_type") or "note",
        data["title"],
        data["author"],
        data.get("reference") or "",
        data["tier"],
        data["status"],
        content_hash(content),
        json.dumps(_source_provenance(data, version, digest), sort_keys=True),
        recorded,
        recorded,
    )
    if existing is not None:
        source_pk = int(existing[0])
        cur.execute(
            """UPDATE public.source_records SET source_type = %s, title = %s, author = %s,
               reference = %s, tier = %s, status = %s, content_hash = %s,
               provenance = %s::jsonb, created_at = %s, updated_at = %s
               WHERE id = %s""",
            (*values, source_pk),
        )
    else:
        cur.execute(
            """INSERT INTO public.source_records
               (source_type, title, author, reference, tier, status, content_hash,
                provenance, created_at, updated_at, source_id, workspace_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) RETURNING id""",
            (*values, source_id, DEFAULT_WORKSPACE_ID),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"source insert returned no id: {source_id}")
        source_pk = int(row[0])
    stats["sources"] += 1

    chunks = source_chunks(data["title"], content)
    expected = sorted(chunk_hash(source_id, text) for text in chunks)
    cur.execute(
        "SELECT content_hash FROM public.knowledge_chunks WHERE source_id = %s", (source_pk,)
    )
    stored = sorted(str(row[0]).strip() for row in cur.fetchall())
    embedded_with = existing[2] if existing is not None else None
    if stored == expected and embedded_with == version:
        # Same chunk text, same model: keep the vectors, refresh copied metadata.
        cur.execute(
            """UPDATE public.knowledge_chunks SET tier = %s, workspace_id = %s
               WHERE source_id = %s AND (tier, workspace_id) IS DISTINCT FROM (%s, %s)""",
            (data["tier"], DEFAULT_WORKSPACE_ID, source_pk, data["tier"], DEFAULT_WORKSPACE_ID),
        )
        return source_pk

    cur.execute("DELETE FROM public.knowledge_chunks WHERE source_id = %s", (source_pk,))
    vectors = embed(chunks) if chunks else []
    for text, vector in zip(chunks, vectors, strict=True):
        cur.execute(
            """INSERT INTO public.knowledge_chunks
               (source_id, workspace_id, tier, chunk_text, embedding, content_hash)
               VALUES (%s, %s, %s, %s, %s::vector, %s)""",
            (
                source_pk,
                DEFAULT_WORKSPACE_ID,
                data["tier"],
                text,
                vector_literal(vector),
                chunk_hash(source_id, text),
            ),
        )
        stats["chunks"] += 1
    return source_pk


# Claim a row written before seed keys existed (its seed_key is NULL) by the
# identity the old ingest used — exact text, or the note's content hash. Only
# when the key is still unclaimed, so a second run claims nothing.
_CLAIM_DECISION = """UPDATE public.decisions SET seed_key = %s
    WHERE id = (SELECT id FROM public.decisions
                WHERE workspace_id = %s AND seed_key IS NULL AND statement = %s
                ORDER BY id LIMIT 1)
      AND NOT EXISTS (SELECT 1 FROM public.decisions WHERE workspace_id = %s AND seed_key = %s)"""
_CLAIM_GUIDANCE = """UPDATE public.manager_guidance SET seed_key = %s
    WHERE id = (SELECT id FROM public.manager_guidance
                WHERE workspace_id = %s AND seed_key IS NULL AND guidance_text = %s
                ORDER BY id LIMIT 1)
      AND NOT EXISTS (SELECT 1 FROM public.manager_guidance
                      WHERE workspace_id = %s AND seed_key = %s)"""
_CLAIM_NOTE = """UPDATE public.team_notes SET seed_key = %s
    WHERE id = (SELECT id FROM public.team_notes
                WHERE workspace_id = %s AND seed_key IS NULL AND content_hash = %s
                ORDER BY id LIMIT 1)
      AND NOT EXISTS (SELECT 1 FROM public.team_notes WHERE workspace_id = %s AND seed_key = %s)"""


def _claim(cur: Any, sql: str, key: str, legacy_identity: str, stats: dict[str, int]) -> None:
    cur.execute(sql, (key, DEFAULT_WORKSPACE_ID, legacy_identity, DEFAULT_WORKSPACE_ID, key))
    stats["claimed"] += cur.rowcount


def _member(members: Mapping[str, str], persona: Any) -> str | None:
    return members.get(persona) if persona else None


def _upsert_decisions(
    cur: Any,
    items: list[dict[str, Any]],
    source_pks: Mapping[str, int],
    members: Mapping[str, str],
    stats: dict[str, int],
) -> None:
    for item in items:
        source = item.get("source")
        if source is not None and source not in source_pks:
            raise CorpusError(f"decision {item['key']!r}: unknown source {source!r}")
        _claim(cur, _CLAIM_DECISION, item["key"], item["statement"], stats)
        cur.execute(
            """INSERT INTO public.decisions AS d
               (workspace_id, seed_key, statement, rationale, project, owner_member_id,
                authoritative, source_id, version)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (workspace_id, seed_key) DO UPDATE SET
                 statement = EXCLUDED.statement,
                 rationale = EXCLUDED.rationale,
                 project = EXCLUDED.project,
                 owner_member_id = COALESCE(EXCLUDED.owner_member_id, d.owner_member_id),
                 authoritative = EXCLUDED.authoritative,
                 source_id = EXCLUDED.source_id,
                 version = EXCLUDED.version
               WHERE (d.statement, d.rationale, d.project, d.owner_member_id,
                      d.authoritative, d.source_id, d.version)
                 IS DISTINCT FROM
                     (EXCLUDED.statement, EXCLUDED.rationale, EXCLUDED.project,
                      COALESCE(EXCLUDED.owner_member_id, d.owner_member_id),
                      EXCLUDED.authoritative, EXCLUDED.source_id, EXCLUDED.version)""",
            (
                DEFAULT_WORKSPACE_ID,
                item["key"],
                item["statement"],
                item.get("rationale", ""),
                item.get("project", ""),
                _member(members, item.get("owner")),
                bool(item.get("authoritative", False)),
                source_pks.get(source) if source is not None else None,
                int(item.get("version", 1)),
            ),
        )
        stats["decisions"] += cur.rowcount
    # Supersession links, re-applied every run (declared links only; a link set
    # at runtime by knowledge_mark_superseded on a non-seed row is untouched).
    for item in items:
        if item.get("supersedes") is None:
            continue
        cur.execute(
            """UPDATE public.decisions AS old SET superseded_by_id = new.id
               FROM public.decisions AS new
               WHERE old.workspace_id = %s AND old.seed_key = %s
                 AND new.workspace_id = %s AND new.seed_key = %s
                 AND old.superseded_by_id IS DISTINCT FROM new.id""",
            (DEFAULT_WORKSPACE_ID, item["supersedes"], DEFAULT_WORKSPACE_ID, item["key"]),
        )
        stats["links"] += cur.rowcount


def _upsert_guidance(
    cur: Any, items: list[dict[str, Any]], members: Mapping[str, str], stats: dict[str, int]
) -> None:
    for item in items:
        _claim(cur, _CLAIM_GUIDANCE, item["key"], item["guidance_text"], stats)
        scope = item.get("scope", "general")
        cur.execute(
            """INSERT INTO public.manager_guidance AS g
               (workspace_id, seed_key, guidance_text, scope, authoritative,
                approved_by_member_id, provenance)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (workspace_id, seed_key) DO UPDATE SET
                 guidance_text = EXCLUDED.guidance_text,
                 scope = EXCLUDED.scope,
                 authoritative = EXCLUDED.authoritative,
                 approved_by_member_id
                   = COALESCE(EXCLUDED.approved_by_member_id, g.approved_by_member_id),
                 provenance = EXCLUDED.provenance
               WHERE (g.guidance_text, g.scope, g.authoritative, g.approved_by_member_id,
                      g.provenance)
                 IS DISTINCT FROM
                     (EXCLUDED.guidance_text, EXCLUDED.scope, EXCLUDED.authoritative,
                      COALESCE(EXCLUDED.approved_by_member_id, g.approved_by_member_id),
                      EXCLUDED.provenance)""",
            (
                DEFAULT_WORKSPACE_ID,
                item["key"],
                item["guidance_text"],
                scope,
                bool(item.get("authoritative", False)),
                _member(members, item.get("approved_by")),
                json.dumps({"scope": scope}),
            ),
        )
        stats["guidance"] += cur.rowcount


def _upsert_notes(
    cur: Any, items: list[dict[str, Any]], members: Mapping[str, str], stats: dict[str, int]
) -> None:
    """Draft notes. `review_status` and `version` are set on insert only: after
    that, the draft lifecycle belongs to knowledge_confirm_maya_decision
    (P2-09, R-56), and a re-seed must never undo or fake a confirmation."""
    for item in items:
        digest = content_hash(item["content"])
        _claim(cur, _CLAIM_NOTE, item["key"], digest, stats)
        cur.execute(
            """INSERT INTO public.team_notes AS n
               (workspace_id, seed_key, author_member_id, title, content, content_kind,
                review_status, version, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (workspace_id, seed_key) DO UPDATE SET
                 author_member_id = COALESCE(EXCLUDED.author_member_id, n.author_member_id),
                 title = EXCLUDED.title,
                 content = EXCLUDED.content,
                 content_kind = EXCLUDED.content_kind,
                 content_hash = EXCLUDED.content_hash,
                 updated_at = now()
               WHERE (n.author_member_id, n.title, n.content, n.content_kind, n.content_hash)
                 IS DISTINCT FROM
                     (COALESCE(EXCLUDED.author_member_id, n.author_member_id), EXCLUDED.title,
                      EXCLUDED.content, EXCLUDED.content_kind, EXCLUDED.content_hash)""",
            (
                DEFAULT_WORKSPACE_ID,
                item["key"],
                _member(members, item.get("author")),
                item.get("title", ""),
                item["content"],
                item.get("kind", "note"),
                item.get("review_status", ReviewStatus.DRAFT.value),
                int(item.get("version", 1)),
                digest,
            ),
        )
        stats["notes"] += cur.rowcount


def _upsert_projects(cur: Any, items: list[dict[str, Any]], stats: dict[str, int]) -> None:
    """Synthetic company-data rows behind the approved views (0011, P2-08).

    Keyed by `(workspace_id, project_key)`; an existing row is rewritten only
    when a column differs, so a re-run counts 0. Rows not in the file are
    left alone (same rule as the other seeded tables)."""
    for item in items:
        cur.execute(
            """INSERT INTO public.company_projects AS p
               (workspace_id, project_key, project_name, status, tier, owner, summary,
                details, budget, internal_notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (workspace_id, project_key) DO UPDATE SET
                 project_name = EXCLUDED.project_name,
                 status = EXCLUDED.status,
                 tier = EXCLUDED.tier,
                 owner = EXCLUDED.owner,
                 summary = EXCLUDED.summary,
                 details = EXCLUDED.details,
                 budget = EXCLUDED.budget,
                 internal_notes = EXCLUDED.internal_notes
               WHERE (p.project_name, p.status, p.tier, p.owner, p.summary, p.details,
                      p.budget, p.internal_notes)
                 IS DISTINCT FROM
                     (EXCLUDED.project_name, EXCLUDED.status, EXCLUDED.tier, EXCLUDED.owner,
                      EXCLUDED.summary, EXCLUDED.details, EXCLUDED.budget,
                      EXCLUDED.internal_notes)""",
            (
                DEFAULT_WORKSPACE_ID,
                item["project_key"],
                item["project_name"],
                item["status"],
                item["tier"],
                item["owner"],
                item["summary"],
                item.get("details", ""),
                item.get("budget", ""),
                item.get("internal_notes", ""),
            ),
        )
        stats["projects"] += cur.rowcount


def run(source_dir: str | os.PathLike[str], dsn: str, env: Mapping[str, str]) -> dict[str, int]:
    """Ingest one corpus in a single transaction. Returns rows changed per area
    (all zero when the database already matches the corpus)."""
    corpus = load_corpus(source_dir)
    version = model_version()
    warm()  # a first-run model download must not hold a connection or the lock
    stats = {
        "members": 0,
        "sources": 0,
        "chunks": 0,
        "claimed": 0,
        "decisions": 0,
        "links": 0,
        "guidance": 0,
        "notes": 0,
        "projects": 0,
    }
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (INGEST_LOCK_KEY,))
            members = _upsert_workspace_and_members(cur, env, stats)
            source_pks = {
                str(data["source_id"]): _upsert_source(cur, data, version, stats)
                for data in corpus.sources
            }
            _upsert_decisions(cur, corpus.decisions, source_pks, members, stats)
            _upsert_guidance(cur, corpus.guidance, members, stats)
            _upsert_notes(cur, corpus.notes, members, stats)
            # After the workspace upsert: company_projects.workspace_id is an FK.
            _upsert_projects(cur, corpus.projects, stats)
        conn.commit()
    return stats


def _is_runtime_dsn(dsn: str, env: Mapping[str, str]) -> bool:
    return any((env.get(name) or "").strip() == dsn for name in _RUNTIME_DSNS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest the seed corpus (migration role only).")
    parser.add_argument("--source", required=True, help="corpus directory (seed/)")
    args = parser.parse_args(argv)
    dsn = os.environ.get(_MIGRATE_ENV, "").strip()
    if not dsn:
        print(f"error: {_MIGRATE_ENV} is not set (migration role only)", file=sys.stderr)
        return 2
    if _is_runtime_dsn(dsn, os.environ):
        print(
            f"error: {_MIGRATE_ENV} equals a runtime service DSN; "
            "ingest runs with the migration/seed role only",
            file=sys.stderr,
        )
        return 2
    try:
        stats = run(args.source, dsn, os.environ)
    except CorpusError as exc:
        print(f"error: invalid seed corpus: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        # Never print the message: libpq parse errors echo the whole DSN,
        # password included (same rule as scripts/migrate.py).
        print(f"error: database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"ingested: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
