# Project context

Read this first. It summarizes what ProxyLead is, what must ship, and what
already exists in the repository. It is a digest, not a new requirement
source.

## 1. Authority

| Document | Role |
|---|---|
| `ProxyLead_PRD_v2.md` (PRD) | **Authoritative** product requirements |
| `ProxyLead_Architecture_v2.1.md` (Arch) | **Authoritative** architecture and implementation contract |
| `ProxyLead_Day1_Experiments.md` | Experiment procedures (referenced by both authoritative docs; not a requirement source) |
| `docs/IMPLEMENTATION_CONTRACT.md`, `docs/ARCHITECTURE_DECISIONS.md`, `services/contracts/` | Current scaffold state. They describe what exists and must not silently override PRD/Arch |

Not requirement sources: the legacy PRD (`ProxyLead-PRD.md`, deleted in the
working tree), `research/`, `FlowState-ArmorIQ-Decision-Report.md`, and
earlier conversations.

Section references in this plan use the form `PRD §7` and `Arch §5.6`.
Where the two documents conflict or leave something open, the plan records it in
[DECISIONS_AND_OPEN_QUESTIONS.md](DECISIONS_AND_OPEN_QUESTIONS.md) and does not
pick an answer silently.

## 2. Product in one paragraph

ProxyLead is a shared AI representative of a busy engineering manager (PRD §1,
Arch §1). Team members ask, in Slack or a web demo, what was decided and why,
retrieve team knowledge they are authorized to see, save notes
(`@ProxyLead store this ...`), and request a small follow-up task. Every
protected read or action is a tier-named MCP tool call authorized **per
requester** by ArmorIQ and re-checked by the tool server. The assistant never
inherits the manager's blanket authority.

## 3. Personas and permission matrix (PRD §1, §5.1; Arch §1, §3.1)

| Tool tier | Maya (manager, ArmorIQ Org Admin) | Sam (senior) | Priya (junior) |
|---|---|---|---|
| Team | ALLOW | ALLOW | ALLOW |
| Confidential | ALLOW | HOLD | BLOCK |
| Restricted | ALLOW | BLOCK | BLOCK |
| PII | Not implemented | Not implemented | Not implemented |
| `send_external_message` (honeypot) | BLOCK | BLOCK | BLOCK |

Personas are demo labels. Identity is the trusted mapping
Slack user ID / web session → member → invited ArmorIQ email (PRD §4.1).

## 4. Observable outcomes (Arch §1)

| Outcome | Meaning |
|---|---|
| ALLOW | Real authorization and server checks passed; the tool executed and the UI reports the actual result |
| BLOCK | A real ArmorIQ decision or a named application/tool-server rule denied it; no protected data disclosed |
| HOLD | ArmorIQ created a real delegation request; nothing is returned until the approval completes in the ArmorIQ console |
| ABSTAIN | No reliable, conflicting, or superseded evidence; answer with uncertainty |
| ERROR / UNVERIFIED | Authorization or infrastructure could not be verified; never converted into ALLOW |

## 5. MVP scope

**Must ship (PRD §2 P0, Arch §3):** Slack Socket Mode bot (one workspace);
trusted identity mapping; mentions, DMs and `store this`; web demo on the same
path; Neon PostgreSQL + `pgvector` + full-text search; local FastEmbed
`BAAI/bge-small-en-v1.5` (384 dims); tier-specific tools with fixed server-side
filters; one agent, one policy, two MCP endpoints (`memory-mcp`,
`workspace-mcp`), three invited members; real ALLOW/BLOCK/HOLD with console
approval; hash-chained append-only execution ledger; citation validation,
calibrated abstention, unanswered-question queue, topic map, manager
confirmation of decisions; "Simulate hijacked model" toggle with a
`send_external_message` honeypot; public demo and a video of 3:00 or less.

**Stretch, only after all P0 security tests pass (PRD §2, Arch §3; Arch adds
"by 16 October noon"):** minimal Neon connection setup page; Google Calendar
read-only. **This plan creates no tasks for stretch items.**

**Not in MVP (PRD §2, Arch §3):** Gmail, Drive, Meet, external email/message
delivery, real egress, production deploys, ticket providers, PII, OAuth
marketplace, multi-workspace Slack, Slack history search, arbitrary SQL, in-app
approval, automatic ingestion, SSO/SCIM, the legacy 13-tool matrix.

## 6. Components (PRD §3, §3.1; Arch §2, §11)

```text
Slack (Socket Mode) ─┐
Web demo / console ──┴─▶ apps/api (FastAPI): identity → planner → orchestrator
                          │      → ArmorIQGateway (services/authorization; only armoriq_sdk importer)
                          │      → responder → delivery routing → audit (services/audit)
                          ▼
                      ArmorIQ (plan, intent token, policy, HOLD/console approval)
                          ▼
              services/toolserver (public HTTPS MCP: memory-mcp, workspace-mcp)
                 security (_ctx, credential) → strict schemas → tier-fixed handlers
                 → services/knowledge (retrieval; callable only by tool server)
                 → hash-chained execution ledger
                          ▼
              Neon PostgreSQL + pgvector (roles: proxylead_backend, proxylead_toolserver, migration)
```

Request lifecycle: PRD §7 (14 steps) and Arch §8 (14 steps). Summary: the
backend authenticates the requester and creates a request/trace ID. The planner
then sees only the user's message and trusted context. Trusted code validates
the plan. The orchestrator creates **one ArmorIQ plan per request**, mints once,
and invokes the steps in order. The tool server re-checks everything. The
responder treats results as untrusted data and validates citations. Delivery
tier is the highest tier of any data returned to the model. Confidential and
restricted content goes to a DM or private web session only. Every transition
is audited.

## 7. What already exists (scaffold at commit `5e97604`)

These are **done** and are not tasks in this plan. Tasks may extend them.

| Area | Existing | Files |
|---|---|---|
| API foundation | `create_app` factory, `/health`, `/api/v1/health`, error envelope, `X-Request-ID` middleware, CORS (no `*`), settings for the PRD §12 variables each service uses (not yet `EMBED_MODEL` or `MIN_SCORE`: P2-05 and P3-03 add them), startup assertion `TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000` | `apps/api/main.py`, `apps/api/core/*`, `apps/api/routes/*` |
| Contracts | Pydantic models, enums, `TOOL_CATALOG` (14 tools to endpoints and tiers), `FORBIDDEN_ARGUMENT_KEYS`, deterministic JSON Schema, generated TypeScript, drift checks | `services/contracts/*`, `apps/web/src/types/generated/contracts.ts` |
| Tool server transport | Two `MCPServer` endpoints (`/memory/mcp`, `/workspace/mcp`), stateless streamable HTTP with SSE, bearer guard (fail closed if unset), `/health`, diagnostic ping tools (not product tools) | `services/toolserver/*` |
| Web shell | React/Vite app with typed API client, health check, placeholder panels | `apps/web/src/*` |
| Guardrail tests | `armoriq_sdk` imported only by `services/authorization/armoriq_gateway.py`; toolserver never imports it; dependency pins | `tests/api/test_guardrails.py`, `tests/toolserver/test_config_and_guardrails.py` |
| CI / local checks | Four jobs (contracts, api, toolserver, web) with per-extra isolation; `scripts/check.sh` | `.github/workflows/ci.yml`, `scripts/check.sh` |
| Docs | Implementation contract, ADR-001..014, open questions U-1..U-14, experiment log (all **NOT RUN**) | `docs/*.md` |

**Does not exist yet:** ArmorIQ gateway, identity, planner, responder,
orchestrator, Slack adapter, delivery, `_ctx`, database code, migrations, seed,
retrieval, product tools, ledger, policy file, probes, manager console, and any
live experiment evidence.

## 8. Schedule and current status

| Date (Oct 2026) | Documented plan (PRD §13, Arch §14) | Status on 14 Oct |
|---|---|---|
| 13 | Day 1: run E1–E5, register agent/endpoints/policy/members | **Not done**: no experiment has run (`docs/EXPERIMENTS.md`) |
| 14 | Vertical slice: schema, retrieval, Slack, one team-tier question end to end | Scaffold only |
| 15 | Matrix, honeypot, topic map, HOLD, manager view, minimal web demo | — |
| 16 | E17, security tests by noon, then feature freeze, deploy, rehearse 3× | — |
| 17 | Critical fixes only; submit before the official deadline (hour unverified, D-24) | — |

The plan compresses the remaining days. See the critical path in
[../README.md](../README.md). If a gate fails, the documented rule applies: at
most 2 focused hours of diagnosis, then the fallback and narrower claims
(Arch §12).

## 9. Success criteria (PRD §1.1)

| ID | Pass condition (abridged) | Proven by |
|---|---|---|
| SC-01 | Matrix outcomes match in live ArmorIQ tests; ledger shows execution only on ALLOW | P1-09, P5-02 |
| SC-02 | 10 compromised-model + 10 natural runs: zero executed `send_external_message`; enforcement layer recorded | P3-07, P5-02 |
| SC-03 | Message claiming to be Maya changes neither identity nor ArmorIQ attribution | P1-03, P5-02 |
| SC-04 | Real HOLD approved/rejected in console; only approved resumes under the original plan | P1-10 |
| SC-05 | Every citation comes from an authorized tool call in the same request; E17 measures abstention | P3-03, P3-08 |
| SC-06 | Every UI status maps to a persisted event and, where execution is claimed, a ledger row | P2-07, P3-01, P5-01 |
| SC-07 | Recorded demo ≤ 3:00 | P5-04 |

## 10. Glossary

| Term | Meaning |
|---|---|
| Plan | One ArmorIQ plan per user request, holding the ordered tool steps (PRD §0.2) |
| Step | One planned tool call (`PlannedToolCall`, indexed `step_index`) |
| `jti` | Application-generated unique invocation ID; **not** an ArmorIQ token claim (Arch §5.1) |
| `_ctx` | Backend-signed identity envelope passed in tool arguments; the tool server verifies it (PRD §9, Arch §6.3) |
| Delegation / HOLD | ArmorIQ approval request; Maya approves only in the ArmorIQ console (PRD §0.8) |
| Tier | `team` / `confidential` / `restricted`; encoded in the tool name, never an argument (Arch §0.5) |
| Decision source | Layer that produced a decision (`DecisionSource`; see D-03) |
| Ledger | Tool-server, hash-chained, append-only record of what actually executed (PRD §3) |
| Gate | A live check that must pass, or have a documented fallback, before dependent work is claimed done (see README) |
