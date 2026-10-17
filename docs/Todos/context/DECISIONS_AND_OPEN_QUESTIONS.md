# Decisions and open questions

Planning tracker for conflicts and open questions in the authoritative
documents, and for decisions the plan needs. It **extends** the existing list
(U-1..U-14) in `docs/ARCHITECTURE_DECISIONS.md`. When a decision is made,
record an ADR there and mark the row here `RESOLVED → ADR-0xx`. The
ADR log is the source of truth for resolved decisions.

Nothing below is a settled requirement unless marked RESOLVED. A **PROPOSED
default** is a suggestion that lets work continue. It needs the decider's
confirmation and must not be presented as a requirement.

Columns: **Needed before** = the first task that cannot finish (or start)
without the decision. **Blocking?** = `start` (cannot begin), `finish` (can
begin, cannot be accepted), or `defer` (can be decided later).

## 1. Conflicts between PRD v2 and Architecture v2.1

### D-01 — Tool module file names (= U-13)

**RESOLVED 14 Oct 2026 → ADR-027:** PRD names `tools_memory.py` and `tools_workspace.py`. Task files now use these names.

- **PRD §3.1:** `services/toolserver/{…,tools_memory.py,tools_workspace.py,…}`
- **Arch §11:** `services/toolserver/{…,tools_access.py,tools_work.py,…}`
- Neither document resolves the conflict. Observation only: the PRD names match
  the endpoint names (`memory-mcp`, `workspace-mcp`), and Arch §2's Mermaid
  diagram uses the node IDs `Access` and `Work` for those endpoints.
- **Until resolved**, task files say `{MEMORY_TOOLS_MODULE}` (PRD
  `tools_memory.py` / Arch `tools_access.py`) and `{WORKSPACE_TOOLS_MODULE}`
  (PRD `tools_workspace.py` / Arch `tools_work.py`).
- Decider: `Aditya-Rawat01` with `VTCodeCraft`. Needed before **P2-06**. Blocking: **start**.

### D-02 — Probe and eval script names (= U-14)

**RESOLVED 14 Oct 2026 → ADR-017:** PRD names (`probe_v2_e1_allowed.py` … `probe_v2_e5_setup.py`, `eval_e17_grounding.py`). Task files now use these names.

- **PRD §3.1:** `scripts/{check_setup.py, probe_v2_e1_allowed.py, probe_v2_e2_offplan.py, probe_v2_e3_per_user.py, probe_v2_e4_hold.py, probe_v2_e5_setup.py, eval_e17_grounding.py, verify_db_grants.py}`
- **Arch §11:** `scripts/{check_setup.py, probe_e1_allowed.py, probe_e2_offplan.py, probe_e4_per_user.py, probe_e5_hold.py, verify_db_grants.py}`. There is no E5-setup probe and no E17 script, and two names carry legacy numbers (`probe_e4_per_user` tests Arch E3; `probe_e5_hold` tests Arch E4).
- **Arch §12** (last paragraph) says to rename to `probe_v2_e3_per_user.py` and `probe_v2_e4_hold.py` or add a mapping wrapper, and not to leave `probe_e4_per_user.py`.
- **Until resolved**, task files say `{E1_PROBE}`, `{E2_PROBE}`, `{E3_PROBE}`, `{E4_PROBE}`, `{E5_PROBE}`, `{E17_EVAL}`. The PRD names are given first in each task.
- Decider: `samarth-chawla`. Needed before **P1-06**. Blocking: **start** (file names only).

### D-03 — Decision-source label set

**RESOLVED 14 Oct 2026 → ADR-019:** keep the ADR-009 union.

- **PRD §10.1:** `armoriq_issuance`, `armoriq_sdk_plan_check`, `armoriq_proxy_decision`, `tool_server_rule`, `application_rule`, `unknown`.
- **Arch §5.3:** `sdk_plan_check`, `armoriq_proxy_decision`, `armoriq_policy_decision`, `tool_server_rule`, `application_rule`, `unknown`.
- **Arch §6.2 and PRD §6.2** both write `armoriq_sdk_plan_check`.
- Scaffold ADR-009 uses the union with `armoriq_sdk_plan_check` (`DecisionSource` enum). Status: **PROPOSED default (already in code)**. Confirm, or change via ICR.
- Decider: `samarth-chawla`. Needed before **P1-02** finishes. Blocking: **finish**.

### D-04 — `_ctx` field set

**RESOLVED 14 Oct 2026 → ADR-020:** union of fields without the raw email (`requester_member_id` + `armoriq_member_email_ref` env-key name).

- **PRD §9:** requester/workspace, request, plan/step, tool, audience, expiry, app-generated `jti`.
- **Arch §6.3:** request ID, workspace ID, requester ID, mapped ArmorIQ email, role label, issued-at, expiry, `jti`.
- PROPOSED default: the union (SHARED_INTERFACES §5). Open sub-question: putting the raw ArmorIQ email (Arch §6.3) in `_ctx` versus the masking rule (PRD §4.1, which covers UI/logs). If kept, the tool server must never log it.
- Decider: `samarth-chawla`. Needed before **P1-04**. Blocking: **finish**.

### D-15 — Planner input scope

**RESOLVED 16 Oct 2026 by `VTCodeCraft` ([issue #20](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/20); no ADR):** message only. The planner sees the user's message, the trusted identity context and the tool catalog; there is no conversation-history parameter. Implemented in P3-02 (`apps/api/planner.py`): the message travels as one nonce-delimited, HTML-escaped, untrusted block, capped at 4000 characters.

- **PRD §7.2:** the planner sees "the user's message, trusted display context, conversation text allowed by policy, and tool catalog".
- **Arch §5.3 and §8.4:** "only the user's message, trusted identity/context, and tool catalog descriptions".
- Open: whether any prior conversation text is included, and from where (Slack history needs scopes; PRD §4.2 says never assume history access).
- Decider: `VTCodeCraft`. Needed before **P3-02**. Blocking: **finish**.

### D-16 — Demo reset versus runtime database roles

- **Arch §9:** "Restrict reset/demo administration to Maya and a server-side secret" (`ADMIN_TOKEN`, PRD §12).
- **PRD §8.1:** the migration/seed role is "unavailable to runtime processes". **Arch §7.5:** the deployment-only role is "not available to the running backend or tool server".
- A runtime reset endpoint therefore cannot reseed protected tables. Open: what "reset" covers (audit/app metadata only? an operator-run seed script?).
- Decider: `parthgoyal26-03-2005` with `Aditya-Rawat01`. Needed before **P5-03**. Blocking: **finish**.

## 2. Open questions the documents leave unresolved

### D-05 — `_ctx` and underscore arguments in transit (extends U-3)

- Do underscore-prefixed arguments (the backend's `_ctx`, and the SDK's
  `_policy_context` / `_delegation_id` / `_approvals`) reach the tool server
  through the ArmorIQ proxy (E1/E2)?
- Do they reach the handler through `mcp==2.3.0` argument handling? The MCP SDK
  builds argument models from handler signatures and may drop or reject keys
  it does not know. This must be verified in code, not assumed.
- If either fails, the fallbacks need a decision: a raw-request hook, the
  ADR-006 hand-written transport, or another binding. Fail-closed behavior
  stays.
- Decided by: code inspection **inside P1-04** (MCP SDK half), then E1 (P1-06) and E2 (P1-07) (proxy half).
  Blocking: **defer**. P1-04 merges with fail-closed verification; a negative E1/E2 result triggers a follow-up ICR/fallback task. P1-04 never waits on E1, which itself needs P1-04.

### D-06 — MCP credential path and transport compatibility (= U-1, U-2)

- Console-registered credential versus SDK-supplied `X-Armoriq-MCP-Auth`
  (PRD §9, Arch §6.3). Whether proxy requests satisfy the streamable-HTTP
  transport (ADR-006).
- Decided by **E1** (P1-06). Blocking: **defer** for P1-04 (the bearer guard stays until E1 decides, then a follow-up ICR). Needed before **P5-03** finishes.

### D-09 — Are draft notes and summaries returned by search?

**RESOLVED 16 Oct 2026 (`VTCodeCraft` + `Aditya-Rawat01`): drafts are never search evidence.**

1. `knowledge_search_*` returns only `status='active'` rows whose
   `provenance->>'review_status'='authoritative'` (missing/unknown = excluded).
   Implemented in P2-04 (`services/knowledge/retrieval.py`); anyone can save a
   draft, so drafts are a poisoning risk, never evidence.
2. P2-09 does not chunk/embed drafts; drafts are visible only in Maya's
   metadata-only `draft_review_queue` until she confirms (then authoritative
   and ingested).
   **Clarified 17 Oct 2026 by `VTCodeCraft` (co-decider; [issue #24](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/24)):**
   a confirmed note is authoritative AND searchable evidence. In the confirm
   transaction the tool server ingests it as the team-tier source
   `note-<team_notes.id>` (`source_records` provenance `review_status=
   authoritative`, `version`, `superseded_by=null`, `origin=team_note`) with
   chunks and embeddings in the P2-03 layout, using the model loaded at
   startup (no model → `internal`, nothing written). On supersede that
   source becomes `status='superseded'` (with `superseded_by`) and drops out
   of search and `get_source`. Implemented in the PR #79 review fixes.
3. Superseded records are excluded from search; explicit source-read tools
   (P2-08) reach them flagged, never presented as current. The responder
   already never cites drafts (P3-03).

- PRD §8.4 and Arch §7.3 say drafts are non-authoritative and carry
  provenance. PRD §8.2 filters on "active status" (Arch §7.1: "status"). Neither says
  whether drafts are retrievable as evidence or how the responder must label
  them.
- Poisoning risk: any member can save a draft (PRD §4.2 `store this`).
- Decider: `Aditya-Rawat01` with `VTCodeCraft`. Needed before **P2-04** (filters) and **P3-03** (labeling). Blocking: **finish**.

### D-10 — Tier of saved notes

- `knowledge_save_team_note` (PRD §6.1) has "team" in its name, but no section
  states the stored tier. Open: always `team`? What about notes saved from a DM
  that discuss confidential topics?
- Decider: `Aditya-Rawat01` with `samarth-chawla`. Needed before **P2-09**. Blocking: **finish**.

**PROPOSED 16 Oct 2026 by `Aditya-Rawat01` — PENDING `samarth-chawla`'s
confirmation (co-decider).** The earlier "relayed" confirmation is not
visible anywhere, so D-10 is **not resolved** until `samarth-chawla` confirms
on [issue #24](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/24)
(requested there by `VTCodeCraft`, 17 Oct 2026). P2-09 (PR #79) implements
rules 1–3 and 5 as the working default; no ADR yet. (D-09 rule 2 is
co-decided by `VTCodeCraft` and is unaffected.)

1. A saved note is **always `team`-tier and always a draft** — including notes
   saved from a DM that discuss confidential topics (the requester's own
   words, attributed to them, never authoritative until Maya confirms).
2. Drafts are **never chunked or embedded**, so they are never searchable
   evidence (D-09 rule 2). The P2-09 ingestion step is deferred to Maya's
   confirmation: no `knowledge_chunks` row is written for a draft; a
   confirmed note is ingested as source `note-<id>` (D-09 rule 2
   clarification, 17 Oct 2026).
3. Drafts are visible only in Maya's metadata-only review queue
   (`draft_review_queue`, ADR-023) until she confirms them.
4. The backend **refuses** a `store this` (`knowledge_save_team_note`)
   `application_rule` in a thread where above-team-tier evidence was
   delivered — a tier-purity rule at the planner, not in the tool server.
   **UNOWNED FOLLOW-UP: not implemented by any task yet.** P3-04 (the
   orchestrator knows which tiers a thread has delivered) and P3-05 (the
   `store this` entry point) must pick it up. Until then a note saved in a
   thread that delivered confidential content is stored team-tier, and
   once Maya confirms it, it is team-tier evidence (D-09 rule 2).
5. Migration **0013** (claimed on issue #24 before coding as 0012; renumbered
   when main's P2-10 follow-up took 0012 — re-claim on #24) encodes (1) as a
   schema invariant: `team_notes.tier TEXT NOT NULL DEFAULT 'team' CHECK
   (tier IN ('team'))`, plus `provenance` and `approved_by_member_id` /
   `approved_at` columns (PRD §8.1). `team_notes` is toolserver-exclusive, so
   no ICR and no shared-interface change. The PR #79 review fixes also make
   `draft_review_queue` list current drafts only (same columns), add a
   no-self-supersede CHECK, and restrict the toolserver to INSERT/SELECT plus
   UPDATE of the lifecycle columns (no DELETE).

### D-11 — Elevated source tools versus the one-elevated-call rule (= U-9)

**RESOLVED 16 Oct 2026 by `VTCodeCraft` with `Aditya-Rawat01` ([issue #22](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/22), ICR approved; no ADR, no contract schema change). Implemented in P2-08 (PR #71).**

1. `source_ref` is the server-issued `source_id` text. It is approved
   **because** every `knowledge_get_source_*` call re-checks server-side, in
   one fixed parameterized query: the verified-`_ctx` workspace, the tool's
   exact tier (a handler literal), the caller's tier ceiling (dispatch step
   1.3, before the handler), `status='active'` and
   `review_status='authoritative'`; the excerpt comes only from a chunk with
   the source's own workspace and tier. A model-supplied id grants nothing
   beyond what the caller may already read at that tier.
2. View names `approved_company_data_{team,confidential,restricted}` are
   final (migration 0011; rows from `seed/company_projects.json`). The
   toolserver role reads only the views; no runtime role reads or writes the
   base table.
3. New SHARED_INTERFACES §4 code `not_found`: missing, wrong-tier, and
   inactive lookups return byte-identical bodies. It is an executed empty
   lookup, not a rejection: the tool server records an `accepted`/`executed`
   ledger row (or answers `internal` if that row cannot be stored), and the
   gateway maps it to `EXECUTED` with no result and keeps the ArmorIQ
   decision, never a `tool_server_rule` block; a `not_found` without its
   ledger reference becomes `internal` (review finding I1).
4. `read_source_*` returns current rows only. `SourceRow` has no structured
   status, so `include_superseded=true` is rejected with `invalid_arguments`
   (fail closed, D-09 rule 3) until an ICR adds such a field (review finding
   I2).
5. Member tier ceiling (SHARED_INTERFACES §6) confirmed with it: manager →
   `restricted`, senior → `confidential`, junior → `team`; unknown roles fail
   closed (`out_of_scope`).

Residual risks: timing of the single re-check query is not padded (same query
path for every case; normal variance only). The follow-up flow after an
elevated search stays PRD §5.2's "confirm a new request" (P3-02/P3-04).

- PRD §5.2 / Arch §5.3.1 forbid an elevated search plus an elevated source read
  in one plan, and say "if a source needs a separate read, stop and ask the user
  to confirm a new request".
- Open: how the follow-up request identifies the source without trusting a
  model-supplied ID (Arch §6.1 says re-check tier and status; do not trust a
  model source ID). Also: which approved views exist per tier.
- Decider: `Aditya-Rawat01` with `VTCodeCraft`. P2-02 ships a PROPOSED opaque source-reference model (revised by ICR); final answer needed before **P2-08** finishes. Blocking: **finish** (P2-08 only).

### D-12 — Manager console access to protected content (= U-8)

**RESOLVED 14 Oct 2026 → ADR-023:** option (b), a metadata-only view (no title or content); content through `knowledge_get_source_team` via ArmorIQ; no new tool.
**Open follow-up (P2-09 review I4, 17 Oct 2026):** the content path does not work for drafts (`get_source` answers `not_found` for non-authoritative records) and there is no listing of current authoritative notes for supersede, so Maya confirms blind. A P4-04 ICR is required before the console ships confirm/supersede (SHARED_INTERFACES §7 "OPEN FOLLOW-UP").

- PRD §10.2 / Arch §9 require a draft review queue and authoritative decisions
  with provenance.
- PRD §8.1 / Arch §7.5 deny the backend `SELECT` on protected tables. Arch §7.5
  says the console "must access protected knowledge through authorized
  tool-server operations" or "split non-sensitive metadata from protected
  content and document the grants".
- No listing tool exists in PRD §6. Options to decide:
  - (a) a new Maya-only tool, which needs PRD/Arch-consistent naming, a policy
    entry, and console registration;
  - (b) a metadata-only table or view readable by the backend;
  - (c) reusing search tools.
- Decider: `samarth-chawla`, `Aditya-Rawat01`, `parthgoyal26-03-2005`. **Decide by 15 Oct AM, before P1-08 (E5)**: option (a) adds a tool that must be registered and in the policy before E5/E3.
- Implementation owner per option:
  - (a) `Aditya-Rawat01` extends P2-09 by ICR; `samarth-chawla` adds it to the policy (P1-08);
  - (b) `Aditya-Rawat01` adds a migration; `parthgoyal26-03-2005` adds the query (P3-01);
  - (c) `VTCodeCraft` (planner use);
  - panels in all cases: `parthgoyal26-03-2005` (P4-04).
- These are P0 items (PRD §2, §10.2; Arch §16). Shipping without them requires an explicit, recorded descope in the README limitations (P5-04), never a silent "unavailable" panel.
- Blocking: **finish** for P4-04.

### D-13 — Plan schema limits (= U-7)

**RESOLVED 16 Oct 2026 by `VTCodeCraft` ([issue #20](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/20); no ADR):** `MAX_PLAN_STEPS = 3`, enforced by the planner. No `intent` or clarifying-question field is added to the contracts, so no ICR. Clarifications are a planner result variant only (SHARED_INTERFACES §12).

- Maximum steps, an `intent` field, and a clarifying-question output. Bounded
  by PRD §5.2 (≤ 1 elevated call plus an optional team search). Per-tool
  argument schemas are handled by P2-02.
- Decider: `VTCodeCraft`. Needed before **P3-02**. Blocking: **finish**.

### D-14 — User confirmation mechanism

**RESOLVED 16 Oct 2026 by `VTCodeCraft` ([issue #20](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/20); no ADR; co-decider `samarth-chawla` confirmation pending):** the confirmation hook is approved. `EscalationConfirmation{tier, token}` authorizes nothing unless the `confirmation_verifier` (supplied by **P3-04**) accepts it; a missing verifier, any value other than `True`, or an exception fails closed, and model text is never a confirmation. Binding (as implemented in P3-02): an `Escalation` carries the validated proposal and its `plan_digest`; the verifier is called as `(confirmation, ctx, plan_digest)`, and the confirmed call re-validates the stored proposal with no new model call. P3-04 mints the token, binds it to the requester and that digest, and stores the `Escalation`. Interface: SHARED_INTERFACES §12.

- PRD §5.2 (confirm a safe escalation prompt), PRD §7.11 / Arch §5.3 and §8.10
  (confirm a new action, then a new plan).
- Open: how a confirmation is represented and bound to the exact proposed
  action in Slack and web. It must not be something the model can produce by
  itself.
- Decider: `VTCodeCraft` with `samarth-chawla`. Needed before **P3-02** and **P3-04**. Blocking: **finish**.

### D-17 — Does `create_followup_task` ship?

**RESOLVED 14 Oct 2026 → ADR-024:** ships only if implemented and tested by 15 Oct afternoon; otherwise removed from the policy and UI. **Outcome: ships** (P2-10, PR #73; recorded under ADR-024).

- Optional (PRD §6.2). Arch §6.2: if not fully implemented and tested, remove it
  from the policy and UI.
- Decider: `samarth-chawla` (P2-10 owner) with the team, by **16 Oct 12:00**
  (feature freeze, PRD §13). Blocking: **defer**.

### D-18 — Do experiment probes import `armoriq_sdk` directly?

**RESOLVED 14 Oct 2026 → ADR-018:** probes call only `ArmorIQGateway`; the guardrail test also scans `scripts/`.

- PRD §3 and §9 / Arch §2 say `ArmorIQGateway` is "the only module importing
  `armoriq_sdk`".
- PRD §11 E1 step 4 / Arch §12 E1 say probes "use the pinned SDK". The
  existing guardrail test scans `apps/` and `services/` only.
- Options: probes call the gateway (they then also test the real code path), or
  probes import the SDK directly as discovery tools outside runtime packages.
- Decider: `samarth-chawla`. Needed before **P1-06**. Blocking: **start**.

### D-19 — Parallel development before the build gate

**RESOLVED 14 Oct 2026 → ADR-015:** the proposed rule is accepted.

- Arch §0.2 and §12: "Do not build the full product until" E1/E3 pass (or a
  fallback is recorded); "E1 and E3 must pass before the full implementation".
  On 14 Oct no experiment has run, and Day-1 has slipped.
- PROPOSED planning rule:
  - Backend modules with no dependence on unverified ArmorIQ behavior (schema,
    retrieval, planner, responder, Slack adapter, audit store) may be developed
    against fakes now.
  - Merging real gateway integration into the orchestrator needs G1.
  - **UI work (Phase 4) starts only after G1 + G3.**
  - No feature is "done" before its gates.
- Decider: whole team, **today**. Blocking: **start** for Phase 3/4 work.

### D-20 — Member and workspace bootstrap

**RESOLVED 14 Oct 2026 → ADR-021:** env is the source; the seed writes stable IDs; `armoriq_member_email_ref` stores the env-key name.

- PRD §4.1: mapping from env variables. Arch §4: "bootstrap … from
  environment/config or an explicit manager setup step". Arch §7.2: `members`
  has "masked email display, ArmorIQ email reference".
- PROPOSED default:
  - The seed job creates `workspaces` / `members` rows from env at seed time.
  - The `armoriq_member_email_ref` column stores the env key
    (`PERSONA_SAM_EMAIL`), not the raw email.
  - `identity.py` resolves from env + row IDs.
- Decider: `VTCodeCraft` (P1-03) with `Aditya-Rawat01` (P2-01/P2-03). Needed before **P1-03** finishes. Blocking: **finish**.

### D-21 — Web API paths and async result delivery

- Not specified by PRD/Arch. PROPOSED in SHARED_INTERFACES §1.1 (polling).
- Decider: `VTCodeCraft` with `parthgoyal26-03-2005`. Needed before **P4-01**. Blocking: **finish**.

### D-22 — HOLD console link and approver observability (extends U-5, U-11)

- Arch §4: the DM links "the relevant ArmorIQ console surface". The exact page
  and whether the SDK exposes the approver are unknown.
- Decided by **E4** (P1-10). Blocking for **P3-06 finish** (link text) and P4-04 (holds panel).

### D-23 — `MIN_SCORE`, score scale, snippet limits (= U-10)

- Decided by **E17** (P3-08). Until then the responder treats an unset
  `MIN_SCORE` as "abstain unless the test configuration provides a value"
  (fail closed for grounding claims).

### D-24 — Submission deadline hour and format (= U-12)

- PRD §13 / Arch §14: verify from official instructions; do not assume.
- Decider: `parthgoyal26-03-2005`. Needed before **P5-04**. Blocking: **finish**.

### D-26 — Policy file contents in a public repository

**RESOLVED 14 Oct 2026 → ADR-022:** placeholders only in the repository.

- The Arch §5.5 template is keyed by real member emails. PRD §4.1 masks emails,
  and the GitHub repository is **public**.
- PROPOSED default: commit `policy/proxylead-policy.yaml` with placeholders
  only (`<MAYA_EMAIL>`…); substitute real emails only in the ArmorIQ console
  or a git-ignored local file.
- Decider: `samarth-chawla`. Needed before **P1-08**. Blocking: **finish**.

### D-27 — Where the Socket Mode client runs

**RESOLVED 17 Oct 2026 by `VTCodeCraft` ([issue #26](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/26); no ADR):** inside the API process. The FastAPI lifespan starts and stops the Slack Socket Mode client (same process and Render service as the API, sharing settings and the backend database pool), with a single uvicorn worker. It is disabled cleanly when the Slack tokens (or the P3-04 orchestrator handle) are missing, and API startup and `/health` never depend on it: any start failure is logged by exception class and the API keeps serving HTTP. Implemented in P3-05 (PR #78, `services/slack_socket/app.py`, `apps/api/main.py`). The live smoke test (mention + DM reply, duplicate processed once, confidential → DM only) is part of CP-5 (#44).

- Arch §2: "One modular FastAPI backend". Socket Mode keeps a long-lived
  websocket, which affects hosting (D-07).
- PROPOSED default: inside the API process lifespan.
- Decider: `VTCodeCraft`. Needed before **P3-05**. Blocking: **finish**.

### D-28 — Hijacked-model injection vector

- PRD §10.3: "into a seeded source or adversary prompt". Choose one or both. A
  seeded source requires P2-03 seed content; an adversary prompt does not.
- Decider: `VTCodeCraft` with `samarth-chawla`. Needed before **P3-07**. Blocking: **finish**.

### D-29 — Honeypot arguments versus the "no model-selected recipient" rule

**RESOLVED 14 Oct 2026 → ADR-025:** option (a), a fixed `synthetic_sink` destination plus a bounded message.

- **PRD §7.3:** plan validation rejects any "model-selected identity/tier/recipient". The scaffold's `FORBIDDEN_ARGUMENT_KEYS` includes `recipient` and `channel`.
- **PRD §10.3 / Arch §6.2:** when the user confirms a new plan containing `send_external_message`, "ArmorIQ policy must BLOCK the honeypot for every persona".
- If application validation rejects the honeypot's arguments first, the block comes from `application_rule` and ArmorIQ never decides. That breaks the demo claim (R-03).
- Options (not chosen):
  - (a) a honeypot schema with **no free recipient**, for example a fixed enum destination `synthetic_sink` plus a message body;
  - (b) an explicit, documented exemption for the honeypot in plan validation;
  - (c) accept an application-rule block and drop the ArmorIQ-policy claim.
- Decider: `samarth-chawla` with `VTCodeCraft`. P2-02 ships a provisional honeypot model (revised by ICR); needed before **P2-10** and **P3-07** finish. Blocking: **finish** (P2-10, P3-07).

### D-30 — Planner pre-filtering by requester tier versus demonstrating a real ArmorIQ BLOCK

**RESOLVED 16 Oct 2026 by `VTCodeCraft` ([issue #20](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/20); no ADR; co-decider `samarth-chawla` confirmation pending):** option (b) for confidential. The junior/confidential pre-filter is off by default (`ENFORCE_REQUESTER_TIER = False`, a module setting in `apps/api/planner.py`), so Priya's confidential request reaches ArmorIQ for a **real policy BLOCK** (SC-01; the honeypot likewise). `restricted` tools for non-Maya are always refused by the application (`application_rule`). Maya-only maintenance tools (`knowledge_confirm_maya_decision`, `knowledge_mark_superseded`) are never planned from a chat message for any persona; their only route is the trusted manager-console path (P4-04).

- **Arch §5.3.1(a):** use a higher tier only when the topic matches a manager-approved rule "**and the requester is allowed to ask at that tier**".
- **PRD §5.2** omits the requester condition (it says only that restricted is available only to Maya).
- **Arch §15 step 2 / PRD §1.1 SC-01:** Priya's confidential request must receive a **real policy BLOCK**. Arch §15 step 4 needs a real policy BLOCK for the honeypot.
- If the planner refuses tiers the requester may not use, those requests stop at `application_rule` and never reach ArmorIQ. The same applies to the restricted-for-non-Maya rule in P3-02.
- Options (not chosen): (a) the planner applies the requester condition (defense in depth; demo labels show `application_rule`); (b) the planner lets the plan reach ArmorIQ and relies on policy + the tool-server ceiling; (c) (b) for the demo personas only, documented.
- Decider: `VTCodeCraft` with `samarth-chawla`. Needed before **P3-02** finishes. Blocking: **finish**.

## 3. Decisions the plan needs (not addressed by the documents)

### D-07 — Hosting platform(s)

**RESOLVED 14 Oct 2026 → ADR-016:** Render (requirements to verify are listed in the ADR).
**AMENDED 14 Oct 2026 → ADR-030:** the web app is hosted on Vercel and reaches the Render API through a same-origin `/api` rewrite. Render hosts the API and the tool server.

- Requirements from the docs:
  - tool server on public HTTPS with about 1 GB RAM for FastEmbed (PRD §3,
    §8.2; Arch §5.1);
  - backend reachable by the web app, with Slack Socket Mode outbound;
  - web demo public (PRD §2).
- The platform is not named anywhere.
- Decider: `parthgoyal26-03-2005` with the team, **today**. Needed before **P1-05**. Blocking: **start**.

### D-08 — Migration tooling and database driver

**RESOLVED 14 Oct 2026 → ADR-028:** `psycopg` 3 (+ `psycopg_pool`, `pgvector` adapter) and plain numbered SQL migrations applied by `scripts/migrate.py`.

- Not in PRD/Arch. Scaffold ADR-002 notes `psycopg` 3 + `pgvector` adapter.
- PROPOSED default: plain ordered SQL files in `migrations/` applied by a small
  runner using `DATABASE_URL_MIGRATE`.
- Decider: `Aditya-Rawat01`. Needed before **P2-01**. Blocking: **start**.

### D-25 — Team handles and ownership interpretation

**RESOLVED 14 Oct 2026 (ownership; no ADR):** the security owner's handle is `samarth-chawla`. Issues are never assigned: contributors self-assign when they pick a task, and the owners in this plan are suggestions. Participants are the four repository collaborators: `Aditya-Rawat01` (project owner) acts as Maya (Org Admin, console approver, host); the team picks Sam and Priya from `samarth-chawla`, `VTCodeCraft`, `parthgoyal26-03-2005`. Org Admin access and the ArmorIQ API key are part of P1-01 (#2). **Mapping decided by the team on 15 Oct 2026:** Sam (senior) = `samarth-chawla`, Priya (junior) = `parthgoyal26-03-2005` (recorded in the P1-01 Setup record).

- **Security owner handle: confirmed.** `samarth-chawla` (confirmed by the
  project owner on 14 Oct 2026; `amarth-chawla` in the brief was a typo). Task
  ownership itself remains proposed until the team confirms it.
- The other proposed owners and their task assignments: still to confirm
  before assigning issues.
- `parthgoyal26-03-2005` "frontend and delivery": this plan reads "delivery" as
  safe answer delivery (P3-06) plus release delivery (deployment, demo,
  submission). Confirm.
- Decider: team. Blocking: **start** for issue assignment only.

## 4. Scaffold versus documents (supporting infrastructure, not conflicts)

| ID | Observation | Handling |
|---|---|---|
| M-01 | Docs name `services/toolserver/mcp_protocol.py`. The scaffold implements MCP with the official SDK in `servers.py` (ADR-006). `mcp_protocol.py` would hold the ADR-006 fallback (hand-written JSON-RPC + SSE) if E1 requires it | P2-06 keeps `servers.py`; P1-06 records whether the fallback is needed |
| M-02 | Arch §11 lists tests flat under `tests/`. The scaffold uses `tests/api`, `tests/toolserver`, `tests/contracts`, and **CI runs only those folders** | Tasks place documented test names inside the component folder whose CI job installs the right extra (e.g. `tests/api/test_identity.py`). Live end-to-end tests go in `tests/e2e/` (manual, need secrets) |
| M-03 | Scaffold-only variables not in PRD §12 / Arch §17: `LOG_LEVEL`, `CORS_ALLOWED_ORIGINS`, `DATABASE_URL_MIGRATE`, `ARMORIQ_MCP_MEMORY_NAME`, `ARMORIQ_MCP_WORKSPACE_NAME`, `MCP_ALLOWED_HOSTS`, `MCP_ENABLE_DIAGNOSTIC_TOOLS` | Supporting infrastructure. `DATABASE_URL_MIGRATE` serves the migration role required by PRD §8.1 |
| M-04 | Diagnostic tools `memory_scaffold_ping` / `workspace_scaffold_ping` (ADR-011) are not product tools | Disable (`MCP_ENABLE_DIAGNOSTIC_TOOLS=false`) on any endpoint registered in ArmorIQ, so the registry holds product tools only (P1-05, P5-03) |
| M-05 | Stale references: `docs/DEVELOPMENT_WORKFLOW.md` PR checklist says `contracts/`; `services/knowledge/__init__.py` cites superseded ADR-003 | Out of scope for planning. Fix in the first task that touches those files (P2-04 for the docstring), or a small docs PR |
| M-06 | `ApprovalState` app-only values (`timeout`, `interrupted`, `unverified`) and the `AuditEventType` list are scaffold proposals | Treated as PROPOSED (SHARED_INTERFACES §8, §10) |
| M-08 | Plan-proposed supporting variable `PROXYLEAD_E2E` (P5-01, P5-02) enables live e2e tests locally. It is not in PRD §12 / Arch §17 and is never needed at runtime | PROPOSED; confirm in P5-01 |
| M-07 | `.github/workflows/ci.yml` has no job for tests needing both `api` and `toolserver` extras | Integration tests use fakes inside `tests/api` (P3-09). Live e2e tests are manual (P5-01). A new CI job needs a steward PR (`parthgoyal26-03-2005`) |

## 5. Existing U-list mapping

| Existing | This file |
|---|---|
| U-1, U-2 | D-06 |
| U-3 | D-05 |
| U-4 | E3 (P1-09) |
| U-5, U-11 | D-22 / E4 (P1-10) |
| U-6 | E5 (P1-08) |
| U-7 | D-13 |
| U-8 | D-12 |
| U-9 | D-11 |
| U-10 | D-23 |
| U-12 | D-24 |
| U-13 | D-01 |
| U-14 | D-02 |
