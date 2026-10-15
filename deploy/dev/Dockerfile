# ProxyLead dev-tools image (P1-11, ADR-029).
#
# Used ONLY to run the repository checks with pinned toolchains:
#   docker compose run --rm dev bash scripts/check.sh
#
# Python 3.12 (base image) + Node 22 (copied from the official node image, no
# apt nodejs packaging involved) + git (check.sh inspects the worktree with
# `git diff`). The project is installed editable into /app/.venv and the web
# dependencies are preinstalled, so check.sh works verbatim. At runtime
# compose bind-mounts the repository over /app with anonymous volumes
# preserving /app/.venv and /app/apps/web/node_modules, so a clean clone
# (no host .venv / node_modules) still passes.
#
# Build from the repository root (the build context):
#   docker build -f deploy/dev/Dockerfile -t proxylead-dev:dev .

# Node toolchain source. Only /usr/local is copied (node, npm, npx and their
# libraries); it does not overlap with the Python install paths.
FROM node:22-slim AS node-stage

FROM python:3.12-slim

COPY --from=node-stage /usr/local /usr/local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /app

# Project metadata + Python sources first for layer caching.
COPY pyproject.toml ./
COPY apps ./apps
COPY services ./services
COPY tests ./tests
COPY scripts ./scripts
COPY apps/web/package.json apps/web/package-lock.json ./apps/web/

# Editable install of all extras check.sh needs, plus the web dependencies.
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -e ".[api,toolserver,dev]" \
    && (cd apps/web && npm ci) \
    && node --version && npm --version \
    && /app/.venv/bin/python --version && /app/.venv/bin/ruff --version

# git for check.sh's `git diff --exit-code` drift check. Debian slim ships
# bash already (check.sh's shebang).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user (ADR-029). The bind-mounted worktree is root-owned,
# so git needs the safe.directory exemption for this exact path.
RUN useradd --create-home --shell /bin/bash devuser \
    && chown -R devuser:devuser /app
USER devuser
RUN git config --global --add safe.directory /app

CMD ["bash", "scripts/check.sh"]
