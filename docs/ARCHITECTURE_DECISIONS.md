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
| U-8 | Manager review queue needs a read tool (backend cannot read `team_notes`); PRD v2 lists none | Phase 2 design |
| U-9 | `knowledge_get_source_{confidential,restricted}` conflict with the one-elevated-call rule after a search | Phase 2 design |
| U-10 | `MIN_SCORE`, score scale, snippet limits | E17 |
| U-11 | Whether approver identity is observable through the pinned SDK | E4 |
| U-12 | Official submission deadline hour and format | Hackathon organizers |
| U-13 | Tool module names: `tools_memory/tools_workspace.py` (PRD) vs `tools_access/tools_work.py` (Architecture) | Before Phase 2 tool work |
| U-14 | Probe script names: `probe_v2_e*` (PRD, Architecture §12) vs `probe_e*` (Architecture §11 tree) | Before writing probes |
