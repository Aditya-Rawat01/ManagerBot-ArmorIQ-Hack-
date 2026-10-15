"""Ingestion tests (P2-03). DB-touching tests use fakes; the embedding test
needs the cached FastEmbed model and skips otherwise (no download in CI).
"""

import ast
import os
import re
from pathlib import Path

import pytest

from services.knowledge import ingest
from services.knowledge.embeddings import EMBED_DIMS, MODEL_ID

SEED_DIR = Path(__file__).resolve().parents[2] / "seed"


class FakeCursor:
    """Stateful in-memory stand-in for the knowledge tables."""

    def __init__(self, store: dict):
        self.store = store
        self._one: tuple | None = None
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple = ()) -> None:
        store = self.store
        q = " ".join(query.split())
        if q.startswith("INSERT INTO public.workspaces"):
            store.setdefault("workspaces", set()).add(params[0])
        elif q.startswith("INSERT INTO public.members"):
            store.setdefault("members", set()).add(params[0])
        elif "FROM public.source_records" in q and "WHERE workspace_id" in q:
            digest = store.get("sources", {}).get((params[0], params[1]))
            self._one = (1, digest) if digest else None
        elif q.startswith("INSERT INTO public.source_records"):
            store.setdefault("sources", {})[(params[1], params[0])] = params[8]
            self._one = (len(store["sources"]),)
        elif q.startswith("UPDATE public.source_records"):
            self._one = None
        elif "DELETE FROM public.knowledge_chunks" in q:
            pass
        elif q.startswith("INSERT INTO public.knowledge_chunks"):
            store.setdefault("chunks", set()).add((params[1], params[5]))
        elif "FROM public.decisions" in q:
            self._one = (1,) if params[1] in store.get("decisions", set()) else None
        elif q.startswith("INSERT INTO public.decisions"):
            store.setdefault("decisions", set()).add(params[1])
            self._one = (len(store["decisions"]),)
        elif "FROM public.manager_guidance" in q:
            self._one = (1,) if params[1] in store.get("guidance", set()) else None
        elif q.startswith("INSERT INTO public.manager_guidance"):
            store.setdefault("guidance", set()).add(params[1])
        elif "FROM public.team_notes" in q:
            self._one = (1,) if params[1] in store.get("notes", set()) else None
        elif q.startswith("INSERT INTO public.team_notes"):
            store.setdefault("notes", set()).add(params[7])
        elif q.startswith("SELECT id FROM public.members"):
            self._one = None
        else:
            self._one = None

    def fetchone(self) -> tuple | None:
        return self._one

    def fetchall(self) -> list:
        return []


class FakeConn:
    def __init__(self, store: dict):
        self.store = store
        self.committed = False

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def commit(self) -> None:
        self.committed = True


def test_chunking_deterministic() -> None:
    text = " ".join(f"word{i}" for i in range(300))
    first, second = ingest.chunk_text(text), ingest.chunk_text(text)
    assert first == second and len(first) == 3
    assert len(first[0].split()) == 120
    assert first[0].split()[-20:] == first[1].split()[:20]
    assert ingest.chunk_text("") == []


def test_content_hash_stable() -> None:
    assert ingest.content_hash("abc") == ingest.content_hash("abc")
    assert ingest.content_hash("abc") != ingest.content_hash("abd")
    assert len(ingest.content_hash("abc")) == 64


def test_metadata_complete() -> None:
    assert ingest.validate_source_record({"source_id": "x"}) != []
    good = {
        "source_id": "s",
        "title": "t",
        "tier": "team",
        "status": "active",
        "author": "sam",
        "content": "c",
    }
    assert ingest.validate_source_record(good) == []
    assert ingest.validate_source_record({**good, "tier": "gold"}) != []


def test_idempotent_upsert_logic_with_fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict = {}
    monkeypatch.setattr(ingest, "_connect", lambda dsn: FakeConn(store))
    monkeypatch.setattr(ingest, "embed", lambda texts: [[0.0] * EMBED_DIMS for _ in texts])
    first = ingest.run(str(SEED_DIR), "fake-dsn", {})
    assert first["sources"] > 0
    sources_before = len(store.get("sources", {}))
    chunks_before = len(store.get("chunks", set()))
    # Second run sees identical content hashes: nothing new anywhere.
    second = ingest.run(str(SEED_DIR), "fake-dsn", {})
    assert second == {"sources": 0, "decisions": 0, "guidance": 0, "notes": 0, "adopted": 0}
    assert len(store.get("sources", {})) == sources_before
    assert len(store.get("chunks", set())) == chunks_before


def _model_cached() -> bool:
    """True when the FastEmbed model snapshot is on disk (see embeddings cache)."""
    import glob
    import tempfile

    candidates = [
        os.environ.get("FAST_EMBED_CACHE_PATH", ""),
        str(Path.home() / ".cache" / "fastembed"),
        os.path.join(tempfile.gettempdir(), "fastembed_cache"),
    ]
    return any(
        glob.glob(os.path.join(base, "models--*bge-small-en-v1.5*")) for base in candidates if base
    )


@pytest.mark.slow
@pytest.mark.skipif(not _model_cached(), reason="FastEmbed model not cached (no download in tests)")
def test_embeddings_return_384_dims() -> None:
    from services.knowledge.embeddings import embed, model_version

    vectors = embed(["quokka Friday deploy freeze", "unrelated snack poll"])
    assert len(vectors) == 2
    assert all(len(vector) == EMBED_DIMS for vector in vectors)
    assert MODEL_ID in model_version()


def test_ingest_reads_only_the_migrate_dsn() -> None:
    source = Path(ingest.__file__).read_text()
    # The migration role DSN is read via the _MIGRATE_ENV constant; no runtime
    # DSN is ever fetched from the environment in this module.
    assert '_MIGRATE_ENV = "DATABASE_URL_MIGRATE"' in source
    assert 'os.environ.get("DATABASE_URL_BACKEND"' not in source
    assert 'os.environ.get("DATABASE_URL_TOOLSERVER"' not in source
    for path in (
        Path("apps/api/core/config.py"),
        Path("services/toolserver/config.py"),
    ):
        assert "database_url_migrate" not in path.read_text(), f"MIGRATE DSN in {path}"


def test_seed_has_no_emails_or_tokens() -> None:
    pattern = re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|ak_(live|test)_[A-Za-z0-9]{6,}"
        r"|xox[bpa]-[0-9]|xapp-[0-9]"
    )
    offenders = [
        f"{path}:{index}: {line.strip()[:80]}"
        for path in sorted((SEED_DIR).rglob("*.json"))
        for index, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], f"PII/tokens in seed: {offenders}"
    tree = ast.parse(Path(ingest.__file__).read_text())
    assert "Settings" not in {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}, (
        "ingest must not use runtime Settings (migration role only)"
    )


def test_seed_files_validate() -> None:
    for path in sorted((SEED_DIR / "sources").glob("*.json")):
        import json

        data = json.loads(path.read_text())
        assert ingest.validate_source_record(data) == [], path.name


def test_cli_never_prints_the_dsn_on_database_errors(monkeypatch, capsys) -> None:
    """A malformed DSN makes libpq echo it (password included); the CLI must not."""
    from services.knowledge import ingest

    fake_password = "not-a-real-password-0000"
    monkeypatch.setenv(
        "DATABASE_URL_MIGRATE", f"'postgresql://owner:{fake_password}@nowhere.invalid/db'"
    )
    code = ingest.main(["--source", "seed/"])
    captured = capsys.readouterr()
    assert code == 1
    assert fake_password not in captured.out + captured.err
    assert "database error" in captured.err
