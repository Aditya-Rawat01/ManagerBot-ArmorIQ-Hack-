# P4-02 — Frontend foundation: sign-in, session state, typed API client

| Field | Value |
|---|---|
| Phase | 4 — Frontend |
| Owner | `parthgoyal26-03-2005` |
| Size | S–M (2–3 h) |
| Target window | 15 Oct PM (after G1 + G3) |
| Gate role | Entry: **G1 + G3** (Arch §0.2 "experiments before UI"; D-19) |
| Status | Not started |

## 1. Task ID and title

P4-02 — Add demo sign-in (persona + passcode), session-aware routing, and typed API services for the session and request endpoints to `apps/web`, reusing the existing `apiClient`.

## 2. Purpose and expected outcome

The web UI needs authenticated sessions before any chat or console work.
Outcome:

- a sign-in page;
- session state from `GET /api/v1/session`;
- sign-out;
- typed services built on the generated contract types;
- cookie credentials sent on every call;
- no persona or identity ever sent after sign-in.

## 3. Authoritative source references

- PRD §4.1 (persona sign-in + `DEMO_PASSCODE`; server-side session; never a persona from the chat body), §10.2.
- Arch §0.2 (experiments before UI), §9 (public demo safety: session determines identity; secrets and emails off the client; no public sign-up).

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Reviewer: `VTCodeCraft`.

## 5. Prerequisites and dependencies

- Hard: P1-03 (session API), P4-01 (request API contract).
- Gate: **G1 (P1-06) and G3 (P1-09)** passed, or a fallback recorded (D-19).
- Blocks: P4-03, P4-04.

## 6. Exact files/modules this task owns

All under `apps/web/` (steward: this owner):

- `src/services/session.ts` (new), `src/services/requests.ts` (new)
- `src/hooks/useSession.ts` (new)
- `src/pages/SignInPage.tsx` (new)
- `src/App.tsx` (existing): routing between sign-in, playground, and manager console
- `src/services/apiClient.ts` (existing): `credentials: "include"`; `401` handling
- Tests: `src/services/session.test.ts`, `src/pages/SignInPage.test.tsx` (new)

## 7. Existing files it may need to modify

- `apps/web/src/types/generated/contracts.ts`: regenerated only (never edited by hand).
- `apps/web/package.json`: add a router dependency only if needed (record the pin).

## 8. Shared interfaces and contracts

- Consumes: `SessionInfo`, request models (generated TypeScript), the `ErrorResponse` envelope, the session and request endpoints (SHARED_INTERFACES §1).

## 9. Step-by-step implementation requirements

1. Sign-in form: persona select (Maya, Sam, Priya) + passcode → `POST /api/v1/session`. Show generic errors for `401` and `503`.
2. On load, call `GET /api/v1/session`. Unauthenticated → sign-in page.
3. Display the persona and masked email from `SessionInfo` only. No raw email exists client-side.
4. `requests.ts`: `submitRequest({message, confirmationToken?, simulateHijackedModel?})` and `getRequestStatus(id)`. **No persona or identity parameter exists in these functions.**
5. Global `401` handling → back to sign-in.
6. Keep the existing health indicator.
7. Same-origin deployment (ADR-030): `VITE_API_BASE_URL=/` must make the client call relative `/api/v1/...` paths. Keep `credentials: "include"` for local development, where the API runs on another port.

## 10. Acceptance criteria

- Sign-in, reload persistence (cookie), and sign-out work against the local API.
- The TypeScript request service types make identity fields impossible (compile-time) and the payloads contain none (test).
- No secrets or emails in the bundle (`grep` of `dist/` for `@` domains and token patterns).
- Lint, typecheck, tests, and build pass.

## 11. Required unit, integration, and security tests

- `session.test.ts`: sign-in, `401`, `503`, sign-out.
- `SignInPage.test.tsx`: form behavior and error display.
- A payload test that `submitRequest` sends only the allowed fields.
- A config test that `VITE_API_BASE_URL=/` produces relative `/api/v1/...` request URLs (ADR-030).

## 12. Commands for verification

```bash
cd apps/web && npm run lint && npm run typecheck && npm test && npm run build
grep -RIEo "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" apps/web/dist || echo "no emails in bundle"
```

## 13. Explicit non-goals

- No chat UI (P4-03). No manager console (P4-04). No sign-up or SSO.

## 14. Risks and unresolved questions

- Hosting (ADR-030): Vercel proxies `/api/*` to the Render API, so the session cookie is first-party. P5-03 verifies sign-in on the deployed URL.

## 15. Handoff deliverables for dependent agents

- P4-03 / P4-04: the `useSession` hook, services, and route structure.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P4-02] Add web sign-in, session state and typed request services`
- **Body outline:** pages; services; no-identity-in-payload rule; tests; gate note (G1 + G3).
- **Labels:** `phase-4-frontend`, `frontend`, `security`
- **Depends on:** P1-03, P4-01; gate G1 + G3 (P1-06, P1-09)  **Blocks:** P4-03, P4-04

## 17. Definition of done

Merged with web CI green; handoff posted to P4-03 and P4-04.
