# P2-05 — Vector retrieval and reciprocal rank fusion

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `Aditya-Rawat01` |
| Size | M (2–4 h) |
| Target window | 15 Oct |
| Gate role | Prerequisite of E17 (P3-08) |
| Status | Not started |

## 1. Task ID and title

P2-05 — Add `pgvector` similarity search, RRF fusion with full-text results, deduplication, and score reporting to `services/knowledge/retrieval.py`. Load the embedding model once at tool-server startup.

## 2. Purpose and expected outcome

PRD §8.2 requires hybrid retrieval. Outcome: `search` returns fused, deduplicated,
limited hits with the same tier filters as P2-04, plus scores that E17 can
calibrate (`MIN_SCORE`). The query embedding model is loaded exactly once per
tool-server process.

## 3. Authoritative source references

- PRD §3 (load the model once at startup; ~1 GB RAM), §8.2 (vector + FTS; RRF; dedupe by source/chunk; limit; filters; `MIN_SCORE` calibrated, not guessed; store model version and threshold), §8.3.
- Arch §7.1 (pgvector; FastEmbed local; do not recompute embeddings in protected handlers unless needed; Neon cold start), §7.4.

## 4. Owner and proposed contributors

- Owner: `Aditya-Rawat01`.
- Contributor: `parthgoyal26-03-2005` (deployment memory check).

## 5. Prerequisites and dependencies

- Hard: P2-04, P2-03.
- Decision: D-23 (score scale and threshold location) is decided **with** P3-08. Expose the scores here.
- Blocks: P3-08.

## 6. Exact files/modules this task owns

- `services/knowledge/retrieval.py` (extend)
- `tests/toolserver/test_retrieval_fusion.py` (new)

## 7. Existing files it may need to modify

- `services/toolserver/config.py` (steward: same owner): add the `EMBED_MODEL` setting (PRD §12 name; currently missing).
- `services/toolserver/main.py` (steward: same owner): load the embedding model once in the lifespan. Fail health or readiness clearly if loading fails; never fall back to a hosted embedder.

## 8. Shared interfaces and contracts

- The `search` signature is unchanged (P2-04). Hits gain `vector_score`, `text_rank`, and `fused_score` (PROPOSED).
- `KnowledgeSearchHit.score` meaning follows D-23 (ICR when fixed).

## 9. Step-by-step implementation requirements

1. Embed the query with the loaded model (P2-03 `embeddings.py`).
2. Vector query: cosine distance, the **same** fixed WHERE clause as P2-04 (workspace, exact tier, status), with a candidate limit fixed in code.
3. Fuse the FTS and vector candidate lists with RRF (`k` constant PROPOSED, recorded). Deduplicate by chunk, then by source. Apply the final limit.
4. Report the per-hit scores. Do **not** apply an unvalidated threshold (E17 decides). Expose `min_score` as a parameter whose default is "none".
5. Performance: record p50/p95 latency on the seeded corpus and memory use after model load (for P1-05 / P5-03).

## 10. Acceptance criteria

- Hybrid results respect every P2-04 tier test (the same tests run against the fused path).
- RRF ordering is deterministic for fixtures. Duplicates are removed.
- The model loads once per process (test with a load counter).
- Latency and memory numbers are recorded in the issue.

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_retrieval_fusion.py`:
  - RRF math on fixed lists;
  - dedupe;
  - limit;
  - tier isolation on the fused path;
  - single model load.
  The live-DB part is skipped without a DSN.

## 12. Commands for verification

```bash
python -m pytest tests/toolserver/test_retrieval_fusion.py tests/toolserver/test_tier_filters.py -q
```

## 13. Explicit non-goals

- No threshold calibration (P3-08). No reranker. No hosted embeddings.

## 14. Risks and unresolved questions

- Deploy memory (~1 GB). If it does not fit, the fallback needs a documented decision (PRD §8.2 forbids silently using a hosted embedder for confidential text).
- D-23.

## 15. Handoff deliverables for dependent agents

- P3-08: score fields and how to pass `min_score`.
- P5-03: memory and latency figures.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-05] Add pgvector search and RRF fusion to retrieval`
- **Body outline:** fusion spec; model-load rule; tests; perf numbers.
- **Labels:** `phase-2-mcp-knowledge`, `knowledge`
- **Depends on:** P2-04, P2-03  **Blocks:** P3-08

## 17. Definition of done

Merged with fusion and tier tests green; performance numbers posted; handoff
to P3-08.
