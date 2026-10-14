# ProxyLead API image (P1-11, ADR-029).
#
# Build from the repository root (the build context):
#   docker build -f deploy/api/Dockerfile -t proxylead-api:dev .
#
# Render runs this image for the API (ADR-029; Vercel hosts the web app and
# reaches the API through its /api rewrite, ADR-030). Compose uses it for the
# local `api` service. Installs ONLY the `api` extra — never the `toolserver`
# extra (CI isolation parity: .github/workflows/ci.yml `api` job). No
# secrets, `.env` files, credentials, or tokens are baked into any layer;
# every secret arrives as a runtime environment variable.
#
# The API never receives DATABASE_URL_TOOLSERVER or DATABASE_URL_MIGRATE
# (PRD §8.1 role separation); that is enforced in docker-compose.yml, where
# each service gets an explicit environment mapping (never a wholesale
# `env_file: .env`, which would leak every variable into every service).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# Dependency pins live in pyproject.toml (`pip install ".[api]"`).
# Source trees are copied explicitly so the build never depends on what
# .dockerignore lets through. `services/` is included because the API imports
# the shared contracts (and later the authorization helpers).
COPY pyproject.toml ./
COPY apps ./apps
COPY services ./services
RUN pip install --no-cache-dir ".[api]"

# Render provides PORT at runtime; default 8000 matches local development.
EXPOSE 8000

# Run as a non-root user (ADR-029).
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://localhost:{os.environ.get('PORT','8000')}/health\")"

# Shell form so ${PORT} actually expands (Render assigns the port at runtime).
# `exec` preserves signal forwarding to uvicorn. Compose overrides this with
# `--reload` for local development (see docker-compose.yml).
CMD exec uvicorn apps.api.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}
