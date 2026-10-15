# ProxyLead security notes

Implementation evidence for the controls in `ProxyLead_Architecture_v2.1.md` §13.
Each section names the task that added it. Later tasks append their sections;
nothing here claims live ArmorIQ behavior (gates E1–E5, E17 are recorded in
`docs/EXPERIMENTS.md`).

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
