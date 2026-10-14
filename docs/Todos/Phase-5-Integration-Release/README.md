# Phase 5 — Integration and Release

This phase covers live integration of every component, the full security
regression (Arch §13, SC-01..SC-06), public deployment, the demo, and
submission.

Sources: PRD §1.1, §13, §14; Arch §13, §14, §15, §16.

## Tasks

| ID | Title | Owner | Size | Hard dependencies |
|---|---|---|---|---|
| [P5-01](P5-01-cross-service-integration.md) | Cross-service integration + live e2e slices | `VTCodeCraft` | M–L | P3-09, P4-03, P4-04, P4-05, P1-09, P2-08, P2-09 |
| [P5-02](P5-02-end-to-end-security-regression.md) | Live security regression (Arch §13, SC-01..06) | `samarth-chawla` | M–L | P5-01, P1-07, P1-10, P3-08 |
| [P5-03](P5-03-deployment-and-configuration.md) | Public demo deployment + configuration | `parthgoyal26-03-2005` | M | P1-05, P5-01 |
| [P5-04](P5-04-demo-and-submission.md) | Demo script, README claims, video, submission | `parthgoyal26-03-2005` | M | P5-02, P5-03 |

## Entry criteria

- G1–G3 passed (or fallbacks recorded). G4, G5, G6 recorded.
- All Phase 3 and Phase 4 exit criteria met. Offline suites green.
- D-07, D-16, D-24 decided.

## Deliverables

- `tests/e2e/{test_end_to_end,test_security_regression}.py` (live; skipped in CI).
- `docs/ARCHITECTURE.md`, `docs/SECURITY.md` (verified controls), `docs/DEMO.md`, the final `README.md`.
- Public deployment and reset procedure (`deploy/`).
- Recorded video (≤ 3:00) and a submission confirmation.

## Exit criteria (= project Definition of Done, PRD §14 / Arch §16)

- Every PRD §14 checkbox is either checked with evidence or explicitly reported as unverified/limited in the README.
- **G7:** Arch §13 tests pass (core items by 16 Oct 12:00) or the related claims are removed.
- Public URLs are verified on 17 Oct. The submission is confirmed before the verified deadline.

## Dependencies on other phases

- **In:** every Phase 1–4 task, through P5-01's hard dependencies and the gates.
- **Out:** none.

## Parallelization inside the phase

- P5-03 environment preparation runs in parallel with P5-01 (the same staging setup can be promoted).
- P5-02 starts per bullet as soon as the needed flows work in staging; it does not need P5-01 fully finished for every bullet.
- P5-04 drafting (DEMO.md skeleton) can start once G3/G4 are known; recording waits for P5-02 and P5-03.

## Feature freeze

16 Oct 12:00 (PRD §13; Arch §14). After that only critical fixes, routed to
the owning task. Stretch items (Neon setup page, Google Calendar) are considered
**only** if G7 passed by noon. This plan creates no tasks for them.
