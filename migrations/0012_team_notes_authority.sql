-- 0012_team_notes_authority.sql — P2-09: draft authority metadata on team_notes.
--
-- Claimed as migration 0012 on issue #24 BEFORE coding. `team_notes` is a
-- toolserver-exclusive table (SHARED_INTERFACES DB ledger: backend has no
-- SELECT), so this is no shared-interface change and needs no ICR; the
-- ADR-023 `draft_review_queue` columns are untouched.
--
-- Adds what PRD v2 §8.1 requires every record to carry and P2-09 step 9
-- writes, none of which the 0003 table had:
--   * tier            — D-10 (resolved 16 Oct 2026): saved notes are ALWAYS
--                       team-tier. The CHECK encodes that as an invariant:
--                       no future code path can store another tier.
--   * provenance      — request_id / jti / recorded_at from the verified
--                       `_ctx`, injected by trusted code, never arguments.
--   * approved_by /   — step 9.2: Maya's confirmation provenance (who
--     approved_at         confirmed, and when). NULL while draft.
--
-- version and supersession constraints already exist (0003), so nothing else
-- changes. Existing rows take the defaults (team-tier drafts, unapproved).
-- Grants travel with objects (migrations/README.md rule 3): table-level
-- grants from 0005 cover the new columns; the seed insert lists its columns
-- explicitly and is unaffected.

ALTER TABLE public.team_notes
    ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'team'
        CHECK (tier IN ('team')),
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS approved_by_member_id TEXT
        REFERENCES public.members (id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
