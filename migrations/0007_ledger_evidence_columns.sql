-- 0007_ledger_evidence_columns.sql — P2-07: columns the ledger writer needs
-- that P2-01 did not create (task step 1): the sha256 of the canonical
-- business arguments (never raw arguments) and the workspace id from the
-- verified _ctx. Both nullable, so the change is purely additive; no new
-- tables or views. The toolserver role's existing SELECT + INSERT on
-- public.tool_execution_ledger covers the new columns (restated below for
-- explicitness, per the grants-travel-with-objects rule); the backend role
-- keeps no access (0005 revokes stand — new columns grant nothing new).
ALTER TABLE public.tool_execution_ledger
    ADD COLUMN IF NOT EXISTS arguments_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS workspace_id TEXT;

GRANT SELECT, INSERT ON public.tool_execution_ledger TO proxylead_toolserver;

-- Atomic jti claims (replay protection BEFORE execution). The ledger row can
-- only be written after the tool ran (it carries the outcome), so a read-only
-- "seen?" check against the ledger lets two concurrent calls with the same
-- jti both execute. The tool server claims the jti here first with
-- INSERT ... ON CONFLICT DO NOTHING; exactly one caller wins. Append-only.
CREATE TABLE IF NOT EXISTS public.tool_jti_claims (
    jti         TEXT PRIMARY KEY,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

REVOKE ALL ON public.tool_jti_claims FROM PUBLIC;
REVOKE ALL ON public.tool_jti_claims FROM proxylead_backend;
GRANT SELECT, INSERT ON public.tool_jti_claims TO proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.tool_jti_claims FROM proxylead_toolserver;
