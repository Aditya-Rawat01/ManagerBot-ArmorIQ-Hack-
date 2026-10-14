# P1-11 — Docker dev environment and deployable images

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ (supporting infrastructure, Wave 1) |
| Owner | `parthgoyal26-03-2005` |
| Size | M (2–3 h) |
| Target window | 14–15 Oct (merges right after P1-05) |
| Gate role | Supporting infrastructure; CP-1 checks it |
| Status | Not started |

## 1. Task ID and title

P1-11 — Add a Docker Compose development environment (API, tool server, web, local Postgres + pgvector, one-off migration runner, dev-tools container) and the API Docker image used on Render (ADR-029).

## 2. Purpose and expected outcome

Teammates should not need exact local Python/Node versions or a shared Neon account to build and test. Outcome:

- `docker compose up --build` starts the API (8000), tool server (8100), web dev server (5173) and a local Postgres with `pgvector` (5432);
- `docker compose run --rm dev bash scripts/check.sh` runs the same checks as CI with Python 3.12 and Node 22;
- `docker compose run --rm migrate` gives the migration step its own container, the only one with the migration DSN (it becomes useful once P2-01 adds `scripts/migrate.py`);
- `deploy/api/Dockerfile` is the image Render runs for the API (ADR-029).

The tool server image itself comes from P1-05 (`deploy/toolserver/Dockerfile`); this task reuses it.

## 3. Authoritative source references

Docker is **supporting infrastructure**. Neither PRD v2 nor Architecture v2.1 requires it; the decision is recorded in ADR-029. It must respect these documented constraints:

- PRD §3 (separately deployed tool server; about 1 GB RAM for FastEmbed), §8.1 (migration/seed role unavailable to runtime processes), §9 (public HTTPS MCP endpoint), §12 (environment variables; never expose secrets).
- Arch §2 (deployment shape: one backend, one separately deployed tool server, one web app), §7.1 (Neon + `pgvector`), §7.5 (separate database roles), §17 (`.env.example` placeholders only; never commit `.env`).
- ADR-016 (Render), ADR-028 (`psycopg` 3 + SQL migrations), ADR-029 (containerization).

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005` (deployment steward).
- Contributors: `Aditya-Rawat01` (Render host; tool-server image from P1-05).
- Reviewer: `samarth-chawla` (secrets and least-privilege checks).

## 5. Prerequisites and dependencies

- Hard: P1-05 (`deploy/toolserver/Dockerfile` exists and is used by compose).
- Decisions: D-07 resolved (Render, ADR-016); containerization decided (ADR-029).
- Blocks: none hard. Every later task can use the local database for DSN-based tests (P2-01, P2-03, P2-07, P3-01).

## 6. Exact files/modules this task owns

- `docker-compose.yml` (new, repository root): services `api`, `toolserver`, `web`, `db`, `migrate`, `dev`
- `deploy/api/Dockerfile` (new)
- `deploy/web/Dockerfile` (new; local development only; Render serves the web app as a static site)
- `deploy/dev/Dockerfile` (new; Python 3.12 + Node 22 tools image for `scripts/check.sh`)
- `.dockerignore` (new, repository root)
- `docs/DOCKER.md` (new): how to run, environment, security rules, troubleshooting

## 7. Existing files it may need to modify

- `.env.example` (steward: this owner): add commented local-compose values only, for example a `postgresql://…@db:5432/…` placeholder and `MCP_ALLOWED_HOSTS` including `toolserver:8100`. No secrets.
- `README.md` (steward: this owner): a short "Run with Docker" pointer to `docs/DOCKER.md`.
- `deploy/toolserver/Dockerfile` (P1-05 owner): only through that owner if compose needs a small change.

## 8. Shared interfaces and contracts

- Run commands, unchanged from the scaffold: `uvicorn apps.api.main:create_app --factory`, `uvicorn services.toolserver.main:create_app --factory`, `npm run dev` / `npm run build` in `apps/web`.
- Environment variable names come from PRD §12 / `.env.example`. Containers read them from `.env` at runtime (`env_file:`), never at image build time. The exception is `VITE_API_BASE_URL`, which Vite bakes in at build time and must contain no secret.
- Ports: 8000 (API), 8100 (tool server), 5173 (web), 5432 (local Postgres).

## 9. Step-by-step implementation requirements

1. **`.dockerignore`.** Exclude `.env`, `.env.*` (keep `.env.example`), `.venv/`, `**/node_modules/`, `apps/web/dist/`, `**/__pycache__/`, `.git/`, `research/`, `FlowState-ArmorIQ-Decision-Report.md`.
2. **`deploy/api/Dockerfile`.** `python:3.12-slim`; copy `pyproject.toml`, `apps/`, `services/`; `pip install ".[api]"` (never the `toolserver` extra); create and switch to a non-root user; `CMD uvicorn apps.api.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}`.
3. **`deploy/web/Dockerfile`** (development). `node:22` image; `npm ci` in `apps/web`; run `npm run dev -- --host 0.0.0.0`. File watching through a bind mount may need polling on Windows; document it.
4. **`deploy/dev/Dockerfile`.** Python 3.12 + Node 22; `pip install -e ".[api,toolserver,dev]"` and `npm ci`. It is used only to run checks: `docker compose run --rm dev bash scripts/check.sh`.
5. **`docker-compose.yml`:**
   - `db`: a `pgvector/pgvector` Postgres image, a named volume, and local-only credentials from `.env`.
   - `api`: build `deploy/api/Dockerfile`; `env_file: .env`; bind-mount the source with `--reload` for development; depends on `db`.
   - `toolserver`: build `deploy/toolserver/Dockerfile` (P1-05); `env_file: .env`; `MCP_ALLOWED_HOSTS` includes `toolserver:8100` and `localhost:8100`; `MCP_ENABLE_DIAGNOSTIC_TOOLS` may be `true` locally only.
   - `web`: build `deploy/web/Dockerfile`; `VITE_API_BASE_URL=http://localhost:8000`.
   - `migrate`: reuses the tool-server image; it is the **only** service given `DATABASE_URL_MIGRATE`; profile-gated so `up` does not start it. Its command is `python scripts/migrate.py` once P2-01 adds that script.
   - `dev`: build `deploy/dev/Dockerfile`; profile-gated; bind-mounts the repository.
6. **Least privilege.**
   - `api` never receives `DATABASE_URL_TOOLSERVER` or `DATABASE_URL_MIGRATE`.
   - `toolserver` never receives `DATABASE_URL_BACKEND` or `DATABASE_URL_MIGRATE`.
   - No secret is baked into any image.
   - Images run as non-root.
7. **`docs/DOCKER.md`:**
   - prerequisites (Docker Desktop on Windows/macOS);
   - first run and the daily commands;
   - how to point at Neon instead of the local `db`;
   - what Docker does **not** cover: ArmorIQ needs the public HTTPS tool server on Render; real secrets are shared privately; local Postgres is not Neon, so grant checks must also pass on Neon.
8. Verify the commands in §12 on a clean clone.

## 10. Acceptance criteria

- On a clean clone with `cp .env.example .env`, `docker compose up --build` starts all four services. `GET http://localhost:8000/health` and `GET http://localhost:8100/health` return 200, and `http://localhost:5173` shows the web app with the API health indicator.
- `docker compose run --rm dev bash scripts/check.sh` ends with "all checks passed".
- `docker compose config` shows `DATABASE_URL_MIGRATE` only on `migrate`, and no backend/toolserver DSN cross-over.
- `docker run --rm <api image> pip show mcp` fails (not installed), and the tool-server image has no `armoriq-sdk`.
- `docker history` / `docker inspect` of each image shows no secret values, and the images run as a non-root user.
- `.dockerignore` keeps `.env`, `.venv`, `node_modules` and `research/` out of the build context (check with a test build).
- `docs/DOCKER.md` exists and is linked from `README.md`.

## 11. Required unit, integration, and security tests

- Manual verification recorded in the PR handoff report: the commands in §12 with their result lines.
- Security checks: the DSN separation from `docker compose config`; no secrets in images; non-root user; per-image dependency split.
- No new automated test is required. `scripts/check.sh` and CI stay unchanged.

## 12. Commands for verification

```bash
cp .env.example .env
docker compose config                                  # inspect env per service
docker compose up --build -d
curl -fsS http://localhost:8000/health && curl -fsS http://localhost:8100/health
docker compose run --rm dev bash scripts/check.sh
docker compose run --rm api pip show mcp || echo "mcp not in api image (expected)"
docker compose run --rm toolserver pip show armoriq-sdk || echo "armoriq-sdk not in toolserver image (expected)"
docker compose down
```

## 13. Explicit non-goals

- No Kubernetes, no CI image publishing, no Docker-based CI job.
- No production database in Docker (production uses Neon).
- No change to application code, `scripts/check.sh` or `.github/workflows/ci.yml`.
- Not the tool-server image (P1-05) and not the final Render configuration (P5-03).

## 14. Risks and unresolved questions

- Windows bind-mount performance and file watching (Vite polling, uvicorn reload). Document the workaround.
- The local `pgvector` Postgres version may differ from Neon's. Pin a version close to Neon's; grant checks must still pass on Neon (P2-01, CP-2).
- The deadline is 17 Oct. Keep the scope minimal; do not delay Wave 1 security work.

## 15. Handoff deliverables for dependent agents

- All teammates: the `docs/DOCKER.md` quick start, plus the local DSN pattern for DB-backed tests.
- P2-01: `docker compose run --rm migrate` wiring ready for `scripts/migrate.py`.
- P5-03: `deploy/api/Dockerfile` for the Render API service.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-11] Add Docker Compose dev environment and API image (ADR-029)`
- **Body outline:** services and ports; least-privilege environment rules; files; verification commands; non-goals.
- **Labels:** `phase-1-security`, `deployment`
- **Depends on:** P1-05  **Blocks:** — (soft: P2-01, P5-03)

## 17. Definition of done

Compose environment and API image merged; the §12 commands verified on a clean clone and recorded in the PR; `docs/DOCKER.md` linked from the README; CP-1's Docker check passes.
