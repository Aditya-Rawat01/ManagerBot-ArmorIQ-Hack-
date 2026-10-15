-- 0008_ledger_attempts_constraints.sql — P2-07 follow-up (review findings).
--
-- 1. The ledger identity column becomes GENERATED ALWAYS, so a writer cannot
--    supply ids by accident (a primary-key clash is never mistaken for a
--    duplicate jti; the writer also checks the violated constraint name).
-- 2. Row-format CHECKs on the ledger, added NOT VALID: they bind every NEW
--    row but do not scan existing ones, so this file cannot fail on a ledger
--    that already holds rows. A later migration may VALIDATE them once the
--    operator has confirmed the existing rows comply. They mirror the
--    writer's own validation (services/toolserver/ledger.py): hex digests,
--    identifier-only error_class (no free text), a fixed validation_outcome
--    set, and no 'not_reached' rows (a ledger row means the server was reached).
-- 3. public.tool_call_attempts: one append-only row per call that passed the
--    credential check, with its verify_tool_call outcome ('accepted',
--    'invalid_context', 'context_expired', 'replay'). Rejections and replays
--    leave durable evidence without touching the ledger's unique jti. No raw
--    arguments, no _ctx tokens: there is no column for them. Identity columns
--    are set only when the _ctx verified. Not hash-chained.
--
-- No schema change is needed for the writer's other follow-up: created_at is
-- now part of the hashed payload (services/toolserver/ledger.py). The shared
-- Neon ledger held zero rows when that changed, so no stored chain broke.
--
-- Grants travel with objects (migrations/README.md rule 3): same pattern as
-- tool_jti_claims in 0007 — toolserver SELECT + INSERT only, backend and
-- PUBLIC nothing, UPDATE/DELETE revoked from both runtime roles.

-- --- 1. identity -----------------------------------------------------------
ALTER TABLE public.tool_execution_ledger ALTER COLUMN id SET GENERATED ALWAYS;

-- --- 2. ledger row-format checks (new rows only) ---------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'public.tool_execution_ledger'::regclass
                     AND conname = 'tool_execution_ledger_arguments_sha256_hex') THEN
        ALTER TABLE public.tool_execution_ledger
            ADD CONSTRAINT tool_execution_ledger_arguments_sha256_hex
            CHECK (arguments_sha256 IS NOT NULL AND arguments_sha256 ~ '^[0-9a-f]{64}$')
            NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'public.tool_execution_ledger'::regclass
                     AND conname = 'tool_execution_ledger_hashes_hex') THEN
        ALTER TABLE public.tool_execution_ledger
            ADD CONSTRAINT tool_execution_ledger_hashes_hex
            CHECK (prev_hash ~ '^[0-9a-f]{64}$' AND entry_hash ~ '^[0-9a-f]{64}$')
            NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'public.tool_execution_ledger'::regclass
                     AND conname = 'tool_execution_ledger_error_class_identifier') THEN
        ALTER TABLE public.tool_execution_ledger
            ADD CONSTRAINT tool_execution_ledger_error_class_identifier
            CHECK (error_class IS NULL OR error_class ~ '^[A-Za-z_][A-Za-z0-9_.]{0,127}$')
            NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'public.tool_execution_ledger'::regclass
                     AND conname = 'tool_execution_ledger_validation_outcome') THEN
        ALTER TABLE public.tool_execution_ledger
            ADD CONSTRAINT tool_execution_ledger_validation_outcome
            CHECK (validation_outcome IN (
                'accepted', 'invalid_arguments', 'out_of_scope', 'precondition_failed')
                AND (validation_outcome = 'accepted'
                     OR execution_status = 'rejected_by_tool_server'))
            NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'public.tool_execution_ledger'::regclass
                     AND conname = 'tool_execution_ledger_reached') THEN
        ALTER TABLE public.tool_execution_ledger
            ADD CONSTRAINT tool_execution_ledger_reached
            CHECK (execution_status <> 'not_reached')
            NOT VALID;
    END IF;
END
$$;

-- --- 3. attempts -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.tool_call_attempts (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Service credential that passed the bearer check (MVP: one per endpoint).
    credential_id       TEXT NOT NULL
                        CHECK (credential_id ~ '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'),
    mcp_endpoint        TEXT NOT NULL CHECK (mcp_endpoint IN ('memory-mcp', 'workspace-mcp')),
    tool_name           TEXT NOT NULL
                        CHECK (tool_name ~ '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'),
    -- verify_tool_call outcome (services/toolserver/security.py codes).
    outcome             TEXT NOT NULL CHECK (outcome IN (
                            'accepted', 'invalid_context', 'context_expired', 'replay')),
    jti                 TEXT,
    request_id          TEXT,
    workspace_id        TEXT,
    requester_member_id TEXT,
    original_entry_id   BIGINT REFERENCES public.tool_execution_ledger (id) ON DELETE RESTRICT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Accepted calls and replays passed signature verification: jti is known.
    CHECK (outcome NOT IN ('accepted', 'replay') OR jti IS NOT NULL),
    -- Unverified _ctx: no identity is trusted, so none is stored.
    CHECK (outcome IN ('accepted', 'replay')
           OR (jti IS NULL AND request_id IS NULL AND workspace_id IS NULL
               AND requester_member_id IS NULL)),
    CHECK (original_entry_id IS NULL OR outcome = 'replay')
);
CREATE INDEX IF NOT EXISTS tool_call_attempts_jti_idx ON public.tool_call_attempts (jti);
CREATE INDEX IF NOT EXISTS tool_call_attempts_request_idx
    ON public.tool_call_attempts (request_id, created_at);

REVOKE ALL ON public.tool_call_attempts FROM PUBLIC;
REVOKE ALL ON public.tool_call_attempts FROM proxylead_backend;
GRANT SELECT, INSERT ON public.tool_call_attempts TO proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.tool_call_attempts FROM proxylead_toolserver;
REVOKE UPDATE, DELETE ON public.tool_call_attempts FROM proxylead_backend;
-- IDENTITY inserts need no sequence grant (0005); keep the new sequence closed.
REVOKE ALL ON SEQUENCE public.tool_call_attempts_id_seq
    FROM PUBLIC, proxylead_backend, proxylead_toolserver;
