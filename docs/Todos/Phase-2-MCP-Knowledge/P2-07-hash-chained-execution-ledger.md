# P2-07 — Hash-chained execution ledger, verifier, and `jti` idempotency

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | M (2–4 h) |
| Target window | 14 Oct (on the E1 critical path) |
| Gate role | Execution evidence for G1 and SC-06 |
| Status | Not started |

## 1. Task ID and title

P2-07 — Implement `services/toolserver/ledger.py`: an append-only, hash-chained record of what the tool server actually did, with a chain verifier and `jti` replay protection.

## 2. Purpose and expected outcome

"A plan or ALLOW is not proof of execution" (PRD §10.1). Outcome:

- every tool-server call that passes the credential check produces a ledger record of its validation and execution outcome;
- records are chained so the verifier detects modification and reordering (required by PRD §3) and interior deletion. Tail truncation is a documented limitation without an external anchor;
- a `jti` executes at most once, and the replay guard used by P1-04 is backed by the ledger.

## 3. Authoritative source references

- PRD §2 P0 (append-only ledger correlated with plan, step, app invocation ID, and actual execution), §3 (hash-chained; verifier test detects modification and reordering; do not claim "tamper-proof"), §9 (idempotency/replay), §10.1.
- Arch §3.1 (ledger row), §5.1 (application `jti`; dedupe until token/plan expiry), §7.2 (fields; never infer execution), §10 (integrity guarantees and limitations), §13 (duplicate `jti` rejected or safely deduplicated; audit matches actual execution).

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewer: `samarth-chawla` (integrity, replay).

## 5. Prerequisites and dependencies

- Hard: P2-01 (ledger table).
- Blocks: P2-06; P1-04 (soft: replay guard); P1-10 (soft: duplicate-resume dedupe).

## 6. Exact files/modules this task owns

- `services/toolserver/ledger.py` (existing placeholder; documented)
- `tests/toolserver/test_ledger_chain.py` (new)
- `tests/toolserver/test_idempotency.py` (new; Arch §11 documented name)
- `migrations/<next>_ledger_constraints.sql` (new, only if constraints beyond P2-01 are needed)

## 7. Existing files it may need to modify

- None outside owned files.

## 8. Shared interfaces and contracts

- Provides (SHARED_INTERFACES §9, PROPOSED): `append(entry) -> LedgerEntryId`, `seen_jti(jti) -> bool` (implements P1-04's `ReplayGuard`), `verify_chain() -> VerificationReport`.
- `ToolInvocationResult.ledger_entry_id` (FIXED) references these IDs.

## 9. Step-by-step implementation requirements

1. Entry fields:
   - `request_id`, `trace_id`, `plan_id`, `armoriq_plan_id` (if present), `step_index`, `tool_name`, `jti`, `requester_member_id`, `workspace_id` — all from the **verified** `_ctx`;
   - `validation_outcome`, `execution_status` (`ToolExecutionStatus`), `result_id`, `error_class`;
   - `sha256` of the canonical business arguments (never raw arguments);
   - timestamps, `prev_hash`, `entry_hash`.
2. `entry_hash = sha256(prev_hash ‖ canonical_json(entry_without_hash))`. The genesis `prev_hash` is a fixed constant.
3. Serialize appends (for example a transaction-scoped advisory lock) so concurrent calls cannot fork the chain.
4. **Idempotency.**
   - A unique constraint covers executed `jti`.
   - A second call with the same `jti` does not execute. It returns a deduplicated rejection (`replay`, `tool_server_rule`) referencing the original entry.
   - Decide and document how rejected attempts are recorded (for example a separate attempt sequence) without violating uniqueness.
5. `verify_chain()` recomputes every hash in order and returns the first broken position. It detects edited fields, swapped order, and deleted interior rows. It **cannot** detect removal of the newest rows (tail truncation) without an external anchor; document this.
6. Document the guarantees and limitations in the module docstring: it detects changes by anyone who cannot recompute the chain, but not by a database superuser who rewrites the whole chain. Never write "tamper-proof".
7. No protected content and no secrets in ledger rows.

## 10. Acceptance criteria

- The verifier detects each of: field modification, row reordering, interior row deletion (tests). The tail-truncation limitation is documented.
- Concurrent appends (for example 20 parallel) produce one valid chain.
- A duplicate `jti` never executes twice (test), and the duplicate is visible as a deduplicated outcome.
- `seen_jti` is usable by P1-04 (interface test with the P1-04 protocol).

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_ledger_chain.py`:
  - `test_chain_valid`
  - `test_detects_modification`
  - `test_detects_interior_deletion`
  - `test_detects_reordering`
  - `test_concurrent_appends_single_chain` (live DB; skipped without a DSN)
- `tests/toolserver/test_idempotency.py`:
  - `test_duplicate_jti_not_executed`
  - `test_duplicate_returns_reference_to_original`
  - `test_no_raw_arguments_stored`

## 12. Commands for verification

```bash
python -m pytest tests/toolserver/test_ledger_chain.py tests/toolserver/test_idempotency.py -q
```

## 13. Explicit non-goals

- No backend audit events (P3-01). No UI. No external anchoring of hashes.

## 14. Risks and unresolved questions

- Recording rejected attempts without breaking `jti` uniqueness (step 4).
- Backend read access for SC-06 correlation (SHARED_INTERFACES §7, PROPOSED).

## 15. Handoff deliverables for dependent agents

- P2-06: the `append` usage and the dedupe response.
- P1-04: the `ReplayGuard` binding.
- P3-01 / P4-04: how to correlate an audit step with a ledger entry (`jti`, `ledger_entry_id`).
- P1-06: the query to find an entry by `jti` for evidence.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-07] Implement hash-chained execution ledger with verifier and jti idempotency`
- **Body outline:** entry fields; chain construction; idempotency; verifier tests; limitation statement.
- **Labels:** `phase-2-mcp-knowledge`, `security`, `database`, `mcp`
- **Depends on:** P2-01  **Blocks:** P2-06 (and soft: P1-04, P1-10)

## 17. Definition of done

Merged with chain and idempotency tests green; limitation statement in the
docstring; handoff posted to P2-06 and P1-04.
