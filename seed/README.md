# Seed corpus (P2-03)

Synthetic data only (PRD §0.9): personas (`maya`/`sam`/`priya`) as authors, no
real people, emails, or companies. Nothing here leaves the repository except
through the migration-role ingest into a database.

## Format

- `sources/*.json` — one source record per file: `source_id` (stable, used by
  E1/E17 handoffs), `title`, `tier`, `status` (`active`/`superseded`),
  `source_type`, `author` (persona label), `reference`, `recorded_at`
  (synthetic date, kept in provenance), `case` (this table), `provenance`
  extras, `content` (plain text).
- `decisions.json` — `decisions` rows; `supersedes_statement` links the
  superseded row after insert.
- `guidance.json` — `manager_guidance` rows (`approved_by` persona).
- `notes.json` — `team_notes` drafts (`review_status: draft`). Drafts are NOT
  search evidence (D-09 interim): searchable retrieval (P2-04) only returns
  authoritative, non-superseded records; Maya reaches drafts through the
  review queue instead.
- Members/workspace rows come from env at seed time (D-20/ADR-021), not files.
- Runs without member emails leave `owner_member_id` / `author_member_id` /
  `approved_by_member_id` NULL (honest unknown, never fake rows). A later run
  with emails present adopts those orphans by exact-text match (reported as
  `adopted`).

## Chunking (PROPOSED, recorded)

Word windows: 120 words per chunk, 20 words overlap, split on whitespace
(`services/knowledge/ingest.py::chunk_text`). Deterministic: same text always
yields the same chunks. Each chunk carries `workspace_id` + `tier`; the
source's provenance records the embed model, tool, dimension, case, and
recorded date.

## Embeddings

FastEmbed `BAAI/bge-small-en-v1.5`, 384 dims, local only
(`services/knowledge/embeddings.py`). Model version string
(`MODEL_ID+fastembed-<pkg>`) is stored per source. Never a hosted API (R-54).

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
| notes: `Deploy freeze lifted early?` | draft | poisoning-test | Contradicts the E1 record; must never surface as fact |
| notes: `Blameless postmortem template idea` | draft | draft-benign | Ordinary unconfirmed draft |

Decisions mirror the freeze (authoritative), the CI conflict (both sides
authoritative → conflict), and the checklist supersession (v1 → v2 link).

## E1 probe record (for P1-06 / #7)

- source_id: `team-friday-deploy-freeze` (team tier, active)
- Known search phrase: **quokka Friday deploy freeze**
- The word `quokka` appears in exactly this record (keyword search isolates
  it); the freeze protocol gives the semantic match. No other record contains
  it.
