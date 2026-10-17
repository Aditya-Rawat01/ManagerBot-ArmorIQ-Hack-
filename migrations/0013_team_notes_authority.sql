-- 0013_team_notes_authority.sql — P2-09: draft authority metadata on team_notes.
--
-- Claimed as migration 0012 on issue #24 BEFORE coding; renumbered to 0013
-- when the merge of main (P2-10 follow-up, claimed 0012 on issue #23, pushed
-- first) left two 0012 files. `migrate.py` refuses duplicate numbers, and
-- only this file (the later claim) moved — the re-claim is recorded on #24.
-- Amended by the PR #79 review fixes (unmerged, never applied to Neon).
--
-- `team_notes` is a toolserver-exclusive table (SHARED_INTERFACES DB ledger:
-- backend has no SELECT), so this is no shared-interface change and needs no
-- ICR. The ADR-023 `draft_review_queue` keeps its exact 0006 column list;
-- only its filter tightens (superseded drafts leave the queue, review I1).
--
-- 1. Columns PRD v2 §8.1 requires every record to carry and P2-09 step 9
--    writes, none of which the 0003 table had:
--      * tier            — D-10: saved notes are ALWAYS team-tier. The CHECK
--                          encodes that as an invariant: no future code path
--                          can store another tier.
--      * provenance      — request_id / jti / recorded_at from the verified
--                          `_ctx`, injected by trusted code, never arguments.
--      * approved_by /   — step 9.2: Maya's confirmation provenance (who
--        approved_at         confirmed, and when). NULL while draft.
--    Existing rows take the defaults (team-tier drafts, unapproved).
-- 2. CHECK: a note never supersedes itself (review I2).
-- 3. `draft_review_queue` lists current drafts only (review I1).
-- 4. Least privilege for the tool server on `team_notes` (review M2): no
--    DELETE (history is never deleted), and UPDATE only on the lifecycle
--    columns P2-09 writes. Content, title, kind, author, workspace, tier,
--    version and hashes are immutable for the runtime role. The seed ingest
--    runs as the migration role and is unaffected.
-- Confirmed notes are also written to `source_records` / `knowledge_chunks`
-- (D-09 rule 2): the 0005 toolserver grants on those tables already cover it,
-- so nothing is widened here.

ALTER TABLE public.team_notes
    ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'team'
        CHECK (tier IN ('team')),
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS approved_by_member_id TEXT
        REFERENCES public.members (id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

-- Review I2: a note can never supersede itself, whatever the code does. Added
-- NOT VALID (enforced for every new or updated row at once), then validated
-- only when no existing row violates it, so the migration never fails on a
-- database that already holds such a row (none exists on a fresh database or
-- on Neon before 0013: the supersede tool ships with this migration). If the
-- constraint is left NOT VALID, fix the row and run
-- `ALTER TABLE public.team_notes VALIDATE CONSTRAINT team_notes_not_self_superseded`.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.team_notes'::regclass
          AND conname = 'team_notes_not_self_superseded'
    ) THEN
        ALTER TABLE public.team_notes
            ADD CONSTRAINT team_notes_not_self_superseded
            CHECK (superseded_by_id IS DISTINCT FROM id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.team_notes WHERE superseded_by_id = id) THEN
        ALTER TABLE public.team_notes VALIDATE CONSTRAINT team_notes_not_self_superseded;
    END IF;
END
$$;

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

-- Review M2: least privilege on team_notes for the tool server. Its only
-- statements are INSERT (save), SELECT (+ FOR UPDATE, which needs UPDATE on
-- at least one column), and UPDATEs of the lifecycle columns (confirm:
-- review_status / approved_by_member_id / approved_at / updated_at;
-- supersede: superseded_by_id / provenance / updated_at). Revoking the
-- table-level UPDATE also drops any column grants, so the column grant
-- follows it. The backend stays denied (0005).
REVOKE DELETE ON public.team_notes FROM proxylead_toolserver;
REVOKE UPDATE ON public.team_notes FROM proxylead_toolserver;
GRANT UPDATE (review_status, approved_by_member_id, approved_at, superseded_by_id,
              provenance, updated_at)
    ON public.team_notes TO proxylead_toolserver;
GRANT SELECT, INSERT ON public.team_notes TO proxylead_toolserver;
REVOKE ALL ON public.team_notes FROM proxylead_backend;
