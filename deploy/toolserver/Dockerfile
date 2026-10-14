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

WORKDIR /app

# Dependency pins live in pyproject.toml (`pip install ".[toolserver]"`).
# Source trees are copied explicitly so the build never depends on what
# .dockerignore lets through.
COPY pyproject.toml ./
COPY apps ./apps
COPY services ./services
RUN pip install --no-cache-dir ".[toolserver]"

# Render provides PORT at runtime; default 8100 matches local development
# (services/toolserver README) and .env.example MCP_ALLOWED_HOSTS.
EXPOSE 8100

# Run as a non-root user (ADR-029).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://localhost:{os.environ.get('PORT','8100')}/health\")"

# Shell form so ${PORT} actually expands (Render assigns the port at runtime).
# `exec` preserves signal forwarding to uvicorn.
CMD exec uvicorn services.toolserver.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8100}
