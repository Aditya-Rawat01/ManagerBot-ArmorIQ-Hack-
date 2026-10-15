"""Seed ingestion (P2-03): corpus files in `seed/` into the knowledge tables.

Operator only, repository root:

    DATABASE_URL_MIGRATE='<owner DSN>' python -m services.knowledge.ingest --source seed/

Connects ONLY with `DATABASE_URL_MIGRATE` (the migration/seed role) — never a
runtime DSN. Idempotent: re-running inserts nothing new (sources keyed by
workspace + source_id with content-hash comparison; chunks by workspace +
content hash; decisions/guidance/notes by exact-text match). Members and the
workspace are upserted from env (D-20/ADR-021); personas without a configured
email are skipped. No raw emails are stored — only masked display values plus
the env-key reference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterator, Mapping
from typing import Any

import psycopg

from services.contracts import DEFAULT_WORKSPACE_ID
from services.knowledge.embeddings import embed, model_version

CHUNK_WORDS = 120
CHUNK_OVERLAP_WORDS = 20

_MIGRATE_ENV = "DATABASE_URL_MIGRATE"
_RUNTIME_DSNS = ("DATABASE_URL_BACKEND", "DATABASE_URL_TOOLSERVER")

_MEMBER_ENV: tuple[tuple[str, str, str, str, str], ...] = (
    ("maya", "Maya", "manager", "PERSONA_MAYA_EMAIL", "SLACK_USER_MAYA_ID"),
    ("sam", "Sam", "senior", "PERSONA_SAM_EMAIL", "SLACK_USER_SAM_ID"),
    ("priya", "Priya", "junior", "PERSONA_PRIYA_EMAIL", "SLACK_USER_PRIYA_ID"),
)


def _mask_email(address: str) -> str:
    """First-char masking (mirrors apps.api.identity.mask_email)."""
    local, _, domain = address.partition("@")
    labels = domain.split(".")
    if not local or len(labels) < 2 or not all(labels):
        return "***"
    return f"{local[0]}***@{'.'.join([f'{label[0]}***' for label in labels[:-1]] + [labels[-1]])}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(
    text: str, *, words: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP_WORDS
) -> list[str]:
    """Deterministic word windows: `words` per chunk, `overlap` shared."""
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


def validate_source_record(data: Mapping[str, Any]) -> list[str]:
    """Missing PRD §8.1 metadata fields. Empty means complete."""
    required = ("source_id", "title", "tier", "status", "author", "content")
    missing = [name for name in required if not data.get(name)]
    if data.get("tier") not in ("team", "confidential", "restricted"):
        missing.append("tier(valid)")
    if data.get("status") not in ("active", "superseded"):
        missing.append("status(valid)")
    return missing


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _iter_sources(source_dir: str) -> Iterator[dict[str, Any]]:
    import glob

    for path in sorted(glob.glob(os.path.join(source_dir, "sources", "*.json"))):
        data = _load_json(path)
        missing = validate_source_record(data)
        if missing:
            raise ValueError(f"{path}: incomplete record, missing {missing}")
        yield data


def _connect(dsn: str) -> Any:
    return psycopg.connect(dsn)


def _upsert_workspace_and_members(conn: Any, env: Mapping[str, str]) -> dict[str, str]:
    """Workspace + member rows from env. Returns persona -> member id (may be empty)."""
    member_ids: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.workspaces (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (DEFAULT_WORKSPACE_ID, "ProxyLead demo workspace"),
        )
        for persona, display, role, email_key, slack_key in _MEMBER_ENV:
            email = (env.get(email_key) or "").strip()
            if not email:
                continue
            cur.execute(
                """INSERT INTO public.members
                   (id, workspace_id, persona, display_name, app_role, masked_email,
                    slack_user_id, armoriq_member_email_ref, active)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    f"m-{persona}",
                    DEFAULT_WORKSPACE_ID,
                    persona,
                    display,
                    role,
                    _mask_email(email),
                    env.get(slack_key) or None,
                    email_key,
                ),
            )
            cur.execute("SELECT id FROM public.members WHERE id = %s", (f"m-{persona}",))
            row = cur.fetchone()
            if row is not None:
                member_ids[persona] = row[0]
    return member_ids


def _upsert_source(conn, data: Mapping[str, Any], version: str) -> tuple[int, bool]:
    """Insert or refresh one source + its chunks. Returns (source PK, changed)."""
    content = str(data["content"])
    digest = content_hash(content)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, content_hash FROM public.source_records "
            "WHERE workspace_id = %s AND source_id = %s",
            (DEFAULT_WORKSPACE_ID, data["source_id"]),
        )
        existing = cur.fetchone()
        if existing and existing[1] == digest:
            return existing[0], False
        provenance = dict(data.get("provenance") or {})
        provenance.update(
            {
                "embed_model": version.split("+")[0],
                "embed_tool": version,
                "embed_dim": 384,
                "case": data.get("case", ""),
                "recorded_at": data.get("recorded_at", ""),
            }
        )
        if existing:
            cur.execute(
                """UPDATE public.source_records SET title=%s, author=%s, tier=%s,
                   status=%s, content_hash=%s, provenance=%s::jsonb, updated_at=now()
                   WHERE id=%s""",
                (
                    data["title"],
                    data["author"],
                    data["tier"],
                    data["status"],
                    digest,
                    json.dumps(provenance),
                    existing[0],
                ),
            )
            cur.execute("DELETE FROM public.knowledge_chunks WHERE source_id = %s", (existing[0],))
            source_pk = existing[0]
        else:
            cur.execute(
                """INSERT INTO public.source_records
                   (source_id, workspace_id, source_type, title, author, reference,
                    tier, status, content_hash, provenance)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id""",
                (
                    data["source_id"],
                    DEFAULT_WORKSPACE_ID,
                    data.get("source_type", "note"),
                    data["title"],
                    data["author"],
                    data.get("reference", ""),
                    data["tier"],
                    data["status"],
                    digest,
                    json.dumps(provenance),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"source insert returned no id: {data['source_id']}")
            source_pk = row[0]
        chunks = chunk_text(f"{data['title']}\n\n{content}")
        vectors = embed(chunks)
        for text, vector in zip(chunks, vectors, strict=True):
            cur.execute(
                """INSERT INTO public.knowledge_chunks
                   (source_id, workspace_id, tier, chunk_text, embedding, content_hash)
                   VALUES (%s,%s,%s,%s,%s::vector,%s)
                   ON CONFLICT (workspace_id, content_hash) DO NOTHING""",
                (
                    source_pk,
                    DEFAULT_WORKSPACE_ID,
                    data["tier"],
                    text,
                    "[" + ",".join(repr(float(v)) for v in vector) + "]",
                    content_hash(f"{data['source_id']}:{text}"),
                ),
            )
        return source_pk, True


def _upsert_decisions(
    conn,
    items: list[dict[str, Any]],
    source_pks: dict[str, int],
    member_ids: Mapping[str, str] | None = None,
) -> int:
    members = member_ids or {}
    added = 0
    with conn.cursor() as cur:
        for item in items:
            cur.execute(
                "SELECT 1 FROM public.decisions WHERE workspace_id=%s AND statement=%s",
                (DEFAULT_WORKSPACE_ID, item["statement"]),
            )
            if cur.fetchone():
                continue
            owner = members.get(item["owner"]) if item.get("owner") else None
            cur.execute(
                """INSERT INTO public.decisions
                   (workspace_id, statement, rationale, project, owner_member_id,
                    authoritative, source_id, version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    DEFAULT_WORKSPACE_ID,
                    item["statement"],
                    item.get("rationale", ""),
                    item.get("project", ""),
                    owner,
                    bool(item.get("authoritative", False)),
                    source_pks.get(item.get("source", "")),
                    int(item.get("version", 1)),
                ),
            )
            new_id = cur.fetchone()[0]
            if item.get("supersedes_statement"):
                cur.execute(
                    "SELECT id FROM public.decisions WHERE workspace_id=%s AND statement=%s",
                    (DEFAULT_WORKSPACE_ID, item["supersedes_statement"]),
                )
                old = cur.fetchone()
                if old:
                    cur.execute(
                        "UPDATE public.decisions SET superseded_by_id=%s WHERE id=%s",
                        (new_id, old[0]),
                    )
            added += 1
    return added


def _upsert_guidance(
    conn, items: list[dict[str, Any]], member_ids: Mapping[str, str] | None = None
) -> int:
    members = member_ids or {}
    added = 0
    with conn.cursor() as cur:
        for item in items:
            cur.execute(
                "SELECT 1 FROM public.manager_guidance WHERE workspace_id=%s AND guidance_text=%s",
                (DEFAULT_WORKSPACE_ID, item["guidance_text"]),
            )
            if cur.fetchone():
                continue
            approver = members.get(item["approved_by"]) if item.get("approved_by") else None
            provenance = {"scope": item.get("scope", "general")}
            cur.execute(
                """INSERT INTO public.manager_guidance
                   (workspace_id, guidance_text, scope, authoritative,
                    approved_by_member_id, provenance)
                   VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    DEFAULT_WORKSPACE_ID,
                    item["guidance_text"],
                    item.get("scope", "general"),
                    bool(item.get("authoritative", False)),
                    approver,
                    json.dumps(provenance),
                ),
            )
            added += 1
    return added


def _upsert_notes(
    conn, items: list[dict[str, Any]], member_ids: Mapping[str, str] | None = None
) -> int:
    members = member_ids or {}
    added = 0
    with conn.cursor() as cur:
        for item in items:
            digest = content_hash(item["content"])
            cur.execute(
                "SELECT 1 FROM public.team_notes WHERE workspace_id=%s AND content_hash=%s",
                (DEFAULT_WORKSPACE_ID, digest),
            )
            if cur.fetchone():
                continue
            author = members.get(item["author"]) if item.get("author") else None
            if item.get("kind", "note") not in ("note", "summary"):
                raise ValueError(f"bad content_kind in seed note: {item.get('title')}")
            cur.execute(
                """INSERT INTO public.team_notes
                   (workspace_id, author_member_id, title, content, content_kind,
                    review_status, version, content_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    DEFAULT_WORKSPACE_ID,
                    author,
                    item.get("title", ""),
                    item["content"],
                    item.get("kind", "note"),
                    item.get("review_status", "draft"),
                    int(item.get("version", 1)),
                    digest,
                ),
            )
            added += 1
    return added


def run(source_dir: str, dsn: str, env: Mapping[str, str]) -> dict[str, int]:
    """Ingest one corpus. Returns counts of rows added/changed per area."""
    version = model_version()
    stats = {"sources": 0, "decisions": 0, "guidance": 0, "notes": 0}
    source_pks: dict[str, int] = {}
    with _connect(dsn) as conn:
        member_ids = _upsert_workspace_and_members(conn, env)
        for data in _iter_sources(source_dir):
            source_pk, changed = _upsert_source(conn, data, version)
            source_pks[str(data["source_id"])] = source_pk
            stats["sources"] += 1 if changed else 0
        for name, loader in (
            ("decisions.json", _upsert_decisions),
            ("guidance.json", _upsert_guidance),
            ("notes.json", _upsert_notes),
        ):
            path = os.path.join(source_dir, name)
            if not os.path.exists(path):
                continue
            items = _load_json(path)
            if name == "decisions.json":
                stats["decisions"] += loader(conn, items, source_pks, member_ids)
            else:
                key = "guidance" if name == "guidance.json" else "notes"
                stats[key] += loader(conn, items, member_ids)
        conn.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest the seed corpus (migration role only).")
    parser.add_argument("--source", required=True, help="corpus directory (seed/)")
    args = parser.parse_args(argv)
    dsn = os.environ.get(_MIGRATE_ENV, "")
    if not dsn:
        print(f"error: {_MIGRATE_ENV} is not set (migration role only)", file=sys.stderr)
        return 2
    stats = run(args.source, dsn, os.environ)
    print(f"ingested: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
