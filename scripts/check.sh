#!/usr/bin/env bash
# Run every local quality check (mirrors .github/workflows/ci.yml).
# Usage (repo root, Git Bash / macOS / Linux):  bash scripts/check.sh
# Expects the root .venv (pip install -e ".[api,toolserver,dev]") and
# apps/web/node_modules (npm ci) to exist; see README "Local setup".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -d .venv/Scripts ]]; then BIN=".venv/Scripts"; else BIN=".venv/bin"; fi

step() { printf '\n==> %s\n' "$*"; }

step "python: ruff lint + format (whole repository)"
"$BIN/ruff" check .
"$BIN/ruff" format --check .

step "services/contracts: JSON Schema drift"
"$BIN/python" -m services.contracts.schema_export --check

step "python tests: tests/contracts, tests/api, tests/toolserver"
"$BIN/python" -m pytest -q

step "apps/web: generated types, lint, typecheck, tests, build"
(cd apps/web \
  && npm run -s gen:types && git diff --exit-code -- src/types/generated \
  && npm run -s lint && npm run -s typecheck && npm test --silent && npm run -s build)

step "all checks passed"
