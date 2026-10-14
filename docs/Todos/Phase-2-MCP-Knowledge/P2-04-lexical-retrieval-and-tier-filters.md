# P2-04 — Lexical retrieval with hard-coded tier filters

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | S–M (2–3 h) |
| Target window | 14–15 Oct (on the E1 critical path) |
| Gate role | Application data authorization (Arch §5.4 layer 3) |
| Status | Not started |

## 1. Task ID and title

P2-04 — Implement `services/knowledge/retrieval.py` full-text search with workspace, tier, status, and limit filters fixed by trusted code. This is the E1 retrieval path.

## 2. Purpose and expected outcome

Tier enforcement in SQL must exist from the first product tool. Outcome: a
retrieval function the tool handlers call with a tier **constant**, returning
bounded, provenance-rich hits from PostgreSQL full-text search. P2-05 adds
vector search and RRF to the same function.

## 3. Authoritative source references

- PRD §6.1 (tier hard-coded in server code and SQL; bounded results with source IDs and provenance), §8.2 (FTS; filter workspace, tier, active status, requester access before returning evidence; result limit), §9 (parameterized queries, limits).
- Arch §0.5, §3 must-ship 3, §6.1 (fixed SQL filters; no arbitrary SQL/sort/columns; limits; filter before content reaches the model), §7.1 (metadata filters), §7.5, §13 (fixed filters; changing arguments cannot upgrade a tier).

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewer: `samarth-chawla`.

## 5. Prerequisites and dependencies

- Hard: P2-01, P2-03 (increment 1).
- Decisions: **D-09** (drafts in results) is needed to finish.
- Blocks: P2-05, P2-06.

## 6. Exact files/modules this task owns

- `services/knowledge/retrieval.py` (new; documented; extended later by P2-05, same owner)
- `services/knowledge/__init__.py` (docstring; also fix the stale ADR-003 reference, M-05)
- `tests/toolserver/test_tier_filters.py` (new; Arch §11 documented name)
- `tests/toolserver/test_retrieval_lexical.py` (new)
- `tests/toolserver/test_knowledge_import_boundary.py` (new): `services.knowledge` is imported only from `services/toolserver` (and its own tests)

## 7. Existing files it may need to modify

- None outside owned files. Uses `services/toolserver/db.py` (P2-01, same owner).

## 8. Shared interfaces and contracts

- Provides (PROPOSED): `search(conn, *, tier: DataTier, workspace_id: str, query: str, limit: int) -> list[RetrievedHit]`. `tier` is passed by the tool handler as a constant, never from arguments.
- Hits map onto `KnowledgeSearchHit` / `SourceCitation` (FIXED); score semantics UNRESOLVED (D-23).

## 9. Step-by-step implementation requirements

1. One fixed, parameterized SQL statement:
   - `websearch_to_tsquery` (or equivalent) over the chunk `tsvector`;
   - `WHERE workspace_id = $1 AND tier = $2` with an **exact** tier match;
   - an active-status filter and the D-09 draft rule;
   - `ORDER BY` a rank expression fixed in code;
   - `LIMIT` bounded by a code constant.
2. Return a source ID, chunk ID, snippet (truncated to a code constant), rank, raw score, and provenance (author, created, version, review status, `superseded_by`). Superseded records are returned **flagged**, never as current (PRD §8.3), or excluded per D-09. Record which.
3. No parameter controls sorting, columns, tables, or tier. No string interpolation of inputs.
4. The import-boundary test fails if `apps/`, `services/audit`, `services/slack_socket`, or `services/authorization` import `services.knowledge`.
5. A database timeout or error raises a typed error. Handlers turn it into a fail-closed tool error, never an empty "no results" success.

## 10. Acceptance criteria

- For each tier, `search` returns only rows of that tier. A query matching higher-tier text returns nothing at a lower tier (fixture data).
- No public parameter can change the tier filter (code review + test that the SQL text is constant).
- Limits on rows and snippet length are enforced.
- The import boundary is enforced by a test.

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_tier_filters.py`:
  - per-tier isolation;
  - no tier argument path;
  - restricted text never in team results.
  Runs against a live Neon branch or local Postgres when the DSN is set, plus pure SQL-text assertions offline.
- `tests/toolserver/test_retrieval_lexical.py`: ranking stable; snippet truncation; superseded flagging; DB error surfaces as an error.
- `tests/toolserver/test_knowledge_import_boundary.py`.

## 12. Commands for verification

```bash
python -m pytest tests/toolserver/test_tier_filters.py tests/toolserver/test_retrieval_lexical.py tests/toolserver/test_knowledge_import_boundary.py -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No vector search or RRF (P2-05). No MCP tool wiring (P2-06). No threshold calibration (P3-08).

## 14. Risks and unresolved questions

- D-09 (drafts).
- Requester-access filtering beyond tier (PRD §8.2 "requester access") is covered by the member tier ceiling in P2-08 handlers. Confirm no per-record ACL is needed.

## 15. Handoff deliverables for dependent agents

- P2-06: the `search` signature and error types.
- P2-05: the function to extend, plus the fixture set.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-04] Implement lexical retrieval with hard-coded tier filters`
- **Body outline:** fixed SQL rules; outputs; boundary test; acceptance criteria.
- **Labels:** `phase-2-mcp-knowledge`, `knowledge`, `security`
- **Depends on:** P2-01, P2-03  **Blocks:** P2-05, P2-06

## 17. Definition of done

Merged with tier-isolation tests green; handoff posted to P2-06.
