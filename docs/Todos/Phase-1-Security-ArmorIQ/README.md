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

## Issue tracking and readiness

GitHub issue numbers are recorded in [../GITHUB_ISSUES.md](../GITHUB_ISSUES.md) (mapping table). Decisions and owner handles are tracked in [#1](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/1). No issue is assigned until the owner handles are confirmed (D-25). "Ready" means work may begin; it is not a claim that a gate has passed.

| Task | Issue | Can start | Must wait for before merge |
|---|---|---|---|
| P1-01 | [#2](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/2) | Now (needs real ArmorIQ Org Admin access) | — |
| P1-02 | [#3](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/3) | Now (offline, fake SDK) | D-03 decided; P1-04 merged so the gateway uses the real `sign_ctx` |
| P1-03 | [#4](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/4) | Now | D-20 decided |
| P1-04 | [#5](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/5) | Now | D-04 decided; D-05 code inspection recorded |
| P1-05 | [#6](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/6) | After D-07 | — (scaffold deploy only; no product tool before P1-04 merges) |
| P1-06 (E1) | [#7](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/7) | After P1-01, P1-02, P1-05, P2-06 merge, and after D-02, D-18 | Live E1 record in `docs/EXPERIMENTS.md` |
| P1-07 (E2) | [#8](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/8) | After P1-06, P1-04 merge | Live E2 record |
| P1-08 (E5) | [#9](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/9) | YAML draft now (placeholders, D-26); console work after P1-01, P1-06, P2-08, P2-10 merge | D-26, D-17, D-12 (option (a) changes the tool list), D-02, D-18; live E5 record |
| P1-09 (E3) | [#10](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/10) | After P1-08 merges | Live E3 record meeting the ArmorIQ evidence rule |
| P1-10 (E4) | [#11](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/11) | After P1-09 merges and a Maya approver is scheduled | Live E4 record (PASS or documented fallback) |

**Plan note.** Phase 1 cannot be completed before Phase 2. E1, E5, and E3 need these Phase 2 PRs merged and deployed first: P2-01, P2-02, P2-03 (seed record), P2-04, P2-07, P2-06, P2-08, P2-10 (P2-09 is a soft input to E5). The root [PR merge order](../README.md#pr-merge-order) interleaves them: Phase 1 setup is Wave 1, E1/E2 are Wave 5, and E5/E3/E4 are Wave 6.

## Phase 1 completion validation

Run this **after** every Phase 1 PR, and the Phase 2 PRs listed above, are merged and the integration branch (`main`) is up to date and deployed. Each individual PR still runs its own scoped tests (task file §11–§12) before it merges.

"Verified" below means the command or file exists in the repository today. Items created by a task are **unverified until that task's PR merges**. Probe file names are pending D-02; the PRD names are shown.

### 1. Local automated checks (verified)

```bash
bash scripts/check.sh   # ruff lint+format, schema drift, pytest, web gen:types/lint/typecheck/test/build
```

To match CI's per-extra isolation, run each in a fresh virtual environment:

```bash
python -m pip install -e ".[dev]" && python -m pytest tests/contracts
python -m pip install -e ".[api,dev]" && python -m pip check && python -m pytest tests/api
python -m pip install -e ".[toolserver,dev]" && python -m pip check && python -m pytest tests/toolserver
```

### 2. Unit and contract tests: gateway and security context

| File | Status | Owner task |
|---|---|---|
| `tests/api/test_guardrails.py` (single `armoriq_sdk` importer, pins) | Verified (exists) | P1-02 keeps it green |
| `tests/toolserver/test_auth.py` (bearer guard) | Verified (exists) | P1-04 keeps it green |
| `tests/toolserver/test_config_and_guardrails.py` (no `armoriq_sdk` in tool server) | Verified (exists) | P1-04 / P2-06 |
| `tests/api/test_armoriq_gateway.py` | Unverified (P1-02 creates it) | P1-02 |
| `tests/api/test_identity.py`, `tests/api/test_session.py` | Unverified (P1-03) | P1-03 |
| `tests/api/test_ctx_envelope.py`, `tests/toolserver/test_security_ctx.py` | Unverified (P1-04) | P1-04 |
| `tests/api/test_check_setup_logic.py` | Unverified (P1-08) | P1-08 |

These all run inside `bash scripts/check.sh` once merged. They are offline and **never** count as gate evidence.

### 3. Real ArmorIQ integration experiments (all unverified; scripts do not exist yet)

| Gate | Command (PRD names, pending D-02) | Record |
|---|---|---|
| E5 setup check | `python scripts/check_setup.py` | `docs/EXPERIMENTS.md` § E5 |
| G1 E1 | `python scripts/probe_v2_e1_allowed.py` | § E1 |
| G5 E2 | `python scripts/probe_v2_e2_offplan.py` | § E2 |
| G2 E5 | `python scripts/probe_v2_e5_setup.py` | § E5 |
| G3 E3 | `python scripts/probe_v2_e3_per_user.py` | § E3 |
| G4 E4 | `python scripts/probe_v2_e4_hold.py` (scenarios approve/reject/timeout/expiry/duplicate) | § E4 |

Rules:

- Experiments run against the real ArmorIQ organization and the deployed HTTPS tool server, with real `.env` values.
- Each record states the deployed commit SHA, and an outcome of `PASS`, `FAIL`, `BLOCKED ON ACCESS`, or `INCONCLUSIVE`.
- Nothing is marked `PASS` without a real run and inspected evidence.

### 4. Evidence-based security checks (drawn from E1–E5 and P1-04)

| Check | Evidence source | Counts as ArmorIQ policy evidence? |
|---|---|---|
| Allowed access: Maya `knowledge_search_team` reaches the tool server with a correlated ledger row | E1 | ALLOW + ledger row |
| Allowed access per member: team cells; Maya confidential/restricted (elevated) | E3, E5 `check_setup.py` | Only with an ArmorIQ source + evidence reference + ledger row |
| Denied access: Priya confidential, Sam/Priya restricted | E3 | **Only** under the ArmorIQ evidence rule (P1-09 §10). Otherwise `FAIL` |
| HOLD: Sam confidential | E3, then E4 | E3 evidence rule; E4 approve/reject/timeout/expiry/duplicate/self-approval |
| Honeypot: `send_external_message` for all three members | E3 (three cells) | Evidence rule; zero `egress_sink` writes |
| Off-plan attempt (including the honeypot) | E2 | No. Labeled `armoriq_sdk_plan_check` unless E2 proves a proxy decision |
| Forged `_` fields, tampered args, direct calls without valid `_ctx`, replayed `jti` | E2 + P1-04 tests | No: `tool_server_rule` |
| Tool-server tier ceiling (Sam restricted, Priya confidential, direct call) | E3 step 5 (separate table) | No: `tool_server_rule`, reported separately |
| Name mismatch fails closed | E5 | Only if the enforcing layer is an ArmorIQ source |

### 5. Regression and full-suite run

1. `bash scripts/check.sh` on the up-to-date `main`.
2. `python scripts/check_setup.py` against the deployed environment.
3. Re-run any experiment whose code path (gateway, `_ctx`, tool server, policy) changed after its recorded run. Recommendation: compare the recorded commit SHA with the deployed SHA.
4. Fix failures in the owning task's branch, merge in order, and repeat steps 1–3 until all pass.

Phase 1 is complete only when:

- G0–G3 pass with real evidence;
- G4 and G5 are recorded (PASS or a documented fallback; a G4 fallback means no "working approval" claim);
- every check in table 4 is recorded honestly.
