# Phase 2 — MCP and Knowledge

This phase covers the database and migrations, the synthetic knowledge corpus,
tier-enforced retrieval, the 14 product tools on `memory-mcp` / `workspace-mcp`,
and the hash-chained execution ledger. It is the only place where protected
content is read (R-20).

Sources: PRD §3, §6, §8, §9, §10.1; Arch §6, §7, §10, §13.

## Tasks

| ID | Title | Owner | Size | Hard dependencies |
|---|---|---|---|---|
| [P2-01](P2-01-database-schema-migrations-and-roles.md) | Neon schema, migrations, roles, grant verification | `Aditya-Rawat01` | M | D-08 |
| [P2-02](P2-02-tool-argument-and-result-contracts.md) | Tool argument and result contracts | `Aditya-Rawat01` | S | — |
| [P2-03](P2-03-seed-corpus-ingestion-and-embeddings.md) | Seed corpus, ingestion, local embeddings | `Aditya-Rawat01` | M | P2-01 |
| [P2-04](P2-04-lexical-retrieval-and-tier-filters.md) | Lexical retrieval + tier filters | `Aditya-Rawat01` | S–M | P2-01, P2-03 |
| [P2-05](P2-05-vector-retrieval-and-rank-fusion.md) | Vector retrieval + RRF | `Aditya-Rawat01` | M | P2-04, P2-03 |
| [P2-06](P2-06-memory-mcp-wiring-and-team-search.md) | Guarded dispatch + `knowledge_search_team` | `Aditya-Rawat01` | M | P1-04, P2-02, P2-04, P2-07 |
| [P2-07](P2-07-hash-chained-execution-ledger.md) | Hash-chained ledger + `jti` idempotency | `Aditya-Rawat01` | M | P2-01 |
| [P2-08](P2-08-elevated-tier-and-source-read-tools.md) | Elevated-tier search + source-read tools | `Aditya-Rawat01` | M | P2-06 |
| [P2-09](P2-09-knowledge-maintenance-tools.md) | Drafts, Maya confirmation, supersession | `Aditya-Rawat01` | M | P2-06 |
| [P2-10](P2-10-workspace-mcp-honeypot-and-followup-task.md) | Honeypot + optional follow-up task | `samarth-chawla` | S | P2-01, P2-06 |

## Entry criteria

- D-08 decided (migration tooling and driver). D-01 decided before P2-06.
- Neon project available to the DB steward.
- P1-04 merged before any product tool is deployed (G0).

## Deliverables

- `migrations/`, `seed/`, `services/knowledge/{ingest,embeddings,retrieval}.py`.
- `services/toolserver/{db,schemas,ledger}.py`, `{MEMORY_TOOLS_MODULE}`, `{WORKSPACE_TOOLS_MODULE}`.
- `services/contracts/tool_args.py`.
- `scripts/verify_db_grants.py` and `scripts/migrate.py` (supporting).
- All 14 product tools guarded, deployed, and listed by `tools/list`.

## Exit criteria

- Backend role denied on every protected table and view (live `verify_db_grants.py`; part of G0).
- Every product tool goes through `guarded(...)`. Tier isolation, member ceiling, source-leakage, maintenance-authority, honeypot no-network, and ledger-chain tests are green in CI.
- The deployed tool server serves all shipped tools (input to E5/E3).
- Hybrid retrieval returns scored results ready for E17 calibration.

## Dependencies on other phases

- **In:** P1-04 (`verify_tool_call`, G0) → P2-06. P1-05 (deployment) to expose the tools.
- **Out:** P2-06 → P1-06 (E1). P2-08/P2-10 → P1-08 (E5) → P1-09 (E3); P2-09 is a soft input to P1-08. P2-02 → P3-02/P3-03. P2-01 → P3-01. P2-05 → P3-08 (E17). P2-09 → P3-05, P4-04.

## Parallelization inside the phase

- Start immediately: **P2-02** (unblocks P3-02), **P2-01**.
- After P2-01: P2-03 and P2-07 in parallel; then P2-04.
- P2-06 once P1-04, P2-02, P2-04, and P2-07 are merged. This is the critical path to E1.
- After P2-06: P2-08 and P2-10 (different owners, different files) in parallel. P2-09 after P2-08, because both edit `{MEMORY_TOOLS_MODULE}`. P2-05 any time after P2-04.

## Coordination points

- `{MEMORY_TOOLS_MODULE}` is edited by P2-06 → P2-08 → P2-09 in sequence (same owner).
- `{WORKSPACE_TOOLS_MODULE}` is created and owned by P2-10. Its single registration line in `servers.py` is reviewed by the steward (`Aditya-Rawat01`).
- The view names in `tool_args.py` are placeholders until P2-08 (ICR).
