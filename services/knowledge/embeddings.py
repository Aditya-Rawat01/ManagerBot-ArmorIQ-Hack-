"""Local embeddings for ingestion and retrieval (P2-03).

FastEmbed `BAAI/bge-small-en-v1.5` (384 dims) runs on this machine only —
confidential text never goes to a hosted embedding API (R-54). The model loads
once per process and is reused by ingestion (P2-03) and query embedding
(P2-05).

Two load policies:

* `warm()` / `embed()` load lazily (first call downloads the model when it is
  not cached). The ingest CLI uses them, warming before its transaction opens.
* `embed_loaded()` NEVER loads: it embeds with the model a successful `warm()`
  already loaded and raises `EmbeddingUnavailable` otherwise. The tool server
  warms once at startup and uses only this on the request path, so a failed
  warm fails every search closed instead of retrying a download per request.

CPU sizing: ONNX Runtime defaults to one thread per host core, which on a
fractional-CPU container (Render Free: 0.1 CPU) means many threads fighting
for a sliver of CPU. The model runs with `EMBED_THREADS` and embeds are
serialized by a lock, so concurrent requests queue instead of thrashing.

`fastembed` is imported lazily (inside `_load` / `model_version`), so modules
that only need `MODEL_ID` / `EMBED_DIMS` — and `services.knowledge.ingest`'s
pure helpers — import without it installed.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastembed import TextEmbedding

MODEL_ID = "BAAI/bge-small-en-v1.5"
EMBED_DIMS = 384
#: ONNX Runtime intra-/inter-op threads (fastembed `threads=`).
EMBED_THREADS = 1


class EmbeddingUnavailable(RuntimeError):
    """The model is not loaded and this caller may not load it (fail closed)."""


def model_version() -> str:
    """Recorded alongside every ingested chunk (Arch §7.1)."""
    import fastembed

    return f"{MODEL_ID}+fastembed-{fastembed.__version__}"


_model: TextEmbedding | None = None
_lock = threading.Lock()  # guards the one-time load
_embed_lock = threading.Lock()  # serializes inference (see the module docstring)


def _load() -> TextEmbedding:
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                _model = TextEmbedding(MODEL_ID, threads=EMBED_THREADS)
    return _model


def warm() -> None:
    """Load the model now (first call downloads it if not cached).

    Ingestion calls this BEFORE opening its database transaction, so a slow
    first-run download never holds a connection or the ingest lock open. The
    tool server calls it once in its lifespan (readiness depends on it).
    """
    _load()


def is_loaded() -> bool:
    """True once a `warm()` / `embed()` call has loaded the model."""
    return _model is not None


def _run(model: TextEmbedding, texts: list[str]) -> list[list[float]]:
    with _embed_lock:
        vectors = list(model.embed(texts))
    for vector in vectors:
        if len(vector) != EMBED_DIMS:
            raise ValueError(f"expected {EMBED_DIMS} dims, got {len(vector)}")
    return [[float(value) for value in vector] for vector in vectors]


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts locally, loading the model on first use (ingest path).

    Returns one 384-dim vector per input, in order.
    """
    return _run(_load(), texts)


def embed_loaded(texts: list[str]) -> list[list[float]]:
    """Embed with the already-loaded model; never loads or downloads.

    Request path (tool server): raises `EmbeddingUnavailable` when no
    successful `warm()` happened in this process.
    """
    model = _model
    if model is None:
        raise EmbeddingUnavailable("embedding model is not loaded")
    return _run(model, texts)
