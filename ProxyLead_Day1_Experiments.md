# ProxyLead Day-1 ArmorIQ Experiments

**Date:** 13 October 2026  
**Owner:** project team  
**Status:** Run against the actual ArmorIQ organization and deployed HTTPS MCP server. No experiment may be marked passed from a mock.

## Access status

At creation, this ChatGPT session has no authenticated ArmorIQ console, project repository connection, API key, or deployed MCP endpoint available. Therefore **NOT RUN — E1 has not been executed**. This is a limitation of available access, not a platform failure. Run the steps below from the project environment as soon as the owner has credentials.

## Gate order

1. Create Neon schema and one synthetic team-tier record.
2. Deploy the minimal public HTTPS MCP server with JSON-RPC 2.0/SSE and bearer authentication.
3. Register `proxylead` and the first intended MCP endpoint. Confirm quota and exact names.
4. Run E1, then E2/E3/E4/E5.
5. Record timestamps, sanitized commands, SDK/proxy responses, relevant console evidence, tool-server logs, and matching ledger IDs.

## E1 — real planned call reaches MCP

**Question:** Does a planned SDK invocation reach the tool server through ArmorIQ, and which MCP credential path is used?

### Preconditions
- `armoriq-sdk==0.6.10`, Python 3.12.
- Active ArmorIQ org and API key.
- `proxylead` agent registered.
- One public HTTPS MCP endpoint registered with exact name.
- One invited/active Maya member email.
- Minimal seeded record and `knowledge_search_team` handler.
- Tool server logs and append-only ledger enabled.

### Procedure
1. Start the tool server and record its deployment URL/version.
2. Confirm `tools/list` works using the documented MCP protocol and the service credential.
3. In a minimal Python probe, instantiate the pinned SDK exactly as supported by the PRD v1.0 source notes; create a dedicated Maya client; start a proxy-mode session; create a one-step plan containing only `knowledge_search_team`; invoke that step.
4. Capture the SDK result and exception type/message without printing secrets.
5. Check tool-server logs for the inbound call and the ledger for the matching application `jti` / request ID. If no correlation identifier is passed by the SDK, record what stable metadata is available and do not fabricate a correlation.
6. Inspect the actual inbound auth mechanism. Only test `ARMORIQ_MCP_CREDENTIALS` or console-registered credentials as supported by the SDK/platform; record which path works.
7. Verify `MCPInvocationResult.result` and content shape; do not assume `.data`.
8. Mark PASS only if the real call path, actual tool result, and ledger row are correlated. If the SDK rejects before network, label that fact and continue with the documented fallback; it does not prove hosted-proxy enforcement.

### Result record
- Started at (UTC):
- SDK version:
- Agent name / MCP name as registered:
- Endpoint URL (no secrets):
- Session/plan ID:
- SDK return or exception:
- Tool-server request observed?:
- Ledger row ID / `jti`:
- Credential path observed:
- Outcome: `PASS` / `FAIL` / `BLOCKED ON ACCESS` / `INCONCLUSIVE`
- Evidence paths/screenshots:
- Next action:

## E2 — off-plan call

Plan a team search, then request a different action through the SDK. Record whether the SDK rejects client-side before network. Separately run the documented direct proxy probe only if its request shape is verified against the pinned SDK. Never call a client-side SDK mismatch a hosted-proxy block.

## E3 — per-user decisions

Use separate clients for Maya, Sam, and Priya. Test team, confidential, restricted, and honeypot tools. Expected matrix:

| Tier/tool | Maya | Sam | Priya |
|---|---|---|---|
| Team search | ALLOW | ALLOW | ALLOW |
| Confidential search | ALLOW | HOLD | BLOCK |
| Restricted search | ALLOW | BLOCK | BLOCK |
| `send_external_message` | BLOCK | BLOCK | BLOCK |

Record the layer that enforced each outcome and confirm no ledger execution on BLOCK/HOLD. Verify no client identity leaks under concurrent runs.

## E4 — HOLD and approval lifecycle

Trigger Sam's confidential search. Verify a real ArmorIQ delegation appears. Maya approves or rejects in the ArmorIQ console. Confirm SDK resume uses the same plan/step, no protected result is released before approval, and rejection/timeout/expiry does not execute. If token expires, fail and ask for a new user request; never auto-replan.

## E5 — setup/policy conformance

Check active member emails, exact registered agent/MCP/tool names, policy schema, org quotas, and tool-to-policy matching. Intentionally test a name mismatch and confirm fail-closed behavior.

## E17 — grounding evaluation

After retrieval is running, evaluate answerable, weak, unrelated, conflicting, and superseded-source questions. Calibrate `MIN_SCORE` using a held-out set and report false accepts/false abstains. E17 is not a prerequisite for the first E1 tool call, but it is required before making grounding/abstention claims.
