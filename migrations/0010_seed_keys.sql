-- 0010_seed_keys.sql — P2-03 follow-up: stable seed keys for keyed upserts.
--
-- The seed ingest (services/knowledge/ingest.py) used to find its own
-- decisions / guidance / notes by exact text, so editing a seeded statement
-- inserted a second row and metadata edits never applied. Each seed item now
-- carries an explicit `key` (seed/*.json), stored in `seed_key` and upserted
-- with INSERT ... ON CONFLICT (workspace_id, seed_key) DO UPDATE.
--
-- * Nullable on purpose: rows written at runtime by the tool server (P2-09)
--   leave it NULL and are never touched by the seed. A plain unique index
--   treats NULLs as distinct (PostgreSQL default), so runtime rows never
--   conflict, and the index builds on rows written by an earlier ingest (all
--   NULL until the next ingest run claims each one once by its old identity).
-- * Additive only: no new tables or views. The 0005 matrix stands and is
--   restated below (grants travel with objects): proxylead_toolserver keeps
--   full CRUD on these tables (table-level grants cover the new column);
--   proxylead_backend keeps NOTHING (protected content).

ALTER TABLE public.decisions ADD COLUMN IF NOT EXISTS seed_key TEXT;
ALTER TABLE public.manager_guidance ADD COLUMN IF NOT EXISTS seed_key TEXT;
ALTER TABLE public.team_notes ADD COLUMN IF NOT EXISTS seed_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS decisions_seed_key_uidx
    ON public.decisions (workspace_id, seed_key);
CREATE UNIQUE INDEX IF NOT EXISTS manager_guidance_seed_key_uidx
    ON public.manager_guidance (workspace_id, seed_key);
CREATE UNIQUE INDEX IF NOT EXISTS team_notes_seed_key_uidx
    ON public.team_notes (workspace_id, seed_key);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.decisions TO proxylead_toolserver;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.manager_guidance TO proxylead_toolserver;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.team_notes TO proxylead_toolserver;
REVOKE ALL ON public.decisions FROM proxylead_backend;
REVOKE ALL ON public.manager_guidance FROM proxylead_backend;
REVOKE ALL ON public.team_notes FROM proxylead_backend;
