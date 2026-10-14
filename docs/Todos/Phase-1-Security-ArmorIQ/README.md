# Phase 1 — Security and ArmorIQ

Establish real ArmorIQ compatibility, trusted identity, the signed `_ctx`
boundary, the policy, and the required live experiments E1–E5. **The security
gates live here, at the start.** Protected product functionality is not
"ready" until the gates below have passed or have a documented fallback.

Sources: PRD §0, §4.1, §5.3, §7, §9, §11, §13; Arch §0, §5, §6.3, §12, §14.

## Tasks

| ID | Title | Owner | Size | Hard dependencies | Gate |
|---|---|---|---|---|---|
| [P1-01](P1-01-armoriq-org-members-and-agent-setup.md) | ArmorIQ org, members, `proxylead` agent | `samarth-chawla` | S | — | prereq |
| [P1-02](P1-02-armoriq-sdk-verification-and-gateway.md) | SDK surface verification + `ArmorIQGateway` | `samarth-chawla` | L | — | — |
| [P1-03](P1-03-trusted-identity-and-web-session.md) | Trusted identity + web session | `VTCodeCraft` | M | — | — |
| [P1-04](P1-04-signed-ctx-and-toolserver-request-security.md) | Signed `_ctx` + tool-server request security | `samarth-chawla` | M | — | **G0** |
| [P1-05](P1-05-toolserver-public-https-experiment-deployment.md) | Tool server on public HTTPS (experiments) | `parthgoyal26-03-2005` | S–M | D-07 | prereq |
| [P1-06](P1-06-e1-real-call-reaches-mcp.md) | E1: real call reaches MCP | `samarth-chawla` | M | P1-01, P1-02, P1-05, P2-06 | **G1** |
| [P1-07](P1-07-e2-off-plan-and-tamper.md) | E2: off-plan and tamper | `samarth-chawla` | S–M | P1-06, P1-04 | **G5** |
| [P1-08](P1-08-policy-and-e5-setup-conformance.md) | Policy + E5 setup conformance | `samarth-chawla` | M | P1-01, P1-06, P2-08, P2-10 | **G2** |
| [P1-09](P1-09-e3-per-member-outcomes.md) | E3: per-member outcomes | `samarth-chawla` | M | P1-08 | **G3** |
| [P1-10](P1-10-e4-hold-approval-resume.md) | E4: HOLD → console approval → resume | `samarth-chawla` | M | P1-09 | **G4** |

## Entry criteria

- Repository at or after `5e97604` with green CI.
- Real ArmorIQ console access for the owner, and a team member able to act as Maya (Org Admin).
- Decisions resolved before the affected task starts: D-02 and D-18 (probes), D-07 (hosting). D-19 (parallel-work rule) confirmed by the team.

## Deliverables

- `services/authorization/armoriq_gateway.py`, `services/authorization/ctx_envelope.py`, `services/toolserver/security.py` (extended), `apps/api/identity.py`, `apps/api/routes/session.py`.
- `policy/proxylead-policy.yaml` (placeholders), `scripts/check_setup.py`, five probe scripts (names per D-02).
- `docs/EXPERIMENTS.md` E1–E5 records; `docs/SECURITY.md` (SDK surface, request verification, policy facts).
- Tool server reachable over public HTTPS.

## Security gates (what each gate unlocks)

| Gate | Check | Task | Unlocks | If it fails |
|---|---|---|---|---|
| **G0** | `_ctx` + credential verification merged; backend DB role denied protected reads (P2-01 test) | P1-04, P2-01 | Deploying **any** product tool (P2-06+) | Do not deploy product tools |
| **G1** | E1 PASS (or documented fallback) | P1-06 | E2/E5; merging real gateway integration into the orchestrator (P3-04); Phase 4 start (with G3) | 2-hour diagnosis → fallback (ADR-006 transport / PRD v1 SDK-proxy fallback); narrow claims |
| **G2** | E5 PASS: policy accepted, names verified, mismatch fails closed | P1-08 | E3 | Fix names/schema to verified values only |
| **G3** | E3 PASS: matrix matches live, no identity leakage | P1-09 | Phase 4 start (with G1); any per-member outcome claim; P5-01 | Fallback recorded; no matrix claim |
| **G4** | E4 PASS: approve/reject/timeout/expiry/duplicate verified | P1-10 | Presenting a working approval demo | Show genuine pending state + console link; label resume "unverified" |
| **G5** | E2 recorded with true enforcement layers | P1-07 | Showing off-plan/honeypot enforcement labels (P3-07, P5-02) | Show only labels that evidence supports |

Arch §12 build gate: **E1 and E3 must pass before the full implementation.**
Under D-19 (PROPOSED), backend modules that do not depend on unverified
ArmorIQ behavior may be built against fakes earlier. UI starts after G1 + G3.

## Exit criteria

- G0–G3 passed, or a documented fallback is recorded and announced. G4 and G5 recorded (PASS or fallback).
- Every `docs/EXPERIMENTS.md` row for E1–E5 shows an honest outcome with evidence references.
- ICRs filed for every interface the evidence changed.

## Dependencies on other phases

- **In:** P2-06 (first product tool, which needs P2-01/02/03/04/07) for E1; P2-08/P2-10 (tier tools + honeypot) for E5/E3; P2-09 is a soft input (re-run `check_setup.py`).
- **Out:** P3-04 (gateway), P3-05/P4-01/P4-02 (identity), P3-06/P4-04 (E4 console link), P3-07 (E2 labels), P5-02 (all evidence).

## Parallelization inside the phase

- Immediately and in parallel: P1-01, P1-02, P1-03, P1-04, P1-05 (after D-07). The P1-08 YAML draft can also start now.
- Sequential (live): P1-06 → P1-07 ‖ P1-08 → P1-09 → P1-10. All live runs are by one owner; schedule a Maya approver for P1-10.
