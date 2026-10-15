-- 0006_metadata_view.sql — manager draft-review metadata view (P2-01, ADR-023).
--
-- Option (b): a metadata-only view readable by proxylead_backend. Columns are
-- exactly the ADR-023 allowlist — record ID, version, author member ID,
-- created time, review status, content kind. NO title and NO content: Maya
-- opens content only through knowledge_get_source_team via ArmorIQ (P2-08),
-- and confirms through knowledge_confirm_maya_decision (P2-09).

CREATE OR REPLACE VIEW public.draft_review_queue AS
SELECT
    id AS record_id,
    version,
    author_member_id,
    created_at,
    review_status,
    content_kind
FROM public.team_notes
WHERE review_status = 'draft';

REVOKE ALL ON public.draft_review_queue FROM PUBLIC;
REVOKE ALL ON public.draft_review_queue FROM proxylead_toolserver;
GRANT SELECT ON public.draft_review_queue TO proxylead_backend;
