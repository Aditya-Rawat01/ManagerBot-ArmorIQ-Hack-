# Migrations (P2-01, DB steward: `Aditya-Rawat01`)

Plain numbered SQL, applied in order by `scripts/migrate.py` with
`DATABASE_URL_MIGRATE` (ADR-028). The migration role is never available to
running services (PRD v2 §8.1).

## Files

| File | Content |
|---|---|
| `0001_extensions.sql` | `vector` extension (pgvector; 384-dim embeddings, PRD v2 §8.2) |
| `0002_core_tables.sql` | `workspaces`, `members`, `slack_events`, `topic_access_rules` |
| `0003_knowledge_tables.sql` | `source_records`, `knowledge_chunks` (HNSW + FTS trigger/GIN), `decisions`, `manager_guidance`, `team_notes` |
| `0004_runtime_tables.sql` | `plans`, `plan_steps`, `approval_requests`, events, `tool_execution_ledger`, `delivery_events`, `unanswered_questions`, `tasks`, `egress_sink` |
| `0005_roles_grants.sql` | `proxylead_backend` / `proxylead_toolserver` group roles, full grant matrix, revokes, statement timeouts |
| `0006_metadata_view.sql` | `draft_review_queue` metadata-only view for the manager console (ADR-023) |

## Rules (binding)

1. **Numbered and ordered.** Files match `NNNN_name.sql`, run once in numeric
   order. Duplicate numbers fail the migration run and the lint test.
2. **Append-only.** Never edit a merged migration. Every change — including a
   grant fix — is a new numbered file.
3. **Grants travel with objects.** `REVOKE`-first baseline lives in `0005`;
   every later migration that creates a table or view MUST grant (or
   explicitly deny) both runtime roles in the same file. Grants are never
   inherited: `ALTER DEFAULT PRIVILEGES` is deliberately not used, so a
   forgotten grant fails closed (permission denied) instead of open.
4. **Enum literals equal `services/contracts/enums.py` values.** The offline
   lint test (`tests/toolserver/test_migrations.py`) asserts every
   `DataTier`, `ReviewStatus`, `DecisionSource`, `ApprovalState`,
   `ToolExecutionStatus`, `DeliveryChannel`, and `EntryPoint` value appears
   in the SQL. Change an enum only with a new migration plus a contract ICR.

## Operator procedure (Neon)

Secrets stay private: DSNs and passwords live only in the operator's shell,
the Neon console, and deployment secret stores — never in this repo.

1. Create the Neon project/database (any region; enable the `vector`
   extension or let `0001` create it as owner). Method used for role
   creation: **SQL in `0005_roles_grants.sql`**, run as the project owner
   via `DATABASE_URL_MIGRATE`. If Neon ever requires a console-only step,
   record it here.
2. `export DATABASE_URL_MIGRATE='<owner DSN>'` (never commit it).
3. `python scripts/migrate.py` — expect `applied N`; re-running applies
   nothing (exit 0).
4. Create LOGIN users out-of-band and attach them to the group roles:
   ```sql
   CREATE USER proxylead_backend_login WITH PASSWORD '<private>';
   CREATE USER proxylead_toolserver_login WITH PASSWORD '<private>';
   GRANT proxylead_backend TO proxylead_backend_login;
   GRANT proxylead_toolserver TO proxylead_toolserver_login;
   ```
   Group roles themselves are `NOLOGIN` by design (least privilege; a
   compromised DSN cannot alter roles or grants).
5. Share `DATABASE_URL_BACKEND` / `DATABASE_URL_TOOLSERVER` (pointing at the
   login users) privately with whoever runs E1–E5.
6. `python scripts/verify_db_grants.py` — expect exit 0 with
   *permission denied* on every protected table under the backend role.

## Local verification (compose `db`, no secrets needed for the shape)

`docker-compose.yml` runs `pgvector/pgvector:0.8.0-pg16` on
`127.0.0.1:5432` and a profile-gated `migrate` service that applies exactly
these files with the local owner DSN. Without Docker, the offline lint tests
still verify numbering, grants, and enum parity.

## Grant matrix (summary; `0005_roles_grants.sql` is authoritative)

| Object | `proxylead_backend` | `proxylead_toolserver` |
|---|---|---|
| `workspaces`, `members` | SELECT | — (denied) |
| `slack_events`, `topic_access_rules`, `plans`, `plan_steps`, `approval_requests`, `unanswered_questions` | full CRUD (UPDATE on approvals required for resolution) | — (denied) |
| `request_events`, `decision_events`, `delivery_events` | SELECT + INSERT (append-only) | — (denied) |
| `source_records`, `knowledge_chunks`, `decisions`, `manager_guidance`, `team_notes` | — (denied) | full CRUD |
| `tool_execution_ledger` | — (denied; PROPOSED backend SELECT unconfirmed) | SELECT + INSERT (append-only) |
| `tasks` | — (denied) | full CRUD |
| `egress_sink` | — (denied; PROPOSED count unconfirmed) | SELECT + INSERT |
| `draft_review_queue` view | SELECT | — (denied) |
| sequences | USAGE + SELECT | USAGE + SELECT |

`statement_timeout`: backend `10s`, toolserver `30s` (cold starts time out;
authorization is never bypassed because a query is slow — Arch §7.1).
Vector index: HNSW (`m=16, ef_construction=64`, cosine) — no training pass,
serves a growing seed corpus from row one.
