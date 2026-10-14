# Phase 4 — Frontend

This phase covers the web entry point (typed request API), demo sign-in, the
team playground with honest outcome labels, the Maya-only manager console with
audit visibility, and access-control tests. It uses the same backend path as
Slack, with no demo bypass (Arch §9).

Sources: PRD §4.1, §10.2, §10.3; Arch §0.2, §9, §13.

## Tasks

| ID | Title | Owner | Size | Hard dependencies |
|---|---|---|---|---|
| [P4-01](P4-01-web-request-api.md) | Web request submit/status API | `VTCodeCraft` | M | P1-03, P3-04 |
| [P4-02](P4-02-frontend-foundation-session-and-api-client.md) | Sign-in, session, typed services | `parthgoyal26-03-2005` | S–M | P1-03, P4-01; **G1 + G3** |
| [P4-03](P4-03-team-web-playground.md) | Team web playground | `parthgoyal26-03-2005` | M | P4-02 |
| [P4-04](P4-04-manager-console-and-audit-view.md) | Manager console and audit view | `parthgoyal26-03-2005` | L | P4-02, P3-01, P3-02 |
| [P4-05](P4-05-frontend-tests-and-access-control.md) | Frontend tests and access control | `parthgoyal26-03-2005` | S–M | P4-03, P4-04 |

## Entry criteria

- **G1 (E1) and G3 (E3) passed, or fallbacks recorded** (Arch §0.2 "experiments before UI", §12 build gate; D-19). P4-01 is backend work and may start before the gate.
- D-21 decided (web API paths and polling).
- Contracts for the session and request APIs regenerated (`npm run gen:types`).

## Deliverables

- `apps/api/routes/{requests,manager}.py` and `services/contracts/{api_requests,api_manager}.py`.
- Web pages: sign-in, playground, manager console, with tests.
- Access-control and no-simulated-approval suites.

## Exit criteria

- A team member can sign in, ask a question, and see an honest status. HOLD shows pending with no approve control. The hijack toggle works and shows the server-reported layer and zero sink writes.
- Maya sees the timeline, holds (console link only), unanswered questions, topic rules, and health. The D-12 panels are built through the decided path or marked unavailable.
- Web CI and API CI are green, and P4-05 suites cover every `/api/v1` route.

## Dependencies on other phases

- **In:** P1-03 (session), P3-04 (orchestrator), P3-01 (queries), P3-02 (topic map), P3-07 (toggle fields), P1-07/P1-10 (label and HOLD wording), D-12.
- **Out:** P5-01 (integration), P5-03 (web deployment), P5-04 (demo screenshots).

## Parallelization inside the phase

- P4-01 (`VTCodeCraft`) runs while the gates are pending.
- After G1 + G3: P4-02, then P4-03 and P4-04 in parallel (same owner; split with a contributor if needed), then P4-05.

## Coordination points

- Contract registration lines in `services/contracts/__init__.py` / `schema_export.py` (steward `VTCodeCraft`) are touched by P4-01 and P4-04. Rebase and regenerate on conflict.
- `apps/api/routes/router.py` (steward `VTCodeCraft`): P4-01 and P4-04 each add one include line.
