# P2-09 — Knowledge maintenance tools (drafts, Maya confirmation, supersession)

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | M (2–4 h) |
| Target window | 15 Oct |
| Gate role | Required for G2 (registry complete) |
| Status | Implemented (P2-09, PR #79); D-10 and D-09 resolved |

## 1. Task ID and title

P2-09 — Implement `knowledge_save_team_note`, `knowledge_confirm_maya_decision`, and `knowledge_mark_superseded` with server-side authorship, draft status, version binding, and a Maya-only role check.

## 2. Purpose and expected outcome

The memory-authority rules prevent poisoning (PRD §8.4). Outcome:

- notes and summaries are always saved as **drafts**, attributed to the verified requester;
- only a verified manager `_ctx` (and the ArmorIQ policy) can confirm a record, and only an exact `record_id` + `version`;
- supersession preserves history.

## 3. Authoritative source references

- PRD §0.8 (Maya's confirmation is her own authorized action, not hold approval), §4.2 (`store this` → `knowledge_save_team_note`, attributed, draft), §5.3 (Maya-only maintenance tools), **§6.1**, **§8.4** (drafts; only Maya confirms; bound to exact record/version; memory never changes policy).
- Arch §6.1 maintenance tools, §7.3 (authority rules; preserve source, author, timestamp, classification, review status, version, supersession), §13 (notes or summaries cannot become authoritative without Maya's verified confirmation), §15 demo step 5.

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewer: `samarth-chawla`.

## 5. Prerequisites and dependencies

- Hard: P2-06.
- Decisions: **D-10** (tier of saved notes) and D-09 (search visibility of drafts) are needed to finish.
- Blocks: P5-01. Soft inputs to P1-08 (re-run setup check), P3-05 (`store this`, fakes until merged), P4-04 (confirm action).

## 6. Exact files/modules this task owns

- `services/toolserver/tools_memory.py` (extend; sequential with P2-08)
- `tests/toolserver/test_maintenance_tools.py` (new)

## 7. Existing files it may need to modify

- `migrations/<next>_*.sql` (DB steward, same owner): only if version or supersession constraints are missing.

## 8. Shared interfaces and contracts

- Argument and result models from P2-02 (`content_kind` ∈ {`note`, `summary`}; `record_id` + `version`).
- Author, workspace, and request provenance come from the verified `_ctx` (SHARED_INTERFACES §5). Never from arguments.

## 9. Step-by-step implementation requirements

1. **`knowledge_save_team_note`.**
   - Insert a `team_notes` row with `review_status=draft`, `author_member_id` and `workspace_id` from `_ctx`, `content_kind`, a content hash, `version=1`, provenance (`request_id`, timestamp), and the tier per D-10.
   - Ingest it for retrieval per D-09 (chunk + embedding via the P2-03 pipeline functions, or deferred; record which).
   - Idempotent per `jti`.
2. **`knowledge_confirm_maya_decision`.**
   - Require `ctx.app_role == manager`. Otherwise reject with `out_of_scope`, independent of the ArmorIQ policy.
   - Load the record. If the current version ≠ `args.version`, reject with `precondition_failed`.
   - Mark exactly that version authoritative (or create the `decisions` entry, per the recorded design). Store `approved_by` = Maya's member ID and a timestamp.
   - Never accept "approved" text as input.
3. **`knowledge_mark_superseded`.**
   - Manager role only, version-bound.
   - Set `superseded_by` with provenance. Never delete or overwrite history.
4. No tool in this module reads or writes policy, permissions, or identity (R-57).
5. Register all three through `guarded(...)`.

## 10. Acceptance criteria

- A saved note is always `draft`, and its author equals the `_ctx` requester even if the arguments try to set an author (rejected by schema).
- Sam or Priya calling confirm or supersede (with a valid `_ctx`) gets a `tool_server_rule` rejection.
- Confirming a stale version fails. Confirming the current version succeeds once; a repeat with the same `jti` is deduplicated.
- Superseding keeps both records; the old one shows `superseded_by`.

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_maintenance_tools.py`:
  - `test_note_saved_as_draft_with_ctx_author`
  - `test_summary_kind_is_draft`
  - `test_non_manager_cannot_confirm`
  - `test_confirm_binds_exact_version`
  - `test_supersede_preserves_history`
  - `test_no_policy_or_identity_side_effects`

## 12. Commands for verification

```bash
python -m pytest tests/toolserver/test_maintenance_tools.py -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No manager review-queue listing (D-12). No UI. No automatic summarization.

## 14. Risks and unresolved questions

- D-10 (tier), D-09 (search visibility), D-12 (how Maya sees drafts to confirm them).

## 15. Handoff deliverables for dependent agents

- P3-05: the `store this` tool call contract (arguments, result, draft semantics).
- P4-04: confirm and supersede argument requirements (`record_id` + `version`).
- P1-08: the tool list is complete for registration.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-09] Implement draft notes, Maya decision confirmation and supersession tools`
- **Body outline:** three tools; authority rules; version binding; tests.
- **Labels:** `phase-2-mcp-knowledge`, `mcp`, `security`, `knowledge`
- **Depends on:** P2-06; D-10, D-09  **Blocks:** P5-01 (soft: P1-08, P3-05, P4-04)

## 17. Definition of done

Merged, deployed, tests green; handoff posted to P3-05, P4-04, and P1-08.
