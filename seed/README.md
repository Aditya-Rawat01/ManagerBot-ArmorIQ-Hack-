# Seed corpus (P2-03)

Synthetic data only (PRD §0.9): personas (`maya`/`sam`/`priya`) as authors, no
real people, emails, or companies. Nothing here leaves the repository except
through the migration-role ingest into a database.

## Format

- `sources/*.json` — one source record per file, validated by
  `services/knowledge/ingest.py::validate_source_record` (unknown keys are
  rejected; every key below is required except `source_type`, `reference`,
  and `provenance`):
  - `source_id` — stable, used by E1/E17 handoffs; upsert key with the
    workspace. The `note-` prefix is reserved for team notes Maya confirmed
    at runtime (P2-09: source `note-<team_notes.id>`); a seed source using
    it is rejected, and the ingest never reads, rewrites or deletes those
    runtime sources.
  - `title`, `content` (plain text), `source_type`, `reference`.
  - `author` — persona label (`maya`/`sam`/`priya`).
  - `tier` — `DataTier` (`team`/`confidential`/`restricted`).
  - `status` — `active` or `superseded`; `superseded_by` — the successor's
    `source_id` when `status` is `superseded`, otherwise `null` (always
    explicit, always consistent with `status`).
  - `review_status` — `ReviewStatus` (`authoritative`/`draft`); `version` —
    integer ≥ 1.
  - `recorded_at` — synthetic ISO date; stored as `created_at` and
    `updated_at` (midnight UTC) and in provenance.
  - `provenance` — extras only (e.g. `escalation_topic`); keys the ingest
    writes itself are reserved and rejected.
- `decisions.json` — `decisions` rows, each with a stable `key`; `source`
  names a `source_id` (a typo fails the run) and `supersedes` names the `key`
  of the decision it replaces (`superseded_by_id` is re-linked every run).
- `guidance.json` — `manager_guidance` rows (`key`, `approved_by` persona).
- `notes.json` — `team_notes` drafts (`key`, `review_status: draft`). Drafts
  are NOT search evidence (D-09, resolved 16 Oct 2026): search (P2-04/P2-05,
  both the lexical and the vector leg) only returns active records whose
  `review_status` is `authoritative` (missing or unknown = excluded); Maya
  reaches drafts through the review queue instead.
- `company_projects.json` — synthetic `company_projects` rows behind the
  P2-08 approved views `approved_company_data_{team,confidential,restricted}`
  (migration 0011 creates the table, views, and grants only; it inserts no
  rows, so a fresh `scripts/migrate.py` never needs a seeded workspace). Keys:
  `project_key` (≤ 128 chars, unique; the `source_id` of a `read_source_*`
  row), `project_name` (≤ 200 chars; the row `title`), `tier`, `status`
  (`active`/`superseded`), `owner` (persona), `summary`, and the optional
  text columns `details`, `budget`, `internal_notes` (each view exposes only
  its tier's columns). Unknown keys are rejected
  (`services/knowledge/ingest.py::validate_project`).
- Members/workspace rows come from env at seed time (D-20/ADR-021), not files.
  A persona without `PERSONA_<NAME>_EMAIL` is not written (the run warns with
  the env-key name only) and its FKs stay NULL — an honest unknown, never a
  fake row. A later run with the email set fills those FKs in place.
- **No eval labels in records.** The case of each record lives only in the
  case map below (P3-08 owns the E17 question set in `seed/eval/`); labels
  are never ingested, and a `case` key is rejected.

## Stored layout and idempotency

`python -m services.knowledge.ingest --source seed/` (migration role only;
refuses a DSN equal to `DATABASE_URL_BACKEND`/`DATABASE_URL_TOOLSERVER`).
Validates the whole corpus and loads the model first, then runs ONE
transaction under `pg_advisory_xact_lock(5786091178244190257)` (ASCII
`PLSEED01`, `ingest.INGEST_LOCK_KEY`). Re-running changes nothing: every count
in the printed stats is 0.

- `source_records`: keyed by `(workspace_id, source_id)`. `content_hash` =
  sha256 of `content`. `provenance` = the file's extras plus `recorded_at`,
  `version`, `review_status`, `superseded_by`, `embed_model`, `embed_tool`
  (`MODEL_ID+fastembed-<pkg>`), `embed_dim`, `chunk_words`, `chunk_overlap`,
  and `ingest_digest` — sha256 over the canonical JSON of every ingested field
  plus the model version, chunking parameters, and layout version. An equal
  digest skips the record; any other change rewrites the row.
- `knowledge_chunks`: re-embedded only when the chunk texts or the model
  version change; a metadata-only change just updates the `tier` (and
  `workspace_id`) copied from the source. Chunk `content_hash` =
  sha256(`<source_id>:<chunk text>`).
- `decisions` / `manager_guidance` / `team_notes`: keyed by
  `(workspace_id, seed_key)` (migration 0010), upserted in place. A row from
  an older ingest (NULL `seed_key`) is claimed once by its old identity
  (exact statement / guidance text / note content hash). Runtime rows (NULL
  `seed_key`) are never touched. A note's `review_status` and `version` are
  set on insert only: after that the draft lifecycle belongs to
  `knowledge_confirm_maya_decision` (P2-09, R-56).
- `company_projects`: keyed by `(workspace_id, project_key)` (the 0011 unique
  constraint), loaded after the workspace row exists (its FK target) and
  rewritten in place only when a column differs; counted in the `projects`
  stat (0 on a re-run). Rows that are no longer in the file are left alone,
  like the other seeded tables. Upgrading a database that already has
  0001–0010 and an older ingest: `scripts/migrate.py` applies 0011 (an empty
  table), then one ingest run loads the rows (`projects` = the file's row
  count; every other count is 0 when the earlier ingest used the same
  layout version).

## Chunking (PROPOSED, recorded)

Word windows: 120 words per chunk, 20 words overlap, split on whitespace
(`services/knowledge/ingest.py::chunk_text`; overlap must be smaller than the
window). Deterministic: same text always yields the same chunks. Chunked text
is the title, a blank line, then the content. Each chunk carries
`workspace_id` + `tier`.

## Embeddings

FastEmbed `BAAI/bge-small-en-v1.5`, 384 dims, local only
(`services/knowledge/embeddings.py`; `fastembed` is imported only when the
model loads). Model version string (`MODEL_ID+fastembed-<pkg>`) is stored per
source. Never a hosted API (R-54). The model cache follows FastEmbed:
`FASTEMBED_CACHE_PATH`, else `<tempdir>/fastembed_cache`.

## Case map (PRD §8.3)

| source_id | Tier | Case | Purpose |
|---|---|---|---|
| `team-friday-deploy-freeze` | team | answerable-team; **E1 probe** | Known phrase below; expected source for P1-06 |
| `team-standup-format` | team | answerable-team | Everyday question coverage |
| `team-incident-rotations` | team | answerable-team | Everyday question coverage |
| `conf-runway-review` | confidential | answerable-confidential | E3 HOLD cell material |
| `conf-vendor-renewal` | confidential | answerable-confidential | E3 HOLD cell material |
| `restr-workforce-planning` | restricted | answerable-restricted | E3 BLOCK cell material |
| `team-ci-runner-hosted` | team | conflict-a | Conflicting pair, older side |
| `team-ci-runner-selfhosted` | team | conflict-b | Conflicting pair, newer side (abstain: conflict) |
| `team-release-checklist-v1` | team | superseded-old | `status=superseded`, points at v2 |
| `team-release-checklist-v2` | team | superseded-new | Current; never show v1 as current |
| `team-billing-rumor` | team | weak-evidence | Below-threshold rumor (abstain: no_evidence) |
| `team-office-snacks` | team | unrelated | No-evidence questions match nothing relevant |
| `team-api-timeouts` | team | escalation-team | Same topic, team step |
| `conf-api-key-rotation` | confidential | escalation-confidential | Same topic, confidential step (multi-chunk) |
| `restr-api-breach-postmortem` | restricted | escalation-restricted | Same topic, restricted step |
| note `note-freeze-lifted-early` ("Deploy freeze lifted early?") | draft | poisoning-test | Contradicts the E1 record; must never surface as fact |
| note `note-postmortem-template-idea` ("Blameless postmortem template idea") | draft | draft-benign | Ordinary unconfirmed draft |

Decisions mirror the freeze (authoritative), the CI conflict (both sides
authoritative → conflict), and the checklist supersession (v1 → v2 link).

## E17 eval set (`seed/eval/`, P3-08)

Labeled questions for `scripts/eval_e17_grounding.py`; never ingested (the
ingest reads only the files listed above). Fixed split: `e17_train.json`
(27 cases, ids `tr-*`) calibrates `MIN_SCORE`; `e17_heldout.json` (20 cases,
ids `ho-*`) is only measured. Each case is an object with exactly these keys
(validated by `load_cases`; unknown keys are rejected):

- `id`, `question`, `persona` (`maya`/`sam`/`priya`: the requester whose
  signed `_ctx` the harness uses), `tiers` (the search tools the plan calls,
  in order; tiers above the persona's ceiling must be refused);
- `case_type`: `answerable`, `unrelated`, `weak_evidence`, `conflict`,
  `superseded`, or `escalation` (PRD §8.3);
- `expected_answerable`, `expected_source_ids` (answerable: the sources that
  ground the answer; conflict: the conflicting pair), and
  `expected_abstain_reason` (`null`, `no_evidence`, or `conflict`).

Synthetic questions only (PRD §0.9).

## E1 probe record (for P1-06 / #7)

- source_id: `team-friday-deploy-freeze` (team tier, active)
- Known search phrase: **quokka Friday deploy freeze**
- The word `quokka` appears in exactly this record (keyword search isolates
  it); the freeze protocol gives the semantic match. No other record contains
  it.
