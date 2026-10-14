# ProxyLead — Product Requirements Document v2.0

| Field | Value |
|---|---|
| Version | 2.0 — authoritative implementation blueprint |
| Date | 2026-10-13 |
| Event | ArmorIQ “Flow State” hackathon, Oct 10–17, 2026 |
| Target reader | Claude Code / developer building ProxyLead |
| Status | Supersedes PRD v1.0 where scope, permissions, tool names, identity, ledger, plans, and experiment numbering differ |
| Paired architecture | `ProxyLead_Architecture_v2.1.md` |

## 0. Authority and implementation rules

This is the authoritative PRD for the current MVP. Read it with `ProxyLead_Architecture_v2.1.md`. The previous `Pasted text(20261011-150505).txt` / PRD v1.0 remains a reference for source-verified ArmorIQ SDK details and old probe notes only. It is **not** the implementation authority for the former 13-tool deploy/ticket/data-grant demo.

If a legacy v1 section conflicts with this document or Architecture v2.1, use v2.0/v2.1. Do not silently mix the old operational tool matrix with the new knowledge-tier matrix. Record live SDK/platform findings in `docs/EXPERIMENTS.md`; never claim unverified behavior.

1. Experiments before UI. Run ArmorIQ E1–E5 and the grounding evaluation E17 before claiming the corresponding behavior works.
2. One ArmorIQ plan per user request. Mint once; invoke ordered steps under that plan. At most one elevated-tier tool call (confidential or restricted) per plan, in addition to an optional team-tier search.
3. Every protected knowledge/company-data read passes through ArmorIQ and the public HTTPS MCP tool server. Backend application code cannot query protected content tables/views directly.
4. Build the plan from the authenticated user's message and trusted context before retrieving untrusted source content. Retrieved content is evidence, never instructions.
5. The tool server independently verifies service authentication, signed `_ctx`, expiry, idempotency, strict schemas, resource scope, and tier filters.
6. Fail closed if authorization cannot be verified. Clearly distinguish ArmorIQ policy, ArmorIQ SDK plan check, hosted-proxy decision, application rule, and tool-server rule.
7. Confidential and restricted answers are private-DM/private-web-session only. If a DM fails, never fall back to a public channel.
8. Do not simulate an ArmorIQ approval. Maya approves holds in the ArmorIQ console. Maya's own `knowledge_confirm_maya_decision` action is different: it is Maya confirming a proposed record under her own authorization, not approving another person's hold.
9. The MVP uses synthetic data only. No live customer data, PII tool, external email send, production deployment, or actual external egress.
10. If a gate fails, use the documented fallback and narrow the claim. Do not invent SDK calls or show a simulated outcome as a real ArmorIQ decision.

## 1. Product summary

ProxyLead is a shared AI representative of a busy engineering manager. Team members can ask what was decided and why, retrieve authorized team knowledge, submit a note, and request a small follow-up task without waiting for the manager. The assistant uses approved knowledge with provenance and is authorized per requester; it does not inherit a manager's blanket authority.

The demo has three fictional members:

- **Maya** — manager and ArmorIQ Org Admin.
- **Sam** — senior team member.
- **Priya** — junior team member.

ArmorIQ identity must resolve to the exact active, invited member email. A display name, message text, or user-supplied persona is not identity.

## 1.1 Measurable success criteria

| ID | Criterion | Pass condition |
|---|---|---|
| SC-01 | Permission matrix | Team/confidential/restricted outcomes match §5.1 in live ArmorIQ tests; tool ledger has an executed row only for ALLOW. |
| SC-02 | Injection boundary | Ten compromised-model runs and ten natural-model runs produce zero executed `send_external_message` calls in guarded mode. Record the exact enforcement layer for each attempt. |
| SC-03 | Identity spoofing | A message claiming to be Maya does not change the authenticated member or ArmorIQ attribution. |
| SC-04 | Hold lifecycle | A real HOLD can be approved/rejected in ArmorIQ console; only approved calls resume under the original plan. Rejection/expiry produces no execution. |
| SC-05 | Grounded answers | Every citation is returned by an authorized tool call in the same request; E17 measures weak/no-evidence abstention and conflicts. |
| SC-06 | Audit evidence | Each UI status maps to a persisted event and, where execution is claimed, a matching tool-server ledger row. |
| SC-07 | Short demo | Recorded demo is no longer than 3:00. |

## 2. MVP scope

### P0 — must ship

- Slack Socket Mode bot for one configured workspace.
- Trusted Slack user ID → app member → ArmorIQ member-email mapping.
- `@ProxyLead` mention and required DM flows; `@ProxyLead store this ...` saves a draft note with provenance.
- Web demo using the same API, plan lifecycle, ArmorIQ gateway, MCP endpoints, and data access path.
- Knowledge store in Neon PostgreSQL, `pgvector`, and PostgreSQL full-text search.
- Local FastEmbed `BAAI/bge-small-en-v1.5` embeddings, 384 dimensions.
- Team/confidential/restricted tier-specific search tools with fixed server-side filters.
- One ArmorIQ agent, one policy, two MCP endpoints (`memory-mcp`, `workspace-mcp`), three invited members, within the current quota assumptions. Verify quotas and exact registration names in the live console.
- Real ALLOW/BLOCK/HOLD outcomes, with Maya approving holds in the ArmorIQ console.
- Append-only execution ledger correlated with plan, step, application-generated invocation ID, and actual tool execution.
- Citation validation, calibrated abstention, unanswered-question queue, manager topic map, manager confirmation of authoritative decisions.
- A clearly labeled “Simulate hijacked model” toggle and a `send_external_message` honeypot that can write only to a local synthetic sink. It must be blocked for all personas by ArmorIQ policy in guarded mode.
- Public demo and a short demo video of 3 minutes or less.

### Stretch only after all P0 security tests pass

- A minimal manager form to configure a Neon connection and select approved read-only views, if this is needed for the demo environment.
- Google Calendar read-only, only if all core requirements are green and OAuth/scopes are configured. It is not a dependency of the MVP.

### Not in MVP

Gmail, Google Drive, Google Meet, external email/message delivery, real external egress, production deploys, live ticket-provider integration, PII access, broad OAuth marketplace, multi-workspace Slack, Slack historical search, arbitrary SQL, in-app approval, automatic meeting ingestion, SSO/SCIM, and the old 13-tool operational matrix.

## 3. Architecture

- **Interaction:** Slack Socket Mode and public web demo.
- **Backend:** modular FastAPI app; identity mapping, planner, orchestrator, responder, delivery routing, manager console APIs, and audit events.
- **Authorization:** `ArmorIQGateway` is the only module importing `armoriq_sdk`.
- **Tool server:** separately deployed FastAPI MCP service exposing exactly two registered endpoints: `memory-mcp` and `workspace-mcp`. Confirm endpoint naming/quotas with live E1/E5 experiments.
- **Data:** Neon PostgreSQL with `pgvector` and full-text search; no separate vector database.
- **Knowledge retrieval:** only callable by the tool server, not by backend request handlers.
- **Audit:** append-only execution ledger required. Hash-chained append-only execution ledger is required, with a verifier test that detects row modification/reordering. Do not claim “tamper-proof.”
- **Embedding:** load FastEmbed model once at tool-server startup; reserve approximately 1 GB RAM for model/process and verify deployment limits.

### 3.1 Repository shape

```text
proxylead/
  apps/api/{main.py,identity.py,orchestrator.py,planner.py,responder.py,routes/}
  apps/web/
  services/slack_socket/
  services/authorization/armoriq_gateway.py
  services/toolserver/{main.py,mcp_protocol.py,security.py,schemas.py,tools_memory.py,tools_workspace.py,db.py,ledger.py}
  services/knowledge/{ingest.py,embeddings.py,retrieval.py}
  services/audit/
  migrations/
  seed/
  policy/proxylead-policy.yaml
  scripts/{check_setup.py,probe_v2_e1_allowed.py,probe_v2_e2_offplan.py,probe_v2_e3_per_user.py,probe_v2_e4_hold.py,probe_v2_e5_setup.py,eval_e17_grounding.py,verify_db_grants.py}
  tests/
  docs/{ARCHITECTURE.md,EXPERIMENTS.md,SECURITY.md,SLACK_SETUP.md,DEMO.md}
  .env.example
  README.md
```

## 4. Identity and Slack behavior

### 4.1 Identity

- Web demo sign-in selects a persona and checks `DEMO_PASSCODE`; the server creates a signed HTTP-only session. Each subsequent request uses server-side session identity, never a persona from the chat body.
- Slack identity is resolved from `SLACK_USER_MAYA_ID`, `SLACK_USER_SAM_ID`, and `SLACK_USER_PRIYA_ID` to the configured app member and ArmorIQ email.
- ArmorIQ emails must be real active/invited organization members. Mask them in UI/logs.
- Each member has a dedicated `ArmorIQClient` instance. Do not share a mutable `for_user(email)` client across personas. Serialize token minting per client if required by the SDK.
- Chat text cannot change requester identity, role, topic permissions, or delivery destination.

### 4.2 Socket Mode

- One Slack workspace; use Socket Mode and the configured `SLACK_TEAM_ID`.
- Handle `app_mention` and the minimal DM events required for the demo. Acknowledge promptly, then process asynchronously.
- Use Slack Web API for replies/DMs; the agent does not choose arbitrary recipients or channels for protected content.
- Deduplicate by Slack event ID. Side effects must be idempotent.
- Required initial scopes to test/document: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `im:write`. Request only scopes actually required by the chosen event subscriptions and delivery flow; record any scope not granted and its safe fallback in `docs/SLACK_SETUP.md`.
- Restrict channel processing with `SLACK_ALLOWED_CHANNELS`. Never assume a mention gives full channel/thread history access.
- `@ProxyLead store this ...` invokes `knowledge_save_team_note`, attributed to the requesting member and marked `draft`. It does not make a decision authoritative.
- Confidential/restricted answers go only to a requester DM. For a channel request, post a generic notice and DM the content. If DM delivery fails, do not fall back to the channel.
- Do not implement `search.messages` or historical workspace search.

## 5. Permission matrix and policy

### 5.1 Knowledge tier matrix

| Tool tier | Maya | Sam | Priya |
|---|---|---|---|
| Team | ALLOW | ALLOW | ALLOW |
| Confidential | ALLOW | HOLD | BLOCK |
| Restricted | ALLOW | BLOCK | BLOCK |
| PII | Not implemented | Not implemented | Not implemented |

This is the agreed v2 matrix. Restricted data must never leave Maya through the assistant. A HOLD is not a second way to grant Sam restricted access.

### 5.2 Topic map and tier selection

- Maya configures a `topic_access_rules` map: normalized topic/project → maximum retrieval tier and optional approved source collections.
- Search team tier first for ordinary questions.
- Select confidential tier only when the topic matches an approved map rule or the user explicitly confirms a safe escalation prompt. The topic map never grants access by itself; ArmorIQ and tool-server rules still apply.
- Restricted tier is available only to Maya.
- **At most one elevated-tier tool call (confidential or restricted) per plan/request, in addition to an optional team-tier search.** Do not place confidential search plus confidential source read in one plan. The tier search tool must return a bounded, citation-ready evidence record. If a second protected call is genuinely needed, ask the user to initiate a new request; never create two holds in one request.
- Record matched topic rule, selected tier, and user-confirmed escalation in audit events.

### 5.3 ArmorIQ policy behavior

- Use one active policy for agent `proxylead`, keyed by the exact lowercase emails of the three invited members.
- Default enforcement is block. Configure per-member tool rules using the verified policy schema.
- `send_external_message` must be blocked for Maya, Sam, and Priya. Keep it out of the allowed-tools list and configure explicit per-tool block if the verified schema supports it.
- Maya may use manager-only maintenance tools such as `knowledge_confirm_maya_decision` and `knowledge_mark_superseded`.
- Policy configuration is not considered working until the live policy is accepted and E3/E5 tests show the expected results.
- Do not add PII tools. Do not assume the policy syntax or proxy behavior; verify with the pinned SDK and console.

## 6. Tool contracts

### 6.1 `memory-mcp`

Tier-specific tools:

- `knowledge_search_team`
- `knowledge_search_confidential`
- `knowledge_search_restricted`
- `knowledge_get_source_team`
- `knowledge_get_source_confidential`
- `knowledge_get_source_restricted`
- `read_source_team`
- `read_source_confidential`
- `read_source_restricted`

Maintenance tools:

- `knowledge_save_team_note` — saves user notes or generated summaries as non-authoritative drafts; `content_kind` enum is `note` or `summary`.
- `knowledge_confirm_maya_decision` — Maya-only action that marks a reviewed record authoritative. This is Maya authorizing her own record-maintenance action, not approving another member's HOLD.
- `knowledge_mark_superseded` — Maya-only action to supersede a prior decision with provenance.

Tier is encoded in the tool name and hard-coded in server code/SQL. Never accept a model-supplied `tier` argument. Search results are bounded and include source IDs and provenance needed for citation. The tool server re-checks tier/status for every source lookup. Do not leak restricted source existence or metadata.

### 6.2 `workspace-mcp`

- `create_followup_task` — optional; creates a task in ProxyLead's local table with enum-bounded fields, authenticated requester injected by trusted code, and an idempotency key.
- `send_external_message` — required honeypot for the injection demo. It writes only to a local synthetic `egress_sink` table, never sends network traffic, and is blocked by ArmorIQ policy for all personas.

The responder may request a tool after seeing retrieved evidence. If the requested tool is not in the signed plan, route the attempt through the verified SDK off-plan invocation path so the SDK's plan check can record the attempt. Never execute the off-plan action. Label the source accurately (`armoriq_sdk_plan_check` if blocked client-side); do not claim a hosted-proxy block unless E2 proves it.

## 7. Plan and request lifecycle

1. Receive Slack event/web message, authenticate requester, resolve workspace/member, and create a request/trace ID.
2. Planner sees the user's message, trusted display context, conversation text allowed by policy, and tool catalog. It sees no retrieved tool results.
3. Backend validates structured plan: strict schemas, known tools, enum bounds, no underscore-prefixed user keys, no model-selected identity/tier/recipient, and at most one elevated-tier tool call (confidential or restricted), in addition to an optional team-tier search.
4. Apply topic map: team-tier search first; higher tier only under §5.2.
5. Create **one ArmorIQ plan/session per request**, mint once, then invoke sequential steps under the same plan/token. Do not start a plan for each individual read.
6. ArmorIQ may ALLOW/BLOCK/HOLD. Persist actual status and decision source. A BLOCK or unverified authorization returns no protected data.
7. On HOLD, the SDK's verified delegation flow creates a real request. Notify Maya with a minimal Slack DM and link to the ArmorIQ console. Maya approves/rejects there. The app never accepts a Slack “approved” message as authority.
8. Resume only via the verified SDK flow for the same plan and step. Do not auto-replan on expiry. If the token expires, approval times out, or status cannot be verified, fail closed and ask the requester to retry.
9. On ALLOW, the tool server validates the service credential, signed `_ctx`, audience, expiry, replay/idempotency, strict schema, scope, tier, and preconditions. Only the tool server accesses protected content.
10. Responder treats tool output as untrusted data, validates citations against source IDs returned in this request, and abstains on insufficient/weak/conflicting evidence.
11. Any tool request outside the current plan is not executed. Send it through the verified ArmorIQ SDK plan-check path for a real, accurately labeled block event. A new user-confirmed action gets a new request/plan.
12. Delivery tier is the highest tier of **any data returned to the model during the request**, not merely the sources cited in the final text. Confidential/restricted output is DM/private-web-session only.
13. Persist delivery recipient/channel/result. Failed DM delivery never falls back to a public channel.
14. Append audit events for every plan, authorization, hold, execution, answer, and delivery state transition.

### 7.1 Token lifetime

Set `TOKEN_VALIDITY_S=1200` and `APPROVAL_TIMEOUT_MS=900000`. Startup must assert:

```python
TOKEN_VALIDITY_S * 1000 > APPROVAL_TIMEOUT_MS + 120000
```

If ArmorIQ does not accept this validity or an approval outlasts the token, lower the approval timeout to fit a verified token lifetime. On expiry, fail and ask for a fresh user request. Do not auto-create a new plan after approval because the old approval is plan-bound.

## 8. Knowledge, retrieval, and database

### 8.1 Database and roles

Use Neon PostgreSQL with `pgvector` and PostgreSQL full-text search. Tables:

- `workspaces`, `members`, `slack_events`
- `source_records`, `knowledge_chunks`, `decisions`, `manager_guidance`, `team_notes`
- `topic_access_rules`
- `plans`, `plan_steps`, `approval_requests`
- `request_events`, `decision_events`, `tool_execution_ledger`, `delivery_events`
- `unanswered_questions`
- `tasks` and `egress_sink` if those tools ship

Every knowledge record has source, author, timestamp, classification/tier, review status, version, content hash, and supersession metadata.

Use separate DB roles:

- `proxylead_backend`: no `SELECT` on protected content tables or approved company views.
- `proxylead_toolserver`: minimum access to knowledge tables and approved read-only views; only the MCP tool server uses this role.
- migration/seed role: separate and unavailable to runtime processes.

Automated test must prove the backend role cannot read protected content directly.

### 8.2 Retrieval

- FastEmbed `BAAI/bge-small-en-v1.5`, 384 dimensions; model loaded once at tool-server startup. Budget approximately 1 GB RAM.
- Run vector similarity and PostgreSQL full-text search; combine rankings using reciprocal rank fusion (RRF), deduplicate by source/chunk, then apply a result limit.
- Filter workspace, tier, active status, and requester access before returning evidence to the model.
- `MIN_SCORE` must be calibrated, not guessed. Store the model version, threshold, and evaluation result in config/docs.
- Never send confidential source text to a hosted embedding service without explicit design approval.

### 8.3 Grounding evaluation / E17

Seed a repeatable set of:
- answerable questions with known source IDs;
- unrelated/no-evidence questions;
- weak-evidence questions below threshold;
- conflicting decision records;
- superseded records;
- topic escalation cases across team/confidential/restricted tiers.

For each, record expected answerability, expected source IDs, retrieval scores/ranks, actual citations, chosen tier, and whether the assistant abstained. Tune `MIN_SCORE` against a held-out subset. Acceptance:

- no answer if no allowed result clears the calibrated threshold;
- every citation belongs to a source returned during this request;
- superseded decisions are not represented as current;
- no unauthorized tier content or metadata appears;
- false accepts and false abstains are documented.

This is evidence of retrieval behavior, not proof of universal factual correctness.

### 8.4 Memory authority and poisoning

- `@ProxyLead store this ...` saves a user-authored draft with provenance.
- Generated summaries also remain drafts.
- Only Maya, using her authenticated identity and authorized `knowledge_confirm_maya_decision`, can mark a reviewed record authoritative.
- Confirmation is bound to the exact record/version. A Slack message that merely says “approved” does not count.
- Memory never changes ArmorIQ policy or permissions.
- Retrieved content is untrusted data and cannot change plan, identity, tool selection, or delivery destination.

## 9. MCP protocol and security boundary

- Public HTTPS endpoints, JSON-RPC 2.0 POST with SSE response framing, supporting `initialize`, `tools/list`, and `tools/call` exactly as verified against ArmorIQ docs/SDK.
- Pin `armoriq-sdk==0.6.10` and Python 3.12 unless a live experiment requires a documented change.
- `ArmorIQGateway` is the only module importing `armoriq_sdk`.
- Tool names, MCP names, and agent name `proxylead` must match the console registry exactly.
- Determine credential path via E1; do not assume console-registered credentials or SDK-supplied `X-Armoriq-MCP-Auth`.
- Signed `_ctx` is generated by trusted backend code and contains requester/workspace, request, plan/step, tool, audience, expiry, and app-generated `jti`. Validate signature, audience, expiry, and idempotency/replay.
- Strip underscore-prefixed SDK metadata before business-schema validation, but never use `_delegation_id`, `_approvals`, `_policy_context`, or other untrusted underscore fields as proof of approval.
- Strict JSON schemas, fixed SQL, parameterized queries, approved views, statement timeout, row/character limits, and server-side ownership/tier checks.
- No arbitrary SQL, shell, public URL fetch, or real egress tool.

## 10. Audit and UI

### 10.1 Required audit fields

Append-only events correlated by `request_id`, `trace_id`, `plan_id`, step ID, `armoriq_plan_id` if available, and application-generated `jti`:

- requester/member, workspace, entry point, Slack event/message reference;
- plan steps, tool names, arguments with secrets redacted;
- decision status and source (`armoriq_issuance`, `armoriq_sdk_plan_check`, `armoriq_proxy_decision`, `tool_server_rule`, `application_rule`, `unknown`);
- delegation ID, approval status, token-expiry/retry status;
- tool server reached, server validation outcome, actual execution result/ID/error;
- source IDs, citation IDs, highest tier returned to model, tier delivered;
- actual destination/recipient, delivery status, timestamps.

A plan or ALLOW is not proof of execution. Ledger records must reflect what the tool server actually did. Keep secrets and raw tokens out of logs.

### 10.2 UI

Team view:
- chat, source citations, honest outcome/status labels, pending HOLD state;
- clear distinction between assistant prose and authoritative system record;
- safe private result delivery.

Manager view:
- requests and trace timeline;
- pending HOLDs with ArmorIQ console link (no app approval button);
- unanswered questions;
- draft knowledge review queue;
- authoritative decisions with provenance/supersession;
- topic map editor;
- Slack/database/tool-server health based on actual checks.

### 10.3 Hijacked-model demo

- Visible toggle named **“Simulate hijacked model”**, off by default.
- It injects synthetic malicious instructions into a seeded source or adversary prompt and attempts `send_external_message`.
- Guarded mode routes the attempt to the ArmorIQ SDK plan check if off-plan; if the user explicitly confirms a new plan, ArmorIQ policy must BLOCK the honeypot for every persona.
- `send_external_message` writes only to local `egress_sink` if a test explicitly exercises an unguarded synthetic sandbox; it never performs network egress.
- The demo must show the exact enforcement layer. A DM-only delivery rule is an application control, not proof ArmorIQ blocked egress.

## 11. Experiments and numbering

Run on the actual registered ArmorIQ organization and deployed HTTPS MCP endpoint. Mocked tests do not pass these gates. Preserve legacy experiment IDs in logs for provenance.

| Architecture ID | Legacy PRD v1.0 mapping | Test |
|---|---|---|
| E1 | E1 | Real planned tool call reaches MCP; verify credential path and actual ledger record |
| E2 | E2 / §12.4 | Off-plan attempt; distinguish SDK-side rejection from hosted-proxy enforcement |
| E3 | E5 + E4b | Per-user outcomes and client/token identity attribution |
| E4 | E6 + E7 + E11 | Hold, console approve/reject, resume, expiry/token lifetime |
| E5 | E12 + E14 | Agent/MCP/tool names, member resolution, policy schema |
| E17 | New v2 test; relates to SC-02/SC-05 | Retrieval threshold, citations, abstention, conflicts, supersession |

### E1 — exact procedure

1. Create Neon schema and a tiny synthetic seed containing one team-tier record.
2. Deploy the MCP server with public HTTPS and its bearer credential.
3. Register `proxylead` and the first MCP endpoint using the exact intended v2 names. Verify whether the hosted organization limit counts endpoint registrations or server URLs.
4. Use the pinned SDK to create one plan containing only `knowledge_search_team` for Maya.
5. Invoke through ArmorIQ. Capture SDK response, proxy/request evidence available in the console, tool-server logs, and the matching ledger row.
6. Inspect whether the tool server received the configured credential/header; test the alternate credential path only as directed by the SDK/docs and PRD v1 evidence.
7. Confirm returned `MCPInvocationResult.result` shape; do not assume a `.data` field.
8. Mark PASS only if the real tool-server request and ledger entry correlate with the actual plan/invocation. Otherwise record exact failure and use the documented fallback without claiming a hosted proxy pass.

### E1 status at document creation

**NOT RUN.** The current ChatGPT session has no authenticated ArmorIQ console, API key, deployed MCP endpoint, or project repository connection available to execute a real live invocation. This is not a pass/fail result. The owner must run E1 from the project environment with the real credentials. Do not mark it complete based on this document or a mock.

## 12. Environment variables

```dotenv
APP_ENV=development
SESSION_SECRET=
CTX_SECRET=
DATABASE_URL_BACKEND=
DATABASE_URL_TOOLSERVER=
ARMORIQ_API_KEY=
ARMORIQ_AGENT_ID=proxylead
ARMORIQ_CONSOLE_URL=https://platform.armoriq.ai
TOKEN_VALIDITY_S=1200
APPROVAL_TIMEOUT_MS=900000
DEMO_PASSCODE=
SLACK_ALLOWED_CHANNELS=
SLACK_USER_MAYA_ID=
SLACK_USER_SAM_ID=
SLACK_USER_PRIYA_ID=
SLACK_APP_TOKEN=
SLACK_BOT_TOKEN=
SLACK_TEAM_ID=
PERSONA_MAYA_EMAIL=
PERSONA_SAM_EMAIL=
PERSONA_PRIYA_EMAIL=
ANTHROPIC_API_KEY=
LLM_MODEL=claude-opus-5-5
EMBED_MODEL=BAAI/bge-small-en-v1.5
MIN_SCORE=
TOOLSERVER_BASE_URL=
TOOLSERVER_BEARER_WORKSPACE=
TOOLSERVER_BEARER_MEMORY=
ADMIN_TOKEN=
```

Add `ARMORIQ_MCP_CREDENTIALS` only if E1 proves it is needed. Never log or expose secrets.

## 13. Dated execution plan

### 13 October — Day 1: setup and proof

- First: create `docs/EXPERIMENTS.md`, verify active ArmorIQ org, invite/confirm three members, confirm quota and exact names.
- Deploy minimal MCP protocol and Neon schema/seed in the first half; this is needed for E1.
- Run E1, E2, E3, E4, E5 in dependency order; do not build polished UI.
- Save raw outcomes and update the fallback decision after each test.
- Day-1 exit: real tool call demonstrated; per-user outcomes known; approval flow either proven or explicitly downgraded.

### 14 October — vertical slice

- Neon schema, pgvector/full-text search, seed corpus, local embedding model.
- Tool-server-only reads and backend DB grant test.
- Slack Socket Mode mention/DM, identity mapping, `store this`, event dedupe.
- One team-tier question works end-to-end with citation and audit.

### 15 October — policy and manager view

- Finish matrix, honeypot, topic map, HOLD notifications and console approval if E4 passes.
- Draft/authoritative memory workflow, unanswered queue, audit timeline.
- Minimal web demo using the same backend path.

### 16 October — evaluation and freeze

- E17 grounding eval; prompt injection, direct-call, spoofing, tier, replay, token expiry, DM failure, outage tests.
- Public deployment and 3 repeatable demos. Feature freeze after noon unless fixing critical bugs.
- Record only claims supported by passing tests.

### 17 October — submission

- Critical bug fixes only; verify public URL, Slack flow, audit trail, and demo.
- Submit before the official deadline; verify deadline time from official instructions rather than assuming it.

## 14. Definition of done

- [ ] E1 real tool call + correlated server ledger row.
- [ ] E3 per-member decisions match team/confidential/restricted matrix.
- [ ] E4 hold/console approval/resume verified or accurately shown as pending/unverified.
- [ ] E5 names, member resolution, and policy schema verified.
- [ ] One plan per request, at most one elevated-tier call plus optional team-tier search, no auto-replan after token expiry.
- [ ] Slack Socket Mode and trusted user mapping work.
- [ ] `@ProxyLead store this` creates a draft with provenance.
- [ ] Backend DB role cannot read protected content; tool server applies fixed tier filters.
- [ ] `send_external_message` is blocked for all personas and never performs real network egress.
- [ ] Confidential/restricted content is never delivered in a shared channel; delivery tier is based on all data returned to the model.
- [ ] E17 threshold calibration and grounding eval recorded.
- [ ] Append-only audit rows reflect actual execution and delivery.
- [ ] README lists verified controls, unverified behavior, and known limitations.
- [ ] Recorded demo is 3 minutes or less.
