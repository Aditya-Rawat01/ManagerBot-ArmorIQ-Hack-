# P5-04 — Demo script, README claims, video, and submission

| Field | Value |
|---|---|
| Phase | 5 — Integration and Release |
| Owner | `parthgoyal26-03-2005` |
| Size | M (3–4 h) |
| Target window | 16 Oct PM (rehearsals) → 17 Oct (record and submit) |
| Gate role | SC-07 (≤ 3:00) |
| Status | Not started |

## 1. Task ID and title

P5-04 — Write `docs/DEMO.md`, update `README.md` with verified controls, unverified behavior, and limitations, rehearse three times from a clean reset, record a demo of 3:00 or less, and submit before the verified official deadline.

## 2. Purpose and expected outcome

The submission must show real calls and observed outcomes, with honest claims.
Outcome:

- a demo script matching Arch §15, adjusted to the gate outcomes;
- a README whose claims each cite a passing test or experiment;
- a video of 3:00 or less;
- a submission confirmed before the deadline.

## 3. Authoritative source references

- PRD §1.1 SC-07, §10.3 (show the exact enforcement layer), §13 (16 Oct: three repeatable demos; 17 Oct: verify the deadline from official instructions; do not assume), §14 (README lists verified controls, unverified behavior, known limitations; demo ≤ 3 min).
- Arch §0.10, §14 (rehearse three times; record limitations), **§15 demo script**, §16 DoD (README and demo distinguish real ArmorIQ decisions from app/tool-server rules and unverified behavior).

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Contributors: all owners (rehearsal roles: Maya approver, Sam, Priya).

## 5. Prerequisites and dependencies

- Hard: P5-02 (claims list), P5-03 (public URLs, reset).
- Decisions: **D-24** (official deadline hour and format) is needed to finish.
- Blocks: — (final task)

## 6. Exact files/modules this task owns

- `docs/DEMO.md` (new; documented)
- `README.md` (steward: this owner): status, verified controls, unverified behavior, limitations, demo link
- Submission checklist in the P5-04 issue (no secrets)

## 7. Existing files it may need to modify

- None beyond the owned files. Claims come from `docs/SECURITY.md` and `docs/EXPERIMENTS.md` (steward `samarth-chawla`). Do not edit them here.

## 8. Shared interfaces and contracts

- None new. All statements must reference the P5-02 / E-records evidence.

## 9. Step-by-step implementation requirements

1. `docs/DEMO.md` follows Arch §15 steps 1–5, with:
   - exact prompts and personas;
   - the expected **observed** outcome and its evidence source;
   - fallback wording where a gate did not pass (for example "pending — resume unverified" if G4 failed; "SDK plan check" rather than "proxy" if E2 shows that).
2. Rehearse three times from a clean reset (Arch §14). Log timing and failures in the issue. Fix only critical bugs (via the owners).
3. README:
   - what ProxyLead is;
   - how to run it;
   - verified controls (each linked to evidence);
   - unverified behavior;
   - known limitations;
   - planned-not-implemented connectors (Gmail, Drive, Meet).
   No "zero hallucinations", "tamper-proof", or "perfect injection prevention" claims.
4. Record the video, 3:00 or less: synthetic data only; no secrets or real emails on screen.
5. Verify the official deadline and format (D-24) from the official event source. Submit with a buffer. Record the confirmation in the issue.

## 10. Acceptance criteria

- `docs/DEMO.md` and the README are merged. Every claim maps to evidence.
- Three rehearsals completed. The video is at most 3:00 (SC-07).
- Submission confirmation recorded before the verified deadline.

## 11. Required unit, integration, and security tests

- A screen-content check of the video (no secrets, emails, or tokens visible) by a second team member.
- README link check.

## 12. Commands for verification

```bash
git grep -nE "tamper-proof|zero hallucination|perfect" -- README.md docs/DEMO.md || echo "no forbidden claims"
```

## 13. Explicit non-goals

- No feature work after the freeze except critical fixes routed to owners.

## 14. Risks and unresolved questions

- D-24 deadline uncertainty. Live demo flakiness (Neon cold start, Slack delays): warm up before recording.

## 15. Handoff deliverables for dependent agents

- None (final). Post the submission record and links on the P5-04 issue and in the "Status snapshot" of `docs/Todos/README.md`.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P5-04] Finalize demo script, README claims, ≤3-minute video and submission`
- **Body outline:** demo steps with fallbacks; rehearsal log; claim-evidence table; deadline verification; submission confirmation.
- **Labels:** `phase-5-release`, `documentation`
- **Depends on:** P5-02, P5-03; D-24  **Blocks:** —

## 17. Definition of done

Video recorded (≤ 3:00); DEMO.md and README merged with evidence-backed claims;
submission confirmed and recorded.
