-- 0013_team_notes_authority.sql — P2-09: draft authority metadata on team_notes.
--
-- Claimed as migration 0012 on issue #24 BEFORE coding; renumbered to 0013
-- when the merge of main (P2-10 follow-up, claimed 0012 on issue #23, pushed
-- first) left two 0012 files. `migrate.py` refuses duplicate numbers, and
-- only this file (the later claim) moved — the re-claim is recorded on #24.
-- `team_notes` is a
-- toolserver-exclusive table (SHARED_INTERFACES DB ledger: backend has no
-- SELECT), so this is no shared-interface change and needs no ICR. The
-- ADR-023 `draft_review_queue` keeps its exact 0006 column list; only its
-- filter tightens (superseded drafts leave the queue, review I1).
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

-- Review I1: a superseded draft is no longer awaiting review. The tool server
-- refuses to confirm it (`knowledge_confirm_maya_decision` checks
-- superseded_by_id), and Maya's metadata-only queue stops listing it. The
-- column list is exactly 0006's (CREATE OR REPLACE VIEW requires it; ADR-023
-- allowlist: no title, no content). The 0006 grants are restated (grants
-- travel with objects, migrations/README.md rule 3).
CREATE OR REPLACE VIEW public.draft_review_queue AS
SELECT
    id AS record_id,
    version,
    author_member_id,
    created_at,
    review_status,
    content_kind
FROM public.team_notes
WHERE review_status = 'draft'
  AND superseded_by_id IS NULL;

REVOKE ALL ON public.draft_review_queue FROM PUBLIC;
REVOKE ALL ON public.draft_review_queue FROM proxylead_toolserver;
GRANT SELECT ON public.draft_review_queue TO proxylead_backend;
