-- 0005_roles_grants.sql — roles, grants, revokes, timeouts (P2-01).
--
-- Design (ADR-028, grant matrix in migrations/README.md):
-- * `proxylead_backend` and `proxylead_toolserver` are NOLOGIN group roles.
--   The operator creates LOGIN users out-of-band and grants membership
--   (passwords/DSNs never enter the repo; procedure in migrations/README.md).
-- * Deny-by-default: every existing table is revoked from both roles first,
--   then each role receives exactly the privileges its writers need.
-- * The backend role gets NOTHING on protected knowledge tables, approved
--   views, the ledger, or the task/egress tables (PRD v2 §8.1, Arch §7.5).
--   Backend SELECT on non-content ledger columns and the egress_sink count is
--   PROPOSED (SHARED_INTERFACES §7) and therefore NOT granted here.
-- * No runtime role may UPDATE or DELETE ledger or event tables (append-only).
-- Every future migration that creates objects MUST grant (or explicitly deny)
-- them in the same file — grants are never inherited (see README).

-- --- roles ---------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'proxylead_backend') THEN
        CREATE ROLE proxylead_backend WITH NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'proxylead_toolserver') THEN
        CREATE ROLE proxylead_toolserver WITH NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO proxylead_backend, proxylead_toolserver;

-- --- deny-by-default baseline (existing objects) --------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM proxylead_backend, proxylead_toolserver;

-- --- backend: identity bootstrap reads ------------------------------------
GRANT SELECT ON public.workspaces TO proxylead_backend;
GRANT SELECT ON public.members TO proxylead_backend;

-- --- backend: app-metadata read/write (P3-01, P3-05) ------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON public.slack_events TO proxylead_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.topic_access_rules TO proxylead_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.plans TO proxylead_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.plan_steps TO proxylead_backend;
-- UPDATE is required: approval rows move pending -> resolved (P1-10, P3-01).
GRANT SELECT, INSERT, UPDATE, DELETE ON public.approval_requests TO proxylead_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.unanswered_questions TO proxylead_backend;

-- --- backend: audit event tables are append-only ----------------------------
GRANT SELECT, INSERT ON public.request_events TO proxylead_backend;
GRANT SELECT, INSERT ON public.decision_events TO proxylead_backend;
GRANT SELECT, INSERT ON public.delivery_events TO proxylead_backend;
REVOKE UPDATE, DELETE ON public.request_events FROM proxylead_backend;
REVOKE UPDATE, DELETE ON public.decision_events FROM proxylead_backend;
REVOKE UPDATE, DELETE ON public.delivery_events FROM proxylead_backend;
REVOKE UPDATE, DELETE ON public.request_events FROM proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.decision_events FROM proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.delivery_events FROM proxylead_toolserver;

-- --- backend: explicit denial on protected content (defense in depth) ------
-- (Already denied by the baseline revoke; repeated so the denial is greppable
-- and survives default-privilege changes.)
REVOKE ALL ON public.source_records FROM proxylead_backend;
REVOKE ALL ON public.knowledge_chunks FROM proxylead_backend;
REVOKE ALL ON public.decisions FROM proxylead_backend;
REVOKE ALL ON public.manager_guidance FROM proxylead_backend;
REVOKE ALL ON public.team_notes FROM proxylead_backend;
REVOKE ALL ON public.source_records FROM PUBLIC;
REVOKE ALL ON public.knowledge_chunks FROM PUBLIC;
REVOKE ALL ON public.decisions FROM PUBLIC;
REVOKE ALL ON public.manager_guidance FROM PUBLIC;
REVOKE ALL ON public.team_notes FROM PUBLIC;

-- --- backend: no access to the ledger or the task/egress tables ------------
REVOKE ALL ON public.tool_execution_ledger FROM proxylead_backend;
REVOKE ALL ON public.tasks FROM proxylead_backend;
REVOKE ALL ON public.egress_sink FROM proxylead_backend;

-- --- toolserver: knowledge tables (P2-03 seed, P2-04/P2-08 reads, P2-09 writes)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.source_records TO proxylead_toolserver;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.knowledge_chunks TO proxylead_toolserver;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.decisions TO proxylead_toolserver;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.manager_guidance TO proxylead_toolserver;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.team_notes TO proxylead_toolserver;

-- --- toolserver: ledger is INSERT + SELECT (verifier/idempotency), never more
GRANT SELECT, INSERT ON public.tool_execution_ledger TO proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.tool_execution_ledger FROM proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.tool_execution_ledger FROM proxylead_backend;
REVOKE ALL ON public.tool_execution_ledger FROM PUBLIC;

-- --- toolserver: task and honeypot tables (P2-10) ---------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON public.tasks TO proxylead_toolserver;
GRANT SELECT, INSERT ON public.egress_sink TO proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.egress_sink FROM proxylead_toolserver;

-- --- sequences: none needed --------------------------------------------------
-- Every id is an IDENTITY column, so INSERT on the table is enough. Granting
-- the sequences would let the backend read `last_value` of protected tables'
-- sequences (row counts of team_notes, source_records, ...).
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM proxylead_backend, proxylead_toolserver;

-- --- trigger function (lexical column maintenance runs on toolserver writes)
GRANT EXECUTE ON FUNCTION public.knowledge_chunks_tsv_trigger() TO proxylead_toolserver;

-- --- per-role statement timeout (Arch §7.5; cold starts must time out, never
-- --- bypass authorization: PRD v2 §8.2, Arch §7.1) ---------------------------
-- Role settings are NOT inherited through membership: these apply only to
-- sessions that SET ROLE to the group. The LOGIN users get their own
-- timeouts in the operator procedure (migrations/README.md step 4), and
-- services/toolserver/db.py also sets one per connection.
ALTER ROLE proxylead_backend SET statement_timeout = '10s';
ALTER ROLE proxylead_toolserver SET statement_timeout = '30s';
