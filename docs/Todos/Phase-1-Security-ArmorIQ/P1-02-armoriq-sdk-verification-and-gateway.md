# P1-02 — ArmorIQ SDK surface verification and `ArmorIQGateway`

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | L (5–8 h) |
| Target window | 14 Oct (offline part), refined after E1–E4 |
| Gate role | Code path used by G1–G4 |
| Status | Not started |

## 1. Task ID and title

P1-02 — Verify the installed `armoriq-sdk==0.6.10` surface from source and implement `services/authorization/armoriq_gateway.py`, the only `armoriq_sdk` importer.

## 2. Purpose and expected outcome

The rest of the backend talks to ArmorIQ only through one gateway with a
stable Python interface. Outcome:

- a written record of the SDK classes, methods, parameters, return types, and exceptions this project uses, read from the installed package source (not guessed);
- a gateway that keeps one SDK client per member, creates exactly one plan per request, mints once, invokes steps in order, handles HOLD through the SDK's policy/delegation path, routes off-plan requests through the SDK plan check without executing them, and maps every result or exception to `ArmorIQDecision` / `ApprovalStatus` / `ToolInvocationResult`, failing closed.

Live behavior is **not** claimed here. E1–E4 confirm or correct the mapping.

## 3. Authoritative source references

- PRD §0.2, §0.6, §0.10; §3 (gateway is the only SDK importer); §4.1 (one client per member, serialize minting); §6.2 (off-plan path, labels); §7.5–7.8, §7.11; §7.1 (token lifetime); §9 (pin 0.6.10).
- Arch §0.1, §0.7; §5.1 (SDK uses `session.start_plan(...)`, intent token, `invoke_with_policy` adds `_policy_context`/`_delegation_id`/`_approvals`, no `jti`/revocation, token validity); §5.2; §5.3 (persist IDs, decision-source labels); §5.6 (real HOLD flow, background wait); §6.2 (`client.invoke(...)` off-plan path).

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributor/reviewer: `VTCodeCraft` (orchestrator consumer, P3-04).

## 5. Prerequisites and dependencies

- Hard: none. Offline work uses the installed SDK (`pip install -e ".[api,dev]"`).
- Soft: P1-04 (`ctx_envelope.sign`); stub the call behind the agreed interface until P1-04 merges. P1-01 (API key) for any manual smoke test.
- Decisions: D-03 (label set), needed to **finish**.
- Blocks: P1-06 (if D-18 says probes use the gateway), P3-04, P3-07.

## 6. Exact files/modules this task owns

- `services/authorization/armoriq_gateway.py` (new)
- `services/authorization/__init__.py` (docstring only; must stay import-free so `ctx_envelope` can be imported without `armoriq_sdk`)
- `tests/api/test_armoriq_gateway.py` (new; offline fakes only)
- `docs/SECURITY.md` (new; steward `samarth-chawla`): section "ArmorIQ SDK surface (verified offline from installed source)"

## 7. Existing files it may need to modify

- `apps/api/core/config.py` (steward `VTCodeCraft`): only if a documented variable is missing. All PRD §12 ArmorIQ variables already exist.
- `services/contracts/models.py` / `enums.py` (steward `VTCodeCraft`): add `ARMORIQ_POLICY_SOURCES` (ADR-026); any other change only via ICR, for example if E-evidence requires a new field.
- `tests/api/test_guardrails.py` (steward `VTCodeCraft`): extend the single-`armoriq_sdk`-importer scan to `scripts/` (ADR-018).

## 8. Shared interfaces and contracts

- Provides: `ArmorIQGateway`, `PlanHandle`, `StepOutcome` (SHARED_INTERFACES §3, PROPOSED; confirm by ICR before P3-04 relies on them).
- Consumes: `RequestContext`, `PlanProposal`, `PlannedToolCall`, `ArmorIQDecision`, `ApprovalStatus`, `ToolInvocationResult`, `DecisionSource` (FIXED), `ctx_envelope.sign` (P1-04).
- Settings: `ARMORIQ_API_KEY`, `ARMORIQ_AGENT_ID`, `PERSONA_*_EMAIL`, `TOKEN_VALIDITY_S`, `APPROVAL_TIMEOUT_MS`, `CTX_SECRET`.

## 9. Step-by-step implementation requirements

1. **SDK surface record.** Read the installed `armoriq_sdk` 0.6.10 source. In `docs/SECURITY.md`, record exact signatures, return types, and exception classes for:
   - client construction and `for_user`;
   - plan/session start, plan capture, and intent-token minting, including the token-validity parameter;
   - planned invocation (`invoke`), policy invocation (`invoke_with_policy` and its options for waiting, delegation timeout, and hold callbacks);
   - the invocation result object (`MCPInvocationResult.result`; PRD §11 E1 step 7 says do not assume `.data`);
   - the exception raised on plan mismatch.

   Mark every item "verified from source, not live".
2. **Clients.** Build one SDK client per configured member at gateway construction. Never reuse one client through `for_user` across personas (Arch §5.2). Guard plan creation and token minting per client with a lock. If the SDK is synchronous, run its calls in a worker thread and never block the event loop.
3. **`start_plan`.** Convert a validated `PlanProposal` into one SDK plan containing all steps in order, then mint once with `TOKEN_VALIDITY_S`. Return a `PlanHandle` (app `plan_id`, `armoriq_plan_id` **only if** the SDK exposes it, token expiry). Any failure produces a decision with `outcome=block` or `unverified` and a truthful `source`. Never raise an unhandled exception to the caller.
4. **`invoke_step`.** For step *i*, generate a fresh `jti`, sign `_ctx` (P1-04) with request/plan/step/tool/audience, add it to the arguments, and invoke under the same plan and token. Map the result:
   - success → `ALLOW` + `ToolInvocationResult` (status from the tool-server response; `executed` only with a ledger ID);
   - policy hold → `HOLD` + `ApprovalStatus(pending)`, then wait through the verified SDK path for up to `APPROVAL_TIMEOUT_MS`;
   - policy block → `BLOCK`;
   - tool-server error → `rejected_by_tool_server` with `tool_server_rule`.
5. **HOLD wait.** Run long waits in a background worker (Arch §5.6.3). Resolve to `approved` (resume through the SDK; same plan and step), or to `rejected` / `expired` / `timeout` / `unverified` (no execution). Never resume after token expiry and never re-plan (PRD §7.8).
6. **`attempt_off_plan`.** Route a non-planned tool request through the SDK's off-plan path (`client.invoke(...)`, Arch §6.2) so the SDK plan check records it. Return `BLOCK` with `armoriq_sdk_plan_check` **only** if the SDK raised its mismatch exception before any network call; otherwise label it per the evidence (`unknown` until E2). The gateway never executes it.
7. **Fail closed.** Missing API key, unknown member, network error, unexpected SDK type, or timeout → `unverified` with source `unknown`, or `armoriq_issuance` if minting failed at ArmorIQ. Never `allow`.
8. **Logging.** Log `request_id`, `trace_id`, `plan_id`, `step_index`, `jti`, outcome, and source. Never log tokens, API keys, `_ctx`, or unmasked emails.
9. Provide a `FakeArmorIQGateway` for other tasks' offline tests (in the test module or `tests/api/fakes/`; coordinate the location with P3-04).
10. **Probe entry points (ADR-018).** Probes and `scripts/check_setup.py` call only this gateway. Expose what E1–E5 need (for example a per-member planned call, `attempt_off_plan`, a deliberately misnamed tool for E5) so no script imports `armoriq_sdk`.
11. **Policy evidence set (ADR-026).** Add `ARMORIQ_POLICY_SOURCES = {armoriq_issuance, armoriq_policy_decision, armoriq_proxy_decision}` to `services/contracts/enums.py` and export it. Use it for every "ArmorIQ allowed, blocked or held" claim; `ARMORIQ_DECISION_SOURCES` stays only for the HOLD validator.

## 10. Acceptance criteria

- `docs/SECURITY.md` lists every SDK symbol the gateway uses, with file/line references in the installed package.
- `services/authorization/armoriq_gateway.py` is the only file importing `armoriq_sdk` (existing guardrail test passes).
- Offline tests prove each of the following:
  - one client per member;
  - per-client lock;
  - exactly one plan and one mint per request;
  - sequential steps under one handle;
  - every exception path maps to a non-ALLOW decision;
  - HOLD can only carry an ArmorIQ source;
  - an off-plan attempt never calls the tool server;
  - a fresh `jti` per invocation;
  - no secret appears in captured logs.
- The decision-mapping table is documented and labeled "provisional until E1–E4".
- `ARMORIQ_POLICY_SOURCES` exists, excludes `armoriq_sdk_plan_check`, `tool_server_rule`, `application_rule` and `unknown`, and the JSON Schema and TypeScript are regenerated (ADR-026).
- The guardrail test fails if any file under `scripts/` imports `armoriq_sdk` (ADR-018).

## 11. Required unit, integration, and security tests

`tests/api/test_armoriq_gateway.py` (all offline, names end with `__offline_fake_sdk`):

- `test_one_client_per_member__offline_fake_sdk`
- `test_mint_once_per_request__offline_fake_sdk`
- `test_concurrent_personas_do_not_share_identity__offline_fake_sdk` (3 concurrent requests)
- `test_sdk_exceptions_fail_closed__offline_fake_sdk` (parametrized over every exception class found in step 1)
- `test_hold_maps_to_pending_then_resolves__offline_fake_sdk`
- `test_off_plan_attempt_never_executes__offline_fake_sdk`
- `test_no_secrets_in_logs__offline_fake_sdk`
- Existing, extended to `scripts/`: `tests/api/test_guardrails.py::test_armoriq_sdk_is_imported_only_by_the_gateway`
- `tests/contracts/test_contracts.py`: `ARMORIQ_POLICY_SOURCES` contents (owned by the contract steward; added in this PR)

## 12. Commands for verification

```bash
python -m pip install -e ".[api,dev]"
python -m pytest tests/api/test_armoriq_gateway.py tests/api/test_guardrails.py -q
ruff check . && ruff format --check .
bash scripts/check.sh
```

## 13. Explicit non-goals

- No live claims. No orchestrator, planner, Slack notification, or UI.
- No in-app approval. No policy authoring (P1-08).
- No `_ctx` implementation (P1-04 owns it; the gateway only calls `sign`).

## 14. Risks and unresolved questions

- The SDK surface may differ from the docs' description. Record the actual surface and file an ICR on SHARED_INTERFACES §3 if needed.
- The `for_user` mutation and SDK thread-safety can leak identity across personas. Locks and one client per member mitigate this; E3 verifies.
- The token-validity parameter may be named differently or capped. E4 verifies (PRD §7.1).
- D-03 (labels); D-05 (whether `_ctx` survives the proxy).

## 15. Handoff deliverables for dependent agents

- P3-04: final gateway signatures (ICR merged), `FakeArmorIQGateway`, decision-mapping table.
- P1-06..P1-10: gateway entry points the probes can call (if D-18 chooses the gateway).
- P3-07: `attempt_off_plan` behavior and labeling rules.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-02] Verify ArmorIQ SDK 0.6.10 surface and implement ArmorIQGateway`
- **Body outline:** deliverables (SDK surface record, gateway, fakes); acceptance criteria; tests; evidence = SECURITY.md record + passing offline tests (explicitly not gate evidence).
- **Labels:** `phase-1-security`, `armoriq`, `security`, `backend`
- **Depends on:** — (soft: P1-04, P1-01)  **Blocks:** P1-06, P3-04, P3-07

## 17. Definition of done

Gateway merged with offline tests green in CI; SDK surface documented; ICR for
the gateway interface accepted by the P3-04 owner; no live claim made; handoff
posted.
