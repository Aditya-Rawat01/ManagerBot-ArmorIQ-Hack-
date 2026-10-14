# P4-01 — Web request API (submit message, poll status)

| Field | Value |
|---|---|
| Phase | 4 — Frontend (backend side of the web entry point) |
| Owner | `VTCodeCraft` |
| Size | M (2–4 h) |
| Target window | 15 Oct PM |
| Gate role | Web uses the same path as Slack (Arch §9) |
| Status | Not started |

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

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P4-01] Implement session-authenticated web request submit and status API`
- **Body outline:** endpoints; body schema (forbid identity fields); owner-only reads; persisted-state rule; tests.
- **Labels:** `phase-4-frontend`, `backend`, `security`
- **Depends on:** P1-03, P3-04 (soft: P3-01, P3-07); D-21  **Blocks:** P4-02 (and P4-03 via P4-02)

## 17. Definition of done

Merged with tests green; types regenerated; handoff posted to P4-02 and P4-03.
