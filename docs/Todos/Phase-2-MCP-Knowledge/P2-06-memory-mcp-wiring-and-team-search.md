# P2-06 — `memory-mcp` product-tool wiring and `knowledge_search_team`

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` (tool-server steward) |
| Size | M (3–5 h) |
| Target window | 15 Oct AM (on the E1 critical path) |
| Gate role | First product tool; requires **G0** |
| Status | Not started |

## 1. Task ID and title

P2-06 — Build the guarded tool-dispatch path in the tool server and ship the first product tool, `knowledge_search_team`, end to end: credential → `_ctx` → strict schema → tier-fixed retrieval → ledger → MCP text result.

## 2. Purpose and expected outcome

Every later tool reuses this path, and E1 needs it. Outcome:

- a dispatch wrapper through which **every** product tool runs. There is no unguarded product tool.
- `knowledge_search_team` registered on `memory-mcp` and returning a `KnowledgeSearchResult` as MCP text content;
- a ledger row for every execution;
- a documented registration pattern that P2-10 reuses for `workspace-mcp`.

## 3. Authoritative source references

- PRD §0.3, §0.5, §6.1, §9 (JSON-RPC 2.0 + SSE; `initialize`, `tools/list`, `tools/call`; strip `_` metadata before business validation; strict schemas; fixed SQL; limits), §11 E1 (needs `knowledge_search_team` + ledger row), §3.1 (`mcp_protocol.py`, `security.py`, `schemas.py`, `tools_memory.py`, `db.py`, `ledger.py`).
- Arch §5.1 (tool results use the required text content shape; exact names), §6.1, §6.3 (direct calls rejected; actual results and errors returned; UI distinguishes tool-server rejection), §11, §14 (13 Oct second half: minimal MCP protocol + one tier-named tool).

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewer: `samarth-chawla` (security hook placement).

## 5. Prerequisites and dependencies

- Hard: **P1-04** (`verify_tool_call`), P2-02 (models), P2-04 (retrieval), P2-07 (ledger).
- Decisions: **D-01** (module names) blocks start.
- Blocks: P1-06, P2-08, P2-09, P2-10; the P1-05 redeploy.

## 6. Exact files/modules this task owns

- `services/toolserver/{MEMORY_TOOLS_MODULE}` (new): PRD `tools_memory.py`, Arch `tools_access.py` (D-01)
- `services/toolserver/schemas.py` (new; documented): tool → argument/result model registry wiring P2-02 into dispatch
- `services/toolserver/servers.py` (existing; steward): guarded dispatch and registration
- `services/toolserver/main.py` (existing; steward): wiring as needed
- `tests/toolserver/test_protocol.py` (existing; this is the Arch §11 `test_mcp_protocol.py` equivalent, M-02): extended
- `tests/toolserver/test_tool_dispatch.py` (new)
- `tests/toolserver/test_search_team_tool.py` (new)

## 7. Existing files it may need to modify

- `services/toolserver/config.py` (steward: same owner): only ICR-approved settings.
- `tests/toolserver/test_config_and_guardrails.py` (steward: same owner): keep the diagnostic-name guard. Add a guard that every registered product tool goes through dispatch.

## 8. Shared interfaces and contracts

- Uses: `verify_tool_call` (P1-04), `validate_arguments` (P2-02), `retrieval.search` (P2-04), `ledger.append` (P2-07).
- Provides: the MCP result framing and rejection mapping (SHARED_INTERFACES §4) used by the gateway (P1-02).
- Ledger row fields (§9) come from the **verified** `_ctx`, never from arguments.

## 9. Step-by-step implementation requirements

1. **Dispatch wrapper.** `guarded(endpoint, tool, handler)` performs, in order:
   1. `verify_tool_call` (P1-04), using the hook placement from the D-05 inspection;
   2. `validate_arguments(tool, business_arguments)`;
   3. the member tier ceiling (P2-08 adds the elevated logic; team tier passes for all members);
   4. the handler;
   5. `ledger.append(...)` with the execution outcome;
   6. MCP text content containing the JSON of the result model.

   Any exception → MCP error result with a SHARED_INTERFACES §4 code. No protected details in error text.
2. **Registration.** Every product tool is registered only through `guarded(...)`. A test fails if any product tool is registered otherwise. Diagnostic tools stay config-gated (M-04).
3. **`knowledge_search_team` handler.** Calls `retrieval.search(tier=DataTier.TEAM, workspace_id=ctx.workspace_id, query=args.query, limit=args.limit)` with **`DataTier.TEAM` as a literal constant** and maps the hits to `KnowledgeSearchResult`.
4. Do **not** create `{WORKSPACE_TOOLS_MODULE}`. P2-10 creates and owns it, and adds its one registration line to `servers.py` in a PR you review as steward.
5. Verify end to end locally with an MCP client (initialize → `tools/list` → `tools/call`) using a `_ctx` signed with a test secret.
6. Keep `servers.py` as the transport module (ADR-006). If E1 forces the hand-written transport, it goes into `mcp_protocol.py` under a separate ICR (M-01).

## 10. Acceptance criteria

- `tools/list` on `memory-mcp` shows `knowledge_search_team` (plus diagnostics only if enabled).
- Calls without a bearer, without `_ctx`, or with invalid `_ctx` are rejected before the handler runs (spy assertion).
- A `tier` argument or unknown keys are rejected (`invalid_arguments`).
- A valid call returns team-tier hits only, and writes exactly one ledger row whose `jti`, `request_id`, and `step_index` equal the verified `_ctx` values.
- A deployed build passes the P1-05 smoke tests and is ready for E1.

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_tool_dispatch.py`:
  - `test_all_product_tools_registered_via_guard`
  - `test_rejects_without_ctx_before_handler`
  - `test_rejects_forbidden_and_unknown_args`
  - `test_ledger_row_matches_verified_ctx`
  - `test_errors_do_not_leak_details`
- `tests/toolserver/test_search_team_tool.py`: team-only results; `KnowledgeSearchResult` validation; DB error → tool error (not empty success).
- `tests/toolserver/test_protocol.py`: initialize / list / call over SSE with the product tool.

## 12. Commands for verification

```bash
python -m pip install -e ".[toolserver,dev]"
python -m pytest tests/toolserver -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No elevated or source tools (P2-08). No maintenance tools (P2-09). No workspace tools (P2-10).
- No ArmorIQ registration (P1-06).

## 14. Risks and unresolved questions

- D-05: if the MCP SDK drops `_ctx` before the handler, the hook must read raw arguments (ICR with P1-04).
- D-01 file names.
- The ledger row for **rejected** calls: P2-07 decides how a rejection is recorded without breaking `jti` uniqueness.

## 15. Handoff deliverables for dependent agents

- P1-05 / P1-06: deployable build; the E1 tool is ready; how to find the ledger row by `jti`.
- P2-08 / P2-09: `guarded(...)` usage and test helpers (`signed_ctx_for(member, tool)` fixture).
- P2-10: the registration pattern (`register(server)` + one import/registration line in `servers.py`).

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-06] Wire guarded MCP tool dispatch and knowledge_search_team`
- **Body outline:** dispatch order; registration rule; handler spec; tests; E1 readiness checklist.
- **Labels:** `phase-2-mcp-knowledge`, `mcp`, `security`, `knowledge`
- **Depends on:** P1-04, P2-02, P2-04, P2-07; D-01  **Blocks:** P1-06, P2-08, P2-09, P2-10

## 17. Definition of done

Merged with tests green in the toolserver CI job; build deployed via P1-05;
the E1 owner confirms readiness in the P1-06 issue.
