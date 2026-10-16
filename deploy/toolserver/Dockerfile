# ProxyLead MCP tool server image (P1-05, ADR-016 + ADR-029; model: P2-05).
#
# Build from the repository root (the build context):
#   docker build -f deploy/toolserver/Dockerfile -t proxylead-toolserver:dev .
#
# Render runs this as a Docker web service; P1-11's compose reuses it.
# Installs ONLY the `toolserver` extra — never the API extra (CI isolation
# parity: .github/workflows/ci.yml `toolserver` job). No secrets, `.env`
# files, credentials, or tokens are baked into any layer; every secret
# arrives as a runtime environment variable (Render secret store locally:
# `docker run --env-file`, never `COPY .env`).
#
# The FastEmbed model (BAAI/bge-small-en-v1.5, ~65 MB) IS baked in at build
# time (layer 2 below) and the runtime never downloads it (HF_HUB_OFFLINE=1):
# the server warms it once at startup and /health stays 503 until it loads.
# Layer order keeps rebuilds cheap: dependencies (pyproject.toml) → model
# (services/knowledge/embeddings.py only) → application source.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8100

# Hosted default: diagnostic ping tools off (P1-05 §8, M-04). Local compose
# opts in explicitly (docs/DOCKER.md); Render also sets it to false.
ENV MCP_ENABLE_DIAGNOSTIC_TOOLS=false

# glibc creates up to 8 malloc arenas per core; ONNX Runtime's threads would
# each grow one. Two arenas keep RSS flat on a 512 MB instance (measured with
# EMBED_THREADS=1 at 0.1 CPU: ~335-342 MiB RSS; deploy/toolserver/README §4a).
ENV MALLOC_ARENA_MAX=2

WORKDIR /app

# Run as a non-root user (ADR-029).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/.fastembed-cache \
    && chown appuser:appuser /app/.fastembed-cache

# 1. Dependencies only. Pins live in pyproject.toml (`.[toolserver]`); with no
#    source copied yet the project itself installs empty — the app runs from
#    /app (uvicorn's app dir), so source edits never invalidate this layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[toolserver]"

# 2. The embedding model, before the application source: only a change to the
#    embeddings module (model id, loader) re-downloads it. Fixed cache path
#    (FastEmbed's default is a temp dir). fastembed 0.9.0 has no revision
#    argument (it resolves the hub's current snapshot of
#    Qdrant/bge-small-en-v1.5-onnx-Q), so the revision is printed in the build
#    log for the record; observed 2026-10-16: aa8f8b060edb00e03bfdd08813a2949946c8ba55.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed-cache
COPY services/__init__.py ./services/
COPY services/knowledge/__init__.py services/knowledge/embeddings.py ./services/knowledge/
USER appuser
RUN python -c "from services.knowledge.embeddings import warm; warm()" \
    && echo "fastembed model snapshot:" \
    && ls "$FASTEMBED_CACHE_PATH"/models--*bge-small-en-v1.5*/snapshots
# From here on the model comes from the baked cache only (fail closed: a
# missing or corrupt cache is a 503, never a runtime download).
ENV HF_HUB_OFFLINE=1

# 3. Application source. Only services/ is copied: the tool server never
#    imports apps/, so API and web source stay out of this image.
#    .dockerignore still guards nested secrets.
COPY --chown=appuser:appuser services ./services

# Render provides PORT at runtime; default 8100 matches local development
# (services/toolserver README) and .env.example MCP_ALLOWED_HOSTS.
EXPOSE 8100

# /health is readiness: 503 until the model is warm. Loading takes ~40 s at
# 0.1 CPU (Render Free), so the start period allows a slow cold start.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://localhost:{os.environ.get('PORT','8100')}/health\")"

# Shell form so ${PORT} actually expands (Render assigns the port at runtime).
# `exec` preserves signal forwarding to uvicorn.
CMD exec uvicorn services.toolserver.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8100}
