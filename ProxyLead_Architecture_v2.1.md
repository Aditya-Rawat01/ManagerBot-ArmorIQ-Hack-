# ProxyLead --- Architecture v2

**Implementation contract for Claude Code**\
**Version:** 2.0\
**Date:** 13 October 2026\
**Hackathon deadline:** 17 October 2026\
**Status:** Build only after the Day-1 ArmorIQ gates pass. This revision is Architecture v2.1 and overrides conflicting implementation details in PRD v1.0; the aligned PRD v2.0 is `ProxyLead_PRD_v2.md`.

> **Purpose:** Build a Slack-first AI representative of a manager that
> answers questions from approved team knowledge, reads explicitly
> approved company data, and requests real authorization for protected
> disclosures or actions. The same backend and authorization path powers
> the public web demo. This is a four-day hackathon MVP, not a
> general-purpose integrations platform.

## 0. Binding implementation rules

1.  Read this document and the existing ProxyLead PRD before coding.
    Treat the PRD's ArmorIQ SDK/version findings, policy model,
    tool-server protocol, identity rules, and experiment fallbacks as
    binding unless a live experiment disproves them. Do not invent SDK
    methods or claim unverified behavior.
2.  **Experiments before UI.** Do not build the full product until the
    three required ArmorIQ experiments pass or the documented fallback
    is selected and accurately disclosed.
3.  **All protected reads are tool calls.** The agent/backend must never
    query knowledge tables or approved company views directly. Protected
    reads must pass through the ArmorIQ SDK/gateway and the public HTTPS
    MCP tool server.
4.  **Plan from the user's message only.** Create the proposed plan
    before retrieving any document, Slack content, database row, or
    memory. Retrieved content is untrusted data. It cannot add a tool
    call to the signed plan. If it suggests a new action, ask the user
    to confirm that action, then create a new plan.
5.  **Tool names encode the data tier.** Never accept a model-supplied
    `tier`, `sensitivity`, `resource_scope`, `subject_user_id`, or
    equivalent as the security control. Tier-specific tools have
    hard-coded tier filters in trusted server code.
6.  ArmorIQ policy, app-level authorization, provider/database
    permissions, and tool-server validation are separate layers. None
    substitutes for another.
7.  Do not claim "ArmorIQ blocked" unless a real ArmorIQ response
    supports that label. Otherwise label the result as an application
    rule, tool-server rule, SDK-side plan check, or unverified.
8.  Do not simulate approval. Maya approves holds in the ArmorIQ
    console. ProxyLead may notify her and show status, but the app must
    not present its own approval button as authoritative in this MVP.
9.  Confidential and restricted answers are **DM-only**. Never post
    protected answer text or sensitive citations in a shared Slack
    channel/thread. In a channel, post only a generic notice such as "I
    sent you a private response."
10. Do not claim zero hallucinations or perfect prompt-injection
    prevention. Ground answers, validate citations, abstain on
    weak/conflicting evidence, and report tested limitations.
11. Implement only the scope in §3. Gmail, Drive, Meet, and other
    external integrations are planned, not completed. Do not build a
    generic connector lifecycle framework during this deadline.
12. The app must remain useful when ArmorIQ or the tool server is
    unavailable: fail closed for protected reads/actions and explain
    that authorization could not be verified.

## 1. Product outcome

ProxyLead reduces the manager's role as the team's "human API." Team
members can ask what was decided, why a decision was made, where an
approved fact came from, or request a small follow-up task. The
manager's authority is not copied into a shared unrestricted agent:
access remains tied to the individual requester and to the sensitivity
of the requested information.

### Demo personas

Use three demo members: **Maya** (manager / Org Admin), **Sam** (senior
team member), and **Priya** (junior team member). The personas are demo
labels. ArmorIQ must resolve each configured email to an active, invited
organization member; never rely on a name typed in chat. Do not expose
real member emails in the UI or logs; mask them.

### Required outcomes

-   **ALLOW:** real authorization and server checks pass; the tool
    executes or returns data, and the UI reports the actual result.
-   **BLOCK:** a real ArmorIQ decision or named application/tool-server
    rule denies the request; do not disclose protected data.
-   **HOLD:** ArmorIQ creates a real delegation/approval request. The
    tool must not return protected content before the approval flow
    completes. Maya approves in the ArmorIQ console. The SDK's verified
    approval/resume path determines whether execution continues.
-   **ABSTAIN:** no reliable evidence, conflicting evidence, or
    stale/superseded evidence; answer with uncertainty and source
    context where safe.
-   **ERROR / UNVERIFIED:** authorization or infrastructure could not be
    verified. Never convert this into ALLOW.

## 2. Architecture

``` mermaid
flowchart TD
  Slack[Slack Socket Mode: one workspace] --> API[FastAPI backend / request orchestrator]
  Web[Web demo + manager console] --> API
  API --> Identity[Trusted identity mapping]
  Identity --> Planner[LLM planner: user message only]
  Planner --> Gateway[ArmorIQGateway]
  Gateway --> MCP[Public HTTPS MCP tool server]
  MCP --> Access[memory-mcp endpoint]
  MCP --> Work[workspace-mcp endpoint]
  Access --> DB[(Neon PostgreSQL + pgvector)]
  Work --> DB
  MCP --> Ledger[Append-only execution ledger]
  Gateway --> Events[Decision and plan events]
  API --> UIAudit[Request, delivery and UI events]
  API --> SlackAPI[Slack Web API for replies and DMs]
  Planner --> Responder[Grounded responder]
  Responder --> API
```

### Deployment shape

-   One modular FastAPI backend.
-   One separately deployed MCP tool-server process exposing **two
    registered MCP endpoints only**: `memory-mcp` and `workspace-mcp`. This
    stays within the documented organization quota of two MCP
    servers/endpoints; verify the exact registration behavior in the
    setup experiment.
-   One web app for chat, manager overview, unanswered questions,
    request/audit detail, and connector status.
-   One Slack app using Socket Mode for one preconfigured workspace. No
    multi-workspace OAuth installation flow in the MVP.
-   One Neon PostgreSQL database with `pgvector` and PostgreSQL
    full-text search. No separate vector database.
-   ArmorIQ SDK imports only inside `armoriq_gateway.py`.
-   Keep the deployment simple; logical modules do not each need their
    own service.

## 3. Scope and exclusions

### Must ship

1.  Slack Socket Mode bot for one workspace; handle mentions and DMs
    required by the demo.
2.  A small, seeded manager/team knowledge base with source records,
    decisions, guidance, and a few example company-data views.
3.  Neon PostgreSQL + pgvector retrieval, using tier-specific ArmorIQ
    tools and server-side filters.
4.  Per-person ArmorIQ policy for Maya, Sam, and Priya, plus real
    ALLOW/BLOCK/HOLD evidence.
5.  Hold notification to Maya by Slack DM; approval itself happens in
    the ArmorIQ console.
6.  Append-only request/decision/execution/delivery audit trail.
7.  Web demo and manager console using the same backend and
    authorization path.
8.  Security tests for identity spoofing, direct tool-server calls,
    prompt injection, plan manipulation, tier leaks, approval, and
    delivery channel.
9.  A demo script that proves the security behavior with reproducible
    requests.

### Stretch only after all core tests pass by 16 October noon

-   A minimal Neon connection setup page: paste connection string, test
    it, and select approved read-only views.
-   Google Calendar read-only, only if it does not delay the core demo
    and provider consent/scopes are ready.

### Planned, not implemented

Gmail, Google Drive, Google Meet, broad OAuth connector lifecycle,
multi-workspace Slack, Slack message-history search, arbitrary SQL,
in-app approval, automatic ingestion from every provider, production
SSO/SCIM, and generalized connector marketplace.

### 3.1 Source-of-truth decisions overriding legacy PRD v1.0

The following are deliberate v2 decisions and supersede the corresponding PRD v1.0 sections until that file is replaced by `ProxyLead_PRD_v2.md`:

| Topic | v2 authoritative decision |
|---|---|
| MVP scope | Slack-first knowledge assistant, not the former 13-operation deploy/ticket/data-grant demo. Only `create_followup_task` (optional) and `send_external_message` (honeypot) are work tools. |
| MCP endpoints | `memory-mcp` and `workspace-mcp`, subject to live registration experiment. |
| Identity | Trusted mapping is Slack user ID → configured member record → invited ArmorIQ email; web demo persona is server-side session state. Never infer identity from chat text. |
| Knowledge permission matrix | Team: Maya/Sam/Priya ALLOW. Confidential: Maya ALLOW, Sam HOLD, Priya BLOCK. Restricted: Maya ALLOW, Sam BLOCK, Priya BLOCK. PII tools do not exist. |
| Ledger | A hash-chained append-only execution ledger is required, with a verifier test that detects row modification/reordering. Correlate every entry with the actual tool execution. Do not claim tamper-proof. |
| Plan lifecycle | One ArmorIQ plan per request; sequential steps under that plan. At most one elevated-tier tool call (confidential or restricted) per plan, in addition to an optional team-tier search. |
| Token expiry | `TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000`; expired requests fail and require a user retry, never auto-replan. |
| Delivery classification | Highest tier of any data returned to the model in this request, not merely cited sources. |
| Experiments | Architecture v2 E1–E5 map to legacy PRD experiments as listed in §12; preserve original IDs in the experiment log to avoid losing provenance. |

## 4. Slack: one-workspace Socket Mode

### Required behavior

-   Configure a single Slack workspace and app using Socket Mode.
-   Keep `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN` server-side. Never
    expose them to browser code, prompts, or logs.
-   Handle `app_mention` and the minimum DM event(s) needed for the
    demo. Acknowledge events promptly and run agent work asynchronously.
-   Deduplicate events using Slack event IDs and enforce idempotency for
    side effects.
-   Use the Slack Web API from the backend to reply or send DMs. **Slack
    posting is not an ArmorIQ tool.** The agent cannot choose arbitrary
    channels or recipients for protected responses.
-   Use a trusted server-side mapping from Slack user ID to an invited
    ArmorIQ member email/persona. Bootstrap this mapping from
    environment/config or an explicit manager setup step. Never trust
    identity claimed in message text.
-   For confidential/restricted results, DM the requester. If asked in a
    channel, post only a generic notice in the thread and send the
    answer privately. Do not leak protected source titles, snippets, or
    citations in the notice.
-   On a HOLD, DM Maya with a minimal request summary and a link to the
    relevant ArmorIQ console surface. Do not include the protected
    content in the notification.
-   Do not use `search.messages`: it requires user-token access and is
    legacy. Do not implement Slack-wide historical search or the
    separate public-channel real-time search for this deadline.
-   Socket Mode is not the Events API HTTP endpoint; do not add a
    needless request-signature endpoint. Validate the Socket Mode
    envelope/event, workspace/team ID, allowed user mapping, and event
    deduplication.
-   If the app cannot access a requested thread/message through
    permitted scopes, report that source as unavailable. Do not infer
    access from a mention.

### Slack permissions

Test/document the minimum scopes for this demo: `app_mentions:read`,
`chat:write`, `channels:history`, `groups:history`, `im:history`, and
`im:write`, subject to Slack's current event/token requirements. Record
which scopes are actually granted in `docs/SLACK_SETUP.md`; request only
what the chosen event subscriptions and delivery flow require. Restrict
channel processing with `SLACK_ALLOWED_CHANNELS`. Support
`@ProxyLead store this ...` by calling `knowledge_save_team_note` with
the authenticated author and provenance; the resulting record is a
draft, never automatically authoritative. If a needed scope is
unavailable, degrade safely instead of silently requesting broad access.

## 5. ArmorIQ integration contract

### 5.1 Platform constraints and setup

-   Pin `armoriq-sdk==0.6.10` and Python 3.12 unless a verified PRD
    experiment requires a change.
-   Design for the current organization limits: **2 MCP endpoints, 2
    policies, 5 members**. Use two endpoints (`workspace-mcp`, `memory-mcp`),
    one agent (`proxylead`), one policy, and three invited members.
    Confirm current limits in the console before bootstrap.
-   The MCP endpoint URL must be public HTTPS. Implement the
    ArmorIQ-documented MCP format: JSON-RPC 2.0 POST with SSE response
    framing, supporting `initialize`, `tools/list`, and `tools/call`;
    tool results use the required text content shape. Use the exact URL
    and registration names expected by the console.
-   The agent name, MCP names, and tool/action names must match the
    ArmorIQ registry/policy exactly. A naming mismatch can mean no
    policy applies; test this with a setup-check script.
-   Read the PRD's SDK/source notes before implementation. The SDK uses
    `session.start_plan(...)`, captures the plan, obtains an intent
    token, and invokes planned tools. Use only API shapes verified in
    the PRD or installed SDK.
-   The SDK's default token validity can be short. Configure token
    validity explicitly to the verified PRD setting (initial target
    `1200` seconds), then test the maximum accepted lifetime and
    approval wait behavior. Set `APPROVAL_TIMEOUT_MS=900000` and assert at startup that `TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000`. If a token expires or cannot be resumed under the original plan, fail the request and ask the user to retry. Never auto-create a new plan after a hold because a new plan is not covered by the original approval.
-   `invoke_with_policy` may add underscore-prefixed metadata such as
    `_policy_context`, `_delegation_id`, and `_approvals`. The tool
    server must strip all underscore-prefixed control parameters before
    strict business-schema validation and execution. Those fields must
    never grant authority by themselves.
-   The SDK does not necessarily provide a token `jti` or revocation.
    Maintain an application-generated unique `jti`/idempotency
    identifier for each invocation; do not claim it is an ArmorIQ token
    claim. Enforce deduplication in the tool server until the relevant
    token/plan expires.
-   If the PRD reports a discrepancy between docs and SDK, trust neither
    by assumption: reproduce it with an experiment and record the
    outcome in `docs/EXPERIMENTS.md`.

### 5.2 One SDK client per identity

`ArmorIQClient.for_user(email)` mutates a field on the client according
to the PRD. Do not share one mutable client concurrently across Maya,
Sam, and Priya.

-   Create one `ArmorIQClient` instance per configured member identity.
-   Use a lock around plan/token minting for each client where required.
-   Resolve `email` from the trusted server-side Slack/web identity
    mapping, not from the LLM or request payload.
-   Ensure the email belongs to an active ArmorIQ organization member.
-   Test at least three concurrent demo personas; verify no cross-user
    identity leakage.

### 5.3 Plan construction

-   The planner receives the authenticated user's message and trusted
    app context only.
-   Construct the initial plan before any untrusted source content is
    retrieved.
-   Each protected retrieval is an explicit, tier-named MCP tool call in
    the plan.
-   The model cannot add or mutate plan steps after reading retrieved
    content. If retrieved content suggests another action, ask the user
    to confirm the exact action; after confirmation, create a fresh plan
    and run authorization again.
-   Persist the app request ID, ArmorIQ plan ID, plan hash if exposed by
    the verified SDK, step order, tool names, and the source of each
    decision.
-   Distinguish `sdk_plan_check`, `armoriq_proxy_decision`,
    `armoriq_policy_decision`, `tool_server_rule`, `application_rule`,
    and `unknown`. Never label an SDK client-side mismatch as a hosted
    proxy block.

### 5.3.1 Topic map, tier escalation, and one HOLD per request

- Maintain a manager-controlled topic map (`topic_access_rules`) configured by Maya in the manager console. Each topic maps to a default maximum retrieval tier (`team`, `confidential`, or `restricted`) and optional approved source collections. The map is configuration, not authority: ArmorIQ and tool-server checks still apply.
- Search the `team` tier first for every ordinary question. Use a higher tier only when (a) the normalized topic matches a manager-approved topic-map rule and the requester is allowed to ask at that tier, or (b) the user explicitly confirms a proposed escalation after seeing a safe explanation.
- Never place multiple elevated-tier tool calls in one plan. A request may contain a team-tier search plus **at most one** higher-tier (confidential or restricted) search/read action. Do not put `search_confidential` and `read_source_confidential` in the same plan; a single tier-scoped search must return only the bounded evidence needed to answer. If a source needs a separate read, stop and ask the user to confirm a new request; do not create multiple HOLDs in one request.
- Keep Maya's topic map editor separate from the ArmorIQ policy. Topic-map permission does not grant data access.
- Audit the topic-map rule ID, matched topic, chosen tier, and whether escalation was user-confirmed.

### 5.4 Authorization layers

1.  **Authentication:** Slack event identity or web session resolved
    server-side.
2.  **Provider/database permissions:** credentials and Postgres roles
    limit what the integration can read.
3.  **Application data authorization:** workspace ownership and the
    tier-specific query filter.
4.  **ArmorIQ intent/plan enforcement:** call must belong to the
    captured plan.
5.  **ArmorIQ per-member policy and HOLD:** policy is evaluated for the
    active member and named tool.
6.  **Tool-server validation:** strict schemas, fixed tier, resource
    scope, idempotency, and preconditions.
7.  **Delivery authorization:** the backend chooses a safe delivery
    destination; confidential/restricted content is DM-only.

Every layer fails closed. An ALLOW from one layer does not bypass
another.

### 5.5 Policy configuration

Create one active policy for agent `proxylead`, keyed by the exact
lowercase emails of the three invited members. Use the PRD policy schema
and validate it in the ArmorIQ console. This is an implementation
template, not proof that the live policy has been accepted:

``` yaml
apiVersion: armor.io/v1
kind: Policy
metadata:
  name: proxylead-team-authority
  targetType: agent
  targetId: "<registered proxylead agent id>"
spec:
  priority: 100
  status: active
  defaultEnforcementAction: block
  memberRules:
    "<MAYA_EMAIL>":
      allowedTools:
        - knowledge_search_team
        - knowledge_search_confidential
        - knowledge_search_restricted
        - knowledge_get_source_team
        - knowledge_get_source_confidential
        - knowledge_get_source_restricted
        - read_source_team
        - read_source_confidential
        - read_source_restricted
        - knowledge_save_team_note
        - knowledge_confirm_maya_decision
        - knowledge_mark_superseded
        - create_followup_task
      enforcementAction: allow_log
      toolEnforcement:
        send_external_message: block
    "<SAM_EMAIL>":
      allowedTools:
        - knowledge_search_team
        - knowledge_search_confidential
        - knowledge_get_source_team
        - knowledge_get_source_confidential
        - read_source_team
        - read_source_confidential
        - knowledge_save_team_note
        - create_followup_task
      enforcementAction: allow_log
      toolEnforcement:
        send_external_message: block
        knowledge_search_confidential: hold
        knowledge_get_source_confidential: hold
        read_source_confidential: hold
        knowledge_search_restricted: block
        knowledge_get_source_restricted: block
        read_source_restricted: block
        knowledge_confirm_maya_decision: block
        knowledge_mark_superseded: block
    "<PRIYA_EMAIL>":
      allowedTools:
        - knowledge_search_team
        - knowledge_get_source_team
        - read_source_team
        - knowledge_save_team_note
        - create_followup_task
      enforcementAction: allow_log
      toolEnforcement:
        send_external_message: block
        knowledge_search_confidential: block
        knowledge_get_source_confidential: block
        read_source_confidential: block
        knowledge_search_restricted: block
        knowledge_get_source_restricted: block
        read_source_restricted: block
        knowledge_confirm_maya_decision: block
        knowledge_mark_superseded: block
    "*":
      allowedTools: []
      enforcementAction: block
```

**Policy acceptance is a gate.** Verify the exact schema, whether held
tools must also appear in `allowedTools`, how tool names are represented
for the two MCP endpoints, and whether per-member `toolEnforcement`
works through the hosted proxy. The policy above is not a substitute for
live tests. Do not create separate policies per tier unless a verified
limitation forces a change; the current quota is two policies.

The application/tool server must still reject an out-of-tier call even
if a policy is accidentally too permissive. PII tools are not
implemented in the MVP and must not be added.

### 5.6 Real HOLD flow

1.  A protected tool call reaches ArmorIQ through the SDK with the
    authenticated member identity.
2.  ArmorIQ policy returns/creates the HOLD/delegation according to the
    real platform behavior.
3.  The SDK's supported `invoke_with_policy`/delegation lifecycle waits
    or polls according to verified behavior. Run long waits in a
    background worker/thread pool; do not block the Slack event
    acknowledgement.
4.  The backend writes the real delegation ID and status to the audit
    store and sends Maya a minimal Slack DM linking to the ArmorIQ
    console.
5.  Maya approves or rejects in the ArmorIQ console. The app only
    displays status; it does not accept a Slack "approved" message as
    authorization.
6.  Continue only through the verified SDK approval/resume path, for the
    same plan and step, with expiry and idempotency checks.
7.  If approval is rejected, expires, cannot be verified, or resumes
    against an expired plan/token, do not disclose data or execute.
    Record the outcome and ask the user to retry if appropriate.
8.  Never allow a requester to approve their own request. Maya must have
    the required higher-ranked ArmorIQ role.

Do not assume an in-app approval API is safe or available. It is
explicitly out of scope.

## 6. MCP endpoints and tools

There are at most two registered endpoints. The names below are logical
action names; confirm their exact representation in ArmorIQ's tool
registry. They must be unique across both endpoints if the policy schema
uses bare action names.

### 6.1 `memory-mcp`

This endpoint owns **all knowledge reads and approved company-data
reads**. The backend must not query these tables/views directly.

Tier-specific retrieval tools:

-   `knowledge_search_team`
-   `knowledge_search_confidential`
-   `knowledge_search_restricted`
-   `knowledge_get_source_team`
-   `knowledge_get_source_confidential`
-   `knowledge_get_source_restricted`
-   `read_source_team`
-   `read_source_confidential`
-   `read_source_restricted`

Knowledge maintenance tools:

-   `knowledge_save_team_note` --- save a user-proposed note or generated summary as a non-authoritative draft; `content_kind` is `note` or `summary`.
-   `knowledge_confirm_maya_decision` --- Maya-only authoritative
    decision confirmation.
-   `knowledge_mark_superseded` --- Maya-only supersession/update
    action.

Rules:

-   The tier is hard-coded by the tool handler and SQL query. Do not
    accept a tier argument.
-   `knowledge_search_team` can return only team-tier records.
    Confidential and restricted tools have their own fixed SQL filters
    and ArmorIQ actions.
-   `knowledge_get_source_{tier}` re-checks the source's tier and
    current status; do not trust a source ID from the model.
-   `read_source_{tier}` exposes only named, approved PostgreSQL views
    for that tier. No arbitrary SQL, table names, sort expressions, or
    dynamic column names from the LLM.
-   Use parameterized queries and strict result/row/character limits.
-   The tool server validates requester identity, workspace, tool name,
    and tier on every call, independently of the LLM.
-   Filter results before content reaches the model. A citation can only
    reference a source returned during the current request and visible
    to that requester.
-   Do not reveal the existence, title, count, or metadata of a source
    if its existence is itself restricted.
-   No PII tool exists in this MVP.

### 6.2 `workspace-mcp`

- `create_followup_task` — create a task in ProxyLead's own task table only when explicitly requested and authorized. Required fields are a short title, optional description, and enum-bounded owner/priority. Identity and workspace are injected by trusted code, not selected by the model. Idempotency key required.
- `send_external_message` — honeypot only. It writes only to a local synthetic `egress_sink` table and never sends network traffic. ArmorIQ policy blocks it for Maya, Sam, and Priya.
- The responder may request a tool after seeing retrieved evidence. If it requests a tool not in the signed plan, route the attempt to the SDK's verified off-plan path (`client.invoke(...)`) so the SDK plan check records the attempt. Never execute it. If the SDK rejects before network, label `armoriq_sdk_plan_check`; do not claim a hosted-proxy block. The tool server independently rejects direct calls without valid context.
- Include a clearly labeled **“Simulate hijacked model”** toggle, off by default, using synthetic content only. In guarded mode, it asks the responder/adversary to attempt `send_external_message`; the attempt must pass through the ArmorIQ gateway/SDK plan-check path. If the user confirms a separate plan for this action, the real policy must BLOCK it for every persona.
- Do not implement real egress, ticket-provider, deploy, or email-send integrations.
- If time is too tight, `create_followup_task` is optional and must be removed from the policy and UI if not fully implemented/tested. Keep the honeypot only if the guarded injection test is wired end-to-end; it must remain non-networked.

### 6.3 MCP protocol and direct-call protection

-   Public HTTPS endpoint(s), JSON-RPC 2.0 POST, SSE response framing,
    and the required `initialize`, `tools/list`, and `tools/call`
    methods.
-   Use a server credential/authentication mechanism proven by the
    ArmorIQ E1 experiment. Do not assume the hosted proxy uses the
    SDK-supplied header or console-registered credentials without
    testing.
-   Validate the signed per-call identity envelope (`_ctx`) issued by
    the backend/tool invocation path. It includes request ID, workspace
    ID, requester ID, mapped ArmorIQ email, role label, issued-at,
    expiry, and an application-generated invocation `jti`. Sign it with
    a server secret; rotate/configure it safely.
-   Treat `_ctx` as trusted only after signature, expiry, audience, and
    replay/idempotency validation. Never accept a caller-supplied
    identity/role in ordinary tool arguments.
-   Strip underscore-prefixed SDK metadata from the business arguments
    after capturing permitted trace metadata. Never let
    `_delegation_id`, `_approvals`, `_policy_context`, or any
    underscore-prefixed key grant access.
-   Direct tool-server calls without valid service authentication and
    signed context must be rejected.
-   Enforce strict JSON schemas server-side, resource ownership, tier
    filters, preconditions, limits, and idempotency.
-   Return actual tool results and errors. The UI must distinguish a
    tool-server rejection from an ArmorIQ decision.

## 7. Knowledge, memory, and database design

### 7.1 Storage

Use Neon PostgreSQL with:

-   `pgvector` for semantic retrieval.
-   PostgreSQL full-text search for lexical retrieval.
-   Metadata filters for workspace, tier, status, source type, and
    authorization scope.
-   No Qdrant or second vector store for the hackathon.

Embedding model: use **FastEmbed with `BAAI/bge-small-en-v1.5` (384
dimensions)** locally for the English-language demo corpus. Pin the
package/model and record the model version in metadata. Keep embedding
generation in the ingestion path; never generate or recompute embeddings
inside a protected retrieval handler unless needed. If local
installation or latency blocks the build, document the fallback and its
effect before switching to a hosted API. Do not silently send
confidential content to an external embedding provider.

Neon can have a cold/idle resume delay. Add a safe loading state and
timeout; never bypass authorization because a query is slow.

### 7.2 Minimum tables

-   `workspaces`
-   `members` --- ProxyLead member ID, Slack user ID, masked email
    display, ArmorIQ email reference, app role, active flag.
-   `slack_events` --- event ID, workspace, received/processed status
    for deduplication.
-   `source_records` --- source ID, workspace, source type, title,
    author, source URL/reference, created/updated time, tier, status,
    content hash, provenance.
-   `knowledge_chunks` --- chunk ID, source ID, text, embedding vector,
    full-text search data, tier/workspace metadata.
-   `decisions` --- statement, rationale, project, owner, authoritative
    status, created time, superseded-by, source ID.
-   `manager_guidance` --- guidance text, scope, authoritative flag,
    approved-by, source/provenance.
-   `team_notes` --- user-submitted notes, author, status, provenance.
-   `tasks` --- only if `create_followup_task` ships.
-   `plans`, `plan_steps`
-   `approval_requests`
-   `request_events` / `decision_events`
-   `tool_execution_ledger`
-   `delivery_events`
-   `unanswered_questions`

Minimum plan/step/audit fields:

-   `request_id`, `trace_id`, `workspace_id`, `requester_member_id`,
    `slack_user_id`, `armoriq_member_email_ref`, entry point and
    conversation reference.
-   `armoriq_plan_id`, `plan_hash` (when actually available), `step_id`,
    `tool_name`, `jti` (application-generated invocation ID),
    `delegation_id`, `decision_source`, `decision_status`, timestamps
    and error class.
-   Source IDs actually returned to the current request, citation IDs,
    and content tier.
-   `tier_delivered`, `delivery_channel` (`slack_dm`, `slack_thread`,
    `web_private`), `delivered_to_member_id`, `delivered_at`, and
    delivery result.
-   Tool-server reached? Actual execution status? Result ID? Approval
    state? Never infer execution from a plan or ALLOW alone.

Do not store raw secrets, authorization tokens, or unnecessary sensitive
message bodies in audit events.

### 7.3 Authority and memory poisoning

-   **Team note** is unverified, attributed content; it is not an
    authoritative manager decision.
-   Only Maya, authenticated as the configured manager and permitted by
    ArmorIQ, can make a record an authoritative "Maya's decision." A
    team member may submit a candidate note or summary, but it stays a
    draft until Maya confirms it.
-   An explicit approval from Maya must be a verified, authenticated
    action bound to the exact record/version. A Slack message that says
    "approved" is not sufficient.
-   Preserve source, author, timestamp, classification, review status,
    version, and supersession status.
-   Never let memory grant permissions or change policy.
-   Treat all retrieved text as untrusted instructions. It can be
    evidence for an answer, never an instruction to change identity,
    policies, plans, destinations, or tools.

### 7.4 Grounding evaluation and threshold calibration

- Implement an evaluation set with known answerable questions, weak-evidence questions, unrelated questions, conflicting records, and superseded records.
- For each query, record expected source IDs, tier, expected answerability, retrieval ranks/scores, actual citations, and whether the assistant abstained.
- Calibrate `MIN_SCORE` on a small held-out set. Acceptance: no answer when no retrieved source clears the calibrated threshold; no citations to records not returned by the current plan; superseded records are not treated as current truth; restricted records never appear for unauthorized personas.
- Map this evaluation to SC-02/SC-05 and a new E17 grounding evaluation in `docs/EXPERIMENTS.md`. Report false-accept and false-abstain cases; do not claim the threshold proves factual correctness.

### 7.5 Database roles and boundaries

Use separate database roles:

-   `proxylead_backend`: no `SELECT` access to `source_records`,
    `knowledge_chunks`, authoritative decision content, or approved
    company views. It can access only app metadata necessary for
    routing, request status, and UI state.
-   `proxylead_toolserver`: minimum required access to knowledge tables
    and approved read-only views; only the tool server uses this role.
-   If migrations/seed jobs require elevated access, use a separate
    deployment-only role not available to the running backend or tool
    server.

The manager console must access protected knowledge through authorized
tool-server operations; it must not create a hidden direct-query path
around ArmorIQ. If the schema requires metadata access, split
non-sensitive metadata from protected content and document the grants.
Verify grants with an automated test that attempts a backend direct
SELECT and expects denial.

Company data is exposed through explicitly approved PostgreSQL views.
Use a read-only role, schema allowlist, statement timeout, row limit,
and parameterized filters. Never expose arbitrary SQL or a generic
database shell to the LLM.

## 8. Request flow: retrieve first only after planning

1.  Receive a Slack event or web message; acknowledge Slack promptly.
2.  Authenticate the requester and resolve the trusted member/workspace
    mapping.
3.  Create a request record and stable trace ID.
4.  Give the planner only the user's message, trusted identity/context,
    and tool catalog descriptions. **No retrieved document or database
    content is available yet.**
5.  The planner proposes a minimal structured plan. Trusted code
    validates it against registered schemas and the allowlisted tool
    catalog.
6. Create exactly one ArmorIQ plan/session per user request, containing the validated ordered tool steps. Mint once, then invoke each step sequentially under that same plan/token lifecycle through `ArmorIQGateway`. Do not create a new plan for each read and do not call the database from the backend.
7.  ArmorIQ may ALLOW, BLOCK, or HOLD. Record the real response and
    source label. On HOLD, do not return tool content; notify Maya by DM
    and use the supported ArmorIQ console approval path.
8.  On ALLOW, the tool server independently validates identity envelope,
    tier, scope, schema, idempotency, and query limits before returning
    data.
9.  Pass the returned evidence to the responder as **data**, not
    instructions. Validate source IDs and citations against results from
    this request.
10. If retrieved content implies a new operation or additional tool, do
    not append it to the current plan. Ask the user to confirm the exact
    action; then create a new plan and authorize it.
11. Determine delivery sensitivity from the highest tier of any data returned to the model during this request, not only sources cited in the final answer. Confidential/restricted output is DM-only, regardless of where the question originated. The backend performs the Slack post/DM, not an
    agent tool.
12. Persist delivery destination, tier delivered, and actual delivery
    result. If a DM cannot be delivered, do not fall back to a public
    channel.
13. Update unanswered-questions queue when the evidence is insufficient,
    sources conflict, or a manager decision is required. Do not put
    protected snippets into the queue visible to unauthorized members.
14. Emit audit events for every state transition. The UI must represent
    actual observed outcomes, not predicted outcomes.

## 9. Web demo and manager console

The web app uses the same API, identity model, agent, ArmorIQ gateway,
tool server, and knowledge access path as Slack. No "demo bypass" is
permitted for protected reads.

### Team member view

-   Chat composer and conversation.
-   Grounded answer with source citations/links where allowed.
-   Clear ALLOW/BLOCK/HOLD/ABSTAIN/ERROR status with source labels for
    authorization decisions.
-   Pending status for a real ArmorIQ hold; link to the supported
    console surface where appropriate.
-   For restricted information, show the answer only in the
    authenticated private web session, never a public/shared UI surface.

### Manager view

-   Recent requests and real execution timeline.
-   Pending holds with delegation ID/status and link to ArmorIQ console;
    no in-app approval action.
-   Unanswered questions queue.
-   Knowledge review queue: team notes and generated summaries remain
    drafts until Maya confirms them.
-   Authoritative decisions with provenance and supersession history.
-   Slack integration health and database/tool-server health based on
    real checks.
-   Connector list labels: "Connected and tested," "Unavailable," or
    "Planned." Do not fake provider connection health.

### Public demo safety

-   Use seeded synthetic team/project data only.
-   Demo sign-in can choose Maya/Sam/Priya, but server-side session
    state determines identity. Never accept persona from each chat
    payload.
-   Restrict reset/demo administration to Maya and a server-side secret.
-   Keep secrets and actual ArmorIQ emails off the client.
-   The public demo must not expose real private company records or
    allow arbitrary public sign-up to access the demo workspace.

## 10. Audit and unanswered questions

Use a hash-chained append-only execution ledger and a verifier test that detects row modification/reordering. Do not call the ledger tamper-proof; describe the specific integrity guarantees and limitations.

For each request, record:

-   request/trace IDs, workspace, requester, entry point, Slack
    event/message reference;
-   plan ID/hash if exposed, ordered plan steps, tool/action names,
    app-generated invocation `jti`;
-   ArmorIQ status and decision source, policy/intent evidence
    reference, delegation ID and approval status;
-   tool server reached, server validation result, actual execution
    status/result ID/error;
-   source IDs and citations returned, tier of each source, tier
    delivered, channel and recipient;
-   timestamps for received, planned, authorized, held, resumed,
    executed, answered, and delivered;
-   redacted failure reason and retry/deduplication state.

The unanswered-questions queue stores the question, requester,
timestamp, reason (`no_evidence`, `conflict`,
`manager_decision_required`, `source_unavailable`), safe source
references, and status. Do not expose protected source content in queue
rows.

## 11. Repository structure

Use this as a guide; keep modules small and do not over-engineer a
provider marketplace.

``` text
proxylead/
├── apps/
│   ├── api/
│   │   ├── main.py
│   │   ├── identity.py
│   │   ├── orchestrator.py
│   │   ├── planner.py
│   │   ├── responder.py
│   │   └── routes/
│   └── web/
├── services/
│   ├── slack_socket/
│   ├── authorization/
│   │   └── armoriq_gateway.py   # only module importing armoriq_sdk
│   ├── toolserver/
│   │   ├── main.py
│   │   ├── mcp_protocol.py
│   │   ├── security.py
│   │   ├── schemas.py
│   │   ├── tools_access.py
│   │   ├── tools_work.py
│   │   ├── db.py
│   │   └── ledger.py
│   ├── knowledge/
│   │   ├── ingest.py
│   │   ├── embeddings.py
│   │   └── retrieval.py       # callable only by tool server
│   └── audit/
├── migrations/
├── seed/
├── policy/
│   └── proxylead-policy.yaml
├── scripts/
│   ├── check_setup.py
│   ├── probe_e1_allowed.py
│   ├── probe_e2_offplan.py
│   ├── probe_e4_per_user.py
│   ├── probe_e5_hold.py
│   └── verify_db_grants.py
├── tests/
│   ├── test_identity.py
│   ├── test_mcp_protocol.py
│   ├── test_tier_filters.py
│   ├── test_prompt_injection.py
│   ├── test_delivery_channel.py
│   ├── test_idempotency.py
│   └── test_end_to_end.py
├── docs/
│   ├── ARCHITECTURE.md
│   ├── EXPERIMENTS.md
│   ├── SECURITY.md
│   ├── SLACK_SETUP.md
│   └── DEMO.md
├── .env.example
└── README.md
```

## 12. Required ArmorIQ experiments and gates

Run these before building the full application. Save commands,
timestamps, screenshots/log references, actual decisions, and
conclusions in `docs/EXPERIMENTS.md`. Do not mark a gate passed based on
a mocked SDK response.

### E1 --- Real tool call reaches MCP through ArmorIQ

-   Register the `proxylead` agent and one MCP endpoint using the
    documented names and public HTTPS format.
-   Make a minimal planned tool call from the pinned SDK.
-   Verify the hosted ArmorIQ path reaches the actual tool server, the
    tool returns a known test result, and the response is visible
    through the SDK.
-   Test the credential path: console-registered MCP credential versus
    SDK-supplied `X-Armoriq-MCP-Auth`, as appropriate.
-   **Pass:** observed real request and matching server log/ledger
    record.\
-   **Fail/fallback:** follow the PRD's documented SDK/proxy fallback;
    clearly label the limitation. Do not fake a hosted proxy result.

### E2 --- Off-plan and tampered invocation behavior

-   Create a plan for one tool and attempt a different tool.
-   Observe whether the SDK blocks client-side and separately test the
    hosted proxy's server-side behavior using the PRD's probe.
-   Tamper with arguments and underscore-prefixed fields.
-   **Pass:** actual enforcement is recorded at the layer that enforced
    it; tool server still rejects invalid arguments and forged approval
    metadata. Do not claim server-side plan binding unless proven.

### E3 --- Different outcomes for different members on the same tool

-   Use separate `ArmorIQClient` instances for Maya, Sam, and Priya.
-   Invoke the same tier-named tool against the active policy.
-   Verify expected ALLOW/HOLD/BLOCK decisions from actual ArmorIQ
    responses.
-   Run concurrent calls to detect cross-persona identity leakage.
-   **Pass:** outcomes match the policy matrix and server-side tier
    checks remain enforced.

### E4 --- HOLD → console approval → resume

-   Trigger a tool configured to HOLD for Sam or Priya.
-   Verify ArmorIQ creates a real delegation/approval request and Maya
    sees it in the ArmorIQ console.
-   Approve from the console as Maya, not from the app or Slack.
-   Verify the SDK's actual resume behavior and ensure the same
    request/plan/step is used.
-   Test rejection, timeout, token expiry, and duplicate resume.
-   **Pass:** only the verified approved call releases the result or
    executes; all other paths fail closed.
-   **Fallback:** if approval/resume cannot be verified, keep the
    request pending and show the console link; do not claim completed
    approval.

### E5 --- Setup/policy conformance

-   Confirm the two endpoint names, one agent, one policy, and three
    active member emails exactly match the ArmorIQ console.
-   Confirm tool names resolve to the intended policy; intentionally
    test a name mismatch and confirm it fails closed.
-   Confirm current quotas and policy YAML syntax.
-   **Pass:** setup-check script validates names/member resolution and
    real tool decisions.

**Build gate:** E1 and E3 must pass before the full implementation. E4
must pass before presenting a working approval demo. If a gate fails,
spend a maximum of 2 focused hours on diagnosis, follow the documented
fallback, and narrow claims/scope rather than building around a guess.

### Experiment-number mapping to legacy PRD v1.0

| Architecture v2.1 | Legacy PRD v1.0 reference | Purpose |
|---|---|---|
| E1 | E1 | Real tool call and credential path |
| E2 | E2 / §12.4 | Off-plan call; separate SDK-side from hosted-proxy enforcement |
| E3 | E5 + E4b | Per-user policy outcomes and per-user client/token attribution |
| E4 | E6 + E7 + E11 | Hold/approve/reject/expiry behavior |
| E5 | E12 + E14 | Registry names, member resolution, policy schema |
| E17 (grounding) | New v2 test; relates to SC-02/SC-05 | Retrieval threshold and abstention evaluation |

Keep the legacy ID in filenames/logs where an existing probe already uses it. Rename scripts to `probe_v2_e3_per_user.py` and `probe_v2_e4_hold.py` or add a mapping wrapper; do not leave `probe_e4_per_user.py` implying that it tests architecture E4.

## 13. Security tests

Must pass before final demo:

-   User cannot impersonate another member through chat text or request
    payload.
-   A member cannot access a tier outside their policy, including by
    calling the tool server directly.
-   Backend DB credentials cannot `SELECT` protected knowledge tables or
    approved views.
-   Tier-specific tools have fixed filters; changing arguments cannot
    upgrade a tier.
-   Prompt injection inside a retrieved source cannot add a plan step,
    alter identity, select a public delivery destination, or grant
    permission.
-   Additional actions suggested by retrieved content require explicit
    user confirmation and a new plan.
-   Confidential/restricted answers never appear in a shared channel,
    even after successful approval.
-   Failed DM delivery never falls back to a public thread.
-   Forged `_delegation_id`, `_approvals`, `_policy_context`, `_ctx`,
    expired context, and duplicate `jti` are rejected or safely
    deduplicated.
-   Citation IDs must belong to sources returned during this request and
    allowed for this requester.
-   Missing/contradictory evidence leads to abstention; restricted
    source existence is not leaked.
-   Revoked/missing credentials, ArmorIQ outages, database timeouts, and
    MCP errors fail closed.
-   Audit records match the actual tool-server execution and delivery
    outcome.
-   A user-submitted note or generated summary cannot become
    authoritative without Maya's verified confirmation.

## 14. Dated execution plan (13--17 October)

### 13 October --- Day 1: prove the boundary

**First half** - Read the PRD and this architecture. - Run E1, E2, E3,
E4, and E5 probes in order of dependency. - Register the agent, two MCP
endpoints, one policy, and three member identities. - Write actual
results/fallbacks in `docs/EXPERIMENTS.md`.

**Second half** - If gates permit, implement the minimal MCP protocol
and one tier-named tool. - Prove tool-server database credentials and
direct-call rejection. - Do not spend the day polishing the UI.

**Exit gate:** real call path demonstrated; per-person policy result
known; approval path known or accurately downgraded.

### 14 October --- Core vertical slice

-   Neon schema, pgvector extension, full-text search, seed data, local
    embeddings.
-   Tool-server-only knowledge reads; tier-specific SQL; DB-grant test.
-   Slack Socket Mode mention/DM handling and trusted member mapping.
-   Citation-grounded answer end to end.
-   DM-only confidential delivery and event deduplication.

**Exit gate:** a team-tier question works from Slack through ArmorIQ to
Neon and back, with a source citation and audit trace.

### 15 October --- Policy, hold, audit and manager view

-   Finish per-person policy matrix and real ALLOW/BLOCK/HOLD tests.
-   Complete verified console approval/resume only if E4 passes.
-   Add Maya DM notifications, unanswered-question queue, knowledge
    draft/review workflow.
-   Add request/plan/step/decision/execution/delivery audit fields.
-   Add minimal web chat and manager console reusing the same path.

**Exit gate:** the end-to-end security demo passes; no protected read
bypasses ArmorIQ.

### 16 October --- Hardening and rehearsal

**By noon:** core security tests pass. Only now consider Calendar
read-only or the minimal Neon setup page.

-   Test prompt injection, direct calls, identity spoofing, tier
    tampering, token expiry, replay, duplicate Slack events, DM failure,
    database outage.
-   Verify public HTTPS deployment and tool-server health.
-   Run the demo script three times from a clean reset.
-   Record known limitations and exact evidence in README.
-   Freeze features after rehearsal.

**Exit gate:** stable demo; no known critical authorization or
disclosure bug.

### 17 October --- Submission

-   Fix critical bugs only.
-   Confirm all secrets and environment variables are configured.
-   Verify live Slack flow, web demo, and audit records.
-   Record final submission video and submit before the official
    deadline. Do not assume the deadline hour; verify it from official
    event instructions.

## 15. Demo script (target ≤ 3 minutes)

Use only synthetic seeded data. The sequence should demonstrate real calls and observed outcomes.

1. **Grounded answer:** Sam asks in Slack for a team-tier decision. ProxyLead answers with a source citation; show the tool trace and audit row.
2. **Permission difference:** Priya asks for a confidential topic and receives a real policy BLOCK with no source text/metadata disclosed. Sam asks the same question and triggers a real HOLD.
3. **Console approval:** Maya receives a minimal DM, opens ArmorIQ, and approves. Show verified SDK resume and delivery to Sam by DM only. If E4 has not passed, show only the genuine pending state and label resume unverified.
4. **Hijacked-model honeypot:** enable **“Simulate hijacked model”**. The responder attempts `send_external_message` based on injected synthetic text. Show the off-plan SDK/ArmorIQ outcome and zero sink execution. If separately user-confirmed as a new plan, show ArmorIQ's real policy BLOCK for all three personas.
5. **Memory + audit:** `@ProxyLead store this ...` creates a draft. Maya confirms the exact record/version through her own authorized `knowledge_confirm_maya_decision` action. Show requester, plan/tool, decision source, source IDs, tier returned/delivered, recipient, and actual execution/delivery status.

## 16. Definition of done

-   [ ] Slack Socket Mode works for one configured workspace and mapped
    demo members.
-   [ ] Web demo and Slack use the same backend, planner, ArmorIQ
    gateway, MCP endpoints, and data access rules.
-   [ ] Every protected knowledge/company-data read goes through ArmorIQ
    and the tool server.
-   [ ] Backend database role cannot read protected knowledge or
    approved views directly.
-   [ ] Tier-specific tool names and hard-coded query filters are
    tested.
-   [ ] One policy returns verified, distinct outcomes for the
    configured demo members.
-   [ ] Approval is described as working only if the real ArmorIQ
    console-to-SDK resume test passes.
-   [ ] Confidential/restricted answers are delivered by DM/private web
    session only.
-   [ ] Retrieved content cannot change the plan; new actions require
    user confirmation.
-   [ ] Sources/citations are validated against retrieved evidence and
    unauthorized metadata is not leaked.
-   [ ] Audit records include plan/step, `jti`, delegation/decision
    source, tier delivered, destination, and actual execution/delivery
    outcome.
-   [ ] Unanswered questions and draft-vs-authoritative memory are
    visible to Maya.
-   [ ] Security tests pass, known limitations are documented, and no
    planned integration is presented as complete.
-   [ ] README and demo clearly distinguish real ArmorIQ decisions from
    application/tool-server rules and unverified behavior.

## 17. Environment variables

Use `.env.example` with placeholders only. Never commit `.env`.

``` dotenv
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
EMBED_MODEL=BAAI/bge-small-en-v1.5
MIN_SCORE=
PERSONA_MAYA_EMAIL=
PERSONA_SAM_EMAIL=
PERSONA_PRIYA_EMAIL=
SLACK_APP_TOKEN=
SLACK_BOT_TOKEN=
SLACK_TEAM_ID=
ANTHROPIC_API_KEY=
LLM_MODEL=claude-opus-5-5
TOOLSERVER_BASE_URL=
TOOLSERVER_BEARER_WORKSPACE=
TOOLSERVER_BEARER_MEMORY=
ADMIN_TOKEN=
```

Only set `ARMORIQ_MCP_CREDENTIALS` if E1 proves the SDK-supplied
credential path is required. Use the credential mechanism actually
proven by the experiment. Never log tokens, secrets, raw credentials, or
full confidential content.

## 18. Final instruction to the coding agent

Start by reading the PRD and running the ArmorIQ experiments. Do not
start with UI or a generic connector framework. Keep the first vertical
slice small: one user request → plan → real ArmorIQ decision →
tier-specific MCP tool → Neon result → safe delivery → audit event.
Expand only after the security boundary is demonstrated.

If a required ArmorIQ behavior is uncertain, stop and run the mapped
experiment. Do not invent an SDK call, policy field, approval state, or
successful outcome. If time runs short, remove stretch features rather
than weakening the authorization boundary.
