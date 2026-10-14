# P2-08 — Elevated-tier search and source-read tools

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | M (3–5 h) |
| Target window | 15 Oct |
| Gate role | Required for G2/G3 (E5, E3) |
| Status | Not started |

## 1. Task ID and title

P2-08 — Implement the remaining eight read tools with tier constants, a server-side member tier ceiling, source re-checks, approved per-tier views, and no restricted-existence leaks:

- `knowledge_search_confidential`, `knowledge_search_restricted`;
- `knowledge_get_source_{team,confidential,restricted}`;
- `read_source_{team,confidential,restricted}`.

## 2. Purpose and expected outcome

E3 and the demo need every tier. The tool server must enforce tiers even if
the ArmorIQ policy is too permissive. Outcome: eight guarded tools on
`memory-mcp`, each with its own fixed SQL filter. The handlers reject members
above their tier ceiling, re-check source tier and status, read only named
approved views, and return identical responses for "not found" and "not
allowed".

## 3. Authoritative source references

- PRD §5.1, §5.2 (one elevated call per plan; bounded, citation-ready evidence), §6.1 (re-check tier/status for every source lookup; no restricted existence or metadata leak), §8.1, §9.
- Arch §5.5 (reject out-of-tier calls even if the policy is too permissive), **§6.1** (all rules), §7.5 (approved read-only views; schema allowlist; statement timeout; row limit), §13 (no tier access outside policy, even by direct call; restricted existence not leaked).

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewer: `samarth-chawla`.

## 5. Prerequisites and dependencies

- Hard: P2-06.
- Decisions: **D-11** (source reference and view set) is needed to finish. Confirm the PROPOSED member tier ceiling (SHARED_INTERFACES §6) with `samarth-chawla`.
- Blocks: P1-08, P1-09, P5-01.

## 6. Exact files/modules this task owns

- `services/toolserver/tools_memory.py` (extend; same owner as P2-06/P2-09; do not develop in parallel branches with P2-09)
- `migrations/<next>_approved_views.sql` (new): synthetic company-data tables and approved per-tier views
- `tests/toolserver/test_elevated_tier_tools.py` (new)
- `tests/toolserver/test_source_leakage.py` (new)

## 7. Existing files it may need to modify

- `services/contracts/tool_args.py` (P2-02, same owner): replace view-name placeholders via ICR.
- `seed/` (same owner): synthetic rows for the views.

## 8. Shared interfaces and contracts

- Member tier ceiling (SHARED_INTERFACES §6, PROPOSED): Maya → restricted, Sam → confidential, Priya → team, from the verified `_ctx` role. Denial → `out_of_scope`, `tool_server_rule`.
- Result models from P2-02. Ledger rows per P2-07.

## 9. Step-by-step implementation requirements

1. **Search tools.** Same handler as P2-06 with `DataTier.CONFIDENTIAL` / `DataTier.RESTRICTED` as literal constants.
2. **Tier ceiling** applied in the dispatch path before the handler, keyed on the verified `_ctx` role. This is independent of ArmorIQ (R-26).
3. **`knowledge_get_source_{tier}`.**
   - Resolve the reference per D-11.
   - Re-check, in one parameterized query: workspace, **exact tier = handler constant**, active status, and current version (Arch §6.1).
   - Missing, wrong-tier, or inactive sources return the **same** `not_found` response, with no title, count, or timing difference beyond normal variance (R-55).
4. **`read_source_{tier}`.**
   - `view` must be one of that tier's approved view names (allowlist constant).
   - Fixed SQL per view, bounded parameterized filters, `LIMIT` constant, character truncation, statement timeout.
   - No dynamic columns, tables, or sort expressions.
5. **Views migration.** Synthetic company-data tables (for example project status) and per-tier views. Only `proxylead_toolserver` gets `SELECT` on them; the backend is denied (extend the P2-01 grant check list).
6. Register all eight tools through `guarded(...)`.

## 10. Acceptance criteria

- Each tool returns only its tier (fixtures with cross-tier text).
- With a valid `_ctx`:
  - Priya calling any confidential or restricted tool is rejected by `tool_server_rule`;
  - Sam calling any restricted tool is rejected;
  - Sam calling a confidential tool is **not** rejected by the tool server (ArmorIQ HOLD gates it).
- Wrong-tier, missing, and inactive source lookups produce byte-identical responses.
- `read_source_*` rejects unknown view names and enforces row and character limits.
- The backend role is denied on the new views (`verify_db_grants.py` updated and passing).

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_elevated_tier_tools.py`:
  - per-tool tier isolation;
  - ceiling matrix (3 members × 3 tiers × 3 tool families);
  - limits.
- `tests/toolserver/test_source_leakage.py`:
  - identical not-found responses;
  - no restricted metadata in errors;
  - unknown view rejected.
- `scripts/verify_db_grants.py` includes the new views.

## 12. Commands for verification

```bash
python -m pytest tests/toolserver -q
python scripts/verify_db_grants.py
```

## 13. Explicit non-goals

- No arbitrary SQL. No PII. No ArmorIQ policy changes (P1-08).

## 14. Risks and unresolved questions

- D-11: the follow-up request flow for source reads after an elevated search.
- Timing side channels for existence checks. Keep the same query path for all cases; document the residual risk.

## 15. Handoff deliverables for dependent agents

- P1-08 / P1-09: the full read-tool list deployed; expected tool-server rejections for the E3 step-5 check.
- P3-02: view names and source-reference rules for planning.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-08] Implement elevated-tier search, get_source and read_source tools`
- **Body outline:** eight tools; tier ceiling; re-check and leakage rules; views migration; tests.
- **Labels:** `phase-2-mcp-knowledge`, `mcp`, `security`, `knowledge`
- **Depends on:** P2-06; D-11  **Blocks:** P1-08, P5-01 (and P1-09 via P1-08)

## 17. Definition of done

Merged, deployed, tests green, grant check updated; handoff posted to P1-08.
