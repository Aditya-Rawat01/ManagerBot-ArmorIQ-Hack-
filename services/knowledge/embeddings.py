"""Local embeddings for ingestion and retrieval (P2-03).

FastEmbed `BAAI/bge-small-en-v1.5` (384 dims) runs on this machine only —
confidential text never goes to a hosted embedding API (R-54). The model loads
once per process and is reused by ingestion (P2-03) and query embedding
(P2-05).
"""

from __future__ import annotations

import threading

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
                _model = TextEmbedding(MODEL_ID)
    return _model


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts locally. Returns one 384-dim vector per input, in order."""
    model = _load()
    vectors = list(model.embed(texts))
    for vector in vectors:
        if len(vector) != EMBED_DIMS:
            raise ValueError(f"expected {EMBED_DIMS} dims, got {len(vector)}")
    return [[float(value) for value in vector] for vector in vectors]
