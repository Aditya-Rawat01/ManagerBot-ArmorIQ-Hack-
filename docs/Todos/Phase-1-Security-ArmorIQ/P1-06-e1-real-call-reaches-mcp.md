# P1-06 — E1: a real planned tool call reaches the MCP server through ArmorIQ

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | M (2–4 h; max 2 h diagnosis on failure, Arch §12) |
| Target window | 15 Oct AM (Day-1 gate, already late) |
| Gate role | **G1** (build gate with G3, Arch §12) |
| Status | Not started — `docs/EXPERIMENTS.md` E1 = NOT RUN |

## 1. Task ID and title

P1-06 — Run experiment E1 on the real ArmorIQ organization and the deployed HTTPS tool server, and record the result.

## 2. Purpose and expected outcome

E1 proves (or disproves) the core path: pinned SDK → ArmorIQ → public MCP
endpoint → real tool handler → ledger row. It also determines four things:

- the MCP credential path (D-06);
- whether the proxy's request shape fits the transport (U-2);
- whether `_ctx` and other `_` arguments arrive (D-05);
- the real `MCPInvocationResult.result` shape.

Expected outcome: `PASS`, `FAIL`, `BLOCKED ON ACCESS`, or `INCONCLUSIVE`,
recorded with evidence, plus the chosen fallback if not `PASS`.

## 3. Authoritative source references

- PRD §0.1, §1.1 SC-01, **§11 E1 exact procedure (steps 1–8)**, §11 "E1 status" (NOT RUN; must be run by the owner), §9 (credential path via E1), §12 (`ARMORIQ_MCP_CREDENTIALS` only if E1 proves it).
- Arch §6.3 (credential mechanism proven by E1), §12 E1 (pass: observed real request and matching server log/ledger record; fail: documented fallback, no fake hosted-proxy result), §12 build gate, §14.
- `ProxyLead_Day1_Experiments.md` E1 procedure and result record.

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributors: `Aditya-Rawat01` (tool-server logs and ledger query), `parthgoyal26-03-2005` (deployment access).

## 5. Prerequisites and dependencies

- Hard:
  - P1-01 (org, agent, Maya active);
  - P1-02 (gateway or SDK surface);
  - P1-05 (public URL);
  - **P2-06** (`knowledge_search_team` with ledger, which itself needs P1-04, P2-02, P2-04, P2-07, P2-03 seed, P2-01 schema).
- Decisions: **D-02** (probe file name) and **D-18** (probe uses gateway or SDK) block start.
- Blocks: P1-07, P1-08, P3-04 (merging real gateway integration), Phase 4 start (with G3).

## 6. Exact files/modules this task owns

- `scripts/{E1_PROBE}`: PRD name `probe_v2_e1_allowed.py`, Arch name `probe_e1_allowed.py` (D-02).
- `docs/EXPERIMENTS.md` § E1 (steward: this owner).

## 7. Existing files it may need to modify

- None. A needed code change (for example the ADR-006 transport fallback) becomes a separate ICR or task owned by the file's steward.

## 8. Shared interfaces and contracts

- Uses: the gateway (SHARED_INTERFACES §3) or the raw SDK (per D-18); `_ctx` (§5); ledger correlation fields (§9); `KnowledgeSearchResult` (§4).
- Produces evidence that may change: the §3 decision mapping, §5 transport status, §4 credential path, and §9 correlation fields (ICRs).

## 9. Step-by-step implementation requirements

Follow PRD §11 exactly:

1. Confirm the schema and one synthetic team-tier record exist (P2-01 / P2-03).
2. Confirm the deployed tool server (P1-05) runs the P2-06 build. Record the commit SHA.
3. Register `proxylead` (already done in P1-01) and the **`memory-mcp`** endpoint with the public URL, using the exact v2 names. Record whether the org limit counts endpoint registrations or server URLs.
4. The probe creates one plan containing only `knowledge_search_team` for Maya, using Maya's dedicated client.
5. Invoke through ArmorIQ. Capture the SDK response or exception (type and message, secrets removed), any console evidence, the tool-server logs, and the matching ledger row (by `jti` / `request_id`).
6. Inspect the credential the tool server actually received (bearer and/or `X-Armoriq-MCP-Auth`). Test the alternate path only as the SDK and docs direct. Record which works. Add `ARMORIQ_MCP_CREDENTIALS` only if proven needed.
7. Record the real `MCPInvocationResult.result` shape. Do not assume `.data`.
8. Record whether `_ctx` arrived and verified, and which `_` keys arrived (names only).
9. Mark `PASS` **only** if the real tool-server request and the ledger row correlate with the actual plan and invocation. Otherwise record the exact failure. Spend at most 2 hours on diagnosis, then apply the documented fallback (for example the ADR-006 transport, or the PRD v1 SDK/proxy fallback) and narrow the claims.
10. Use the result record template in `docs/EXPERIMENTS.md`. Store screenshots outside the repository and reference them.

## 10. Acceptance criteria

- The E1 record is complete:
  - UTC time, SDK version, registered names, endpoint URL (no secrets), plan/session IDs;
  - SDK return or exception, tool-server request observed (yes/no), ledger row and `jti`;
  - credential path, `_ctx` arrival, result shape, outcome, enforcement layer, evidence references, next action.
- The outcome is honest. `PASS` requires correlated request + ledger evidence.
- U-1 / U-2 / U-3 (D-05, D-06) are updated with the evidence, and ICRs are filed where SHARED_INTERFACES must change.

## 11. Required unit, integration, and security tests

- Live experiment only. It is never run in CI.
- The probe must refuse to run without the required env variables and must never print secrets.
- A mocked run does not count.

## 12. Commands for verification

```bash
python scripts/<E1 probe per D-02>.py      # from repo root, with .env containing real values
git diff -- docs/EXPERIMENTS.md
```

## 13. Explicit non-goals

- No per-member policy testing (E3). No off-plan testing (E2). No HOLD (E4).
- Do not register `workspace-mcp` unless needed for the quota observation (P1-08 registers it).

## 14. Risks and unresolved questions

- The proxy may not reach the endpoint, may use another credential, or may strip `_ctx`. Each requires a recorded fallback decision; never weaken tool-server checks.
- The Day-1 gate is already late. Every hour of slip moves G3 and the Phase 4 start.

## 15. Handoff deliverables for dependent agents

- All tasks: the E1 outcome and the fallback in force.
- P1-04 / P2-06: credential path and `_ctx` transport findings.
- P1-02 / P3-04: the actual result shape and mapping corrections.
- P5-03: the final credential configuration.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-06] E1: prove a planned ArmorIQ call reaches the MCP tool server`
- **Body outline:** PRD §11 procedure checklist; pass/fail definition; evidence fields; fallback rule; "mocks never pass".
- **Labels:** `phase-1-security`, `armoriq`, `experiment`, `gate`, `security`
- **Depends on:** P1-01, P1-02, P1-05, P2-06; D-02, D-18  **Blocks:** P1-07, P1-08, P3-04 (merge), P4-02 (with P1-09)

## 17. Definition of done

E1 record merged in `docs/EXPERIMENTS.md` with an honest outcome and evidence;
fallback decision recorded if not `PASS`; ICRs filed for any interface change;
G1 status posted on the issues of every dependent task (P1-07, P1-08, P3-04, P4-02).
