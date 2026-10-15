"""Local embeddings for ingestion and retrieval (P2-03).

FastEmbed `BAAI/bge-small-en-v1.5` (384 dims) runs on this machine only —
confidential text never goes to a hosted embedding API (R-54). The model loads
once per process and is reused by ingestion (P2-03) and query embedding
(P2-05).

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


def model_version() -> str:
    """Recorded alongside every ingested chunk (Arch §7.1)."""
    import fastembed

    return f"{MODEL_ID}+fastembed-{fastembed.__version__}"


_model: TextEmbedding | None = None
_lock = threading.Lock()


def _load() -> TextEmbedding:
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                _model = TextEmbedding(MODEL_ID)
    return _model


def warm() -> None:
    """Load the model now (first call downloads it if not cached).

    Ingestion calls this BEFORE opening its database transaction, so a slow
    first-run download never holds a connection or the ingest lock open.
    """
    _load()


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts locally. Returns one 384-dim vector per input, in order."""
    model = _load()
    vectors = list(model.embed(texts))
    for vector in vectors:
        if len(vector) != EMBED_DIMS:
            raise ValueError(f"expected {EMBED_DIMS} dims, got {len(vector)}")
    return [[float(value) for value in vector] for vector in vectors]
