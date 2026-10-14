# P2-01 — Neon schema, migrations, database roles, and grant verification

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` (DB steward) |
| Size | M (3–5 h) |
| Target window | 14 Oct |
| Gate role | Part of **G0** (backend role cannot read protected content) |
| Status | Not started |

## 1. Task ID and title

P2-01 — Provision Neon PostgreSQL with `pgvector`, create the documented tables, separate runtime roles and grants, and prove that the backend role cannot read protected content.

## 2. Purpose and expected outcome

The database boundary is a security layer (Arch §5.4 layer 2). Outcome:

- a Neon database with `pgvector` and full-text search;
- every table listed in PRD §8.1 / Arch §7.2;
- three roles (`proxylead_backend`, `proxylead_toolserver`, a migration role);
- grants that deny the backend `SELECT` on protected content;
- runtime tables that are append-only where required;
- a toolserver connection helper;
- `scripts/verify_db_grants.py`, which demonstrates the denial against the real database.

## 3. Authoritative source references

- PRD §2 P0 (Neon, `pgvector`, FTS), §3 (no separate vector DB), **§8.1** (tables; metadata on every knowledge record; three roles; automated test that the backend cannot read protected content), §9 (fixed SQL, parameterized queries, statement timeout, limits), §11 E1 step 1, §13 (14 Oct).
- Arch §7.1 (storage; Neon cold-start handling), **§7.2** (minimum tables and plan/step/audit fields), **§7.5** (roles; grant verification test; approved read-only views; schema allowlist; statement timeout; row limit), §10, §13 (backend credentials cannot `SELECT` protected tables).

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Reviewers: `samarth-chawla` (grants), `parthgoyal26-03-2005` (audit tables used by P3-01).

## 5. Prerequisites and dependencies

- Hard: none.
- Decisions: D-08 resolved (ADR-028: `psycopg` 3 + plain numbered SQL migrations). D-20 resolved (ADR-021).
- Blocks: P2-03, P2-04, P2-07, P2-10, P3-01, G0.

## 6. Exact files/modules this task owns

- `migrations/` (new directory; DB steward): initial numbered SQL migrations and `migrations/README.md` (ordering and append-only rule).
- `scripts/migrate.py` (new; supporting infrastructure per ADR-028): applies pending migrations with `DATABASE_URL_MIGRATE`.
- `scripts/verify_db_grants.py` (new; documented name).
- `services/toolserver/db.py` (new; documented name): toolserver-role connection helper with statement timeout and read-only transactions for read tools.
- `tests/toolserver/test_db_grants.py` (new; live database; skipped when DSNs are absent).

## 7. Existing files it may need to modify

- `pyproject.toml` (steward `VTCodeCraft`): add `psycopg[binary]`, `psycopg_pool` and `pgvector` (ADR-028) to the `toolserver` and `api` extras.
- `docs/ARCHITECTURE_DECISIONS.md` (steward `VTCodeCraft`): ADR for the grant design (D-08 is ADR-028).
- `.env.example`: no change expected (`DATABASE_URL_BACKEND`, `DATABASE_URL_TOOLSERVER`, `DATABASE_URL_MIGRATE` exist).

## 8. Shared interfaces and contracts

- Table ownership and grants: SHARED_INTERFACES §7 (this task makes the PROPOSED grants concrete; any deviation goes through an ICR).
- Enum values stored in the database must equal `services/contracts/enums.py` values (`DataTier`, `ReviewStatus`, `DecisionSource`, `ApprovalState`, `ToolExecutionStatus`, `DeliveryChannel`, `EntryPoint`).

## 9. Step-by-step implementation requirements

1. Create the Neon project and database. Enable `vector`. Create the three roles using the method Neon permits (console or SQL); record the method. Keep the migration role's DSN out of every runtime configuration.
2. Write migrations for every table in PRD §8.1:
   - `workspaces`, `members`, `slack_events`;
   - `source_records`, `knowledge_chunks`, `decisions`, `manager_guidance`, `team_notes`;
   - `topic_access_rules`;
   - `plans`, `plan_steps`, `approval_requests`;
   - `request_events`, `decision_events`, `tool_execution_ledger`, `delivery_events`;
   - `unanswered_questions`, `tasks`, `egress_sink`.

   Use the Arch §7.2 columns. Every knowledge record carries source, author, timestamp, tier, review status, version, content hash, and supersession metadata (PRD §8.1). `knowledge_chunks` has `vector(384)` and a generated `tsvector` with a GIN index; add a vector index (type recorded in the ADR). Use CHECK constraints for enum columns.
3. `members.armoriq_member_email_ref` follows D-20 (PROPOSED: env-key reference, not a raw email).
4. Grants:
   - `proxylead_backend`: **no `SELECT`** on `source_records`, `knowledge_chunks`, `decisions`, `manager_guidance`, `team_notes`, or any approved view. Read/write on app-metadata tables (`slack_events`, `topic_access_rules`, plan/approval/event/delivery tables, `unanswered_questions`). `SELECT` on non-content ledger columns and the `egress_sink` count only if SHARED_INTERFACES §7 is confirmed.
   - `proxylead_toolserver`: the minimum needed for P2-04..P2-10.
   - No runtime role may `UPDATE` or `DELETE` ledger or event tables.
   - Set a per-role `statement_timeout`.
5. `services/toolserver/db.py`: async or sync connection helper using `psycopg` 3 and `psycopg_pool` (ADR-028), a single pool, statement timeout, a read-only transaction helper, and parameterized queries only (no string-built SQL from inputs).
6. `scripts/verify_db_grants.py`: connect as the backend role and attempt `SELECT` on each protected table and view. Expect *permission denied*; any success exits non-zero. Then confirm the allowed tables work and that the toolserver role reaches the knowledge tables. Print table names and results only, never DSNs.
7. Neon cold start: document the expected delay and the timeout used (Arch §7.1). Never bypass authorization because a query is slow.

## 10. Acceptance criteria

- Migrations apply cleanly to an empty database, and re-running applies nothing.
- `python scripts/verify_db_grants.py` exits `0` against the real database and shows *permission denied* for every protected table and view under the backend role.
- `tests/toolserver/test_db_grants.py` passes when DSNs are present and is skipped (not failed) without them.
- Runtime roles cannot `UPDATE` or `DELETE` the ledger (demonstrated).
- No DSN or password in the repository or logs. The ADR is recorded.

## 11. Required unit, integration, and security tests

- Live: `verify_db_grants.py`; `test_db_grants.py` (backend denial per protected table; append-only denial).
- Offline: a migration lint test that every migration file is numbered and none was edited after merge (checksum list in `migrations/README.md`, optional).

## 12. Commands for verification

```bash
python scripts/migrate.py            # uses DATABASE_URL_MIGRATE (operator only)
python scripts/verify_db_grants.py   # uses DATABASE_URL_BACKEND and DATABASE_URL_TOOLSERVER
python -m pytest tests/toolserver/test_db_grants.py -q
```

## 13. Explicit non-goals

- No seed content (P2-03). No retrieval (P2-04). No approved company-data views (P2-08 adds them in its own migration).
- No backend data-access code (P3-01).

## 14. Risks and unresolved questions

- The Neon role-creation method or limits may differ. Record them.
- Neon role-creation limits (ADR-028 assumes roles and grants can be expressed in SQL; record any console-only step).
- D-12 may later require a metadata-only table or view for the manager console (a new migration by the deciding task).

## 15. Handoff deliverables for dependent agents

- All: table and column list, role names, how to obtain DSNs (privately), the migration procedure.
- P3-01: the backend-writable tables and their columns.
- P2-07: ledger table definition and constraints.
- P5-03: migration and grant procedure for the final environment.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-01] Create Neon schema, migrations, DB roles, and grant verification`
- **Body outline:** tables; roles and grants matrix; verify script; acceptance criteria; evidence = verify-script output (no DSNs).
- **Labels:** `phase-2-mcp-knowledge`, `database`, `security`, `gate`
- **Depends on:** D-08  **Blocks:** P2-03, P2-04, P2-07, P2-10, P3-01

## 17. Definition of done

Migrations and grant verification merged; live verification output posted
(redacted); ADR recorded; handoff posted to P2-03, P2-07, and P3-01.
