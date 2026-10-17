# P4-01 — Web request API (submit message, poll status)

| Field | Value |
|---|---|
| Phase | 4 — Frontend (backend side of the web entry point) |
| Owner | `VTCodeCraft` |
| Size | M (2–4 h) |
| Target window | 15 Oct PM |
| Gate role | Web uses the same path as Slack (Arch §9) |
| Status | Implemented on `w6/a-p401-p307` (offline tests green; not merged). Handoff notes: §15.1 |

## 1. Task ID and title

P4-01 — Implement `POST /api/v1/requests` and `GET /api/v1/requests/{request_id}`: session-authenticated web entry into the same orchestrator, with no identity fields accepted from the body, and owner-only status and private answer retrieval.

## 2. Purpose and expected outcome

The web demo must use the same identity, orchestrator, gateway, and tool path
as Slack, with no demo bypass (Arch §9). Outcome:

- two typed endpoints and their contract models;
- the session cookie (P1-03) as the only identity source;
- `web_private` answers readable only by the requesting member.

## 3. Authoritative source references

- PRD §2 P0 (web demo uses the same API, plan lifecycle, gateway, MCP endpoints, data path), §4.1 (session identity; never a persona from the chat body), §7.12 (private web session for elevated tiers), §10.2 (honest status labels; pending HOLD state), §10.3 (toggle).
- Arch §8, §9 (no demo bypass; restricted answers only in the authenticated private session; session determines identity), §13.

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Consumer: `parthgoyal26-03-2005` (P4-02/P4-03). Security reviewer: `samarth-chawla`.

## 5. Prerequisites and dependencies

- Hard: P1-03 (session), P3-04 (`handle`).
- Soft: P3-01 (`request_status` query), P3-07 (option field).
- Decisions: **D-21** (paths and polling) is needed to finish.
- Blocks: P4-02, P4-03.

## 6. Exact files/modules this task owns

- `apps/api/routes/requests.py` (new)
- `services/contracts/api_requests.py` (new: `CreateRequestBody`, `CreateRequestResponse`, `RequestStatusView`, `StepStatusView`)
- `tests/api/test_requests_api.py` (new)

## 7. Existing files it may need to modify

All steward `VTCodeCraft` (same owner):

- `apps/api/routes/router.py`: include the router.
- `services/contracts/__init__.py`, `services/contracts/schema_export.py`: register the models. Regenerate the JSON Schema and TypeScript.

## 8. Shared interfaces and contracts

- Endpoints in SHARED_INTERFACES §1.1 (PROPOSED → FIXED once this merges and P4-02 acknowledges).
- `CreateRequestBody`: `message` (bounded), `confirmation_token?` (D-14), `simulate_hijacked_model` (bool, default false). `extra="forbid"`, so `persona`, `member_id`, `email`, `tier`, and `recipient` are rejected (`422` envelope, no echo).
- `RequestStatusView`:
  - lifecycle state;
  - per step: tool, outcome, `DecisionSource`, `execution_status`, approval state;
  - answer text (only if delivered to this session);
  - citations (`SourceCitation`, only those validated and allowed);
  - `abstained` + reason;
  - delivery channel.

## 9. Step-by-step implementation requirements

1. Both endpoints require a valid session (P1-03). Otherwise `401`.
2. `POST`: build `RequestContext(entry_point=web)` from the session only, call `orchestrator.handle(...)`, return `202 {request_id}`.
3. `GET`: return `404` unless the request belongs to the session's member. Same response for "does not exist" and "not yours".
4. Status values come only from persisted audit and delivery records (P3-01). Never from predictions (R-72).
5. A HOLD shows the `pending` state with the console note (no approve button).
6. No raw emails, `_ctx`, `jti` secrets, or protected content of other tiers in any response.

## 10. Acceptance criteria

- A body with `persona` / `member_id` / `email` / `tier` / `recipient` → `422` envelope, and identity is unchanged.
- Another member's request ID → `404`, identical to a missing ID.
- The status view equals the persisted state in fixtures (BLOCK, HOLD pending, ALLOW, ABSTAIN, ERROR).
- Contracts are exported. JSON Schema and TypeScript regenerated without drift.

## 11. Required unit, integration, and security tests

`tests/api/test_requests_api.py` (`__offline_fakes`):

- auth required;
- identity fields rejected;
- owner-only read;
- status mirrors persisted state;
- hijack option passed through only from web;
- no email or `_ctx` in responses.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_requests_api.py -q
python -m services.contracts.schema_export --check
cd apps/web && npm run gen:types && git diff --exit-code -- src/types/generated
```

## 13. Explicit non-goals

- No manager endpoints (P4-04). No UI. No websocket or streaming (D-21 PROPOSED polling).

## 14. Risks and unresolved questions

- D-21, D-14 (confirmation tokens).

## 15. Handoff deliverables for dependent agents

- P4-02 / P4-03: endpoint contracts, generated TypeScript types, polling guidance (interval, terminal states).

### 15.1 Handoff notes (as implemented)

Interfaces: SHARED_INTERFACES §1.1 (endpoints, now as implemented), §10
(`request_detail`, `simulation_activated`), §12 (orchestrator web inbox).
D-21 is still open: the spec default (polling, the §1.1 paths) is
implemented here as the P4-01 interface, not recorded as a decision.

**Endpoints** (`apps/api/routes/requests.py`, contracts
`services/contracts/api_requests.py`, TS in
`apps/web/src/types/generated/contracts.ts`):

- `POST /api/v1/requests`, body `CreateRequestBody` = `{message,
  confirmation_token?, simulate_hijacked_model}` only (`extra="forbid"`,
  strict types; `message` ≤ 4000 chars, non-blank unless
  `confirmation_token` is set; a confirmation cannot enable the simulation).
  Any other key (`persona`, `member_id`, `email`, `tier`, `recipient`, ...)
  → `422 validation_error`, no input echo. No/invalid session → `401
  unauthenticated` (checked before the body). No orchestrator → `503
  requests_unavailable`. Refused by the orchestrator → `400
  request_rejected`; not recorded → `503 request_not_recorded`. Success →
  `202 CreateRequestResponse {request_id}` (`req-<32 hex>`).
- `GET /api/v1/requests/{request_id}` → `RequestStatusView` for the session's
  own request. Another member's ID, a missing ID and a malformed ID all give
  the same `404 not_found`. `503` without the orchestrator / database.
- `RequestStatusView`: `request_id`, `state` (`processing` |
  `pending_approval` | `awaiting_confirmation` | `completed`), `outcome`
  (`allow` | `block` | `hold` | `abstain` | `error` | `unverified` |
  `clarification` | null), `note` (HOLD: console note, no approve action),
  `steps[]` (`StepStatusView`: `step_index`, `tool`, `outcome`,
  `decision_source`, `is_armoriq_policy`, `execution_status`,
  `approval_state`), `off_plan_attempts[]` (`OffPlanAttemptView`: `tool`,
  `outcome`, `decision_source`, server-worded `enforcement_layer`,
  `is_armoriq_policy`, `executed: false`, `simulated`, `rejection`),
  `simulation_active`, `egress_sink_executions`, `answer_text`,
  `citations[]` (`CitationView`: `source_id`, `tier`, `title`), `abstained`,
  `abstain_reason`, `delivery_channel`, `delivery_status`, `confirmation`
  (`ConfirmationOfferView` or null), `last_event_at`.

**Where values come from.** State, outcome, steps, off-plan attempts,
simulation flag, sink count, citation IDs/tiers and delivery come only from
persisted rows: `services.audit.queries.request_status` (owner gate) and the
new owner-only `request_detail` (same owner rule). `answer_text`, citation
titles and `confirmation` come from the requester's in-process web inbox
(`apps/api/web_inbox.py`, `Orchestrator.web_inbox`): the orchestrator puts
the delivered text there only after P3-06 recorded the `pending`
`web_private` row; `main.py` registers `web_inbox.put_offer` as the web D-14
confirmation presenter. Items are bound to member + workspace (anyone else
gets nothing), expire after 1 hour, at most 1000 are kept. **A restart
loses unread answers and offers** (single uvicorn worker, D-27): the status
stays readable, the text does not; the user asks again. The web
`delivery_events` row stays `pending` (follow-up: record `delivered` on the
owner's first read).

**For P4-02 / P4-03.** Sign in (P1-03 cookie), `POST`, then poll `GET` every
2 s while `processing`, every 5 s while `pending_approval` (a HOLD can last
`APPROVAL_TIMEOUT_MS`, 15 min); stop at `completed` or
`awaiting_confirmation`. To confirm an escalation, post a NEW request with
`confirmation_token = confirmation.token` (owner-only, single use, expires at
`confirmation.expires_at`; never log or display it); to decline, let it
expire. Show `enforcement_layer` / `decision_source` as given; claim
"ArmorIQ policy" only when `is_armoriq_policy` is true (ADR-026). The
simulation toggle is `simulate_hijacked_model` (P3-07 §15.1).

**Deploy:** no new variable. The routes need the orchestrator settings
(`ARMORIQ_API_KEY`, `PERSONA_{MAYA,SAM,PRIYA}_EMAIL`, `CTX_SECRET`,
`DATABASE_URL_BACKEND`, `GEMINI_API_KEY`) and the session settings
(`SESSION_SECRET`, `DEMO_PASSCODE`); one uvicorn worker.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P4-01] Implement session-authenticated web request submit and status API`
- **Body outline:** endpoints; body schema (forbid identity fields); owner-only reads; persisted-state rule; tests.
- **Labels:** `phase-4-frontend`, `backend`, `security`
- **Depends on:** P1-03, P3-04 (soft: P3-01, P3-07); D-21  **Blocks:** P4-02 (and P4-03 via P4-02)

## 17. Definition of done

Merged with tests green; types regenerated; handoff posted to P4-02 and P4-03.
