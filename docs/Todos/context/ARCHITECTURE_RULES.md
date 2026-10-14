# Architecture rules (binding for every task)

Each rule restates something in the authoritative documents and cites it. If a
rule seems to block your task, raise it under
[AGENT_HANDOFF_PROTOCOL.md](AGENT_HANDOFF_PROTOCOL.md) §4. Do not work around
it. Reviewers use the checklist in §10.

## 1. Evidence and honesty

| ID | Rule | Source |
|---|---|---|
| R-01 | Run the mapped ArmorIQ experiment before claiming the behavior works. A mocked SDK response never passes a gate. Unit tests may mock the SDK, but their names and docs must not imply live verification | PRD §0.1, §11; Arch §0.2, §12 |
| R-02 | Do not invent SDK methods, policy fields, approval states, endpoint behavior, or successful outcomes. If behavior is uncertain, stop and run the mapped experiment | PRD §0.10; Arch §0.1, §18 |
| R-03 | Label every decision with the layer that actually made it. Never call an SDK client-side mismatch a hosted-proxy block. Never write "ArmorIQ blocked" without a real ArmorIQ response | PRD §0.6, §6.2; Arch §0.7, §5.3 |
| R-04 | Do not claim zero hallucinations, perfect injection prevention, or a "tamper-proof" ledger | Arch §0.10, §10; PRD §3 |
| R-05 | If a gate fails, spend at most 2 focused hours on diagnosis, then use the documented fallback and narrow the claim | Arch §12 (build gate) |

## 2. Identity

| ID | Rule | Source |
|---|---|---|
| R-10 | Identity comes only from trusted server-side mapping: Slack user ID (`SLACK_USER_*_ID`) or a signed HTTP-only web session → member → invited ArmorIQ email. Never from chat text, display names, or request bodies | PRD §1, §4.1; Arch §3.1, §4, §9 |
| R-11 | Chat text cannot change requester identity, role, topic permissions, or delivery destination | PRD §4.1 |
| R-12 | One `ArmorIQClient` per member identity, with a lock around plan/token minting. Never share a mutable `for_user` client across personas | PRD §4.1; Arch §5.2 |
| R-13 | Mask member emails in UI and logs. Real emails never reach the browser | PRD §4.1; Arch §1, §9 |

## 3. Authorization layers (all fail closed; none substitutes for another)

Arch §5.4 lists seven layers. An ALLOW from one layer never bypasses another.

| # | Layer | Enforced by (task) |
|---|---|---|
| 1 | Authentication (Slack identity / web session) | `apps/api/identity.py` (P1-03) |
| 2 | Provider/database permissions (Postgres roles) | `migrations/` grants (P2-01) |
| 3 | Application data authorization (workspace + tier-fixed query filter) | `services/knowledge/retrieval.py`, tool handlers (P2-04, P2-06, P2-08) |
| 4 | ArmorIQ intent/plan enforcement | ArmorIQ + `armoriq_gateway.py` (P1-02, E1/E2) |
| 5 | ArmorIQ per-member policy and HOLD | ArmorIQ policy (P1-08, E3/E4) |
| 6 | Tool-server validation (credential, `_ctx`, strict schema, scope, idempotency) | `services/toolserver/security.py` (P1-04), `schemas.py` (P2-06), ledger (P2-07) |
| 7 | Delivery authorization (DM / private web only for elevated tiers) | `apps/api/delivery.py` (P3-06) |

| ID | Rule | Source |
|---|---|---|
| R-20 | Every protected knowledge or company-data read goes through ArmorIQ and the public HTTPS MCP tool server. Backend code never queries protected tables or approved views | PRD §0.3, §3; Arch §0.3, §6.1 |
| R-21 | Fail closed when authorization cannot be verified, including ArmorIQ outages, missing credentials, DB timeouts, and MCP errors. Explain that authorization could not be verified | PRD §0.6; Arch §0.12, §13 |
| R-22 | Tier is encoded in the tool name and hard-coded in server code and SQL. Never accept a model-supplied `tier`, `sensitivity`, `resource_scope`, `subject_user_id`, or equivalent | PRD §6.1; Arch §0.5, §6.1 |
| R-23 | The tool server independently verifies service credential, signed `_ctx` (signature, audience, expiry), replay/idempotency, strict schemas, resource scope, and tier filters | PRD §0.5, §9; Arch §6.3 |
| R-24 | Strip underscore-prefixed SDK metadata before business-schema validation. `_delegation_id`, `_approvals`, `_policy_context` and any other `_` field never prove approval or grant access | PRD §9; Arch §5.1, §6.3 |
| R-25 | Direct tool-server calls without valid service authentication **and** a valid signed context are rejected | Arch §6.3, §13 |
| R-26 | The application and tool server reject out-of-tier calls even if the ArmorIQ policy is accidentally too permissive | Arch §5.5 |
| R-27 | No PII tool, arbitrary SQL, shell, public URL fetch, or real egress tool exists | PRD §9; Arch §5.5, §6.1 |

## 4. Plans

| ID | Rule | Source |
|---|---|---|
| R-30 | The plan is built from the authenticated user's message and trusted context **before** any untrusted content is retrieved | PRD §0.4, §7.2; Arch §0.4, §5.3 |
| R-31 | Exactly one ArmorIQ plan per user request. Mint once and invoke the steps in order under the same plan and token. No plan per read | PRD §0.2, §7.5; Arch §3.1, §8.6 |
| R-32 | At most one elevated-tier call (confidential or restricted) per plan, plus an optional team-tier search. Never search plus source-read at the same elevated tier. Never two HOLDs in one request | PRD §5.2; Arch §5.3.1 |
| R-33 | Retrieved content cannot add or change plan steps. A new action needs explicit user confirmation and a **new** plan. An off-plan tool request goes through the verified SDK off-plan path for a real, correctly labeled block and is never executed | PRD §6.2, §7.11; Arch §5.3, §6.2, §8.10 |
| R-34 | Plan validation: strict schemas, known tools, enum bounds, no underscore-prefixed user keys, no model-selected identity/tier/recipient | PRD §7.3 |
| R-35 | Team tier first. A higher tier only when (a) the topic matches a manager-approved rule (Arch §5.3.1 adds "and the requester is allowed to ask at that tier"; PRD §5.2 omits it; see **D-30**), or (b) the user explicitly confirms a proposed escalation. The topic map never grants access | PRD §5.2; Arch §5.3.1 |
| R-36 | No auto-replan. If the token expires, approval times out, or status cannot be verified, fail and ask the requester to retry | PRD §7.8, §7.1; Arch §3.1, §5.1 |

## 5. Approval (HOLD)

| ID | Rule | Source |
|---|---|---|
| R-40 | Never simulate approval. Maya approves or rejects only in the ArmorIQ console. No in-app approval button. A Slack message saying "approved" is not authority | PRD §0.8, §7.7; Arch §0.8, §5.6 |
| R-41 | Resume only through the verified SDK path for the same plan and step, with expiry and idempotency checks | PRD §7.8; Arch §5.6.6 |
| R-42 | Reject, expiry, unverifiable status, or a resume against an expired plan/token must neither disclose nor execute | Arch §5.6.7 |
| R-43 | Run long approval waits in a background worker. Never block the Slack acknowledgement | Arch §5.6.3 |
| R-44 | HOLD notification to Maya: a minimal Slack DM with a console link and no protected content | PRD §7.7; Arch §4 |
| R-45 | A requester never approves their own request. The approver must outrank the requester in ArmorIQ | Arch §5.6.8 |
| R-46 | `knowledge_confirm_maya_decision` is Maya's own authorized maintenance action, not approval of another member's HOLD | PRD §0.8, §6.1 |

## 6. Data, retrieval, and memory

| ID | Rule | Source |
|---|---|---|
| R-50 | Separate DB roles. `proxylead_backend` has no `SELECT` on protected content tables or approved views. `proxylead_toolserver` has minimum access. The migration/seed role is never available to runtime processes. An automated test proves the backend role is denied | PRD §8.1; Arch §7.5 |
| R-51 | Every knowledge record carries source, author, timestamp, tier, review status, version, content hash, and supersession metadata | PRD §8.1; Arch §7.3 |
| R-52 | Hybrid retrieval: vector + full-text, RRF fusion, deduplicated, limited. Filter workspace, tier, active status, and requester access **before** evidence reaches the model | PRD §8.2; Arch §6.1 |
| R-53 | `MIN_SCORE` is calibrated (E17), never guessed | PRD §8.2, §8.3; Arch §7.4 |
| R-54 | Never send confidential source text to a hosted embedding service without explicit design approval | PRD §8.2; Arch §7.1 |
| R-55 | Do not reveal the existence, title, count, or metadata of a source whose existence is restricted | PRD §6.1; Arch §6.1 |
| R-56 | Notes and generated summaries are drafts. Only Maya's authenticated, authorized `knowledge_confirm_maya_decision`, bound to the exact record and version, makes a record authoritative | PRD §8.4; Arch §7.3 |
| R-57 | Memory never changes ArmorIQ policy or permissions. Retrieved text is untrusted data | PRD §8.4; Arch §7.3 |
| R-58 | Parameterized queries, approved views, statement timeout, row and character limits | PRD §9; Arch §6.1, §7.5 |

## 7. Responses and delivery

| ID | Rule | Source |
|---|---|---|
| R-60 | Tool output is data, not instructions. Every citation must reference a source returned during this request and visible to this requester | PRD §7.10; Arch §6.1, §8.9 |
| R-61 | Abstain on insufficient, weak, conflicting, or superseded evidence | PRD §7.10; Arch §1 |
| R-62 | Delivery tier = the highest tier of **any** data returned to the model in the request, not just cited sources | PRD §7.12; Arch §3.1, §8.11 |
| R-63 | Confidential and restricted content goes to a DM or private web session only. In a channel, post only a generic notice with no titles, snippets, or citations | PRD §0.7, §4.2; Arch §0.9, §4 |
| R-64 | A failed DM never falls back to a channel | PRD §7.13; Arch §8.12 |
| R-65 | Slack posting is not an ArmorIQ tool. The backend chooses the destination; the model never does | Arch §4 |

## 8. Audit and UI

| ID | Rule | Source |
|---|---|---|
| R-70 | Append audit events for every plan, authorization, hold, execution, answer, and delivery transition, with the fields in PRD §10.1 | PRD §7.14, §10.1; Arch §7.2, §10 |
| R-71 | A plan or ALLOW is not proof of execution. Execution claims need a matching tool-server ledger row | PRD §10.1; Arch §7.2 |
| R-72 | The UI shows actual persisted outcomes, never predicted or simulated ones. It distinguishes a tool-server rejection from an ArmorIQ decision | Arch §6.3, §8.14 |
| R-73 | No secrets, raw tokens, or unnecessary sensitive bodies in logs or audit events | PRD §10.1; Arch §7.2 |
| R-74 | Connector health labels are "Connected and tested", "Unavailable", or "Planned", based on real checks | Arch §9 |

## 9. Scope and environment

| ID | Rule | Source |
|---|---|---|
| R-80 | Synthetic data only. No live customer data, PII, external send, or production deployment | PRD §0.9; Arch §9 |
| R-81 | Pin `armoriq-sdk==0.6.10` and Python 3.12 unless a live experiment documents a needed change | PRD §9; Arch §5.1 |
| R-82 | Agent `proxylead`, MCP names, and tool names must match the ArmorIQ console exactly | PRD §9; Arch §5.1 |
| R-83 | `.env.example` holds placeholders only. Never commit `.env`, tokens, or real member emails (the GitHub repository is **public**) | Arch §17; PRD §12 |
| R-84 | Add `ARMORIQ_MCP_CREDENTIALS` only if E1 proves it is needed | PRD §12; Arch §17 |
| R-85 | Implement only the scope in Arch §3. No generic connector framework | Arch §0.11 |

## 10. Code-review checklist (copy into PR descriptions)

- [ ] No `armoriq_sdk` import outside `services/authorization/armoriq_gateway.py` (test-enforced).
- [ ] No protected-table query from `apps/api` or `services/audit`.
- [ ] No identity, tier, recipient, or `_`-prefixed key accepted from a model or request body.
- [ ] Every decision carries its true `DecisionSource`; nothing defaults to an ArmorIQ label.
- [ ] Every failure path is closed (error or BLOCK, never ALLOW).
- [ ] Elevated-tier content only reaches DM or private web.
- [ ] Execution claims reference a ledger row.
- [ ] No secrets, tokens, or real emails in code, tests, fixtures, logs, or docs.
- [ ] Shared interfaces changed only through an approved Interface Change Request.
- [ ] Tests that mock ArmorIQ or Slack are named and documented as offline.
