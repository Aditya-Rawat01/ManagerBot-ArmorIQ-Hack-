# P3-04 — Request orchestrator (one plan per request, end to end)

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `VTCodeCraft` |
| Size | L (5–8 h) |
| Target window | 15 Oct (fakes); real-gateway integration merges after **G1** |
| Gate role | Implements PRD §7 / Arch §8 |
| Status | Implemented on `feat/agent-p3-04-orchestrator` (offline and local-database tests green; not merged). Handoff notes: §15 |

## 1. Task ID and title

P3-04 — Implement `apps/api/orchestrator.py`, the request lifecycle shared by Slack and web: request record → plan → validation → one ArmorIQ plan → sequential steps → HOLD handling → responder → delivery → audit, all failing closed.

## 2. Purpose and expected outcome

Slack and web must use exactly the same path (Arch §9). Outcome: one async
entry point that runs the 14 lifecycle steps of PRD §7 / Arch §8 and emits an
audit event for every transition. It tracks the highest tier returned to the
model, never auto-replans, and turns every unverifiable state into a safe
user-facing result.

## 3. Authoritative source references

- PRD **§7** (steps 1–14), §7.1, §0.2, §0.6, §5.2, §6.2 (off-plan routing), §8.4.
- Arch **§8** (steps 1–14), §5.3, §5.6 (HOLD; background wait; same plan/step), §6.2, §9 (no demo bypass), §1 (outcomes ALLOW/BLOCK/HOLD/ABSTAIN/ERROR), §13.

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Reviewers: `samarth-chawla` (gateway use), `parthgoyal26-03-2005` (audit and delivery integration).

## 5. Prerequisites and dependencies

- Hard: P1-02 (gateway + fake), P1-03, P3-01, P3-02, P3-03.
- Soft: P3-06 (`deliver`, `notify_hold`; use a fake until merged).
- Gate: merging the real-gateway wiring requires **G1** (D-19). Development against `FakeArmorIQGateway` may start earlier.
- Decisions: D-14 (confirmation), D-19.
- Blocks: P3-05, P3-07, P3-09, P4-01.

## 6. Exact files/modules this task owns

- `apps/api/orchestrator.py` (new; documented)
- `tests/api/test_orchestrator.py` (new)
- `tests/api/fakes/` (new, shared test doubles for gateway, planner LLM, responder LLM, Slack sender, delivery; this task owns the directory, and P3-09 extends it with the owner's agreement)

## 7. Existing files it may need to modify

- `apps/api/main.py` (steward: same owner): construct the singletons (gateway, stores, workers) in the lifespan.

## 8. Shared interfaces and contracts

- Provides: `handle(ctx, message, options) -> request_id` (SHARED_INTERFACES §12, PROPOSED).
- Consumes: gateway §3, audit store §10, planner/responder §12, delivery §11, identity §2, correlation IDs §13.

## 9. Step-by-step implementation requirements

1. `handle` validates the `RequestContext`, creates `request_id`/`trace_id`, calls `create_request`, returns immediately, and runs the rest in a background task. The Slack acknowledgement is never blocked (R-43).
2. Plan (P3-02).
   - Rejected → audit `plan_rejected`, then deliver a safe message.
   - Clarification or escalation → deliver the prompt (D-14).
3. `gateway.start_plan` **once** (R-31). Failure → BLOCK/UNVERIFIED, deliver "authorization could not be verified".
4. For each step in order:
   1. `invoke_step` → record the decision.
   2. BLOCK → stop the remaining protected steps. HOLD → `notify_hold` (P3-06), record `approval_requests`, and wait in the background (the gateway handles the SDK wait). On a non-approved resolution: no data, tell the requester to retry, **no replan**.
   3. ALLOW → record the execution reference (`jti`, `ledger_entry_id`) and add the result to the evidence set. Update `highest_tier_returned` = max(tier of every tool whose result was returned to the model).
5. Responder (P3-03) with the evidence set and the request's returned-source IDs. Any `requested_tools` → `gateway.attempt_off_plan(...)` for each, recorded with the true label; **never executed**. If a requested action is legitimate, ask the user to confirm a new request (D-14).
6. Abstained answers with reason `no_evidence` / `conflict` / `manager_decision_required` / `source_unavailable` → `add_unanswered` with safe references only.
7. Delivery: `deliver(ctx, output, highest_tier_returned)` (P3-06). The destination is chosen by trusted code only.
8. Every transition emits an `AuditEvent` with `request_id`, `trace_id`, `plan_id`, `step_index`, `jti`, and `decision_source`. An audit write failure in a protected path → stop and fail closed.
9. Global timeout and exception guard: any unexpected error → ERROR outcome, audit, generic message. Never ALLOW.
10. No direct DB reads of protected content. Use only the gateway (R-20).

## 10. Acceptance criteria

- Exactly one `start_plan` per request (test with the fake gateway counter).
- Steps run in order under one handle. BLOCK stops later protected steps.
- HOLD: no content delivered before approval. Rejected, expired, or timed-out → no data and no new plan.
- Requested tools from the responder are only ever sent to `attempt_off_plan`.
- `highest_tier_returned` includes uncited results (test: confidential result returned but not cited → delivery tier confidential).
- Every lifecycle step has an audit event (sequence assertion).

## 11. Required unit, integration, and security tests

`tests/api/test_orchestrator.py` (`__offline_fakes`):

- `test_one_plan_per_request`
- `test_block_stops_protected_steps`
- `test_hold_waits_and_no_replan_on_expiry`
- `test_off_plan_requests_never_execute`
- `test_delivery_tier_uses_all_returned_data`
- `test_unanswered_queue_has_no_protected_snippets`
- `test_audit_event_sequence`
- `test_gateway_exception_fails_closed`
- `test_audit_write_failure_fails_closed`

## 12. Commands for verification

```bash
python -m pytest tests/api/test_orchestrator.py -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No Slack event parsing (P3-05). No HTTP routes (P4-01). No hijack simulation (P3-07).
- No in-app approval.

## 14. Risks and unresolved questions

- Real SDK HOLD behavior (E4) may change the wait/resume shape. Keep it isolated behind the gateway.
- D-14, D-19.

## 15. Handoff deliverables for dependent agents

- P3-05 / P4-01: how to call `handle` and what each entry point must supply.
- P3-07: the `RequestOptions.simulate_hijacked_model` hook.
- P3-09: the shared fakes and scenario helpers.
- P5-01: what to observe end to end.

### 15.1 Handoff notes (as implemented)

Interfaces: SHARED_INTERFACES §12 (orchestrator, confirmation), §3 (gateway
`on_hold`), §10 (event mapping), §13 (IDs); D-14 binding details in
DECISIONS. Module docstring of `apps/api/orchestrator.py` has the step map.

**P3-05 (Slack) and P4-01 (web): calling `handle`**

- Get the singleton from `app.state.orchestrator` (`handle` is also
  `app.state.orchestrator_handle`, which P3-05 already dispatches to). Both
  are absent when the API runs without the orchestrator settings: the web
  route must then answer 503 (`ErrorResponse`), never a fake success.
- Supply a trusted `RequestContext` built only from server-side identity
  (Slack user mapping or the signed session): requester exactly as
  `resolve_slack_user` / `resolve_web_session` return it (it is checked
  against the member table), the single workspace ID, the entry point, and
  for Slack the team/channel/thread/event IDs. `request_id` / `trace_id` are
  provisional: `handle` returns the orchestrator's own `req-<32 hex>`, which
  is the ID to show and to poll (`request_status`).
- `handle` returns after `create_request`; the answer arrives later through
  P3-06 delivery (Slack) or, for web, as a `pending` `web_private` delivery
  row (the P3-06 answer-storage gap is still open: P4-01's ICR). It raises
  `RequestRejectedError` (bad context/options: 400/403 class, nothing
  recorded) or `RequestNotRecordedError` (audit store down: 503).
- Slack: `handle(ctx, message)` only. A Slack context carrying
  `RequestOptions` is refused.
- Web: `handle(ctx, message, RequestOptions(confirmation_token=..., simulate_hijacked_model=...))`.
  The token comes from the session's stored offer (below), never from the
  message body text.

**D-14 confirmations (P3-05 follow-up: Slack button; P4-01: web)**

- Register a presenter per entry point:
  `orchestrator.set_confirmation_presenter(EntryPoint.SLACK_MENTION, fn)` (and
  `SLACK_DM`, `WEB`). `fn(ctx, offer)` (sync or async) is called after the
  escalation prompt was delivered, with the ORIGINAL request's context and a
  `ConfirmationOffer(request_id, escalation_id, requested_tier, expires_at,
  token)`. Slack: post Confirm / Cancel buttons whose value is the token (no
  token in message text). Web: keep the offer server-side for the owning
  session and show Confirm / Cancel. An exception in `fn` revokes the offer.
- On a click: `await orchestrator.confirm(ctx, token, accept)` with a
  trusted context for the clicking member, using the SAME entry point and
  Slack channel as the message that carried the prompt (the confirmation is
  bound to member, workspace and channel). It returns a new `request_id`;
  the confirmed lookup runs under that request with a new ArmorIQ plan.
- Until a presenter is registered the prompt is delivered without a control
  and the offer simply expires (5 minutes).

**P3-07 (hijacked-model simulation)**

- `RequestOptions.simulate_hijacked_model` exists (default `False`), is
  accepted for `EntryPoint.WEB` only (refused for Slack contexts) and is
  inert: `_request_flow` only logs it. P3-07 adds the injection at that
  point (or around the responder call in `_answer`) and the "simulation"
  audit marker. Off-plan routing is already in place: every responder
  `requested_tools` entry goes to `gateway.attempt_off_plan` only, recorded
  as `authorization_decided` with `off_plan: true`, `executed: false` and
  the returned label; a PLANNED tool name is refused as `application_rule`
  without any SDK call (the SDK plan check binds names only, E2, so it would
  execute). The confirmed honeypot plan is a normal confirmed request.

**P3-09 (integration and injection tests)**

- `tests/api/fakes/`: `ScriptedGateway` (counters; `Allow`, `AllowUnknown`,
  `Block`, `ToolServerReject`, `Unverified`, `HoldThen` (waits on an
  `asyncio.Event`, reports the hold from a thread), `HoldPending`, `Raise`;
  `off_plan_would_execute` must stay empty), `RecordingAudit` (store write
  API + the store's real event checks, `fail={name: nth}`, `timeline`),
  `FakeDelivery` (bound to the real P3-06 signatures), `llm`
  (`FakeModelClient`, `GatedModelClient`, `draft`, `DraftRecorder`),
  `RecordingSlackSender`, and `scenario` (`orchestrator_settings`,
  `request_ctx`, `search`, `build_harness`, `Harness.ask/confirm`).

**P5-01 (end to end): what to observe**

- One `plan_proposed` + `plan_minted` per request with the gateway `plan_id`;
  per step `authorization_decided` (true label) and `tool_executed` with a
  `jti` and `ledger_entry_id` that match the tool server's ledger row.
- HOLD: `hold_created` and Maya's `hold_notice` DM while the request waits,
  then `approval_resolved`; no content before approval; never a second plan.
- Elevated data (cited or not) → DM / `web_private` only; generic notice in
  the thread for mentions.
- Off-plan honeypot attempts: `armoriq_sdk_plan_check` decision events with
  `executed: false` and no tool-server or ledger trace.
- Abstentions: `answer_abstained` + an `unanswered_questions` row with only
  team source IDs.
- Any failure: an `authorization_decided` event with source `unknown` and
  `request_outcome: error`, and one generic message.

**Deploy (Render API service), by name only:** `ARMORIQ_API_KEY`,
`PERSONA_MAYA_EMAIL`, `PERSONA_SAM_EMAIL`, `PERSONA_PRIYA_EMAIL`,
`CTX_SECRET`, `DATABASE_URL_BACKEND`, `GEMINI_API_KEY` (ADR-032; all required for
the orchestrator); `MIN_SCORE` (until set, answers abstain
`threshold_unset`), `LLM_MODEL`, `ARMORIQ_AGENT_ID`, `ARMORIQ_CONSOLE_URL`,
`TOKEN_VALIDITY_S`, `APPROVAL_TIMEOUT_MS`, and for Slack `SLACK_BOT_TOKEN`,
`SLACK_APP_TOKEN`, `SLACK_TEAM_ID`, `SLACK_ALLOWED_CHANNELS`,
`SLACK_USER_{MAYA,SAM,PRIYA}_ID`. One uvicorn worker (D-27; the confirmation
store is in-process).

**Known limits**

- Real SDK HOLD behaviour is unverified until E4; the orchestrator relies on
  the gateway's `on_hold` observer (added here, steward review by
  `samarth-chawla`). A gateway that returns HOLD without waiting is recorded,
  Maya is notified, and the request ends `interrupted` (no data, ask again).
- The ERROR outcome has no dedicated event type; it is recorded as an
  `unknown`-sourced `authorization_decided` (SHARED_INTERFACES §10).
- `manager_decision_required` / `source_unavailable` abstentions have no
  producer yet (P3-03 emits neither).
- In-flight requests are cancelled on shutdown after a 5 s grace period.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-04] Implement request orchestrator: one ArmorIQ plan per request, fail closed`
- **Body outline:** lifecycle steps; tier tracking; HOLD and no-replan; off-plan routing; audit sequence; G1 merge rule; tests.
- **Labels:** `phase-3-agent-slack`, `agent`, `backend`, `security`
- **Depends on:** P1-02, P1-03, P3-01, P3-02, P3-03 (soft: P1-06, i.e. G1, to merge the real gateway; P3-06)  **Blocks:** P3-05, P3-07, P3-09, P4-01

## 17. Definition of done

Merged with offline tests green; real-gateway wiring merged after G1; handoff
posted to P3-05, P4-01, and P3-09.
