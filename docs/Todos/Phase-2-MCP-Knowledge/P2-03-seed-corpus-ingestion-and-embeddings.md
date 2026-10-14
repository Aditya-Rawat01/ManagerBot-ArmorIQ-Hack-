# P2-03 — Synthetic seed corpus, ingestion, and local embeddings

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | M (3–5 h) |
| Target window | 14 Oct (one team-tier record first, for E1), 15 Oct (full corpus) |
| Gate role | E1 prerequisite (PRD §11 step 1) |
| Status | Not started |

## 1. Task ID and title

P2-03 — Create the synthetic multi-tier seed corpus and the ingestion pipeline (chunking, content hash, FastEmbed `BAAI/bge-small-en-v1.5` embeddings, provenance).

## 2. Purpose and expected outcome

Retrieval, E1, E3, and E17 all need known, synthetic, reproducible data.
Outcome:

- `seed/` holds synthetic decisions, guidance, notes, and source records across team/confidential/restricted tiers, including the E17 case types;
- an ingestion command that runs **with the migration/seed role only** and writes records, chunks, embeddings, and metadata;
- a first increment with one team-tier record, delivered early for E1.

## 3. Authoritative source references

- PRD §0.9 (synthetic data only), §2 P0 (FastEmbed `BAAI/bge-small-en-v1.5`, 384 dims), §8.1 (record metadata; seed role separate), §8.2 (never send confidential text to a hosted embedder), §8.3 (seed answerable, no-evidence, weak, conflicting, superseded, escalation cases), §11 E1 step 1.
- Arch §3 must-ship 2 (seeded knowledge base with source records, decisions, guidance, a few company-data views), §7.1 (embedding in the ingestion path; pin model; record model version), §7.3, §7.4, §9 (public demo uses synthetic data only).

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Contributor: `VTCodeCraft` (demo-question coverage for P3-08 / P5-04).

## 5. Prerequisites and dependencies

- Hard: P2-01.
- Decisions: D-20 (member/workspace bootstrap), D-09 (whether drafts are seeded as searchable), D-10.
- Blocks: P2-04, P2-05, P3-08; provides the E1 seed record used by P1-06 (through P2-04 → P2-06).

## 6. Exact files/modules this task owns

- `seed/` (new; DB steward): corpus files (format chosen and recorded), `seed/README.md` (what each record is for: tier, case type)
- `services/knowledge/ingest.py` (new; documented)
- `services/knowledge/embeddings.py` (new; documented): loads the model once and embeds text (also used by P2-05 for query embedding)
- `tests/toolserver/test_ingest.py` (new)

## 7. Existing files it may need to modify

- `pyproject.toml` (steward `VTCodeCraft`): pin `fastembed` in the `toolserver` extra.
- `.env.example` (steward `parthgoyal26-03-2005`): none expected (`EMBED_MODEL` exists).

## 8. Shared interfaces and contracts

- Writes the tables owned by P2-01 using `DataTier` / `ReviewStatus` values (FIXED).
- `embeddings.embed(texts) -> list[list[float]]` (384 dims) and `MODEL_ID` / version constant (PROPOSED; consumed by P2-05).
- `members` / `workspaces` IDs per D-20 (consumed by P1-03 and the ledger).

## 9. Step-by-step implementation requirements

1. **Increment 1 (E1).** One team-tier `source_records` row with chunk + embedding and a known phrase the E1 probe can search for. Deliver it before the rest.
2. Corpus: synthetic project, team, and decision content only, with no real people, emails, or companies. Include:
   - team-tier answerable decisions;
   - confidential and restricted records (for E3 and the demo);
   - two conflicting decisions;
   - a superseded decision with `superseded_by`;
   - weak-evidence near-misses;
   - unrelated content;
   - drafts (per D-09);
   - topic-escalation cases (PRD §8.3).
3. Every record has source, author, timestamp, tier, review status, version, `sha256` content hash, and supersession metadata (PRD §8.1).
4. Chunking: size and overlap PROPOSED and recorded in `seed/README.md`. Each chunk carries workspace and tier metadata (Arch §7.2).
5. Embeddings: FastEmbed `BAAI/bge-small-en-v1.5` locally, 384 dims. Store the model ID and version in the chunk or source metadata. Never call a hosted embedding API.
6. Members and workspace rows created from env at seed time per D-20. **No raw emails stored** under the PROPOSED default.
7. Ingestion connects **only** with `DATABASE_URL_MIGRATE` and is idempotent (content hash plus version prevents duplicates).
8. Command: `python -m services.knowledge.ingest --source seed/` (PROPOSED; record the final form).

## 10. Acceptance criteria

- Increment 1 is present in the shared database before P2-06 needs it.
- The full corpus covers every PRD §8.3 case type. `seed/README.md` lists each case with its tier and purpose.
- Re-running ingestion creates no duplicates.
- Every chunk has a 384-dim embedding and a recorded model version.
- No real PII anywhere in `seed/` (review + regex scan).

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_ingest.py`:
  - chunking deterministic;
  - content hash stable;
  - metadata complete;
  - idempotent upsert logic (fake DB);
  - `embeddings` returns 384 dims (marked slow; skipped when the model is not cached).
- Security: test that `ingest.py` reads only `DATABASE_URL_MIGRATE` and the runtime settings classes never expose it.

## 12. Commands for verification

```bash
python -m pytest tests/toolserver/test_ingest.py -q
python -m services.knowledge.ingest --source seed/      # operator, migration role
git grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" -- seed/ || echo "no emails"
```

## 13. Explicit non-goals

- No retrieval (P2-04/P2-05). No E17 question set (P3-08 owns `seed/eval/`).
- No live connectors or automatic ingestion (PRD §2 not-in-MVP).

## 14. Risks and unresolved questions

- The FastEmbed install size and first-run download need network access at seed time. Cache it.
- D-09 changes which drafts are seeded as searchable.

## 15. Handoff deliverables for dependent agents

- P2-04/P2-05: record and chunk layout; known test phrases per tier.
- P1-06: the E1 search phrase and expected source ID.
- P3-08: the case-to-source-ID map for the E17 expected results.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-03] Build synthetic multi-tier seed corpus and FastEmbed ingestion`
- **Body outline:** increment 1 for E1; case coverage list; metadata rule; seed-role-only rule; tests.
- **Labels:** `phase-2-mcp-knowledge`, `knowledge`, `database`
- **Depends on:** P2-01  **Blocks:** P2-04, P2-05, P3-08 (and the E1 seed record for P1-06)

## 17. Definition of done

Corpus, ingestion, and tests merged; the shared database seeded; case map
documented; handoff posted.
