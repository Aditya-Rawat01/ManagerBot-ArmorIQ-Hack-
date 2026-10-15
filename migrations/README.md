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
| `0007_ledger_evidence_columns.sql` | `arguments_sha256`, `workspace_id` on the ledger (P2-07 writer fields); append-only `tool_jti_claims` (atomic replay claim before execution) |
| `0008_ledger_attempts_constraints.sql` | P2-07 follow-up: ledger `id` `GENERATED ALWAYS`; `NOT VALID` row-format CHECKs on new ledger rows (hex digests, identifier-only `error_class`, fixed `validation_outcome` set, no `not_reached`); append-only `tool_call_attempts` (one row per call that passed the credential check: `accepted` / `invalid_context` / `context_expired` / `replay`) |

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
   ALTER ROLE proxylead_backend_login SET statement_timeout = '10s';
   ALTER ROLE proxylead_toolserver_login SET statement_timeout = '30s';
   ```
   The `ALTER ROLE` lines are required: role settings on the group roles are
   not inherited by their members (verified: a login user shows
   `statement_timeout = 0` without them). They also work through Neon's
   pooled (`-pooler`) endpoint, where session-level `SET` is not supported.
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
| `tool_jti_claims` (0007) | — (denied) | SELECT + INSERT (append-only) |
| `tool_call_attempts` (0008) | — (denied, incl. INSERT) | SELECT + INSERT (append-only) |
| `tasks` | — (denied) | full CRUD |
| `egress_sink` | — (denied; PROPOSED count unconfirmed) | SELECT + INSERT |
| `draft_review_queue` view | SELECT | — (denied) |
| sequences | — (denied; IDENTITY inserts need no sequence grant) | — (denied) |

`statement_timeout`: backend `10s`, toolserver `30s`, set on the LOGIN users
(operator step 4) and per transaction with `SET LOCAL` by
`apps/api/core/db.py` and `services/toolserver/db.py` (`transaction()` /
`apply_statement_timeout()`; the libpq `options` startup parameter is not used
because Neon's pooled endpoint rejects it). Cold starts time out;
authorization is never bypassed because a query is slow — Arch §7.1.
Vector index: HNSW (`m=16, ef_construction=64`, cosine) — no training pass,
serves a growing seed corpus from row one.

Ledger notes (P2-07, 0007/0008):

- `arguments_sha256` is an **unsalted** SHA-256 of the canonical business
  arguments. For low-entropy arguments (a short query, an enum) it is
  guessable by hashing candidates, so it is evidence for the tool server
  only. Any future backend grant on the ledger (the PROPOSED SC-06
  column-level `SELECT`) MUST exclude `arguments_sha256` — or the writer must
  first switch to an HMAC with a tool-server-only key.
- The 0008 CHECKs are `NOT VALID`: enforced for every new row, existing rows
  not scanned. A later migration may `VALIDATE CONSTRAINT` them once the
  operator has confirmed the existing rows comply.
- The hashed payload gained `created_at` (normalized UTC ISO-8601 with
  microseconds) in the 0008 follow-up. Rows written by the earlier writer do
  not verify under the new code; the shared Neon ledger held no real rows
  when this changed. Do not mix writer versions on one ledger.
