#!/usr/bin/env bash
# Run every local quality check (mirrors .github/workflows/ci.yml).
# Usage (repo root, Git Bash / macOS / Linux):  bash scripts/check.sh
# Expects backend/.venv, mcp-server/.venv and frontend/node_modules to exist
# (see README "Local setup").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

venv_bin() {
  if [[ -d "$1/.venv/Scripts" ]]; then echo "$1/.venv/Scripts"; else echo "$1/.venv/bin"; fi
}

step() { printf '\n==> %s\n' "$*"; }

BACKEND_BIN="$(venv_bin backend)"
MCP_BIN="$(venv_bin mcp-server)"

step "contracts: lint, schema drift, tests (using the backend venv)"
"$BACKEND_BIN/ruff" check contracts/python
"$BACKEND_BIN/ruff" format --check contracts/python
"$BACKEND_BIN/python" -m proxylead_contracts.schema_export --check
(cd contracts/python && "$ROOT/$BACKEND_BIN/python" -m pytest -q)

step "backend: lint and tests"
(cd backend && "$ROOT/$BACKEND_BIN/ruff" check . && "$ROOT/$BACKEND_BIN/ruff" format --check . \
  && "$ROOT/$BACKEND_BIN/python" -m pytest -q)

step "mcp-server: lint and tests"
(cd mcp-server && "$ROOT/$MCP_BIN/ruff" check . && "$ROOT/$MCP_BIN/ruff" format --check . \
  && "$ROOT/$MCP_BIN/python" -m pytest -q)

step "frontend: generated types, lint, typecheck, tests, build"
(cd frontend \
  && npm run -s gen:types && git diff --exit-code -- src/types/generated \
  && npm run -s lint && npm run -s typecheck && npm test --silent && npm run -s build)

step "all checks passed"
