# Architecture decisions

Lightweight ADR log. Each entry: decision, why, consequences. Newest last.
"Verified" means checked against an installed package or official docs during
the bootstrap; it never means an ArmorIQ behavior was tested live.

---

## ADR-001 — Document authority

**Decision.** `ProxyLead_PRD_v2.md` and `ProxyLead_Architecture_v2.1.md` are
the authoritative product and implementation documents.
`ProxyLead_Day1_Experiments.md` defines experiment procedures.

**Context.** The repository also contains an earlier PRD (`ProxyLead-PRD.md`,
Qdrant-based design, different tool names and permission matrix) and
`ProxyLead_Architecture.md` (same content as v2.1 apart from line endings).
At bootstrap time both files were deleted in the working tree but the
deletion was not committed. The bootstrap leaves that uncommitted change
untouched for the team to commit or restore.

**Consequence.** Where the legacy PRD conflicts with PRD v2 / Architecture
v2.1 (storage, tool names, matrix, identity, experiment numbering), v2/v2.1
wins. The legacy PRD remains useful only for source-verified ArmorIQ SDK notes.

## ADR-002 — Neon PostgreSQL + pgvector; no Qdrant

**Decision.** One Neon PostgreSQL database with `pgvector` and PostgreSQL
full-text search for application data and knowledge retrieval. No second
vector store.

**Why.** Architecture v2.1 §7.1 / PRD v2 §8. One database, hybrid retrieval
(vector + FTS with reciprocal rank fusion), and separate Postgres roles for
backend vs tool server.

**Consequence.** No database driver is installed yet. `psycopg` 3, the
`pgvector` adapter, and `fastembed` (model `BAAI/bge-small-en-v1.5`, 384 dims)
are added with the Phase that introduces migrations and retrieval, so the
foundation and CI stay light (`fastembed` pulls in ONNX Runtime).

## ADR-003 — Repository layout *(superseded by ADR-013 on 2026-10-14)*

**Decision (historical).** Use `backend/`, `frontend/`, `mcp-server/`, `contracts/` instead
of the `apps/` + `services/` sketch in PRD v2 §3.1.

**Mapping.**

| PRD v2 §3.1 path | This repository |
|---|---|
| `apps/api/` | `backend/app/` |
| `apps/web/` | `frontend/` |
| `services/slack_socket/` | `backend/app/integrations/slack/` |
| `services/authorization/armoriq_gateway.py` | `backend/app/integrations/armoriq/gateway.py` (the only `armoriq_sdk` importer) |
| `services/toolserver/` | `mcp-server/app/` |
| `services/knowledge/` (retrieval "callable only by tool server") | `mcp-server/app/knowledge/` |
| `services/audit/` | `backend/app/services/audit/` (request/decision/delivery) and `mcp-server/app/ledger` (execution ledger) |
| `scripts/`, `migrations/`, `seed/`, `policy/` | Repository root, added in later phases |

**Why.** Three independently deployable applications with their own
dependency sets; knowledge retrieval physically lives with the only process
allowed to read protected content.

## ADR-004 — Python version

**Decision.** Target Python 3.12 (`requires-python = ">=3.12,<3.14"`, Ruff
`target-version = "py312"`, CI runs 3.12).

**Context.** `armoriq-sdk==0.6.10` declares `<3.14,>=3.10`. The bootstrap
machine had Python 3.13.5 but not 3.12, so local checks during the bootstrap
ran on 3.13.5; CI is the 3.12 reference.

## ADR-005 — Contracts: Pydantic → JSON Schema → TypeScript

**Decision.** Pydantic models in `services/contracts/` (see ADR-013) are the only hand-written
definition. A deterministic JSON Schema is committed; the frontend generates
TypeScript from it with `json-schema-to-typescript`. A Python test and a CI
step fail on drift.

**Why.** Avoid hand-maintained duplicates across three apps.

## ADR-006 — MCP server implementation

**Decision.** Use the official MCP Python SDK, `mcp==2.3.0`. In 2.x the old
`FastMCP` class is `mcp.server.mcpserver.MCPServer` (verified in the installed
package). Two `MCPServer` instances (`memory-mcp`, `workspace-mcp`) are served
with streamable HTTP in **stateless** mode with SSE responses, mounted in one
FastAPI app at `/memory/mcp` and `/workspace/mcp`. Each session manager runs
inside the FastAPI lifespan.

**Verified locally (package source + tests).** Protocol version `2024-11-05`
(the version in ArmorIQ's MCP format doc) is among the SDK's accepted
handshake versions; POST responses are SSE `event: message` frames.

**Not verified.** That ArmorIQ's hosted proxy sends requests this transport
accepts (headers such as `Accept: application/json, text/event-stream`).
This is part of experiment E1.

**Fallback.** If E1 shows an incompatibility, replace the transport with the
minimal hand-written JSON-RPC 2.0 + SSE handler described in ArmorIQ's MCP
format documentation, keeping the same endpoint paths and tool contracts.

## ADR-007 — Pinned dependency versions (resolved 2026-10-14)

| Package | Version | Used by |
|---|---|---|
| fastapi | 0.143.0 | apps/api, services/toolserver |
| uvicorn[standard] | 0.54.0 | apps/api, services/toolserver |
| pydantic | 2.14.1 | services/contracts, apps/api, services/toolserver |
| pydantic-settings | 2.15.0 | apps/api, services/toolserver |
| anthropic | 1.13.0 | apps/api (not yet called) |
| armoriq-sdk | **0.6.10** (required pin) | apps/api (not yet called) |
| slack-bolt | 1.30.0 | apps/api (not yet used) |
| mcp | 2.3.0 | services/toolserver |
| psycopg[binary,pool] | 3.3.6 | apps/api, services/toolserver (P2-01; driver + pool) |
| pgvector | 0.5.1 | apps/api, services/toolserver (P2-01; vector column adapter) |
| pytest | 9.1.1 | all Python |
| httpx | 0.28.1 | tests |
| ruff | 0.17.0 | all Python |
| react / react-dom | 19.3.0 | apps/web |
| vite | 8.3.4 | apps/web |
| typescript | 6.0.3 | apps/web (see ADR-008) |
| vitest | 5.0.4 | apps/web |
| eslint | 10.12.0 | apps/web |

Exact web-app versions live in `apps/web/package.json` and
`apps/web/package-lock.json`. Python pins live in the root `pyproject.toml`.

## ADR-008 — TypeScript 6.0.3, not 7.x

**Decision.** Pin TypeScript 6.0.3. `typescript-eslint` 8.71.2 declares a
peer range of `typescript >=4.8.4 <6.1.0`, so TypeScript 7.0.2 (latest) would
break linting. Revisit when typescript-eslint supports 7.x.

## ADR-009 — Decision-source labels

**Decision.** `DecisionSource` is the union of PRD v2 §10.1 and Architecture
v2.1 §5.3: `armoriq_issuance`, `armoriq_policy_decision`,
`armoriq_sdk_plan_check`, `armoriq_proxy_decision`, `tool_server_rule`,
`application_rule`, `unknown`.

**Why.** The two documents list different but compatible sets
(`armoriq_issuance` only in the PRD, `armoriq_policy_decision` only in the
architecture). Keeping both avoids mislabeling an issuance-time block as an
invoke-time one.

## ADR-010 — MCP endpoint authentication in the scaffold

**Decision.** Each MCP endpoint requires `Authorization: Bearer <token>`
(`TOOLSERVER_BEARER_MEMORY` / `TOOLSERVER_BEARER_WORKSPACE`, constant-time
comparison). If a token is not configured, that endpoint rejects every request
(fail closed). `/health` is public.

**Not yet implemented (Phase 2).** `_ctx` envelope verification,
underscore-key stripping, strict tool schemas, idempotency, the hash-chained
ledger. Which credential path ArmorIQ's proxy uses (console-registered vs
SDK-supplied `X-Armoriq-MCP-Auth`) is decided by E1.

## ADR-011 — Scaffold-only diagnostic tools

**Decision.** Each endpoint exposes one diagnostic tool
(`memory_scaffold_ping`, `workspace_scaffold_ping`) that returns static server
metadata and touches no data. They exist to test the MCP protocol path before
product tools land and can be disabled with `MCP_ENABLE_DIAGNOSTIC_TOOLS=false`.
They are not part of the product tool contract and are not in the ArmorIQ
policy (default-block applies).

## ADR-012 — Slack transport

**Decision.** Slack Bolt for Python with Socket Mode, one workspace
(Architecture v2.1 §4). No HTTP Events endpoint, so no signing-secret
verification endpoint; `SLACK_SIGNING_SECRET` stays commented out in
`.env.example`.

## ADR-013 — Repository layout follows PRD v2 §3.1 / Architecture v2.1 §11

**Decision.** The repository uses exactly the folders both authoritative
documents specify, at the repository root (their `proxylead/` root lists
`.env.example` and `README.md` at the same level):

| Documented path | Content today |
|---|---|
| `apps/api/` | FastAPI app: `main.py`, `routes/` (plus `core/`, `deps.py`, `schemas/`) |
| `apps/web/` | React + TypeScript (Vite) web app |
| `services/authorization/` | Placeholder; `armoriq_gateway.py` will be the only `armoriq_sdk` importer |
| `services/slack_socket/` | Placeholder (Socket Mode adapter) |
| `services/toolserver/` | MCP tool server: `main.py`, `security.py`, `ledger.py` (placeholder) plus `config.py`, `servers.py` |
| `services/knowledge/` | Placeholder (retrieval, callable only by the tool server) |
| `services/audit/` | Placeholder (request/decision/delivery audit) |
| `services/contracts/` | Shared contracts (see below) |
| `tests/` | `tests/api/`, `tests/toolserver/`, `tests/contracts/` |
| `scripts/`, `docs/` | `scripts/check.sh`; project docs |

Files the documents name but that have no content yet (`identity.py`,
`orchestrator.py`, `planner.py`, `responder.py`, `mcp_protocol.py`,
`schemas.py`, `db.py`, tool modules, probe scripts, `migrations/`, `seed/`,
`policy/`, `docs/ARCHITECTURE.md`, `SECURITY.md`, `SLACK_SETUP.md`, `DEMO.md`)
are created in the phase that implements them, not as empty stubs.

**Contracts placement.** Neither document places shared contracts.
`services/contracts/` is used because (a) `services/` is the documented home of
shared server-side modules, (b) both `apps/api` and `services/toolserver` import
it from the same root package tree, and (c) the web app consumes only the
generated JSON Schema (`services/contracts/schemas/`), never Python.

**Python packaging.** One root `pyproject.toml` (project `proxylead`) packages
`apps*` and `services*`. The two separately deployed services keep isolated
dependency sets through extras: `.[api]` (anthropic, armoriq-sdk, slack-bolt)
and `.[toolserver]` (mcp); shared pins are in the base dependencies. CI installs
each extra on its own to prove the isolation. Tests live under one `tests/`
tree with a subfolder per component (the documents list example test files
flat under `tests/`; new cross-cutting tests can go there directly).

**Run commands.** `uvicorn apps.api.main:create_app --factory` and
`uvicorn services.toolserver.main:create_app --factory`, from the repository root.

**Document conflicts noted, not yet relevant.** The PRD names the tool modules
`tools_memory.py` / `tools_workspace.py`; the architecture names them
`tools_access.py` / `tools_work.py` (U-13). The PRD names probes
`probe_v2_e*.py`; the architecture tree uses `probe_e*.py` but its §12 says to
rename them to `probe_v2_*` (U-14).

## ADR-014 — Ruff never touches Markdown

**Decision.** `ruff.toml` excludes `*.md`.

**Why.** Ruff 0.17 formats Python code blocks and line endings inside Markdown.
During the layout change it rewrote the line endings of `ProxyLead_PRD_v2.md`
(an authoritative, read-only document); the file was restored byte-for-byte
before committing, and the exclusion prevents a repeat.

---

ADR-015 to ADR-030 record the planning decisions confirmed by the
project owner on 14 Oct 2026 ("accept all recommendations"; hosting =
Render, with the web app on Vercel per ADR-030). Each maps to a `D-xx` entry in
`docs/Todos/context/DECISIONS_AND_OPEN_QUESTIONS.md`.

## ADR-015 — Parallel work before the build gate (D-19)

**Decision.**
- Backend modules that do not depend on unverified ArmorIQ behavior may be built against fakes before G1/G3: schema, retrieval, planner, responder, Slack adapter, audit store.
- The real ArmorIQ gateway merges into the orchestrator (P3-04) only after G1 (E1).
- UI work (Phase 4) starts only after G1 and G3 (E3) pass or have recorded fallbacks.
- No feature counts as done before its gates.

**Why.** Arch §0.2 and §12 (build gate) against a compressed schedule: no experiment had run by 14 Oct.

## ADR-016 — Hosting platform: Render (D-07)

**Decision.** Render hosts the tool server (P1-05, P5-03), the API, and the web app.

**Amended by ADR-030:** the web app is hosted on Vercel. Render hosts only the API and the tool server.

**Requirements to verify when provisioning** (record the instance type and limits in `deploy/`):
- public HTTPS;
- about 1 GB RAM for the tool server (FastEmbed; PRD §3, §8.2);
- long-running processes that do not sleep when idle, because the API holds the Slack Socket Mode connection and ArmorIQ must reach the tool server;
- secrets only in Render's environment settings;
- the public hostname listed in `MCP_ALLOWED_HOSTS`;
- web and API on the same site where possible, for the session cookie (P1-03).

If an instance type cannot meet these, record the limit; never weaken the authorization boundary to fit.

**Consequence.** P1-05 is no longer blocked on a decision.

## ADR-017 — Probe and evaluation script names (D-02, U-14)

**Decision.** Use the PRD §3.1 names:
- `scripts/probe_v2_e1_allowed.py`
- `scripts/probe_v2_e2_offplan.py`
- `scripts/probe_v2_e3_per_user.py`
- `scripts/probe_v2_e4_hold.py`
- `scripts/probe_v2_e5_setup.py`
- `scripts/eval_e17_grounding.py`
- plus `scripts/check_setup.py` and `scripts/verify_db_grants.py`

The Arch §11 `probe_e*` names are not used.

**Why.** Arch §12 itself directs renaming to `probe_v2_*`, and the PRD names match the v2 experiment numbering.

## ADR-018 — Probes reach ArmorIQ only through `ArmorIQGateway` (D-18)

**Decision.**
- Probe and setup scripts import `services.authorization.armoriq_gateway`, never `armoriq_sdk`.
- A capability a probe needs (for example an off-plan attempt or a name-mismatch call) is added to the gateway (P1-02, or an ICR) instead of importing the SDK in `scripts/`.
- The guardrail test that allows only the gateway to import `armoriq_sdk` is extended to scan `scripts/`.

**Why.** PRD §3 and §9 allow a single `armoriq_sdk` importer. Experiment evidence then covers the production code path.

## ADR-019 — Decision-source labels confirmed (D-03)

**Decision.** Keep the ADR-009 union as the `DecisionSource` set: `armoriq_issuance`, `armoriq_policy_decision`, `armoriq_sdk_plan_check`, `armoriq_proxy_decision`, `tool_server_rule`, `application_rule`, `unknown`.

## ADR-020 — `_ctx` envelope fields (D-04)

**Decision.** `_ctx` carries:
- `request_id`, `trace_id`, `workspace_id`;
- `requester_member_id`, `armoriq_member_email_ref` (the env-key name, for example `PERSONA_SAM_EMAIL`), `app_role`;
- `plan_id`, `step_index`, `tool`;
- `aud`, `iat`, `exp`, `jti`.

It never carries a raw email.

**Why.** The PRD §9 field list does not require the email, and the tool server never uses it. Keeping it out reduces PII in transit and in logs (PRD §4.1). This deliberately deviates from Arch §6.3 ("mapped ArmorIQ email").

## ADR-021 — Member and workspace bootstrap (D-20)

**Decision.**
- The PRD §12 environment variables are the source of truth for the identity mapping.
- The seed job (P2-03, migration role) creates `workspaces` and `members` rows with stable IDs.
- `members.armoriq_member_email_ref` stores the env-key name, never the raw email.
- `apps/api/identity.py` resolves a Slack user ID or web persona to a member ID; the gateway reads the email from the environment.

**Affects:** P1-03, P2-01, P2-03, P2-07, P3-01.

## ADR-022 — Policy file contains placeholders only (D-26)

**Decision.** `policy/proxylead-policy.yaml` keeps `<MAYA_EMAIL>`, `<SAM_EMAIL>`, `<PRIYA_EMAIL>` and `<registered proxylead agent id>`. Real values are substituted only in the ArmorIQ console or a git-ignored local file.

**Why.** The repository is public; PRD §4.1 masks emails.

## ADR-023 — Manager review queue through a metadata-only view (D-12, U-8)

**Decision.** Option (b).
- A metadata-only view, readable by `proxylead_backend`, lists draft records with these columns only: record ID, version, author member ID, created time, review status, content kind. It has **no title and no content**.
- Maya opens a draft's content through the existing `knowledge_get_source_team` tool, which goes through ArmorIQ like any request.
- Maya confirms through `knowledge_confirm_maya_decision`.
- No new tool is added, so the E5 tool list and the policy are unchanged.

**Why.** Arch §7.5 allows splitting non-sensitive metadata from protected content. The backend's denial on content tables (PRD §8.1) is preserved.

**Consequences.**
- P2-01: migration and grant for the view; add the view to the grant check.
- P3-01: the query.
- P4-04: the panels.
- Depends on open decision D-10 (notes stored at team tier) and on D-11 (`get_source` reference shape; RESOLVED 16 Oct 2026: `source_ref` = server-issued `source_id`). Open follow-up: under D-11/D-09, `knowledge_get_source_*` returns only `active` + `authoritative` records (a draft answers `not_found`), so opening a draft's content through `knowledge_get_source_team` needs a decision by P2-09/P4-04 before that flow ships.

## ADR-024 — `create_followup_task` ships only if done by 15 Oct afternoon (D-17)

**Decision.** `create_followup_task` ships only if it is implemented and tested by the afternoon of 15 Oct 2026. Otherwise it is removed from the policy and the UI (Arch §6.2). The P2-10 owner records the outcome in this file.

**Outcome (P2-10, PR #73): ships.**
- `create_followup_task` is implemented and tested on `workspace-mcp` (`services/toolserver/tools_workspace.py`): enum-bounded owner and priority, requester and workspace from the verified `_ctx`, and an idempotency key scoped per workspace and requester. It stays registered, in the policy (P1-08) and in the UI (P4-03).
- The non-networked `send_external_message` honeypot is implemented in the same task. It writes one `egress_sink` row (with `jti` and requester, migration 0012) and has no network code path. The demo uses it as the target of the real ArmorIQ BLOCK (ADR-025, PRD §10.3): the policy blocks it for every persona, so in guarded mode the sink stays empty.

## ADR-025 — Honeypot arguments use a fixed synthetic destination (D-29)

**Decision.**
- `send_external_message` takes a destination restricted to the single enum value `synthetic_sink` plus a bounded message.
- There is no free-form recipient, and no argument key may be in `FORBIDDEN_ARGUMENT_KEYS`. P2-02 defines the exact field names.
- A user-confirmed honeypot plan therefore passes application validation and reaches the ArmorIQ policy, which must BLOCK it for every persona (PRD §10.3, Arch §6.2).

**Why.** This satisfies PRD §7.3 (the model never selects a recipient) without an exemption. It also keeps ArmorIQ, not an application rule, as the deciding layer.

## ADR-026 — ArmorIQ policy evidence sources

**Decision.**
- Add `ARMORIQ_POLICY_SOURCES = {armoriq_issuance, armoriq_policy_decision, armoriq_proxy_decision}` to `services/contracts/enums.py` (P1-02, contract steward review).
- Any claim that ArmorIQ allowed, blocked or held a call uses this set. That covers UI labels, README, `docs/SECURITY.md`, and E3/E5 records.
- A BLOCK or HOLD claim also needs an ArmorIQ evidence reference and proof that the tool server was not reached (P1-08 and P1-09 acceptance).
- `ARMORIQ_DECISION_SOURCES`, which also contains the SDK client-side plan check, stays only for the existing HOLD validator.

**Why.** An SDK plan check, application rule, or tool-server rule must never be reported as an ArmorIQ policy decision (PRD §0.6, Arch §0.7).

## ADR-027 — Tool module file names (D-01, U-13)

**Decision.** Use the PRD §3.1 names:
- `services/toolserver/tools_memory.py`: the `memory-mcp` tools (search, source reads, notes, Maya confirmation, supersession).
- `services/toolserver/tools_workspace.py`: the `workspace-mcp` tools (`send_external_message` honeypot, optional `create_followup_task`).

The Arch §11 names (`tools_access.py`, `tools_work.py`) are not used.

**Why.** The names match the registered endpoint names `memory-mcp` and `workspace-mcp`. Confirmed by the project owner on 14 Oct 2026.

## ADR-028 — Database driver and migrations (D-08)

**Decision.**
- **Driver:** `psycopg` 3 with its connection pool (`psycopg_pool`) and the `pgvector` adapter, pinned in the `api` and `toolserver` extras when P2-01 lands.
- **Migrations:** plain, numbered SQL files in `migrations/` (`0001_*.sql`, `0002_*.sql`, …), applied in order by `scripts/migrate.py` using `DATABASE_URL_MIGRATE`.
  - The migration role is never available to running services (PRD §8.1).
  - Merged migrations are never edited; changes go in a new numbered file.
  - Roles, grants and `REVOKE`s live in these SQL files, so the backend's denial on protected tables is explicit and testable (`scripts/verify_db_grants.py`).

**Why.** Grants stay visible in SQL, there are few dependencies, and it matches ADR-002. No ORM or Alembic for the hackathon. Confirmed by the project owner on 14 Oct 2026.

**Grant design (added in P2-01, #13).** `0005_roles_grants.sql` creates
`proxylead_backend` / `proxylead_toolserver` as NOLOGIN group roles; the
operator creates LOGIN users out-of-band and grants membership (passwords and
DSNs never enter the repo; procedure in `migrations/README.md`). Deny-by
default: every table is revoked from both roles first, then each role gets
exactly its writers' privileges — backend SELECT-denied on all protected
content, approved views, the ledger, and task/egress tables (the PROPOSED
backend SELECTs on non-content ledger columns and the egress count are NOT
granted); ledger and event tables are append-only for every runtime role;
`draft_review_queue` (ADR-023) is backend-SELECT only; no sequence grants
(IDENTITY inserts need none). `statement_timeout`: backend `10s`, toolserver
`30s`, set on the LOGIN users by the operator (group-role settings are not
inherited) — slow queries fail, authorization is never bypassed (Arch §7.1). Vector index: HNSW (`m=16, ef_construction=64`,
cosine); lexical column maintained by trigger with a GIN index. Roles and
grants are expressed in SQL and applied by the migration (owner) role; any
console-only Neon step is recorded in `migrations/README.md` after the first
live run. Every later migration that creates objects grants them in the same
file (no inherited default privileges).


## ADR-029 — Containerization: Docker for local development and Render services

**Decision.** Confirmed by the project owner on 14 Oct 2026 ("go with recommendations"). Supporting infrastructure, not required by PRD v2 or Architecture v2.1.

**Images and services:**
- `deploy/api/Dockerfile` (P1-11) and `deploy/toolserver/Dockerfile` (P1-05): `python:3.12-slim`, non-root.
  - The API image installs only `.[api]`; the tool-server image installs only `.[toolserver]` (the same split as CI).
  - Render runs both as Docker web services. The deployed web app is not a Docker image; Vercel builds and hosts it (ADR-030).
- `docker-compose.yml` (P1-11) runs `api`, `toolserver`, `web` (Vite dev server) and `db`, a local `pgvector/pgvector` Postgres, for development only. Production uses Neon.
- Profile-gated one-off services:
  - `migrate`: the only container given `DATABASE_URL_MIGRATE` (PRD §8.1);
  - `dev`: Python 3.12 + Node 22, for `bash scripts/check.sh`.

**Rules:**
- Secrets come only from `.env` or Render's environment at runtime, never baked into images. `.dockerignore` excludes `.env`, `.venv`, `node_modules`, `research/`.
- Each runtime container gets only its own database DSN.
- Diagnostic MCP tools are off in any Render deployment.

**Why.** One command to run everything, pinned Python and Node versions for every teammate, a free local database for the DB-backed tests, and the same images locally and on Render.

**Not covered.** ArmorIQ still needs the public HTTPS tool server on Render for E1–E5. Real secrets are shared privately. Grant checks must also pass on Neon.

## ADR-030 — Web app hosting: Vercel (amends ADR-016, ADR-029)

**Decision.** Confirmed by the project owner on 14 Oct 2026. The web app (`apps/web`) is deployed on Vercel. Render still hosts the API and the tool server (ADR-016, ADR-029). The database stays on Neon.

**Vercel project** (P5-03 records the settings in `deploy/README.md`):
- Git integration with this repository; the account is held by the project host (`Aditya-Rawat01`).
- Root Directory `apps/web`; framework preset Vite; install `npm ci`; build `npm run build`; output `dist`; Node 22 (from `engines` in `apps/web/package.json`).

**Same-origin API through a Vercel rewrite:**
- `*.vercel.app` and `*.onrender.com` are different sites. A session cookie set by the Render API directly would be a third-party cookie, which Safari blocks by default and other browsers may block or partition.
- So `apps/web/vercel.json` (P5-03) rewrites `/api/:path*` to `https://<render-api-host>/api/:path*`. The browser talks only to the Vercel origin, the cookie stays first-party, and P1-03's `HttpOnly; Secure; SameSite=Lax` cookie works unchanged. Do not switch to `SameSite=None`.
- Vercel builds set `VITE_API_BASE_URL=/`, so the client calls relative `/api/v1/...` paths. Local development keeps `http://localhost:8000` with CORS.
- `CORS_ALLOWED_ORIGINS` on the Render API lists the Vercel production origin plus local development origins; never `*`.
- Only `/api/*` is proxied. Health checks for the API and the tool server go to their Render hosts directly.
- If the app gains client-side routes, `vercel.json` adds a fallback to `/index.html` after the `/api` rule.

**Rules:**
- Vercel holds only `VITE_*` build variables. They are public in the bundle, so no secrets, emails or tokens. Every secret stays in Render's environment.
- Calls through the rewrite stay short. Requests already return `202` and are polled (D-21); no streaming or websockets through the rewrite.
- The Render API remains reachable directly and still enforces the session on every protected route.

**Verify in P5-03 (not assumed):** on the Vercel URL, sign-in sets the cookie on the Vercel host, and reload persistence and sign-out work in Chrome and in Safari (or another WebKit browser). `Set-Cookie` and `Cookie` pass through the rewrite unchanged.

**Why.** Owner choice: zero-configuration Vite builds and per-PR preview deployments. The rewrite avoids third-party cookies without buying a custom domain.

**Not chosen.** Calling the Render API cross-site with `SameSite=None; Secure` (breaks sign-in in Safari), or one custom domain shared by both hosts (needs a domain and DNS setup).

---

## ADR-031 — Web demo session: stdlib HMAC cookie (P1-03)

**Decision.** The web demo session (`POST/GET/DELETE /api/v1/session`) is a compact `base64url(payload).base64url(mac)` cookie signed with `SESSION_SECRET` (HMAC-SHA256, implemented with the standard library in `apps/api/identity.py`). No new dependency (`itsdangerous` and JWT libraries were considered and rejected: one signed opaque value needs neither key management nor claims machinery). Payload is `{persona, exp}` only (12 h lifetime); identity is re-resolved server-side from the member table on every request.

**Flags.** `HttpOnly; SameSite=Lax; Path=/`; `Secure` everywhere except `development`/`test` (local plain-HTTP dev). Works unchanged through the ADR-030 same-origin rewrite; do not switch to `SameSite=None`.

**Known limitations (accepted).** No rate limiting on sign-in beyond a constant-time passcode compare — brute force against the public demo passcode is possible; the passcode protects demo access only, never authorization (every protected read still goes through ArmorIQ per requester). No server-side session revocation list; cookie expiry (12 h) is the only invalidation.

---

## Unresolved questions (decide before the related phase)

| # | Question | Decided by |
|---|---|---|
| U-1 | Does ArmorIQ's hosted proxy reach our MCP endpoint, and with which credential path? | E1 |
| U-2 | Does the proxy's request shape satisfy the MCP SDK's streamable-HTTP transport (Accept header, protocol-version header, stateless mode)? | E1 (fallback: ADR-006) |
| U-3 | Do `_ctx` and other underscore-prefixed arguments arrive at the tool server unchanged through the proxy? | E1 / E2 |
| U-4 | Per-member `toolEnforcement` (ALLOW/HOLD/BLOCK on the same tool) through the hosted proxy | E3 |
| U-5 | HOLD → console approval → SDK resume behavior, approver ranking, where the request appears in the console | E4 |
| U-6 | Exact registry/tool-name representation across the two endpoints; policy schema | E5 |
| U-7 | Per-tool argument schemas, max plan steps, planner `intent` field | Phase 2 design |
| U-8 | Manager review queue needs a read tool (backend cannot read `team_notes`); PRD v2 lists none | **Resolved → ADR-023** |
| U-9 | `knowledge_get_source_{confidential,restricted}` conflict with the one-elevated-call rule after a search | Phase 2 design |
| U-10 | `MIN_SCORE`, score scale, snippet limits | E17 |
| U-11 | Whether approver identity is observable through the pinned SDK | E4 |
| U-12 | Official submission deadline hour and format | Hackathon organizers |
| U-13 | Tool module names: `tools_memory/tools_workspace.py` (PRD) vs `tools_access/tools_work.py` (Architecture) | **Resolved → ADR-027** |
| U-14 | Probe script names: `probe_v2_e*` (PRD, Architecture §12) vs `probe_e*` (Architecture §11 tree) | **Resolved → ADR-017** |
