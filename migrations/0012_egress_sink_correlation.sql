-- 0012_egress_sink_correlation.sql — P2-10 follow-up: correlate honeypot sink
-- rows with the call that wrote them (P2-10 spec §9.1: synthetic destination,
-- bounded message, jti, requester, timestamp).
--
-- * `jti` and `requester_member_id` come from the VERIFIED `_ctx` only (the
--   handler never reads them from tool arguments). Nullable on purpose: rows
--   written before this file have neither, and adding NOT NULL would fail on
--   them. Every row the handler writes from now on carries both.
-- * The partial unique index makes one sink row per invocation a database
--   rule (pre-0012 NULL rows are exempt). A replayed jti is already refused
--   before any handler runs (tool_jti_claims); this is defense in depth.
-- * No FK on requester_member_id: the sink is evidence and must record what
--   the verified envelope said, like the ledger's requester column.
-- * Additive only: no new tables or views. The 0005 matrix stands and is
--   restated below (grants travel with objects): proxylead_toolserver keeps
--   SELECT + INSERT and never UPDATE/DELETE on the sink (table-level grants
--   cover the new columns); proxylead_backend keeps NOTHING.
-- * Least privilege on `tasks`: 0005 granted the toolserver full CRUD, but
--   the only writer (create_followup_task, P2-10) INSERTs and SELECTs, and no
--   planned task (P2-09, P3-04, P4-03, P5-03 / D-16 reset) updates or deletes
--   task rows through the toolserver. UPDATE and DELETE are revoked; a future
--   task that needs them grants them in its own migration.
-- Idempotent: re-running this file changes nothing.

ALTER TABLE public.egress_sink
    ADD COLUMN IF NOT EXISTS jti TEXT,
    ADD COLUMN IF NOT EXISTS requester_member_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS egress_sink_jti_uidx
    ON public.egress_sink (jti) WHERE jti IS NOT NULL;

GRANT SELECT, INSERT ON public.egress_sink TO proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.egress_sink FROM proxylead_toolserver;
REVOKE ALL ON public.egress_sink FROM proxylead_backend;
REVOKE ALL ON public.egress_sink FROM PUBLIC;

GRANT SELECT, INSERT ON public.tasks TO proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.tasks FROM proxylead_toolserver;
REVOKE ALL ON public.tasks FROM proxylead_backend;
REVOKE ALL ON public.tasks FROM PUBLIC;
