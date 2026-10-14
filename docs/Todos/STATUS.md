# Project status

_Generated 2026-10-14 12:25 UTC by `python scripts/update_status.py` from GitHub and the planning docs. Do not edit by hand; re-run the script._

Legend: **Done** = issue closed by a merged PR · **In review** / **Draft PR** = open PR with `Closes #n` · **In progress** = assigned, no PR yet · **Ready** = all prerequisites merged, nobody assigned · **Waiting** = prerequisites not merged yet. Phase 3 backend work may begin against fakes before its prerequisites merge (ADR-015).

## Summary

| Ready | Waiting |
|---|---|
| 7 | 31 |

**Next to merge:** P1-01 (#2) ArmorIQ organization, members, and agent setup — Ready: all prerequisites merged.

## Tasks in merge order

### Wave 1 — Phase 1 setup

| Issue | Task | Status | Details |
|---|---|---|---|
| #2 | P1-01 ArmorIQ organization, members, and agent setup | **Ready** | all prerequisites merged |
| #5 | P1-04 Signed `_ctx` envelope and tool-server request security | **Ready** | all prerequisites merged |
| #4 | P1-03 Trusted identity mapping and web demo session | **Ready** | all prerequisites merged |
| #3 | P1-02 ArmorIQ SDK surface verification and `ArmorIQGateway` | **Ready** | all prerequisites merged |
| #6 | P1-05 Tool-server public HTTPS deployment for experiments | **Ready** | all prerequisites merged |

### Wave 2 — Data and contracts

| Issue | Task | Status | Details |
|---|---|---|---|
| #12 | P2-02 Tool argument and result contracts | **Ready** | all prerequisites merged |
| #13 | P2-01 Neon schema, migrations, database roles, and grant verification | **Ready** | all prerequisites merged |

### Wave 3 — Data layer

| Issue | Task | Status | Details |
|---|---|---|---|
| #14 | P2-07 Hash-chained execution ledger, verifier, and `jti` idempotency | **Waiting** | needs #13 (P2-01) |
| #15 | P2-03 Synthetic seed corpus, ingestion, and local embeddings | **Waiting** | needs #13 (P2-01) |
| #16 | P3-01 Request, plan, decision, approval, and delivery persistence (`services/audit`) | **Waiting** | needs #13 (P2-01) |
| #17 | P3-03 Grounded responder, citation validation, and abstention | **Waiting** | needs #12 (P2-02) |

### Wave 4 — First tool

| Issue | Task | Status | Details |
|---|---|---|---|
| #18 | P2-04 Lexical retrieval with hard-coded tier filters | **Waiting** | needs #13 (P2-01), #15 (P2-03) |
| #19 | P2-06 `memory-mcp` product-tool wiring and `knowledge_search_team` | **Waiting** | needs #5 (P1-04), #12 (P2-02), #18 (P2-04), #14 (P2-07) |
| #20 | P3-02 Planner, plan validation, and topic-map tier selection | **Waiting** | needs #12 (P2-02), #16 (P3-01) |
| #21 | P2-05 Vector retrieval and reciprocal rank fusion | **Waiting** | needs #15 (P2-03), #18 (P2-04) |

### Wave 5 — E1 and remaining tools

| Issue | Task | Status | Details |
|---|---|---|---|
| #7 | P1-06 E1: a real planned tool call reaches the MCP server through ArmorIQ | **Waiting** | needs #2 (P1-01), #3 (P1-02), #6 (P1-05), #19 (P2-06) |
| #22 | P2-08 Elevated-tier search and source-read tools | **Waiting** | needs #19 (P2-06) |
| #23 | P2-10 `workspace-mcp`: `send_external_message` honeypot and optional `create_followup_task` | **Waiting** | needs #13 (P2-01), #19 (P2-06) |
| #24 | P2-09 Knowledge maintenance tools (drafts, Maya confirmation, supersession) | **Waiting** | needs #19 (P2-06) |
| #25 | P3-04 Request orchestrator (one plan per request, end to end) | **Waiting** | needs #3 (P1-02), #4 (P1-03), #16 (P3-01), #20 (P3-02), #17 (P3-03) |
| #26 | P3-05 Slack Socket Mode adapter (mentions, DMs, dedupe, `store this`) | **Waiting** | needs #4 (P1-03), #16 (P3-01) |
| #27 | P3-06 Safe delivery routing and HOLD notification | **Waiting** | needs #16 (P3-01) |
| #8 | P1-07 E2: off-plan and tampered invocation behavior | **Waiting** | needs #5 (P1-04), #7 (P1-06) |

### Wave 6 — Gates

| Issue | Task | Status | Details |
|---|---|---|---|
| #9 | P1-08 ArmorIQ policy and E5 setup/policy conformance | **Waiting** | needs #2 (P1-01), #7 (P1-06), #22 (P2-08), #23 (P2-10) |
| #10 | P1-09 E3: different outcomes for different members on the same tool | **Waiting** | needs #9 (P1-08) |
| #11 | P1-10 E4: HOLD → console approval → resume (and every failure path) | **Waiting** | needs #10 (P1-09) |
| #28 | P3-07 "Simulate hijacked model" mode and honeypot attempt routing | **Waiting** | needs #3 (P1-02), #23 (P2-10), #17 (P3-03), #25 (P3-04) |
| #29 | P4-01 Web request API (submit message, poll status) | **Waiting** | needs #4 (P1-03), #25 (P3-04) |
| #30 | P3-08 E17: grounding evaluation and `MIN_SCORE` calibration | **Waiting** | needs #15 (P2-03), #21 (P2-05), #17 (P3-03) |

### Wave 7 — UI and offline suites

| Issue | Task | Status | Details |
|---|---|---|---|
| #31 | P4-02 Frontend foundation: sign-in, session state, typed API client | **Waiting** | needs #4 (P1-03), #7 (P1-06), #10 (P1-09), #29 (P4-01) |
| #32 | P4-03 Team member web playground | **Waiting** | needs #31 (P4-02) |
| #33 | P4-04 Manager console and audit view (API + UI) | **Waiting** | needs #16 (P3-01), #20 (P3-02), #31 (P4-02) |
| #34 | P4-05 Frontend tests and web access-control verification | **Waiting** | needs #32 (P4-03), #33 (P4-04) |
| #35 | P3-09 Agent integration, prompt-injection, and failure-mode tests (offline) | **Waiting** | needs #25 (P3-04), #26 (P3-05), #27 (P3-06), #28 (P3-07) |

### Wave 8 — Release

| Issue | Task | Status | Details |
|---|---|---|---|
| #36 | P5-01 Cross-service integration and live end-to-end vertical slice | **Waiting** | needs #10 (P1-09), #22 (P2-08), #24 (P2-09), #35 (P3-09), #32 (P4-03), #33 (P4-04), #34 (P4-05) |
| #37 | P5-03 Public demo deployment and configuration | **Waiting** | needs #6 (P1-05), #36 (P5-01) |
| #38 | P5-02 End-to-end security regression (Arch §13, SC-01..SC-06) | **Waiting** | needs #8 (P1-07), #11 (P1-10), #30 (P3-08), #36 (P5-01) |
| #39 | P5-04 Demo script, README claims, video, and submission | **Waiting** | needs #38 (P5-02), #37 (P5-03) |

## Security gates

A gate passes only on a real, recorded run (docs/EXPERIMENTS.md). A closed issue alone is not proof.

| Gate | Check | Issues | Issue state | Recorded result |
|---|---|---|---|---|
| G0 | `_ctx` + tool-server checks; backend DB role denied | #5, #13 | Ready, Ready | — |
| G1 | E1: real call reaches MCP | #7 | Waiting | NOT RUN |
| G2 | E5: policy accepted, names verified | #9 | Waiting | NOT RUN |
| G3 | E3: per-member matrix | #10 | Waiting | NOT RUN |
| G4 | E4: HOLD → console approval → resume | #11 | Waiting | NOT RUN |
| G5 | E2: off-plan / tamper layers recorded | #8 | Waiting | NOT RUN |
| G6 | E17: grounding evaluation | #30 | Waiting | NOT RUN |
| G7 | Arch §13 security regression | #38 | Waiting | — |
