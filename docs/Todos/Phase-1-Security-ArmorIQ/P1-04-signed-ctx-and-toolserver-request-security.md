# P1-04 — Signed `_ctx` envelope and tool-server request security

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | M (3–5 h) |
| Target window | 14 Oct |
| Gate role | **G0**: no product tool may be exposed before this merges |
| Status | Not started |

## 1. Task ID and title

P1-04 — Implement the backend-signed `_ctx` identity envelope and the tool server's per-call verification: service credential, `_ctx` signature, audience, expiry, replay, and underscore-key stripping.

## 2. Purpose and expected outcome

The tool server must reject any call that does not carry valid service
authentication **and** a valid signed context, whoever sends it (Arch §6.3,
§13). Outcome:

- a stdlib-only module that signs and verifies `_ctx`, shared by backend and tool server;
- `services/toolserver/security.py` exposing `verify_tool_call(...)`, which every product tool handler calls before any logic (wired by P2-06);
- a written finding on how `mcp==2.3.0` delivers underscore-prefixed arguments to handlers (D-05, code-inspection half).

## 3. Authoritative source references

- PRD §0.5 (tool server independently verifies service auth, signed `_ctx`, expiry, idempotency, strict schemas, scope, tier), §9 (`_ctx` contents; validate signature, audience, expiry, idempotency/replay; strip underscore SDK metadata and never use it as approval; credential path via E1), §12 (`CTX_SECRET`).
- Arch §5.1 (strip underscore control parameters; application `jti`; dedupe until expiry), §6.3 (envelope fields; signature/expiry/audience/replay; direct calls rejected), §5.4 layer 6, §13 (forged `_delegation_id`, `_approvals`, `_policy_context`, `_ctx`, expired context, duplicate `jti`).

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributor: `Aditya-Rawat01` (wiring in P2-06; replay store in P2-07). Reviewer: `Aditya-Rawat01`.

## 5. Prerequisites and dependencies

- Hard: none.
- Soft: P2-07 provides the ledger-backed `seen_jti`. Implement against a `ReplayGuard` protocol with an in-memory test double; the production binding lands with P2-07.
- Decisions:
  - D-04 (field set) is needed to **finish**.
  - D-05 code-inspection half is resolved **in this task**. The proxy half comes from E1/E2 and may trigger a follow-up ICR.
  - D-06 (credential path): keep the bearer guard until E1 decides.
- Blocks: **P2-06 (hard)**, P1-07.

## 6. Exact files/modules this task owns

- `services/authorization/ctx_envelope.py` (new; imports only the standard library and `pydantic`)
- `services/toolserver/security.py` (existing; ownership moves to `samarth-chawla`)
- `tests/api/test_ctx_envelope.py` (new: signing side)
- `tests/toolserver/test_security_ctx.py` (new: verification side; runs without `armoriq_sdk` installed)
- `tests/toolserver/test_auth.py` (existing bearer tests; keep green)
- `docs/SECURITY.md` section "Tool-server request verification" (steward `samarth-chawla`)

## 7. Existing files it may need to modify

- `services/toolserver/config.py` (steward `Aditya-Rawat01`): `ctx_secret` already exists. Only add an ICR-approved setting, if any.
- `services/toolserver/main.py` (steward `Aditya-Rawat01`): only if the bearer guard wiring changes after E1.

## 8. Shared interfaces and contracts

- Provides: the `_ctx` envelope (SHARED_INTERFACES §5), `verify_tool_call(endpoint, tool, raw_arguments) -> VerifiedCall`, and rejection codes (§4). Confirm by ICR with the P2-06 and P1-02 owners before they rely on it.
- Consumes: `ReplayGuard.seen_jti(jti)` (P2-07, §9); `ToolName`, `McpEndpoint` (FIXED).

## 9. Step-by-step implementation requirements

1. `CtxEnvelope` (Pydantic, `extra="forbid"`, frozen) with the D-04 field set: request, trace, workspace, requester member, app role, ArmorIQ email if D-04 keeps it, plan ID, step index, tool, `aud`, `iat`, `exp`, `jti`.
2. `sign_ctx(envelope, secret) -> str`: canonical JSON (sorted keys, no whitespace, UTF-8), HMAC-SHA256, compact `base64url(payload).base64url(mac)`. `verify_ctx(token, secret, *, expected_aud, expected_tool, now, leeway_s)` raises a typed error per failure (bad format, bad signature, wrong audience, wrong tool, not yet valid, expired). Use a constant-time compare. Empty or unset secret means **always reject**.
3. In `security.py` keep `BearerTokenGuard` as the service credential (ADR-010) until E1 shows the proxy's credential path (D-06).
4. `verify_tool_call(endpoint, tool, raw_arguments)`:
   1. Pop `_ctx`. If it is missing, reject `invalid_context`.
   2. Verify it with `expected_aud=endpoint` and `expected_tool=tool`.
   3. Replay check: `ReplayGuard.seen_jti(jti)` → reject `replay`.
   4. Remove **every** remaining key starting with `_`. Keep only their **names** (never values) as trace metadata.
   5. Return `VerifiedCall(ctx, business_arguments, trace_metadata)`.
   No underscore field ever influences authorization.
5. Rejection results use the codes in SHARED_INTERFACES §4 and never echo argument values or protected details.
6. **D-05 code inspection.** Determine from the installed `mcp` 2.3.0 source how `tools/call` arguments reach a handler registered on `MCPServer`, in particular whether unknown or `_`-prefixed keys are passed, dropped, or rejected. Record the finding in `docs/SECURITY.md`. If `_ctx` cannot reach the handler, specify (ICR with P2-06) where `verify_tool_call` must hook so that it sees the raw arguments. The fail-closed property must hold.
7. Never log the `_ctx` token, the secret, or the email. Log `jti`, `request_id`, and the outcome.

## 10. Acceptance criteria

Each case below is a rejection, with no handler logic run:

- valid bearer but no `_ctx`;
- bad signature;
- wrong `aud`;
- wrong `tool`;
- expired;
- duplicate `jti`;
- unset `CTX_SECRET`.

Also:

- Forged `_approvals`, `_delegation_id`, and `_policy_context` are stripped and change nothing. A test proves the business arguments and the decision are identical with and without them.
- The toolserver test job imports `services.authorization.ctx_envelope` without `armoriq_sdk` installed.
- The D-05 code-inspection finding is recorded. Any needed hook change is agreed by ICR.

## 11. Required unit, integration, and security tests

- `tests/api/test_ctx_envelope.py`: sign/verify round trip; canonicalization stable; tamper detection; wrong secret.
- `tests/toolserver/test_security_ctx.py`:
  - `test_missing_ctx_rejected`
  - `test_tampered_ctx_rejected`
  - `test_wrong_audience_rejected`
  - `test_wrong_tool_rejected`
  - `test_expired_ctx_rejected`
  - `test_duplicate_jti_rejected`
  - `test_forged_underscore_fields_grant_nothing`
  - `test_unset_secret_rejects_everything`
  - `test_ctx_module_does_not_import_armoriq_sdk`
- `tests/toolserver/test_auth.py` (existing): bearer missing → 401; unset → 503.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_ctx_envelope.py -q
python -m pip install -e ".[toolserver,dev]" && python -m pytest tests/toolserver -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No strict per-tool business schemas (P2-02 / P2-06).
- No ledger implementation (P2-07).
- No claim about proxy behavior (E1/E2).
- No permissive or "observe-only" mode: verification always fails closed.

## 14. Risks and unresolved questions

- D-05: the MCP SDK or the ArmorIQ proxy may drop `_ctx`. If E1 shows the proxy strips it, the team needs a fallback decision and must not weaken verification.
- D-04: whether a raw email inside `_ctx` is acceptable.
- Clock skew between backend and tool server. Use a small leeway; document it.

## 15. Handoff deliverables for dependent agents

- P2-06: `verify_tool_call` signature, exact hook placement (per the D-05 finding), rejection codes, how to map them to MCP errors.
- P1-02: `sign_ctx` signature and required fields.
- P2-07: `ReplayGuard` protocol to implement.
- P1-06 / P1-07: what tool-server logs show for each rejection (for evidence).

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-04] Implement signed _ctx envelope and tool-server request verification`
- **Body outline:** deliverables; rejection matrix; D-05 inspection; acceptance criteria; evidence = passing tests + SECURITY.md finding.
- **Labels:** `phase-1-security`, `security`, `mcp`, `gate`
- **Depends on:** — (soft: P2-07)  **Blocks:** P2-06, P1-07

## 17. Definition of done

Merged with tests green in both API and toolserver CI jobs; D-05 inspection
recorded; ICR for `verify_tool_call` accepted by `Aditya-Rawat01`; handoff
posted.
