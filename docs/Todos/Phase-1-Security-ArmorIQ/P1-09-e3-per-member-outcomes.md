# P1-09 — E3: different outcomes for different members on the same tool

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | M (2–4 h) |
| Target window | 15 Oct PM |
| Gate role | **G3** (build gate with G1, Arch §12); SC-01 |
| Status | Not started — E3 = NOT RUN |

## 1. Task ID and title

P1-09 — Run E3: the same tier-named tools invoked by Maya, Sam, and Priya through separate clients must produce the live ALLOW/HOLD/BLOCK matrix with no cross-persona identity leakage.

## 2. Purpose and expected outcome

This is the central claim of the product: one policy, distinct real outcomes
per requester. Outcome:

- live evidence that every matrix cell matches;
- execution only on ALLOW (ledger);
- the tool server still rejects out-of-tier direct calls;
- concurrent runs keep attribution correct.

## 3. Authoritative source references

- PRD §1.1 SC-01, §4.1 (dedicated client per member), §5.1 (matrix), §5.3 (not working until E3/E5 show it), §11 (E3 = legacy E5 + E4b).
- Arch §5.2 (test at least three concurrent personas; no leakage), §5.5, §12 E3 (pass: outcomes match matrix and server-side tier checks remain enforced), §12 build gate.
- `ProxyLead_Day1_Experiments.md` E3 matrix (adds `send_external_message` BLOCK for all).

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributor: `Aditya-Rawat01` (ledger evidence).

## 5. Prerequisites and dependencies

- Hard: P1-08 (policy active; names verified), which includes P2-08 and P2-10 being deployed.
- Decisions: D-02, D-18.
- Blocks: P1-10, P5-01, Phase 4 start (G1 + G3).

## 6. Exact files/modules this task owns

- `scripts/{E3_PROBE}`: PRD name `probe_v2_e3_per_user.py`; Arch §11 tree `probe_e4_per_user.py` (legacy number; Arch §12 says to rename) (D-02).
- `docs/EXPERIMENTS.md` § E3.

## 7. Existing files it may need to modify

- None.

## 8. Shared interfaces and contracts

- Uses: the gateway (§3), `DecisionSource` (D-03), ledger correlation (§9).
- Confirms: the §3 mapping for policy BLOCK and HOLD (U-4). File an ICR if it differs.

## 9. Step-by-step implementation requirements

1. Use three separate clients (one per member). For each member invoke, each in its own one-step plan:
   - `knowledge_search_team`
   - `knowledge_search_confidential`
   - `knowledge_search_restricted`
   - `send_external_message`
   Optionally also the `knowledge_get_source_*` tools.
2. Expected (PRD §5.1 + honeypot):

   | Tool | Maya | Sam | Priya |
   |---|---|---|---|
   | team | ALLOW | ALLOW | ALLOW |
   | confidential | ALLOW | HOLD | BLOCK |
   | restricted | ALLOW | BLOCK | BLOCK |
   | `send_external_message` | BLOCK | BLOCK | BLOCK |

   Leave Sam's HOLD unresolved, or reject it in the console. Approval is E4.
3. For every cell record: decision, `DecisionSource`, ArmorIQ evidence reference, tool server reached (yes/no), ledger row (only for ALLOW).
4. **Concurrency.** Run the three personas in parallel (for example 3 × 5 requests). Verify that each ledger row's `requester_member_id` (from verified `_ctx`) and the console attribution match the invoking member.
5. **Server-side tier check.** Call `knowledge_search_restricted` directly at the tool server with a valid `_ctx` for Sam or Priya. Expect a tool-server rejection independent of ArmorIQ (R-26). This is only possible if the tool server knows the requester's allowed tiers. See risks.
6. Record E3 with the template.

## 10. Acceptance criteria

- All 12 cells match. Any mismatch → `FAIL`, with the 2-hour diagnosis rule and the fallback recorded.
- Ledger rows exist only for ALLOW cells. No execution on BLOCK or HOLD.
- No cross-persona attribution error in the concurrent run.
- Step 5 result recorded honestly (enforced / not enforced).

## 11. Required unit, integration, and security tests

- Live experiment only. Offline equivalents of step 5 belong to P2-08 tests.

## 12. Commands for verification

```bash
python scripts/<E3 probe per D-02>.py
git diff -- docs/EXPERIMENTS.md
```

## 13. Explicit non-goals

- No console approval or resume (P1-10). No UI.

## 14. Risks and unresolved questions

- Per-member `toolEnforcement` may not work through the proxy (U-4). If so, `FAIL` and fallback. Do not fake HOLD in the app (R-40).
- Step 5 depends on the tool-server member tier ceiling (SHARED_INTERFACES §6, PROPOSED in P2-08: Maya → restricted, Sam → confidential, Priya → team). The docs require rejecting out-of-tier calls even when the policy is too permissive (Arch §5.5, §13). Expect Priya → confidential and Sam → restricted to be rejected by `tool_server_rule`. Sam → confidential is gated by ArmorIQ HOLD, not by the tool server.

## 15. Handoff deliverables for dependent agents

- All: the G3 outcome.
- P1-10: Sam's HOLD reproduction steps.
- P3-04 / P4-03: the real observed decision shapes for BLOCK and HOLD.
- P5-02: SC-01 evidence rows.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-09] E3: verify per-member ALLOW/HOLD/BLOCK outcomes and identity isolation`
- **Body outline:** matrix; concurrency test; server-side tier check; pass definition; evidence fields.
- **Labels:** `phase-1-security`, `armoriq`, `experiment`, `gate`, `security`
- **Depends on:** P1-08; D-02, D-18  **Blocks:** P1-10, P5-01, P4-02

## 17. Definition of done

E3 record merged with all cells evidenced; G3 announced (PASS, or FAIL with
fallback); mapping ICRs filed if needed.
