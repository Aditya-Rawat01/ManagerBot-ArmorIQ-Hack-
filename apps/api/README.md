# ProxyLead API (`apps/api`)

FastAPI application. Owns trusted requester identity, request orchestration,
the ArmorIQ gateway (`services/authorization`), and response/delivery routing.
It must never query protected knowledge tables directly; protected reads go
through ArmorIQ and the MCP tool server (`docs/IMPLEMENTATION_CONTRACT.md`).

**Foundation status:** health endpoints, configuration, error envelope,
request IDs and CORS only. No ArmorIQ, Slack, LLM, or database code yet.

## Layout

```text
apps/api/
  main.py            create_app() factory (no module-level app: imports stay side-effect free)
  routes/            health.py (/health, /api/v1/health); router.py mounts /api/v1
  deps.py            shared FastAPI dependencies
  core/              config (env), logging, error envelope, request-ID middleware
  schemas/           HTTP schemas; shared shapes re-exported from services.contracts
tests/api/           pytest suite (repository-level tests/ tree)
```

Documented files that land with their phase: `identity.py`,
`orchestrator.py`, `planner.py`, `responder.py`. Related shared modules:
`services/authorization/` (ArmorIQ gateway; the only `armoriq_sdk` importer,
test-enforced), `services/slack_socket/`, `services/audit/`.

## Setup

From the repository root (Python 3.12; 3.13 also works locally):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[api,dev]"   # macOS/Linux: .venv/bin/python
```

Configuration comes from environment variables and an optional `.env` at the
repository root (copy `.env.example`). For API-only settings, copy
`apps/api/.env.example` to `apps/api/.env`; it overrides the root `.env`, and
real environment variables override both. No secret is required to start the
app or run the tests.

## Run

```bash
uvicorn apps.api.main:create_app --factory --reload --port 8000
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/health
```

Interactive docs: http://localhost:8000/docs (disabled when `APP_ENV=production`).

## Checks

```bash
ruff check . && ruff format --check .
python -m pytest tests/api
```

## Conventions

- Every non-2xx JSON response: `{"error": {"code", "message", "request_id", "details"}}`.
- Every response carries `X-Request-ID` (a well-formed incoming value is reused).
- Validation errors never echo submitted input values; 500s never expose internals.
- `CORS_ALLOWED_ORIGINS` must list explicit origins (`*` is rejected).
- Startup enforces `TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000` (PRD v2 §7.1).
