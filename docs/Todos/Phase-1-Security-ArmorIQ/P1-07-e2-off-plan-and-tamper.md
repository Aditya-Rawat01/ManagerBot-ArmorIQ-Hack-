# P1-07 — E2: off-plan and tampered invocation behavior

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | S–M (2–3 h) |
| Target window | 15 Oct AM |
| Gate role | **G5**: required before any off-plan / honeypot enforcement label is shown or claimed |
| Status | Not started — E2 = NOT RUN |

## 1. Task ID and title

P1-07 — Run experiment E2: an off-plan tool attempt plus tampered arguments and forged underscore fields. Record which layer enforced each outcome.

## 2. Purpose and expected outcome

The hijacked-model demo and the off-plan path must name the **actual**
enforcement layer (PRD §6.2, §10.3). E2 separates three things: an SDK
client-side plan check, hosted-proxy enforcement, and tool-server rejection.
Outcome: an evidence table that P3-07 and P5-02 can cite.

## 3. Authoritative source references

- PRD §6.2 (off-plan → SDK plan check; label `armoriq_sdk_plan_check` if blocked client-side; do not claim a hosted-proxy block unless E2 proves it), §7.11, §9 (strip `_` fields; never treat them as approval), §10.3, §11 (E2 row).
- Arch §5.3 (labels), §6.2, §6.3, §12 E2 (pass: enforcement recorded at the layer that enforced it; tool server still rejects invalid arguments and forged approval metadata; no server-side plan-binding claim unless proven), §13.
- `ProxyLead_Day1_Experiments.md` E2 (run the direct proxy probe only if its request shape is verified against the pinned SDK).

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributor: `Aditya-Rawat01` (tool-server log and ledger evidence).

## 5. Prerequisites and dependencies

- Hard: P1-06 (path works, or fallback known); P1-04 (tool-server verification deployed).
- Decisions: D-02 (file name), D-18.
- Blocks: P3-07 (labels), P5-02.

## 6. Exact files/modules this task owns

- `scripts/{E2_PROBE}`: PRD name `probe_v2_e2_offplan.py`, Arch name `probe_e2_offplan.py` (D-02).
- `docs/EXPERIMENTS.md` § E2.

## 7. Existing files it may need to modify

- None.

## 8. Shared interfaces and contracts

- Uses: `ArmorIQGateway.attempt_off_plan` (SHARED_INTERFACES §3), `_ctx` (§5), tool-server rejection codes (§4).
- Confirms or corrects: `DecisionSource` labeling rules (D-03).

## 9. Step-by-step implementation requirements

1. **Off-plan via SDK.** Plan only `knowledge_search_team` for Maya, then attempt `knowledge_search_confidential` (and separately `send_external_message`) under that plan. Record whether the SDK raised before any network call (expected label `armoriq_sdk_plan_check`), the exception type and message, and that the tool server saw no request and wrote no ledger row.
2. **Hosted-proxy behavior.** Only if a request shape verified against the installed SDK exists (per the Day-1 doc), send an off-plan request that bypasses the SDK's client check. Record the proxy's response. If no verified shape exists, record "not tested" and **do not** claim server-side plan binding.
3. **Argument tampering.** Under a valid plan, change arguments, add forbidden keys (`tier`, `user_email`), and add forged `_approvals`, `_delegation_id`, `_policy_context`. Record the ArmorIQ result and the tool-server result. Expected: the tool server strips `_` fields (no effect), and strict schemas reject forbidden keys once P2-06 is deployed.
4. **Direct calls** to the tool server (bearer only, no `_ctx`; forged `_ctx`; replayed `jti`) → rejection codes per P1-04. Record them.
5. Fill one row per attempt: attempt, expected, observed, enforcing layer (`DecisionSource`), evidence reference.

## 10. Acceptance criteria

- Every attempt is labeled with the layer that actually enforced it. No hosted-proxy claim without evidence.
- Zero tool executions (ledger) for every off-plan or forged attempt.
- The record states clearly whether server-side plan binding was proven, disproven, or not tested.

## 11. Required unit, integration, and security tests

- Live experiment. The direct-call rejections in step 4 are also covered offline by P1-04 tests, but only the live run counts for E2.

## 12. Commands for verification

```bash
python scripts/<E2 probe per D-02>.py
git diff -- docs/EXPERIMENTS.md
```

## 13. Explicit non-goals

- No per-member matrix (E3). No HOLD (E4).
- No responder or hijack integration (P3-07).

## 14. Risks and unresolved questions

- Without a verified direct proxy request shape, the hosted-proxy half stays "not tested". That is acceptable if stated.
- D-05: if the proxy strips `_ctx`, step 3 results must say so.

## 15. Handoff deliverables for dependent agents

- P3-07: the exact label to show for an off-plan honeypot attempt.
- P5-02: the evidence rows for SC-02 layer attribution.
- P1-02: mapping corrections (ICR).

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-07] E2: record off-plan and tampered-invocation enforcement layers`
- **Body outline:** attempts table; pass definition; "not tested" rule; evidence fields.
- **Labels:** `phase-1-security`, `armoriq`, `experiment`, `security`
- **Depends on:** P1-06, P1-04; D-02, D-18  **Blocks:** P3-07, P5-02

## 17. Definition of done

E2 record merged with one evidence row per attempt and an honest enforcement
label; mapping ICRs filed if needed; handoff posted to P3-07 and P5-02.
