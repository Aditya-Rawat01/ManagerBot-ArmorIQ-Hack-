# ProxyLead shared contracts

Single source of truth for data shapes exchanged between the backend,
frontend, and MCP server. **Preliminary (v0.1.0)**: only fields established by
`ProxyLead_PRD_v2.md` and `ProxyLead_Architecture_v2.1.md` are modeled; open
questions are listed in `docs/ARCHITECTURE_DECISIONS.md` and in model
docstrings (`UNRESOLVED`).

## Pipeline (one definition, no hand-maintained duplicates)

```text
contracts/python/src/proxylead_contracts/*.py   (Pydantic v2 — source of truth)
        │  python -m proxylead_contracts.schema_export --write
        ▼
contracts/schemas/proxylead-contracts.schema.json   (committed, deterministic)
        │  cd frontend && npm run gen:types
        ▼
frontend/src/types/generated/contracts.ts   (committed, never edited by hand)
```

- The backend and MCP server import the Python package directly.
- The frontend imports only the generated TypeScript types.
- A test (`test_committed_json_schema_is_up_to_date`) and CI fail if the JSON
  Schema or the generated TypeScript drifts from the Python models.

## Contents

| Module | What it defines |
|---|---|
| `enums.py` | Personas, roles, tiers, endpoint and tool names, decision sources, approval/execution/audit states |
| `tools.py` | `TOOL_CATALOG`: endpoint + tier bound to every tool name; forbidden model-supplied argument keys |
| `models.py` | `AuthenticatedUser`, `RequestContext`, `PlannedToolCall`, `PlanProposal`, `ArmorIQDecision`, `ApprovalStatus`, `ToolInvocationResult`, `SourceCitation`, `KnowledgeSearchHit`, `KnowledgeSearchResult`, `AuditEvent`, `HealthResponse`, `ErrorResponse` |
| `schema_export.py` | Deterministic JSON Schema export and `--check` drift detection |

Validators encode rules that are already agreed, for example: at most one
elevated-tier tool per plan, no model-chosen identity/tier/recipient or
`_`-prefixed arguments, HOLD only from ArmorIQ, and "executed" only with a
ledger entry. They are guardrails on data shape, not the security system.

## Commands

From the repository root, inside a virtualenv (the backend or MCP server venv
works because both install this package in editable mode):

```bash
pip install -e "contracts/python[dev]"
python -m proxylead_contracts.schema_export --write   # after changing models
python -m proxylead_contracts.schema_export --check   # verify no drift
cd contracts/python && pytest
ruff check contracts/python && ruff format --check contracts/python
```

The editable install is required for `schema_export` because it writes into
this directory.

## Changing a contract

1. Edit the Pydantic model; mark anything not yet agreed as `UNRESOLVED`.
2. Run `--write`, then `npm run gen:types` in `frontend/`.
3. Commit the model, schema, and generated TypeScript together.
