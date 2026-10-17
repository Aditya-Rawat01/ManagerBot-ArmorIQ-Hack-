# P3-08 — E17: grounding evaluation and `MIN_SCORE` calibration

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `Aditya-Rawat01` |
| Size | M (3–4 h) |
| Target window | 16 Oct AM (before the noon freeze) |
| Gate role | **G6**: required before any grounding or abstention claim; SC-05 |
| Status | Implemented on `w6/b-p308-e17` (not merged): E17 recorded 2026-10-17 (local DB, not ArmorIQ evidence), `MIN_SCORE=0.65` (ADR-033). Weak-evidence and conflict abstention NOT MET (see `docs/EXPERIMENTS.md` § E17) |

## 1. Task ID and title

P3-08 — Build the E17 evaluation set and harness, calibrate `MIN_SCORE` on a held-out split, and record false accepts and false abstains in `docs/EXPERIMENTS.md`.

## 2. Purpose and expected outcome

`MIN_SCORE` must be calibrated, not guessed (PRD §8.2). Outcome:

- `seed/eval/` holds the labeled questions;
- `eval_e17_grounding.py` runs retrieval + responder against the seeded corpus through the tool-server retrieval path and records every required field;
- `MIN_SCORE`, the model version, and the results are recorded;
- the threshold is set in configuration.

## 3. Authoritative source references

- PRD §0.1, §1.1 SC-05, §8.2 (calibrate; store model version, threshold, evaluation result), **§8.3** (case types; recorded fields; tune on a held-out subset; acceptance list; not proof of universal correctness), §11 (E17 row).
- Arch §7.4 (evaluation set; fields; calibration; acceptance; map to SC-02/SC-05; report false accepts and false abstains).
- `ProxyLead_Day1_Experiments.md` E17.

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Contributor: `VTCodeCraft` (responder abstention behavior).

## 5. Prerequisites and dependencies

- Hard: P2-05 (fused scores), P3-03 (responder abstention), P2-03 (corpus with the case types).
- Decisions: D-02 (resolved: `eval_e17_grounding.py`, ADR-017), D-23 (score scale and threshold location, decided here).
- Blocks: P5-02 (grounding claims), P5-04 (README claims).

## 6. Exact files/modules this task owns

- `seed/eval/` (new): question sets with expected answerability, expected source IDs, tier, and case type; train and held-out split
- `scripts/eval_e17_grounding.py` (new; name per ADR-017)

## 7. Existing files it may need to modify

- `docs/EXPERIMENTS.md` (steward `samarth-chawla`): the § E17 record, submitted in a PR that the steward reviews.
- `.env.example` (steward `parthgoyal26-03-2005`): a comment with the calibrated `MIN_SCORE` value and the date.
- `docs/ARCHITECTURE_DECISIONS.md` (steward `VTCodeCraft`): ADR resolving D-23.

## 8. Shared interfaces and contracts

- Fixes the meaning of `KnowledgeSearchHit.score` and `KnowledgeSearchResult.min_score` (ICR resolving D-23 / U-10).

## 9. Step-by-step implementation requirements

1. Write cases covering every PRD §8.3 type:
   - answerable with known source IDs;
   - unrelated / no evidence;
   - weak evidence below the threshold;
   - conflicting decisions;
   - superseded records;
   - topic escalation across team, confidential, and restricted.
   Split them into train and held-out sets.
2. The harness calls the retrieval and tool path as each persona would. It must not bypass tier filters: run it through the tool handlers with signed test `_ctx`, or through the full orchestrator with fakes for ArmorIQ. Record which, and do not present it as live ArmorIQ evidence.
3. For each case record: expected answerability, expected source IDs, retrieval scores and ranks, actual citations, chosen tier, abstained (yes/no).
4. Choose `MIN_SCORE` on the train split; report metrics on the held-out split. List every false accept and false abstain.
5. Check the PRD §8.3 acceptance items:
   - no answer without an allowed result above the threshold;
   - all citations returned in-request;
   - superseded records not presented as current;
   - no unauthorized tier content or metadata.
6. Record the model ID and version, the threshold, the date, and the results in `docs/EXPERIMENTS.md`. Set `MIN_SCORE` in the deployment configuration (P5-03).

## 10. Acceptance criteria

- The E17 record exists with all fields, held-out metrics, and lists of false accepts and abstains.
- The acceptance items are each marked met or not met with evidence. Failures are not hidden.
- `MIN_SCORE` is calibrated and documented. D-23 is resolved by an ADR.
- The wording states that this is evidence of retrieval behavior, not proof of factual correctness.

## 11. Required unit, integration, and security tests

- The harness's metric functions have unit tests in `tests/toolserver/test_e17_metrics.py` (new; owned by this task).
- The evaluation run itself is the experiment.

## 12. Commands for verification

```bash
python scripts/eval_e17_grounding.py --split train
python scripts/eval_e17_grounding.py --split heldout
python -m pytest tests/toolserver/test_e17_metrics.py -q
```

## 13. Explicit non-goals

- No claim of zero hallucination (Arch §0.10). No reranker or model change.

## 14. Risks and unresolved questions

- A small corpus makes calibration noisy. Report it honestly.
- D-23.

## 15. Handoff deliverables for dependent agents

- P3-03 / P5-03: the `MIN_SCORE` value and its semantics.
- P5-02 / P5-04: the E17 summary for SC-05 and the README.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-08] E17: grounding evaluation and MIN_SCORE calibration`
- **Body outline:** case types; harness path; recorded fields; split; acceptance; evidence.
- **Labels:** `phase-3-agent-slack`, `knowledge`, `experiment`, `testing`
- **Depends on:** P2-05, P3-03, P2-03; D-02, D-23  **Blocks:** P5-02 (and P5-04 via P5-02)

## 17. Definition of done

E17 record and ADR merged; threshold configured; handoff posted.
