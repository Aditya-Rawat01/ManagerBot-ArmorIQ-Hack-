# Implementation contract

How the three applications divide responsibility and exchange data. This file
operationalizes `ProxyLead_PRD_v2.md` and `ProxyLead_Architecture_v2.1.md`
(whose folder layout this repository follows, ADR-013); where they disagree, those documents win and this file
must be updated.

## 1. Applications and ownership boundaries

| Application | Owns | Must never |
|---|---|---|
| `apps/api/` (FastAPI) | Trusted requester identity (Slack user ID / web session → member), request & trace IDs, planner/responder calls, plan validation, the ArmorIQ gateway, signing `_ctx`, delivery routing (thread vs DM), audit events for request/decision/delivery | Query protected knowledge tables or approved views; import `armoriq_sdk` outside `services/authorization/armoriq_gateway.py`; accept identity, tier, or recipient from chat text or request bodies |
| `services/toolserver/` (MCP tool server) + `services/knowledge/` | `memory-mcp` and `workspace-mcp` endpoints, service-credential check, `_ctx` verification, strict argument schemas, hard-coded tier filters, knowledge retrieval, idempotency, hash-chained execution ledger | Import `armoriq_sdk`; trust underscore-prefixed fields as approval; perform real network egress |
| `apps/web/` (React + TS) | Web playground and manager console UI; rendering of real, persisted outcomes | Hold secrets; send a persona/role in chat payloads; show a simulated approval or a predicted outcome as real |
| `services/contracts/` | Data shapes shared by all three | Contain behavior beyond shape validation |

## 2. Data exchange paths

```text
Browser ──HTTPS JSON /api/v1──▶ backend          (contracts: HealthResponse, ErrorResponse, …)
Slack   ──Socket Mode (Bolt)──▶ backend          (identity from Slack user ID mapping only)
backend ──armoriq-sdk 0.6.10──▶ ArmorIQ ──MCP JSON-RPC 2.0 + SSE──▶ mcp-server
mcp-server ──SQL (toolserver role)──▶ Neon PostgreSQL + pgvector
backend ──SQL (backend role: app metadata only)──▶ Neon PostgreSQL
backend ──Slack Web API──▶ Slack (thread reply or DM; chosen by backend, never by the model)
```

- **Frontend ↔ backend:** REST under `/api/v1`, JSON bodies typed by the
  generated contracts. Every error uses the `ErrorResponse` envelope
  `{"error": {"code", "message", "request_id", "details"}}`. Every response
  carries an `X-Request-ID` header.
- **Backend → MCP server:** only through ArmorIQ (`ArmorIQGateway`). Tool
  arguments carry a backend-signed `_ctx` identity envelope (Phase 2). The
  MCP server returns tool results as JSON text content per the ArmorIQ MCP
  format; result payloads use contract models such as `KnowledgeSearchResult`.
- **Direct backend → MCP server calls** are allowed only for health checks.

## 3. Contracts

Source of truth: `services/contracts/` (see `services/contracts/README.md`
for the generation pipeline).

| Contract | Status | Notes |
|---|---|---|
| `AuthenticatedUser` | Established | No raw email; masked only |
| `RequestContext` | Established | Slack refs required for Slack entry points, forbidden for web |
| `PlannedToolCall` | Partially established | Endpoint must match tool; forbidden/underscore argument keys rejected. **Per-tool argument schemas: UNRESOLVED** |
| `PlanProposal` | Partially established | Sequential steps; ≤1 elevated-tier call. **Max steps, `intent`, clarifying question: UNRESOLVED** |
| `ArmorIQDecision` | Established shape | SDK plan check ⇒ BLOCK only; HOLD only from ArmorIQ sources. Mapping from real SDK objects is defined by experiments E1–E4 |
| `ApprovalStatus` | Established shape | **Approver identity observability: UNRESOLVED** |
| `ToolInvocationResult` | Established | EXECUTED requires ledger evidence |
| `KnowledgeSearchResult` / `KnowledgeSearchHit` / `SourceCitation` | Partially established | Tier fixed by tool name. **Score scale, snippet limits, `min_score`: UNRESOLVED (E17)** |
| `AuditEvent` | Provisional | Event-type list will grow in Phase 2 |
| `HealthResponse`, `ErrorResponse` | Established | Used by the scaffold today |

## 4. Fixed names (must match the ArmorIQ console exactly — verify in E5)

- Agent: `proxylead`
- MCP endpoints: `memory-mcp` (`/memory/mcp`), `workspace-mcp` (`/workspace/mcp`)
- Tools: see `ToolName` in `services/contracts/enums.py`
  (14 names from PRD v2 §6).

## 5. Security guardrails (apply from the first line of feature code)

1. Protected reads and actions go through the ArmorIQ gateway and the MCP tool server.
2. The backend owns trusted identity and response routing.
3. User-supplied identity, data tier, or permission level is never an authorization control.
4. Tool names plus trusted server-side code enforce the data tier.
5. Confidential/restricted answers are delivered privately (DM / private web session) only; a failed DM never falls back to a channel.
6. Fail closed when authorization cannot be verified.
7. Approval uses the real ArmorIQ console flow; no fake approval button.
8. Retrieved content is untrusted data, never instructions.
9. Credentials stay out of logs and source control.
10. No security control is described as working until a real test shows it.

## 6. Bootstrap scope (Phase 0 foundation)

- Backend: `GET /health`, `GET /api/v1/health`, error envelope, request IDs, env-based config, CORS.
- Frontend: shell UI with a typed backend health check (loading/success/error).
- MCP server: `/health`, two bearer-protected MCP endpoints with scaffold-only diagnostic ping tools.
- No ArmorIQ call, Slack handling, LLM call, database access, or product tool exists yet.
