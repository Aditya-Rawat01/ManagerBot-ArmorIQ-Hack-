# P3-04 — Request orchestrator (one plan per request, end to end)

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `VTCodeCraft` |
| Size | L (5–8 h) |
| Target window | 15 Oct (fakes); real-gateway integration merges after **G1** |
| Gate role | Implements PRD §7 / Arch §8 |
| Status | Not started |

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

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-04] Implement request orchestrator: one ArmorIQ plan per request, fail closed`
- **Body outline:** lifecycle steps; tier tracking; HOLD and no-replan; off-plan routing; audit sequence; G1 merge rule; tests.
- **Labels:** `phase-3-agent-slack`, `agent`, `backend`, `security`
- **Depends on:** P1-02, P1-03, P3-01, P3-02, P3-03 (soft: P1-06, i.e. G1, to merge the real gateway; P3-06)  **Blocks:** P3-05, P3-07, P3-09, P4-01

## 17. Definition of done

Merged with offline tests green; real-gateway wiring merged after G1; handoff
posted to P3-05, P4-01, and P3-09.
