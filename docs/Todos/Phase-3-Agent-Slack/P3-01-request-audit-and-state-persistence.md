# P3-01 — Request, plan, decision, approval, and delivery persistence (`services/audit`)

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `parthgoyal26-03-2005` |
| Size | M (3–5 h) |
| Target window | 14–15 Oct |
| Gate role | SC-06 (every UI status maps to a persisted event) |
| Status | Not started |

## 1. Task ID and title

P3-01 — Implement the backend's append-only audit store and request-state persistence in `services/audit/`, using the `proxylead_backend` role only.

## 2. Purpose and expected outcome

The orchestrator, gateway results, Slack, delivery, and the manager console all
need one place to record and read what actually happened. Outcome:

- a typed store API that writes requests, plans, plan steps, decisions, approvals, deliveries, unanswered questions, and audit events;
- secrets redacted before persistence;
- read queries for the manager console;
- never touching protected content tables.

## 3. Authoritative source references

- PRD §5.2 (audit topic rule, tier, escalation), §7.6, §7.13, §7.14 (append audit events for every transition), §8.1 (tables; backend role), **§10.1** (required fields; keep secrets and raw tokens out).
- Arch §5.3 (persist request ID, ArmorIQ plan ID, plan hash if exposed, step order, tool names, decision source), §7.2 (minimum plan/step/audit fields), §7.5 (backend role limits), §8.13 (unanswered queue), **§10** (record list; unanswered-question reasons; no protected content in queue rows), §13 (audit matches actual execution and delivery).

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Reviewers: `VTCodeCraft` (main writer, P3-04), `Aditya-Rawat01` (schema).

## 5. Prerequisites and dependencies

- Hard: P2-01 (tables, backend role).
- Blocks: P3-02 (topic map read helper), P3-04, P3-05, P3-06, P4-01, P4-04.

## 6. Exact files/modules this task owns

- `services/audit/__init__.py` (existing placeholder)
- `services/audit/store.py` (new): write API
- `services/audit/queries.py` (new): read queries for the manager console and request status
- `services/audit/redaction.py` (new): payload redaction
- `apps/api/core/db.py` (new, PROPOSED path): backend-role connection pool with statement timeout
- `tests/api/test_audit_store.py` (new), `tests/api/test_audit_redaction.py` (new)

## 7. Existing files it may need to modify

- `pyproject.toml` (steward `VTCodeCraft`): database driver in the `api` extra (same pin as P2-01).
- `services/contracts/enums.py` (steward `VTCodeCraft`): extend `AuditEventType` only via ICR.

## 8. Shared interfaces and contracts

- Provides: the audit store API (SHARED_INTERFACES §10, PROPOSED). Confirm by ICR with the P3-04 owner before P3-04 builds on it.
- Consumes: `AuditEvent`, `RequestContext`, `PlanProposal`, `ArmorIQDecision`, `ApprovalStatus`, `ToolInvocationResult`, `DeliveryChannel` (FIXED).
- Correlation IDs: SHARED_INTERFACES §13.

## 9. Step-by-step implementation requirements

1. `apps/api/core/db.py`: a pool on `DATABASE_URL_BACKEND` only, statement timeout, parameterized queries. It must never be configured with the toolserver or migration DSN (test).
2. Write API (all append-only inserts, except status columns on `plans` / `plan_steps` / `approval_requests`, which move forward only):
   - `create_request(ctx, entry_point refs)`;
   - `record_plan(request_id, plan, matched_topic_rule_id, chosen_tier, escalation_confirmed)`;
   - `record_step_decision(StepOutcome)`;
   - `record_approval(ApprovalStatus)`;
   - `record_execution_ref(jti, ledger_entry_id, status)`;
   - `record_answer(request_id, citations, highest_tier_returned, abstained, reason)`;
   - `record_delivery(request_id, channel, recipient_member_id, tier_delivered, status, error_class)`;
   - `add_unanswered(request_id, requester, reason ∈ {no_evidence, conflict, manager_decision_required, source_unavailable}, safe_refs)`;
   - `append_event(AuditEvent)`.
3. Redaction: before persisting any payload, drop or mask keys matching secrets/tokens/emails/`_ctx`. Store tool arguments only as redacted copies. Never store protected snippets in `unanswered_questions` or event payloads (Arch §10).
4. Read queries:
   - `request_status(request_id, member_id)`: owner-only, for P4-01;
   - `recent_requests`, `timeline(request_id)`, `pending_holds`, `unanswered` (Maya-only callers, for P4-04).

   No query joins protected content tables.
5. Every write includes `request_id` and `trace_id`. Timestamps are timezone-aware UTC.
6. Typed errors. A failed audit write in a protected path makes the caller fail closed (document this for P3-04).

## 10. Acceptance criteria

- Every PRD §10.1 field group can be persisted and read back (test per group).
- Redaction removes tokens, emails, and `_ctx` (property test with nested payloads).
- The backend role used by the store cannot read protected tables (reuses the P2-01 live check; skipped offline).
- Status transitions are forward-only (for example `approved` cannot revert to `pending`).

## 11. Required unit, integration, and security tests

- `tests/api/test_audit_store.py`:
  - field-group round trips;
  - forward-only transitions;
  - owner-only `request_status`;
  - the DSN guard.
  Uses a live database when `DATABASE_URL_BACKEND` is set; otherwise a fake connection.
- `tests/api/test_audit_redaction.py`: secrets, emails, `_ctx`, nested structures.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_audit_store.py tests/api/test_audit_redaction.py -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No execution ledger (P2-07). No HTTP routes (P4-01 / P4-04). No UI.

## 14. Risks and unresolved questions

- Event type list growth: use ICRs.
- D-12 may add metadata-only queries for the review queue later.

## 15. Handoff deliverables for dependent agents

- P3-04 / P3-05 / P3-06: the store API, call order per lifecycle step, and the fail-closed rule.
- P4-01 / P4-04: read query signatures and their authorization assumptions (caller must enforce owner or Maya).

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-01] Implement backend audit and request-state persistence (services/audit)`
- **Body outline:** write and read API; redaction; backend-role-only rule; tests.
- **Labels:** `phase-3-agent-slack`, `backend`, `database`, `security`
- **Depends on:** P2-01  **Blocks:** P3-02, P3-04, P3-05, P3-06, P4-01, P4-04

## 17. Definition of done

Merged with tests green; ICR for the store API accepted by `VTCodeCraft`;
handoff posted.
