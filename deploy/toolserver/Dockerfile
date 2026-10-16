# ProxyLead MCP tool server image (P1-05, ADR-016 + ADR-029).
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
# The FastEmbed model is deliberately NOT downloaded here. Retrieval lands
# in P2-05, which adds the build-time model preparation step.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8100

# Hosted default: diagnostic ping tools off (P1-05 §8, M-04). Local compose
# opts in explicitly (docs/DOCKER.md); Render also sets it to false.
ENV MCP_ENABLE_DIAGNOSTIC_TOOLS=false

WORKDIR /app

# Dependency pins live in pyproject.toml (`pip install ".[toolserver]"`).
# Only services/ is copied: the tool server never imports apps/, so API and
# web source stay out of this image. .dockerignore still guards nested secrets.
COPY pyproject.toml ./
COPY services ./services
RUN pip install --no-cache-dir ".[toolserver]"

# Render provides PORT at runtime; default 8100 matches local development
# (services/toolserver README) and .env.example MCP_ALLOWED_HOSTS.
EXPOSE 8100

# Run as a non-root user (ADR-029).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# FastEmbed model cache (P2-05): fixed path (the library default is a temp
# dir, which would re-download on every cold start) baked at build time, so
# the model loads from disk at startup with no network needed.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed-cache
RUN python -c "from services.knowledge.embeddings import warm; warm()"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://localhost:{os.environ.get('PORT','8100')}/health\")"

# Shell form so ${PORT} actually expands (Render assigns the port at runtime).
# `exec` preserves signal forwarding to uvicorn.
CMD exec uvicorn services.toolserver.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8100}
