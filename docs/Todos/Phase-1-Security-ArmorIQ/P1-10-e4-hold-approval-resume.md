# P1-10 — E4: HOLD → console approval → resume (and every failure path)

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | M (3–4 h; needs a person acting as Maya) |
| Target window | 15 Oct PM |
| Gate role | **G4**: required before presenting a working approval demo; SC-04 |
| Status | Not started — E4 = NOT RUN |

## 1. Task ID and title

P1-10 — Run E4: Sam's confidential search triggers a real HOLD; Maya approves or rejects in the ArmorIQ console; verify SDK resume on the same plan and step; test rejection, timeout, token expiry, and duplicate resume.

## 2. Purpose and expected outcome

The approval story must be real or explicitly downgraded (PRD §0.8, §14).
Outcome:

- evidence of the delegation lifecycle as the pinned SDK actually exposes it;
- the console surface Maya uses (D-22);
- whether approver identity is observable (U-11);
- the accepted token lifetime (PRD §7.1);
- the fail-closed behavior on every non-approved path.

## 3. Authoritative source references

- PRD §0.8, §1.1 SC-04, §7.6–7.8, **§7.1** (token validity; lower the approval timeout if needed; no auto-replan), §11 (E4 = legacy E6 + E7 + E11), §14.
- Arch §5.1 (token validity; test the maximum accepted lifetime and approval wait), **§5.6** (real HOLD flow; background wait; same plan/step; never self-approval; approver rank), §12 E4 (pass and fallback), §15 demo step 3.

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributors: the person holding the Maya account (console approval), `VTCodeCraft` (P3-04 / P3-06 consumers).

## 5. Prerequisites and dependencies

- Hard: P1-09 (Sam HOLD reproduced through the live policy).
- Soft: P2-07 deployed (duplicate-resume dedupe by `jti`); P1-02 HOLD wait implementation.
- Decisions: D-02, D-18.
- Blocks: P5-02; P3-06 finish (console link text, D-22); P4-04 holds panel (D-22).

## 6. Exact files/modules this task owns

- `scripts/{E4_PROBE}`: PRD name `probe_v2_e4_hold.py`; Arch §11 tree `probe_e5_hold.py` (legacy number; Arch §12 says to rename) (D-02).
- `docs/EXPERIMENTS.md` § E4.

## 7. Existing files it may need to modify

- None. Token-lifetime adjustments go through an ICR to `apps/api/core/config.py` defaults or `.env.example` comments (their stewards).

## 8. Shared interfaces and contracts

- Confirms or corrects SHARED_INTERFACES §8 (states, resume mechanics, console link, approver observability) and §3 (HOLD mapping) via ICR.

## 9. Step-by-step implementation requirements

1. **Approve path.** Sam invokes `knowledge_search_confidential` through the policy path with waiting enabled. Record:
   - the SDK's hold signal and the delegation ID (if exposed);
   - that no protected result was returned before approval;
   - where the request appears in Maya's console (exact navigation; D-22).
   Maya approves **in the console**. Record whether the SDK resumes the **same** plan and step, then the tool-server ledger row and the result.
2. **Reject path.** Repeat; Maya rejects. Expect no execution and no ledger row. Record the SDK status.
3. **Timeout path.** Use an approval timeout that still satisfies the startup assertion. Do not approve. Expect no execution; record the status. Never auto-replan.
4. **Token expiry.** Find the maximum `TOKEN_VALIDITY_S` the platform accepts (PRD §7.1). Show that a resume after expiry fails closed. If 1200 s is not accepted, record the accepted value and the lowered `APPROVAL_TIMEOUT_MS` that keeps `TOKEN_VALIDITY_S*1000 > APPROVAL_TIMEOUT_MS + 120000`.
5. **Duplicate resume.** Attempt a second resume or replay of the approved invocation. Expect it to be deduplicated by `jti` (tool server) and executed once.
6. **Self-approval.** Confirm a requester cannot approve their own request. Record how the platform enforces rank.
7. **Approver observability.** Record whether the SDK exposes who approved (U-11).
8. Record E4 with the template. If approve → resume cannot be verified, the outcome is not `PASS`. Apply the fallback: keep the request pending, show the console link, and label resume "unverified" (Arch §12 E4).

## 10. Acceptance criteria

- Only the approved invocation executes, exactly once, under the original plan and step. Every other path shows zero executions.
- The accepted token lifetime and the approval timeout are recorded; config ICRs are filed if values change.
- The D-22 console link is recorded for P3-06 and P4-04.
- The outcome is honest, with the fallback stated if not `PASS`.

## 11. Required unit, integration, and security tests

- Live experiment only. P1-02 offline tests cover state mapping; they are not evidence.

## 12. Commands for verification

```bash
python scripts/<E4 probe per D-02>.py --scenario approve|reject|timeout|expiry|duplicate
git diff -- docs/EXPERIMENTS.md
```

(The probe's CLI shape is up to the owner. Document it in the script.)

## 13. Explicit non-goals

- No in-app approval button. No Slack-based approval (R-40).
- No UI. No HOLD DM implementation (P3-06).

## 14. Risks and unresolved questions

- The SDK may not expose resume as expected. Then use the fallback; never simulate.
- A human must act as Maya in the console during the run. Schedule it.
- D-22, U-11.

## 15. Handoff deliverables for dependent agents

- P3-06: console link and the minimal DM content allowed.
- P3-04: real status values and resume behavior.
- P4-04: holds panel fields.
- P5-02 / P5-04: SC-04 evidence and the demo-script wording (verified or "pending/unverified").

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-10] E4: verify HOLD, ArmorIQ console approval/rejection, resume and expiry`
- **Body outline:** scenarios 1–7; pass and fallback definitions; evidence fields; scheduling note for the Maya approver.
- **Labels:** `phase-1-security`, `armoriq`, `experiment`, `gate`, `security`
- **Depends on:** P1-09; D-02, D-18  **Blocks:** P5-02, P3-06 (finish), P4-04 (holds panel)

## 17. Definition of done

E4 record merged with every scenario evidenced; G4 announced (PASS, or
fallback in force); token and timeout ICRs filed if needed.
