# Phase 3 — Agent and Slack

Planning, tool selection, grounded responses, the request orchestrator, Slack
Socket Mode, safe delivery, HOLD notification, the hijacked-model mode, audit
persistence, and grounding evaluation (E17).

Sources: PRD §4, §5.2, §6.2, §7, §8.3, §8.4, §10; Arch §4, §5.3, §5.6, §6.2, §7.4, §8, §10, §13.

## Tasks

| ID | Title | Owner | Size | Hard dependencies |
|---|---|---|---|---|
| [P3-01](P3-01-request-audit-and-state-persistence.md) | Audit and request-state persistence | `parthgoyal26-03-2005` | M | P2-01 |
| [P3-02](P3-02-planner-plan-validation-and-topic-map.md) | Planner, plan validation, topic map | `VTCodeCraft` | M | P2-02, P3-01 |
| [P3-03](P3-03-grounded-responder-and-citation-validation.md) | Grounded responder, citations, abstention | `VTCodeCraft` | M | P2-02 |
| [P3-04](P3-04-request-orchestrator.md) | Request orchestrator | `VTCodeCraft` | L | P1-02, P1-03, P3-01, P3-02, P3-03 (+G1 to merge real gateway) |
| [P3-05](P3-05-slack-socket-mode-adapter.md) | Slack Socket Mode adapter | `VTCodeCraft` | M | P1-03, P3-01 |
| [P3-06](P3-06-delivery-routing-and-hold-notification.md) | Delivery routing + HOLD DM | `parthgoyal26-03-2005` | M | P3-01 |
| [P3-07](P3-07-hijacked-model-simulation.md) | Simulate hijacked model + honeypot routing | `VTCodeCraft` | M | P1-02, P2-10, P3-03, P3-04 |
| [P3-08](P3-08-e17-grounding-evaluation.md) | E17 grounding eval + `MIN_SCORE` | `Aditya-Rawat01` | M | P2-05, P3-03, P2-03 |
| [P3-09](P3-09-agent-integration-and-injection-tests.md) | Offline agent integration + injection tests | `VTCodeCraft` | M | P3-04, P3-05, P3-06, P3-07 |

## Entry criteria

- D-19 confirmed: modules not dependent on unverified ArmorIQ behavior may be built against fakes before G1/G3.
- P2-02 merged (argument models). P3-01 is needed by most tasks.
- Decisions before finishing: D-13, D-14, D-15, D-30 (planner); D-09 (responder); D-27 (Slack); D-22 (HOLD DM link); D-28, D-29 (hijack).

## Deliverables

- `apps/api/{planner,topic_map,responder,orchestrator,delivery,hijack_simulation}.py`.
- `services/slack_socket/{app,events,sender}.py`, `services/audit/{store,queries,redaction}.py`, `apps/api/core/db.py`.
- `docs/SLACK_SETUP.md`, `seed/eval/`, the E17 script, the E17 record.
- Offline integration and injection test suites.

## Exit criteria

- Slack mention, DM, and `store this` work in the real workspace for a team-tier question, with citation and audit trace (Arch §14, 14 Oct exit gate).
- Real-gateway orchestrator wiring merged after G1. Confidential DM-only delivery is demonstrated.
- E17 recorded and `MIN_SCORE` calibrated (G6).
- P3-09 suites green. Every Arch §13 bullet mapped to offline or live coverage.

## Dependencies on other phases

- **In:** P1-02 (gateway), P1-03 (identity), G1 (real-gateway merge), P1-07 / G5 (labels), P1-10 / D-22 (console link), P2-01/02/03/05/10.
- **Out:** P4-01 (orchestrator `handle`), P4-03 (hijack toggle fields), P4-04 (audit queries, topic map), P5-01, P5-02.

## Parallelization inside the phase

- Start when the dependencies allow: P3-01 (`parthgoyal26-03-2005`) after P2-01; P3-03 (`VTCodeCraft`) after P2-02; P3-02 after P2-02 + P3-01.
- P3-05 and P3-06 can proceed in parallel against the PROPOSED `SlackSender` / `deliver` interfaces.
- P3-04 integrates them; P3-07 then P3-09 follow. P3-08 (`Aditya-Rawat01`) runs in parallel once P2-05 and P3-03 are merged.

## Coordination points

- `apps/api/main.py` lifespan: P3-04 and P3-05 (same steward) add their singletons.
- `tests/api/fakes/`: owned by P3-04; P3-09 extends it.
- `apps/api/responder.py`: P3-03 creates it; P3-07 adds the injection hook (same owner).
