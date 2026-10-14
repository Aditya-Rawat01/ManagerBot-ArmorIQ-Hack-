# P5-01 — Cross-service integration and live end-to-end vertical slice

| Field | Value |
|---|---|
| Phase | 5 — Integration and Release |
| Owner | `VTCodeCraft` |
| Size | M–L (4–6 h) |
| Target window | 16 Oct AM (before the noon freeze) |
| Gate role | Arch §14 exit gates (14 Oct slice; 15 Oct "no protected read bypasses ArmorIQ") |
| Status | Not started |

## 1. Task ID and title

P5-01 — Integrate Slack, web, API, ArmorIQ, tool server, and Neon in a shared staging environment, and prove the live vertical slices end to end with correlated audit and ledger evidence. Write `tests/e2e/test_end_to_end.py` and `docs/ARCHITECTURE.md` (as built).

## 2. Purpose and expected outcome

Each component may pass its own tests and still fail together. Outcome: the
following live flows are demonstrated with evidence:

- a team-tier question from Slack;
- a confidential BLOCK (Priya) and a HOLD (Sam);
- `store this`;
- a Maya confirmation;
- the same question via the web.

A live e2e test module (skipped without secrets) and an as-built architecture
document are delivered.

## 3. Authoritative source references

- PRD §2 P0 (web uses the same path), §13 (14 Oct: one team-tier question end to end with citation and audit; 15 Oct: minimal web demo on the same path), §14.
- Arch §8, §9 (no demo bypass), §11 (`test_end_to_end.py`; `docs/ARCHITECTURE.md`), §14 exit gates, §16 DoD, §18 (first vertical slice).

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Contributors: all owners (their components); `parthgoyal26-03-2005` (staging environment).

## 5. Prerequisites and dependencies

- Hard:
  - P3-09 (offline suites green);
  - P4-03 and P4-04 (web);
  - **P1-09 (G3)**;
  - P2-08 and P2-09 (tools deployed).
- Soft: P1-10 (G4) for the approval flow; P5-03 (environment) can be set up in parallel.
- Blocks: P5-02, P5-03 (final deploy of a verified build).

## 6. Exact files/modules this task owns

- `tests/e2e/__init__.py`, `tests/e2e/test_end_to_end.py` (new; Arch §11 documented name; live; skipped unless `PROXYLEAD_E2E=1` (PROPOSED supporting variable, DECISIONS M-08) and the required variables are set)
- `docs/ARCHITECTURE.md` (new; documented; steward `VTCodeCraft`): as-built components, data flow, trust boundaries, where each Arch §5.4 layer is enforced (file references)

## 7. Existing files it may need to modify

- `pyproject.toml` (steward: same owner): a pytest marker (`e2e`) and keeping `tests/e2e` excluded from the default CI selection.

## 8. Shared interfaces and contracts

- Exercises every interface in SHARED_INTERFACES.
- Interface mismatches found here go through an ICR to the owner. This task does not patch other owners' files.

## 9. Step-by-step implementation requirements

1. With P5-03, bring up staging: API, tool server, web, Slack app, Neon (seeded), active ArmorIQ policy.
2. Run and record each flow, with request ID, audit timeline, ledger `jti`, and delivery record:
   1. Sam asks a team-tier decision in an allowed Slack channel → threaded answer with a citation (Arch §15.1).
   2. Priya asks a confidential topic → real BLOCK, no source text or metadata. Sam asks the same → real HOLD, then Maya's DM (Arch §15.2).
   3. Maya approves in the console → verified resume → DM to Sam only (Arch §15.3). Only if G4 passed; otherwise show the genuine pending state.
   4. `@ProxyLead store this ...` → draft. Maya confirms the exact record and version through her own authorized action (Arch §15.5).
   5. The same question as step 1 via the web playground → identical plan and decision path.
3. For each flow, assert that every UI and Slack status maps to a persisted event, and that every "executed" step has a matching ledger row (SC-06).
4. Encode flows 1, 2 (BLOCK part), 4, and 5 in `tests/e2e/test_end_to_end.py`. These are live and manual; never claimed from mocks.
5. Write `docs/ARCHITECTURE.md` from the actual code, marking anything unverified.

## 10. Acceptance criteria

- Flows 1, 2, 4, and 5 pass live, with an evidence table (request IDs, `jti`, timestamps) posted in the issue. Flow 3 is either passed or shown as genuinely pending (per G4).
- Audit-to-ledger correlation holds for every executed step.
- `docs/ARCHITECTURE.md` merged. No unverified behavior is presented as working.

## 11. Required unit, integration, and security tests

- `tests/e2e/test_end_to_end.py` (live).
- Offline suites (P3-09, P4-05) re-run green on the integration branch.

## 12. Commands for verification

```bash
PROXYLEAD_E2E=1 python -m pytest tests/e2e -m e2e -q    # live, needs staging secrets
bash scripts/check.sh
```

## 13. Explicit non-goals

- No security regression matrix (P5-02). No production hardening (P5-03). No feature work.

## 14. Risks and unresolved questions

- Integration defects close to the freeze. Fix them via the owning tasks, smallest change first.
- Slack scopes or hosting surprises (D-07, D-27).

## 15. Handoff deliverables for dependent agents

- P5-02: staging access, flow scripts, evidence format.
- P5-03: the verified build SHA to deploy.
- P5-04: flow evidence and screenshots for the demo.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P5-01] Integrate all services and prove live end-to-end vertical slices`
- **Body outline:** flows 1–5; evidence table; correlation rule; e2e test; ARCHITECTURE.md.
- **Labels:** `phase-5-release`, `testing`, `backend`
- **Depends on:** P3-09, P4-03, P4-04, P4-05, P1-09, P2-08, P2-09  **Blocks:** P5-02, P5-03

## 17. Definition of done

Live evidence posted; e2e test and ARCHITECTURE.md merged; verified build SHA
handed to P5-03.
