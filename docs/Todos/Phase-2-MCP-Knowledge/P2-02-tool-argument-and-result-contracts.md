# P2-02 — Tool argument and result contracts

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | S (≤ 2 h) |
| Target window | 14 Oct (first task of the day: unblocks P3-02) |
| Gate role | — |
| Status | Not started |

## 1. Task ID and title

P2-02 — Define strict Pydantic argument and result models for all 14 tools in `services/contracts/tool_args.py`.

## 2. Purpose and expected outcome

The planner (backend) and the tool server must validate the same argument
shapes, and the responder must parse the same result shapes. Outcome: one
shared module with an argument model and a result model per tool. Every model
has `extra="forbid"` and length/enum bounds, and none has a tier, identity,
scope, or recipient field.

## 3. Authoritative source references

- PRD §6.1 (tier in tool name only; `content_kind` ∈ {`note`, `summary`}; bounded, citation-ready search results with source IDs and provenance), §6.2 (`create_followup_task` enum-bounded fields + idempotency key; honeypot), §7.3 (strict schemas, enum bounds, no underscore keys, no model-selected identity/tier/recipient), §9 (strict JSON schemas).
- Arch §0.5, §6.1 (no tier argument; no arbitrary SQL, table names, sort expressions, or columns; result/row/character limits), §6.2 (task fields: short title, optional description, enum owner/priority, idempotency key), §6.3.

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewers: `VTCodeCraft` (contract steward; planner consumer), `samarth-chawla` (security).

## 5. Prerequisites and dependencies

- Hard: none.
- Decisions:
  - **D-11** (`knowledge_get_source_*` arguments) and **D-29** (honeypot arguments) do **not** block this task. Ship PROPOSED models marked provisional and revise them by ICR when decided. This task is on the E1 critical path.
  - D-17: keep the `create_followup_task` model even if the tool is dropped later.
  - D-13 does not block (plan-level limits).
- Blocks: P2-06, P2-08, P2-09, P2-10, P3-02, P3-03.

## 6. Exact files/modules this task owns

- `services/contracts/tool_args.py` (new)
- `tests/contracts/test_tool_args.py` (new)

## 7. Existing files it may need to modify

Steward `VTCodeCraft`; review required:

- `services/contracts/__init__.py`: export the new models.
- `services/contracts/tools.py`: extend `FORBIDDEN_ARGUMENT_KEYS` only by ICR.
- `services/contracts/schema_export.py`: register result models only if the web app needs them (P4-03 citations already use `SourceCitation`; probably none).

## 8. Shared interfaces and contracts

- Provides: per-tool models (SHARED_INTERFACES §4) and a mapping `TOOL_ARGUMENT_MODELS: dict[ToolName, type[BaseModel]]` / `TOOL_RESULT_MODELS`.
- Must stay consistent with `TOOL_CATALOG` and `KnowledgeSearchResult` (FIXED).

## 9. Step-by-step implementation requirements

1. Search tools (3): `query` (string, 1–500 chars), `limit` (int, 1–N; N PROPOSED, recorded). No filters, sort, or column fields. The result is `KnowledgeSearchResult` (existing).
2. `knowledge_get_source_{tier}` (3): an opaque source reference whose design follows D-11. The result is a bounded source record (ID, title, author, created_at, version, review status, supersession, bounded excerpt). It carries **no tier field supplied by the caller**; the result's tier comes from the server.
3. `read_source_{tier}` (3): `view` as a `Literal[...]` of approved view names for that tier (placeholders until P2-08 fixes them by ICR), plus bounded, typed filter parameters. The result has rows, a row count, and a truncated flag.
4. `knowledge_save_team_note`: `content` (bounded), `content_kind: Literal["note", "summary"]`, optional `title`. Author, workspace, and provenance are **not** arguments (trusted code injects them from `_ctx`). The result has record ID, version, and `review_status="draft"`.
5. `knowledge_confirm_maya_decision`: `record_id` and `version` (int ≥ 1; binds to the exact version, PRD §8.4). The result has record ID, version, and `review_status="authoritative"`.
6. `knowledge_mark_superseded`: `record_id`, `version`, `superseded_by_record_id`, and a bounded `reason`.
7. `create_followup_task`: `title` (short), optional `description`, `owner` and `priority` as enums (values PROPOSED here, recorded), and `idempotency_key`.
8. `send_external_message`: a provisional model (clearly marked) until **D-29** is decided; revise by ICR.
9. All models: `extra="forbid"`, frozen, and no field named in `FORBIDDEN_ARGUMENT_KEYS` or starting with `_`.
10. Provide the two mapping dicts and `validate_arguments(tool, raw) -> BaseModel`. That function raises a `ValidationError` naming the field, never echoing values.

## 10. Acceptance criteria

- Every `ToolName` has exactly one argument model and one result model (test).
- No model field is forbidden or `_`-prefixed (test iterates all models).
- Unknown keys, a `tier` key, or overlong strings are rejected for every tool (parametrized test).
- Existing contract tests and schema drift checks stay green.

## 11. Required unit, integration, and security tests

- `tests/contracts/test_tool_args.py`:
  - `test_every_tool_has_models`
  - `test_no_forbidden_or_underscore_fields`
  - `test_unknown_keys_rejected[tool]`
  - `test_tier_argument_rejected[tool]`
  - `test_bounds_enforced[tool]`
  - `test_content_kind_enum`
  - `test_confirm_requires_version`

## 12. Commands for verification

```bash
python -m pytest tests/contracts -q
python -m services.contracts.schema_export --check
bash scripts/check.sh
```

## 13. Explicit non-goals

- No handlers, SQL, or plan-level limits (D-13, P3-02).

## 14. Risks and unresolved questions

- D-11 and D-29 may force argument changes after E-evidence. Use an ICR.
- View names are unknown until P2-08. Use placeholders and replace them via ICR.

## 15. Handoff deliverables for dependent agents

- P2-06/08/09/10: `validate_arguments` and the result models.
- P3-02: argument models for plan validation, with the same function.
- P3-03: result models to parse evidence.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-02] Define strict argument and result contracts for all 14 tools`
- **Body outline:** per-tool field table; forbidden-field rule; tests; open items D-11 / D-29.
- **Labels:** `phase-2-mcp-knowledge`, `mcp`, `security`, `backend`
- **Depends on:** — (D-11, D-29 revised later by ICR)  **Blocks:** P2-06, P3-02, P3-03 (and P2-08/P2-09/P2-10 via P2-06)

## 17. Definition of done

Module and tests merged; contract steward approved; handoff posted to P2-06
and P3-02.
