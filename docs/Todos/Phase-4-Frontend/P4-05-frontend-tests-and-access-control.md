# P4-05 — Frontend tests and web access-control verification

| Field | Value |
|---|---|
| Phase | 4 — Frontend |
| Owner | `parthgoyal26-03-2005` |
| Size | S–M (2–3 h) |
| Target window | 16 Oct AM |
| Gate role | Public demo safety (Arch §9) |
| Status | Not started |

## 1. Task ID and title

P4-05 — Add cross-cutting web tests and an API access-control suite proving four things: the web client never sends identity, never shows simulated approval or predicted outcomes, keeps elevated answers private to the requester, and blocks non-Maya users from manager surfaces.

## 2. Purpose and expected outcome

A single place that asserts the public-demo safety rules across all web
surfaces. Outcome: Vitest suites in `apps/web` and an API test module in
`tests/api`, both running in CI.

## 3. Authoritative source references

- PRD §4.1, §10.2, §10.3, §0.8.
- Arch §9 (public demo safety: synthetic data; session identity; reset restricted; secrets and emails off the client; no public sign-up), §13 (impersonation; private delivery), §0.8.

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Reviewer: `samarth-chawla`.

## 5. Prerequisites and dependencies

- Hard: P4-03, P4-04.
- Blocks: P5-01.

## 6. Exact files/modules this task owns

- `apps/web/src/test/accessControl.test.tsx` (new)
- `apps/web/src/test/noSimulatedApproval.test.tsx` (new)
- `apps/web/src/test/noIdentityInPayloads.test.ts` (new)
- `tests/api/test_web_access_control.py` (new)

## 7. Existing files it may need to modify

- `apps/web/src/test/setup.ts` (steward: this owner): shared mocks if needed.

## 8. Shared interfaces and contracts

- Exercises SHARED_INTERFACES §1 (web endpoints) and §2 (identity) through the public API and UI.

## 9. Step-by-step implementation requirements

1. **Payload test.** Intercept every `fetch` during a full UI flow. Assert that no body contains `persona` (except the sign-in call), `member_id`, `email`, `role`, `tier`, or `recipient`.
2. **No simulated approval.** Render every page in every state. Assert there is no button or link whose label or handler approves or rejects a HOLD. The only ArmorIQ link is the console URL.
3. **Truthful rendering.** Given API fixtures, the UI shows exactly the fixture outcomes (no optimistic success states).
4. **API access control** (`tests/api/test_web_access_control.py`):
   - no-session `401` for every `/api/v1` route except health and session creation;
   - non-Maya `403` for manager routes;
   - cross-member request status `404`;
   - `web_private` answer visible only to its owner.
5. **Bundle scan.** The built bundle contains no secrets or email-like strings (CI step or test).

## 10. Acceptance criteria

- All suites green in CI (web job + API job).
- Every `/api/v1` route appears in the API access-control matrix (the test enumerates the app's routes, so new routes fail until classified).

## 11. Required unit, integration, and security tests

- The four files in §6.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_web_access_control.py -q
cd apps/web && npm test && npm run build
```

## 13. Explicit non-goals

- No live end-to-end runs (P5-01). No visual regression testing.

## 14. Risks and unresolved questions

- D-16 (reset endpoint) must be classified in the route matrix once it is decided.

## 15. Handoff deliverables for dependent agents

- P5-02: the route matrix and its classification for the live regression.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P4-05] Add web access-control, no-identity-payload and no-simulated-approval tests`
- **Body outline:** test list; route matrix rule; bundle scan.
- **Labels:** `phase-4-frontend`, `testing`, `security`, `frontend`
- **Depends on:** P4-03, P4-04  **Blocks:** P5-01

## 17. Definition of done

Suites merged and green in CI; the route matrix is complete; handoff posted to
P5-02.
