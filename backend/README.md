# ProxyLead backend

FastAPI application. Owns trusted requester identity, request orchestration,
the ArmorIQ gateway, and response/delivery routing. It must never query
protected knowledge tables directly; protected reads go through ArmorIQ and
the MCP tool server (`docs/IMPLEMENTATION_CONTRACT.md`).

**Foundation status:** health endpoints, configuration, error envelope,
request IDs and CORS only. No ArmorIQ, Slack, LLM, or database code yet.

## Layout

```text
app/
  main.py            create_app() factory (no module-level app: imports stay side-effect free)
  api/               routers only; /health and /api/v1/health
  core/              config (env), logging, error envelope, request-ID middleware
  schemas/           HTTP schemas; shared shapes re-exported from proxylead_contracts
  models/            backend-owned persistence models (later phase)
  services/          business logic (later phase)
  integrations/
    armoriq/         gateway.py will be the ONLY armoriq_sdk importer (test-enforced)
    slack/           Bolt Socket Mode adapter (later phase)
    llm/             Anthropic planner/responder adapter (later phase)
tests/               pytest suite
```

## Setup

Requires Python 3.12 (3.13 also works locally; CI uses 3.12).

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate   macOS/Linux: source .venv/bin/activate
python -m pip install -e ../contracts/python -e ".[dev]"
```

Configuration comes from environment variables and an optional `.env` at the
repository root (copy `../.env.example`). No secret is required to start the
app or run the tests.

## Run

```bash
uvicorn app.main:create_app --factory --reload --port 8000
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/health
```

Interactive docs: http://localhost:8000/docs (disabled when `APP_ENV=production`).

## Checks

```bash
ruff check . && ruff format --check .
python -m pytest
```

## Conventions

- Every non-2xx JSON response: `{"error": {"code", "message", "request_id", "details"}}`.
- Every response carries `X-Request-ID` (a well-formed incoming value is reused).
- Validation errors never echo submitted input values; 500s never expose internals.
- `CORS_ALLOWED_ORIGINS` must list explicit origins (`*` is rejected).
- Startup enforces `TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000` (PRD v2 §7.1).
