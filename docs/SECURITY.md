# ProxyLead security notes

Implementation evidence for the controls in `ProxyLead_Architecture_v2.1.md` §13.
Each section names the task that added it. Later tasks append their sections;
nothing here claims live ArmorIQ behavior (gates E1–E5, E17 are recorded in
`docs/EXPERIMENTS.md`).

Steward: `samarth-chawla`. Sections are appended by their owning tasks in merge
order; later PRs append after earlier ones and never rewrite another task's section.

- "Tool-server request verification" — P1-04.
- "ArmorIQ SDK surface" — P1-02 (appends).
- Policy facts — P1-08 (appends).
- "ArmorIQ SDK telemetry" — security follow-up to the PR #72 review (appends).

## Tool-server request verification (P1-04)

Every product tool call must carry valid service authentication **and** a valid
backend-signed `_ctx` envelope, whoever sends it (Arch §6.3).

- Signing: `services/authorization/ctx_envelope.py` (`sign_ctx`, HMAC-SHA256
  over canonical JSON, `base64url(payload).base64url(mac)`). Field set per
  D-04/ADR-020: no raw member email, only `armoriq_member_email_ref`. The module
  imports only the standard library and pydantic, so the toolserver job imports
  it without `armoriq_sdk`.
- Verification: `services/toolserver/security.py::verify_tool_call`, called by
  every product tool handler before any logic (wired by P2-06). Rejects with
  `invalid_context` (missing/bad `_ctx`, wrong audience/tool, unset secret),
  `context_expired`, or `replay` (via the `ReplayGuard` protocol; ledger-backed
  binding lands in P2-07). Forged `_approvals`, `_delegation_id`,
  `_policy_context` and any other `_`-prefixed key are stripped and change
  nothing; only their names are kept as trace metadata. Rejection messages never
  echo argument values. The `_ctx` token, the secret, and emails are never
  logged; logs carry `jti`, `request_id`, tool, and outcome only.
- The bearer credential guard stays until E1 decides the credential path (D-06).

### D-05 code-inspection finding (P1-04; proxy half deferred to E1/E2)

Read from the installed `mcp==2.3.0` source and confirmed by direct probes:

- `ToolManager.call_tool` (`tool_manager.py:75`) delegates to `Tool.run`
  (`tools/base.py:132`), which validates arguments with
  `FuncMetadata.validate_arguments` (`utilities/func_metadata.py:139`) against
  a pydantic model whose config is `extra="ignore"`
  (`ArgModelBase`, `func_metadata.py`) — so **undeclared keys, including every
  `_`-prefixed key, are silently dropped** before the handler body runs
  (probed: handler never observes `_ctx`).
- Declaring a `_`-prefixed parameter is rejected at registration:
  `InvalidSignature: Parameter _ctx of ... cannot start with '_'` (probed).
- Consequence: `_ctx` **cannot reach a tool handler body through the SDK path**,
  and forged `_` fields cannot either (fail closed by construction). P2-06 must
  therefore call `verify_tool_call(endpoint, tool, raw_arguments)` with the raw
  arguments **above** SDK validation (hook at `MCPServer.call_tool` /
  `ToolManager.call_tool` level in `services/toolserver/servers.py`, or an
  equivalent pre-validation wrapper); otherwise `_ctx` is lost and every call
  rejects with `invalid_context`. Whether `_ctx` survives the ArmorIQ proxy is
  the deferred half, decided by E1 (P1-06) and E2 (P1-07); a negative result
  triggers a follow-up ICR, never weaker verification.
- P2-06 wiring: the hook is the SDK's supported `tools/call` interceptor
  (`mcp.server.mcpserver.Extension.intercept_tool_call`, passed as
  `MCPServer(extensions=[...])` in `services/toolserver/servers.py`), which
  sees the raw `params.arguments` before any argument model. It answers every
  `ToolName` call itself (no `ToolName` is ever forwarded to the SDK tool
  path); the registered product-tool bodies are canaries that raise if the
  interceptor is ever bypassed.

## ArmorIQ SDK surface (verified offline from installed source)

P1-02. Package `armoriq-sdk==0.6.10` (pinned; `armoriq_sdk/_version.py:16`
`__version__ = "0.6.10"`), read from
`.venv/Lib/site-packages/armoriq_sdk/`. Every item below is **verified from
source, not live**: live behavior is confirmed or corrected only by E1–E4
(`docs/EXPERIMENTS.md`). Do not treat this record as gate evidence.

### Client (`armoriq_sdk/client.py`)

| Symbol | Location | Verified behavior relevant to the gateway |
|---|---|---|
| `ArmorIQClient` | `client.py:72` | Single entry point. Constructor `__init__` (`client.py:103`) takes `api_key`, `agent_id`, `user_id`, `timeout=30.0`, `max_retries=3`, `mcp_credentials`, endpoint overrides. Missing/invalid key raises at construction or first call (`_validate_api_key`, `client.py:472`). |
| `for_user(user_email)` | `client.py:374` | Returns `ArmorIQUserScope` (`client.py:1791`). The scope docstring warns `user_email_override` is one shared field, so interleaved requests overwrite each other — the reason the gateway keeps **one client per member** (R-12) and never shares a `for_user` scope across personas. `ArmorIQUserScope.start_session` (`client.py:1817`) binds the email to the new session; `resolve` (`client.py:1828`) fetches membership. |
| `start_session(opts)` | `client.py:302` | Returns `ArmorIQSession`. Gateway path uses one session per request. |
| `capture_plan(llm, prompt, plan, metadata)` | `client.py:551` | Requires a dict plan with a `steps` key; returns `PlanCapture`. Raises `ValueError` otherwise. |
| `get_intent_token(plan_capture, policy, validity_seconds=60.0)` | `client.py:580` | Mints the intent token (backend `POST …/iap/sdk/token`, idempotency key per call). **Token validity is configured here**, not on `start_plan`. `validity_seconds` maps to the request's `expires_in`. HTTP ≥400 surfaces as `PolicyBlockedException` (403/policy denial) or a transport exception. |
| `invoke(mcp, action, intent_token, params, merkle_proof, user_email)` | `client.py:727` | Planned invocation through the proxy. Raises `TokenExpiredException` before any network call when `intent_token.is_expired`. Posts `{mcp, action, params, arguments, intent_token, merkle_proof, _iam_context}` with `Accept: application/json, text/event-stream`. Returns `MCPInvocationResult`. |
| `invoke_with_policy(mcp, action, intent_token, params, options)` | `client.py:1661` | Policy path the gateway uses for every step. Always injects `_policy_context` (`PolicyContext` dump) into params. Re-raises `PolicyBlockedException`. On `PolicyHoldException` with `requiresApproval`: calls `options.on_hold(HoldInfo)`; if `wait_for_approval` and `user_email` are set, creates a delegation request (`create_delegation_request`, `client.py:1544`), polls `get_delegation_status` (`client.py:1597`) by delegation ID with backoff (3 s → 15 s cap), and on `approved` retries `invoke` with `_delegation_id` + `_approvals` added, then `mark_delegation_executed` (`client.py:1617`). `rejected`/`expired` raise `DelegationException`; deadline expiry raises `DelegationException` (timeout). |
| `create_delegation_request(params)` | `client.py:1544` | Takes `DelegationRequestParams`, returns `DelegationRequestResult{delegation_id, status, expires_at}`. |
| `get_delegation_status(delegation_id)` | `client.py:1597` | Returns `pending \| approved \| rejected \| expired \| none`. Raises `DelegationException` on HTTP errors (fail closed, never loops silently). |
| `mark_delegation_executed(user_email, delegation_id, plan_id)` | `client.py:1617` | Idempotency marker after an approved resume executes. |
| `complete_plan / update_plan_status` | `client.py:1638/1642` | Plan lifecycle bookkeeping. Gateway does not rely on them for authorization. |

### Session (`armoriq_sdk/session.py`)

| Symbol | Location | Verified behavior relevant to the gateway |
|---|---|---|
| `SessionMode` | `session.py:52` | `Literal["local", "proxy", "sdk"]`. Gateway uses `proxy` (live path); `local` warms the IAP public key for local verification. |
| `SessionOptions` | `session.py:116` | `{tool_name_parser, default_mcp_name, validity_seconds=3600, llm="agent", mode="local", observability, session_id, context_id}`. Gateway sets `validity_seconds=TOKEN_VALIDITY_S` and `mode="proxy"`. |
| `ArmorIQSession` | `session.py:165` | Per-request session. Gateway creates one session per member client per request via `client.start_session` / `for_user(...).start_session`. |
| `start_plan(tool_calls, goal)` | `session.py:393` | Captures the plan and mints. Delegates to `_start_plan_impl` (`session.py:447`): builds the plan dict via `build_plan_from_tool_calls`, calls `capture_plan`, then `get_intent_token(validity_seconds=self._validity_seconds)`. Raises `ValueError` on an empty call list. Same-plan repeat returns the cached token (gateway still calls it exactly once per request). |
| `check(tool_name, tool_args, user_email)` | `session.py:986` | SDK-side check returning `EnforceResult`. Not an authorization proof; gateway uses the `client.invoke` mismatch path for off-plan labeling. |
| `enforce / enforce_sdk / enforce_local` | `session.py:866/735/497` | Enforcement variants returning `EnforceResult{allowed, action: allow\|block\|hold, reason, delegation_id, matched_policy, obligations}` (`session.py:143`). Gateway does not route product invocations through these; recorded so reviewers can tell them apart from the proxy path. |
| `dispatch(tool_name, tool_args)` | `session.py:1155` | Local dispatch. Never used by the gateway for product tools. |
| `await_approval(delegation_id, timeout, interval, user_email)` | `session.py:1294` | Async approval waiter returning the terminal status string. Gateway HOLD waits run in a background worker and poll by delegation ID; see the `invoke_with_policy` row for the exact resume contract. |

### Plan helpers (`armoriq_sdk/plan_builder.py`)

| Symbol | Location | Verified behavior |
|---|---|---|
| `build_plan_from_tool_calls(tool_calls, goal, tool_name_parser, default_mcp_name)` | `plan_builder.py:57` | Builds `{goal, steps: [{action, tool, mcp, params, description}]}`. Step identity is `(mcp, action)`; the gateway feeds one `ToolCall` per planned step in order. |
| `default_tool_name_parser(default_mcp_name)` | `plan_builder.py:22` | Splits `mcp__action` / bare action names. Gateway passes bare tool names with an explicit per-step MCP. |
| `hash_tool_calls(tool_calls)` | `plan_builder.py:81` | Canonical plan hash used for the session's same-plan cache. |
| `ToolCall{name, args}` | `models.py:235` | The plan input shape. |

### Models (`armoriq_sdk/models.py`)

| Symbol | Location | Verified shape |
|---|---|---|
| `IntentToken` | `models.py:14` | `{token_id, plan_hash, plan_id, signature, issued_at, expires_at, policy, composite_identity, client_info, policy_validation, step_proofs, total_steps, raw_token, jwt_token, policy_snapshot, subtree_delegation}`. `is_expired` / `time_until_expiry` are properties the gateway checks before invoking. No `jti` claim — the application `jti` stays application-generated (Arch §5.1). |
| `PlanCapture` | `models.py:57` | `{plan, llm, prompt, metadata}`. |
| `MCPInvocationResult` | `models.py:87` | `{mcp, action, result: Any, status="success", execution_time, verified, metadata}`. **`.result` is `Any`; do not assume a `.data` field** (PRD §11 E1 step 7). |
| `PolicyContext` | `models.py:157` | `{is_financial, transaction_type, amount, recipient_id}`. Injected as `_policy_context`; stripped by the tool server before business validation (P1-04). |
| `HoldInfo` | `models.py:166` | `{delegation_id?, reason, amount?, approval_threshold?, tool, mcp}`. Delivered to `on_hold`; the gateway persists `delegation_id` when present. |
| `InvokeOptions` | `models.py:179` | `{wait_for_approval?, delegation_timeout_ms?, on_hold?, user_email?, requester_role?, requester_limit?}`. Gateway sets `wait_for_approval=True`, `delegation_timeout_ms=APPROVAL_TIMEOUT_MS`, `user_email` to the member email. |
| `DelegationRequestParams / DelegationRequestResult` | `models.py:192/212` | Request `{tool, action, arguments?, amount?, requesterEmail!, requesterRole?, requesterLimit?, domain?, targetUrl?, planId?, intentReference?, merkleRoot?, reason?}`; result `{delegationId!, status, expiresAt}`. |
| `McpCredential / McpCredentialMap` | `models.py:246+` | Console-registered credential shapes. Which credential path the proxy uses is decided by E1 (D-06); the gateway passes `mcp_credentials` through only if configured and never logs them. |

### Exceptions (`armoriq_sdk/exceptions.py`; all extend `ArmorIQException`, `exceptions.py:8`)

| Exception | Location | Gateway mapping (provisional until E1–E4) |
|---|---|---|
| `IntentMismatchException(action?, plan_hash?)` | `exceptions.py:30` | Off-plan signal. If raised by `client.invoke` before any network call → `BLOCK` + `armoriq_sdk_plan_check`. Never an ArmorIQ policy claim. |
| `PolicyBlockedException(enforcement_action?, reason?, metadata?)` | `exceptions.py:114` | Policy block → `BLOCK` + `armoriq_policy_decision`, keeping `enforcement_action` verbatim. Counts as ArmorIQ evidence only with an evidence reference and proof the tool server was not reached (ADR-026). |
| `PolicyHoldException(delegation_context?, metadata?)` | `exceptions.py:132` | Policy hold → `HOLD` + `ApprovalStatus(pending)`. HOLD is only ever carried with an ArmorIQ source. Surfaces to the gateway only when the SDK did not wait (no `wait_for_approval`/`user_email`) or delegation creation failed; the normal waited path resolves inside `invoke_with_policy` (see `DelegationException` row and the wait-loop rows below). |
| `InvalidTokenException(token_id?)` | `exceptions.py:14` | Unverifiable token → `UNVERIFIED` + `unknown` (or `armoriq_issuance` when minting itself failed at ArmorIQ). |
| `TokenExpiredException(expired_at?)` | `exceptions.py:49` | Extends `InvalidTokenException`. Expired at use → `UNVERIFIED`; never resume, never re-plan (PRD §7.8). |
| `RevokedTokenError(token_id?, reason?)` | `exceptions.py:161` | Proxy refused the token (HTTP 401 + `X-Armoriq-Revoked`) → `UNVERIFIED`. Retry only via a fresh user request (new plan), never silently. |
| `MCPInvocationException(mcp?, action?, status_code?)` | `exceptions.py:67` | Transport/tool-server failure → `UNVERIFIED` + `unknown`, mapped to `ToolInvocationResult(status=error/not_reached)`. A tool-server rejection shape maps to `rejected_by_tool_server` + `tool_server_rule` — never an ArmorIQ label. |
| `DelegationException(target_agent?, delegation_id?, status_code?)` | `exceptions.py:91` | Carries a structured `delegation_id`. From the waited path (`client.py:1761-1788`): `rejected`/`expired` arrive as `DelegationException("Delegation {status}", delegation_id=...)` → `BLOCK` + `armoriq_policy_decision` + `ApprovalStatus(rejected/expired)`; deadline expiry arrives as `DelegationException("Delegation approval timed out after Ns", delegation_id=...)` → `UNVERIFIED` + `unknown` + `ApprovalStatus(timeout)`. Any other shape → `UNVERIFIED` + `unknown`. Status words are matched only in these verified shapes. |
| `ConfigurationException` | `exceptions.py:148` | Bad SDK setup (missing key/endpoints) → `UNVERIFIED` + `unknown` at construction; the app stays importable and `/health` stays green. |

### Decision-mapping table (provisional until E1–E4)

| SDK result / exception | `ArmorIQDecision` | `ApprovalStatus` | `ToolInvocationResult` |
|---|---|---|---|
| `invoke_with_policy` returns `MCPInvocationResult`, no hold observed | `ALLOW` + `armoriq_policy_decision` (E1/E3 confirm the source is really the policy) | none | `executed` only with `tool_server_reached=True` + `ledger_entry_id` (read from the result's `_meta.ledger_entry_id`, SHARED_INTERFACES §4); else `unknown` (never claim ledger-backed execution without the reference). A result with `isError: true` (or SDK `status="error"`) is never this row: see "Tool-server error/rejection shape" |
| `invoke_with_policy` returns success after `on_hold` fired with a `delegation_id` | `ALLOW` + `armoriq_policy_decision` | `approved` with that `delegation_id` (`requested_at` = hold-callback time, `resolved_at` = resume time) | `executed` only on ledger evidence |
| `PolicyBlockedException` | `BLOCK` + `armoriq_policy_decision`, `enforcement_action` kept verbatim | none | `not_reached`, `tool_server_reached=False` |
| `PolicyHoldException` (SDK did not wait) → rejected/expired/timeout/unverifiable | terminal non-ALLOW (`BLOCK` + policy source for rejected/expired; `UNVERIFIED` + `unknown` otherwise) | `rejected`/`expired`/`timeout` where the shape validates | `not_reached`, never executed |
| `DelegationException("Delegation rejected"/"Delegation expired", delegation_id=...)` (`client.py:1779-1784`) | `BLOCK` + `armoriq_policy_decision` | `rejected`/`expired` with that `delegation_id` | `not_reached`, never executed |
| `DelegationException("Delegation approval timed out ...", delegation_id=...)` (`client.py:1785-1788`) | `UNVERIFIED` + `unknown` | `timeout` with that `delegation_id` where it validates | `not_reached`, never executed |
| Any other `DelegationException` | `UNVERIFIED` + `unknown` | none | `not_reached`, never executed |
| `IntentMismatchException` before network | `BLOCK` + `armoriq_sdk_plan_check` | none | `not_reached`, tool server never called |
| Mint/transport/unknown failure | `UNVERIFIED` + `unknown` (`armoriq_issuance` only when minting failed at ArmorIQ) | none or `unverified` | `not_reached`/`error`/`unknown` |
| Tool-server error/rejection shape (`isError: true`, text `{code, message}`; SHARED_INTERFACES §4) | `BLOCK` + `tool_server_rule` (code outside the §4 set → `internal`) | none | `rejected_by_tool_server` with `error_code`; `ledger_entry_id` = the rejection's own row, or the original entry on `replay` |

Rules that never change: `armoriq_sdk_plan_check` outcomes are BLOCK-only;
HOLD is carried only with an ArmorIQ source (`ARMORIQ_DECISION_SOURCES`);
`armoriq_sdk_plan_check`, `tool_server_rule`, `application_rule`, and `unknown`
are never reported as ArmorIQ policy BLOCK/HOLD (ADR-026, R-03). E1–E4 correct
this table via ICR where the evidence differs.

### Gateway `_ctx` signing and offline doubles (P1-02, integrated with P1-04)

- The gateway signs every step's `_ctx` with P1-04's `sign_ctx` by default
  (`sign_ctx_fields` in `services/authorization/armoriq_gateway.py`). It converts
  the gateway's `iat`/`exp` datetimes to epoch seconds at that boundary;
  `CtxEnvelope` validation is unchanged. The envelope carries
  `armoriq_member_email_ref` (`PERSONA_*_EMAIL`), never the raw email (ADR-020).
- A missing `CTX_SECRET` or a signing failure makes the step `UNVERIFIED` +
  `unknown` before any SDK call (fail closed).
- Offline round trip only: `tests/api/test_armoriq_gateway.py` shows the token
  passes `verify_ctx` for the step's endpoint and tool. Whether `_ctx` survives
  ArmorIQ's proxy is still decided by E1/E2 (**NOT RUN**).
- `FakeArmorIQGateway` lives in `tests/fakes/armoriq_gateway.py`, not in production
  code: its decisions carry ArmorIQ source labels for offline tests and are never
  evidence. `tests/api/test_guardrails.py` fails if `apps/`, `services/` or
  `scripts/` import `tests` or name the fake.

## ArmorIQ SDK telemetry (observability shipper)

Raised by the PR #72 (E1 probe) security review. Read from the installed
`armoriq-sdk==0.6.10` source in a throwaway venv, then checked with an offline
probe: sockets blocked, every HTTP call answered by an in-memory transport,
fake `ak_test_` key, `example.test` email. **ArmorIQ was never called.** This is
source evidence, not live evidence.

**What the SDK does by default.** Each `ArmorIQSession` builds an
`ObservabilityRecorder` (`session.py:221` `_init_observability`). Unless it is
disabled, that recorder starts a daemon thread named
`armoriq-observability-shipper` and registers an `atexit` drain
(`observability/shipper.py:83`). Ended traces are POSTed to
`{backend_endpoint}/observability/spans` (`shipper.py:198`; production backend
`https://api.armoriq.ai`, `_build_env.py:36`) with the service API key in
`X-API-Key`. A send happens on the 5 s flush loop, on `session.close()`, and at
interpreter exit. Failures are retried 3 times with 0.5/1/2 s backoff and a
10 s timeout per attempt (`shipper.py:39-41`), then dropped. Failures are never
raised to the caller.

**What the gateway's path sends.** The gateway uses one session per mint
(`_mint_token`: `start_plan`, then `close`). The probe captured exactly one POST
for one mint:

- `product: "armoriq-sdk"`, a random `sessionId`, and one `iap.plan` trace with
  one `iap.plan.start` span;
- `agentId: "proxylead"`, `mode: "proxy"`, `toolCount`;
- the plan **goal** twice (`attributes.input` and span `goal`). The goal is
  `PlanProposal.summary`, which embeds up to 80 characters of each search
  query (`apps/api/planner.py` `_step_purpose`). That is request content.
- `userId: null`. The SDK forwards `userId` only when it is a UUID
  (`session.py:108` `_valid_user_id`), so the member email used by
  `for_user(email)` is **not** sent as the user id. The review's "email as user
  id" claim does not hold for 0.6.10 on this path. No email and no tool
  arguments appeared in the captured body.

Other session paths the gateway does **not** use today do put more on the
trace: `check` and `enforce_sdk` add the raw `userEmail` (`session.py:758`,
`:1012`), and the enforce/dispatch spans add tool arguments (truncated to 4 KB).
So the exposure would grow without any setting change if a later edit used
those paths.

**Cost on the request path.** `close()` stops the shipper and flushes
synchronously. `_mint_token` runs it in the worker thread while the per-member
lock is held. With the telemetry endpoint failing instantly, the probe measured
`close()` blocking for 3.5 s (backoff only). With real timeouts it can block for
about 43.5 s (4 × 10 s plus backoff).

**How to disable it (both verified offline).**

- Env `ARMORIQ_OBSERVABILITY` set to `false`, `0`, `no` or `off` (trimmed,
  case-insensitive; `session.py:254`). The SDK reads **only the process
  environment**. pydantic-settings reads `.env` without exporting it, and
  compose maps variables one by one (`docs/DOCKER.md` §5). So a value placed
  only in `.env` never reaches the SDK.
- `SessionOptions(observability=ObservabilityConfig(enabled=False))`. No
  recorder or thread is created, and this wins over the env var.

**Decision: off by default in ProxyLead.** Telemetry is a second, optional
copy of request-derived text at ArmorIQ. No ProxyLead feature reads it. The
authorization calls (`capture_plan`, `get_intent_token`, `invoke_with_policy`)
are the deliberate ArmorIQ egress, and the audit trail is ProxyLead's own.
Turning telemetry off follows the PRD privacy rules: data minimisation, emails
masked in UI and logs (PRD §4.1), and no egress beyond what authorization needs
(PRD §0.9; ADR-020 and ADR-021 keep raw emails out of `_ctx` and the
database). It also keeps a vendor endpoint's latency out of the mint path.

- `Settings.armoriq_observability` (env `ARMORIQ_OBSERVABILITY`) defaults to
  `False`. A blank value also counts as `False`.
- `ArmorIQGateway._session_options()` passes
  `ObservabilityConfig(enabled=False)` unless that setting is `True`. When it is
  `True`, it passes `None` and the SDK default (on) applies. A settings object
  without the field stays off. The other session options (`mode="proxy"`,
  `validity_seconds=TOKEN_VALIDITY_S`, `llm="proxylead"`) are unchanged. The
  gateway's authorization behavior is not otherwise changed.
- Compose passes `ARMORIQ_OBSERVABILITY` to `api` only, defaulting to `false`.
- Tests: `test_sdk_telemetry_off_by_default__offline_fake_sdk` and
  `test_sdk_telemetry_opt_in_defers_to_sdk__offline_fake_sdk`
  (`tests/api/test_armoriq_gateway.py`), and
  `test_armoriq_observability_is_off_unless_opted_in` (`tests/api/test_config.py`).
  The first test builds a real `ArmorIQSession` offline with the gateway's
  options and `ARMORIQ_OBSERVABILITY=true` in the environment. It asserts that
  no recorder and no shipper thread exist.

Opting in (`ARMORIQ_OBSERVABILITY=true`) is a privacy decision for the steward.
If it is ever turned on, re-check the payload against the pinned SDK version
first. On any SDK bump, re-verify these line references and the disable switch.
