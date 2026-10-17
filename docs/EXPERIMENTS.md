# ArmorIQ experiment log

Procedures: `ProxyLead_Day1_Experiments.md` and PRD v2 §11.
Rule: an experiment passes only with evidence from the real ArmorIQ
organization and the deployed HTTPS MCP endpoint. Mocks never pass a gate.

## Status

| ID | Legacy ID | Question | Status | Evidence |
|---|---|---|---|---|
| E1 | E1 | Planned SDK call reaches the MCP server through ArmorIQ; credential path | **PASS** (2026-10-17 06:37 UTC: ArmorIQ allow → Render tool server executed → ledger row #1 correlated) | §E1 record below |
| E2 | E2 | Off-plan attempt: SDK-side vs hosted-proxy enforcement; underscore-field tampering | **NOT RUN** | — |
| E3 | E5 + E4b | Same tool, different members → ALLOW / HOLD / BLOCK | **NOT RUN** | — |
| E4 | E6 + E7 + E11 | HOLD → console approval/rejection → SDK resume; expiry | **NOT RUN** | — |
| E5 | E12 + E14 | Agent/MCP/tool names, member resolution, policy schema | **NOT RUN** | — |
| E17 | — | Grounding: threshold, citations, abstention | **NOT RUN** | — |

As of the foundation bootstrap (2026-10-14) no ArmorIQ organization, API key,
or deployed MCP endpoint has been used from this repository.

## Setup record (P1-01, Wave 1)

- Run at (UTC): 2026-10-14 ~18:00 (org created, members active, agent registered this session)
- Organization: `ArmorIQ-hackathon`, created Oct 2026, active. Sidebar inventory: 1 agent, 0 MCP servers, 1 policy (starter, see below).
- Quotas: not surfaced anywhere in the console UI (checked Dashboard, Team, Settings). Current usage: 3 members, 1 agent, 1 policy, 0 MCP servers. Expected per Arch §5.1 (2 endpoints, 2 policies, 5 members) — unverified until quotas are visible or E5.
- Members (all Active, Pending 0; full emails never recorded — masked below):

  | Persona | Member (masked) | Console role | Status |
  |---|---|---|---|
  | Maya (manager) | `a***@g***.com` (`Aditya-Rawat01`, project owner) | Org Admin | Active |
  | Sam (senior) | `s***@g***.com` (`samarth-chawla`) | Analyst | Active |
  | Priya (junior) | `p***@g***.com` (`parthgoyal26-03-2005`) | Analyst | Active |

  **Update 17 Oct 2026 (VTCodeCraft):** the Maya persona is the ArmorIQ account
  `w***@g***.com` (VTCodeCraft's work account), not `a***@g***.com`. The E1 live run used it
  and ArmorIQ accepted it as an org member. Its console role must be confirmed as **Org Admin**
  (outranking the Sam and Priya Analyst accounts) before E3/E4; whoever holds this account is
  the console approver for HOLDs. `PERSONA_MAYA_EMAIL` must hold this account's e-mail everywhere.

  Persona-to-account mapping decided by the team on 15 Oct 2026 (D-25):
  Sam = `samarth-chawla`, Priya = `parthgoyal26-03-2005`. The matching
  `PERSONA_SAM_EMAIL` / `PERSONA_PRIYA_EMAIL` values are set privately (never here).
  Hard facts for the gates: Maya is Org Admin and outranks both Analyst accounts (Arch §5.6.8: approver outranks requester, no self-approval).
- Agent: `proxylead` (exact FIXED name), ID `eae6515b-4095-458d-98f0-03310cf4e3cb`, type Assistant, owner Maya account, environment Development.
  Registry status: **pending** (registered, unscanned — the scan needs a reachable URL, which lands with the P1-05 deploy + P1-06 endpoint attach; rescan then).
  Registered with placeholder URL `https://proxylead-not-yet-deployed.invalid`
  (RFC-2606 `.invalid`, never resolves) and placeholder API-key auth. Real Render URL
  + real bearer land in P1-06; the console audit note + URL must then be updated (tracked in P5-03).
- API key: type **live**, stored only in local `.env` (`ARMORIQ_API_KEY`) and later the
  Render secret store. Value and full emails never enter the repository (secret scan below).
- Policy: one starter policy was attached by the registration flow. It is NOT the
  authored policy — P1-08 replaces it (G2 needs the authored, accepted policy). No certificate issued (org has no CA; nothing to fix).
- Console quirk (for teammates): the Custom (manual) wizard renders no URL field but
  the backend rejects an empty `url` (400). `POST /agent/register` on `api.armoriq.ai`
  is dashboard-JWT-only (service API keys get 401). Working path: **Connect by URL**
  with the placeholder — console accepts "endpoint unreachable, will register without scan".
- Pending-delegation location in the console: not identified during setup (unverified). Input for E4/D-22 still open; P1-10 locates it.
- Outcome: setup complete (persona mapping decided 15 Oct 2026). Console facts
  above are the account holder's observations; quotas could not be observed.
  No MCP endpoints registered yet (needs the P1-05 public URL; happens in P1-06).

## Tool-server deployment record (P1-05, Render)

- Tested at (UTC): 2026-10-15 06:34–06:43, by `VTCodeCraft` from a local machine over
  the public internet. Deployed by `Aditya-Rawat01`.
- Public URL: `https://managerbot-armoriq-hack.onrender.com` (Render Docker web
  service built from `deploy/toolserver/Dockerfile`).
- Deployed commit: `0e25b79` (owner-reported from Render → Events), equal to `main` at
  test time. The only change since `069fb72` is `.gitignore`, which is never part of the
  Docker build context.
- Instance (owner-reported): Render **Free**, 512 MB RAM, 0.1 CPU, $0/month. An external
  cron calls `/health` to keep the service awake (30 minutes at test time; being changed
  to 10–12 minutes, see Availability).
- Configuration (names only): `APP_ENV=staging`, `TOOLSERVER_BEARER_MEMORY`,
  `TOOLSERVER_BEARER_WORKSPACE` (64 characters each, different), `CTX_SECRET`,
  `MCP_ENABLE_DIAGNOSTIC_TOOLS=false`, `MCP_ALLOWED_HOSTS`. Values live in Render only.

| Check (public HTTPS) | Result |
|---|---|
| `GET /health` | **200** `{"status":"ok","service":"proxylead-mcp-server","version":"0.1.0","environment":"staging"}`; TLS certificate valid; `http://` → 301 to `https://` |
| `POST /memory/mcp`, `/workspace/mcp` with no bearer | **401** `unauthorized` + `WWW-Authenticate: Bearer` on both |
| Wrong bearer; non-Bearer scheme | **401** on both endpoints |
| Other endpoint's token (workspace → memory, memory → workspace) | **401** both ways |
| Authenticated `initialize` | **200**, SSE `event: message`, `protocolVersion 2024-11-05`, `serverInfo.name` `memory-mcp` / `workspace-mcp` |
| Authenticated `tools/list` | **200**, `"tools":[]` on both endpoints: no diagnostic tools, no product tools (correct before G0 and P2-06) |
| `MCP_ALLOWED_HOSTS` matches the public hostname | Yes: `initialize` through the public hostname returns 200. A mismatch returns 421 (checked locally, including an `https://` prefix) |
| Browser `Origin` + valid bearer | **403** |
| Spoofed `Host` + valid bearer | 403 at Render's edge (the request does not reach the app); the app's 421 path was verified locally |
| `/docs`, `/openapi.json` | **404** (disabled) |

Availability (Render Free):
- With the 30-minute ping, the first request at 06:34:33Z returned 200 after **22.4 s**
  (cold start); following requests took 0.13–0.32 s. The service had spun down.
- Render's Free-tier documentation (checked 15 Oct 2026): a Free web service spins down
  after **15 minutes** without inbound traffic; spin-up takes about a minute; each
  workspace gets 750 Free instance hours per month (all Free services are suspended when
  they run out); Render may restart a Free service at any time.
- A 30-minute interval cannot prevent sleep, so ADR-016's "no idle sleep" was **not met**
  at test time. The owner is changing the ping to every 10–12 minutes (under the 15-minute
  limit). One service pinged around the clock fits in 750 hours/month; a second always-on
  Free service (e.g. the API) would not. Before E1–E5 runs, call `/health` and wait for
  200, because a restart can still leave the service cold.

Resources:
- 512 MB RAM is enough for the scaffold (no product tools, no embedding model). It does
  **not** meet ADR-016's ~1 GB reservation for FastEmbed (PRD §3, §8.2). Before P2-05
  deploys FastEmbed, move this service to an instance with at least 1 GB RAM or record a
  decision; never switch silently to a hosted embedder (PRD §8.2).
- Update 16 Oct 2026 (P2-05 review; a LOCAL measurement, not a Render run): with the
  model warm the tool server image uses ~335–342 MiB RSS under `--memory=512m
  --cpus=0.1`, so 512 MB fits with ~170 MiB headroom and RAM no longer blocks P2-05
  (`deploy/toolserver/README.md` §4a). Confirm the memory metric on Render after the
  next deploy.

Outcome: **PASS** for the P1-05 public smoke tests (task file §10/§12). Recorded
limitations: the ping interval (fix in progress) and RAM (blocks P2-05, not Wave 1).
Handoff to P1-06 (`samarth-chawla`): this URL, commit `0e25b79`, and the bearer tokens via
a private channel only. The ArmorIQ agent registry still holds the placeholder URL until
P1-06 attaches this endpoint. E1–E5 and E17 remain **NOT RUN**: nothing here is ArmorIQ
evidence.

## E1 record (P1-06, #7)

**Status: PASS (live run 2026-10-17 06:37 UTC; record under "E1 live run" below).**
A real planned `knowledge_search_team` call for Maya went through the ArmorIQ
gateway and proxy to the deployed Render tool server, executed, and wrote
ledger row #1, which correlates with every identifier of the run.

History: when the probe was written (16 Oct 2026), its author did
not have the operator values (ArmorIQ API key, Maya's member e-mail, the
deployed `CTX_SECRET`) and the `memory-mcp` console attach had not been done.
A local attempt then stopped with "missing required environment" before any
network or database use. That check read only exported variables and ignored
`.env`, so it does not show missing access. Since PR #72 the probe reads the
same `Settings` as the API.

Not evidence (AGENT_HANDOFF_PROTOCOL §5): the probe
`scripts/probe_v2_e1_allowed.py` and its offline tests
`tests/api/test_e1_probe.py` (fake SDK client, fake ledger).

State before the run (from the readiness note on #7; not checked by this PR,
so check it again on the day):
- Render runs `main` at or after `699a457` (Neon cold-start fix), and
  `tools/list` on `/memory/mcp` returns `knowledge_search_team`.
- A successful call returns `_meta.ledger_entry_id`. Its ledger row is found
  by `jti` (`Ledger.get_by_jti`).
- D-05 (`_ctx` through the proxy) and D-06 (credential path) are still open.
- ArmorIQ registry: the `proxylead` agent is registered (P1-01, placeholder
  URL). The `memory-mcp` attach to the public URL is **pending** (task §9
  step 3). Do not count it as registered until the console shows it.

### Operator procedure (live run)

1. **Console.** Attach `memory-mcp` with
   `https://managerbot-armoriq-hack.onrender.com/memory/mcp`, using the exact
   v2 names. Record whether the org limit counts endpoint registrations or
   server URLs.
2. **Render.** Write down the deployed SHA (Render → Events; it must be
   `699a457` or later). Wake the service:
   `curl -fsS https://managerbot-armoriq-hack.onrender.com/health` until it
   returns 200.
3. **Neon.** Confirm the P2-03 seed record (the `quokka` freeze note) exists.
4. **Local values** (repository root, `python -m pip install -e ".[api]"`).
   Put these in the repo-root `.env` or `apps/api/.env`, or export them:
   `ARMORIQ_API_KEY`, `PERSONA_MAYA_EMAIL`, `CTX_SECRET` (must equal the value
   on Render), `DATABASE_URL_TOOLSERVER` (toolserver login role; also read
   from `services/toolserver/.env`), and optionally `ARMORIQ_AGENT_ID`
   (default `proxylead`). Never paste a value into this file.
5. **Run.**
   `python scripts/probe_v2_e1_allowed.py > e1-record.txt 2> e1-stderr.txt; echo "exit=$?"`
   - stdout: the record, with redacted values.
   - stderr: gateway lines such as `[gateway INFO] gateway step ok
     request_id=… jti=… ledger=…`. The jti and the exception class are there
     even when the call fails.
   - Exit codes: `0` = EXECUTED and the ledger row correlates (PASS
     candidate); `1` = unexpected error (exception type only); `2` = missing
     prerequisites (names only); `3` = completed but not EXECUTED +
     correlated (this includes a run without `DATABASE_URL_TOOLSERVER`).
6. **If ArmorIQ HOLDs the call** (not expected for Maya): approve within 5
   minutes. The SDK waits up to 900 s (`APPROVAL_TIMEOUT_MS`), but `_ctx`
   expires 300 s after signing (`CTX_TTL_S`), so a later approval reaches the
   tool server as `context_expired`.
7. **Render log.** Capture the lines around the run time, with secrets
   removed:
   - the uvicorn access line `POST /memory/mcp` with `200`, or `401` (bearer
     refused; D-06);
   - `verified tool call (tool=knowledge_search_team request_id=<request_id>)`;
   - `executed knowledge_search_team (entry <id>)`;
   - on rejection: `rejecting tool call: missing _ctx` (the proxy stripped
     `_ctx`: D-05 = no), `rejecting tool call: invalid context` (CTX_SECRET or
     format mismatch), `rejecting tool call: context expired`, or
     `rejecting knowledge_search_team: <code>`.
8. **Decide.** Mark PASS **only** if all of these hold: the probe exited `0`
   (EXECUTED, `correlated: true`: row id = `_meta.ledger_entry_id`, and jti,
   request_id, and plan_id match), and the Render log shows the matching
   request. Otherwise record FAIL, BLOCKED ON ACCESS, or INCONCLUSIVE with
   the exact failure. Spend at most 2 hours on diagnosis, then apply the
   documented fallback (ADR-006 transport, or the PRD v1 SDK/proxy fallback),
   narrow the claims, and file ICRs where D-05, D-06, or the result shape
   change SHARED_INTERFACES.

### Record to fill after the live run

```text
### E1 — planned ArmorIQ call reaches the MCP tool server
- Run at (UTC):
- Operator:
- Deployed tool-server SHA (Render → Events):
- SDK version (probe output):
- Agent / MCP names as registered (console): proxylead / memory-mcp
- Endpoint URL (no secrets): https://managerbot-armoriq-hack.onrender.com/memory/mcp
- request_id / trace_id (probe output):
- Plan / session IDs: plan_id= armoriq_plan_id=
- jti (probe record or [gateway ...] stderr line):
- SDK return or exception (decision outcome / source / reason; exception class from stderr):
- Tool server request observed? (Render access line + "verified tool call"):
- Invocation status + error_code (probe output):
- `_meta` present (probe output): yes / no / n.a.
- Ledger row id (probe "Ledger row") vs `_meta.ledger_entry_id`:
- Ledger correlation (probe output): correlated: true / false; mismatches:
- D-05 `_ctx` forwarded: yes / no / ambiguous (+ the Render log line that decided it)
- D-06 credential path: tool_call_attempts.credential_id = ; bearer accepted or 401; X-Armoriq-MCP-Auth seen?
- Render log lines (redacted):
- Result shape (probe "Result shape" + "First content item"):
- Probe exit code:
- Outcome: PASS / FAIL / BLOCKED ON ACCESS / INCONCLUSIVE
- Enforcement layer (DecisionSource):
- Evidence (screenshots and log excerpts stored outside the repository, referenced here):
- Fallback chosen / next action:
```

Follow-up (not part of PR #72): when the gateway returns no invocation record
(proxy HTTP ≥ 400, IntentMismatch, network error), the jti and the error class
are visible only in log lines. A gateway ICR (steward of
`services/authorization/armoriq_gateway.py`) could add a redacted error
summary (exception class + HTTP status) and the jti to `StepOutcome`. It
could also keep the SDK `status` / `verified` / `metadata` fields, which the
gateway currently drops.

### E1 live run (2026-10-17): PASS

- Run at (UTC): 2026-10-17T06:37:17Z → 06:37:57Z (record timestamp 06:37:56.96Z)
- Operator: `VTCodeCraft` (via Claude Code), from PR #72 at `51c9599` (includes #77 and #76),
  in a clean `python:3.12-slim` container with `pip install -e ".[api]"`. Operator values read
  from the operator's local git-ignored `.env`; none appear here.
- Deployed tool-server SHA (Render): `82363f8` (last deploy confirmed by the CP-4/P2-10 probes on
  16 Oct; `tools/list` on `/memory/mcp` listed the 9 memory tools)
- SDK version: `armoriq-sdk 0.6.10`
- Agent / MCP names as registered (console): agent `proxylead`; MCP server `memory-mcp`
  (registered by Aditya-Rawat01 on 17 Oct, URL below, transport HTTP/SSE, console auth "OAuth 2.0",
  scan discovered 9 tools). The agent has **no governing policy**, so ArmorIQ allows by org
  membership (ArmorIQ docs, Policy Evaluation Order).
- Endpoint URL (no secrets): https://managerbot-armoriq-hack.onrender.com/memory/mcp
- request_id / trace_id: `e1-20261017T063718Z-d05c49c3` / `e1-20261017T063718Z-d05c49c3-trace`
- Plan / session IDs: plan_id=`p-b47fad43c1ed4e17` armoriq_plan_id=`af5c79ba-f34c-4fa4-b6fa-a423c910a95e`
- Requester: Maya, member e-mail masked `w***@g***.com`, `requester_member_id=m-maya`.
  RESOLVED 17 Oct 2026: VTCodeCraft confirmed `w***@g***.com` is the Maya persona (Setup record
  updated); its Org Admin role is still to be confirmed in the console before E3/E4.
  Original note: the Setup record above listed the Maya account as `a***@g***.com`. ArmorIQ accepted
  `w***@g***.com` as an org member, but which account is the Org Admin "Maya" must be confirmed
  by Aditya-Rawat01 before E3/E4 (persona mapping).
- jti: `55da5b6839514688abe426732e48e8b7`
- SDK return: decision `allow`, source `armoriq_policy_decision`, reason "policy allowed the
  planned call (provisional until E1/E3)". Gateway log: `gateway plan minted ... outcome=allow
  source=armoriq_issuance`, then `gateway step ok ... step=0 jti=55da5b68... ledger=1`.
- Tool server request observed? Yes. The deployed tool server itself wrote the attempt row
  (#15, `accepted`, 06:37:27.20Z) and the ledger row (#1, `executed`, 06:37:27.55Z) for this jti.
  Render log (service instance `kmljl`; shown in IST, UTC+5:30), captured by Aditya-Rawat01:
  ```
  12:07:25 PM  INFO: 10.29.109.149:0 - "POST /memory/mcp HTTP/1.1" 200 OK
  12:07:25 PM  INFO: 10.29.109.149:0 - "POST /memory/mcp HTTP/1.1" 200 OK
  12:07:27 PM  INFO:services.toolserver.security:verified tool call (tool=knowledge_search_team request_id=e1-20261017T063718Z-d05c49c3)
  12:07:27 PM  INFO:services.toolserver.servers:executed knowledge_search_team (entry 1)
  ```
  = 06:37:25–06:37:27 UTC: the ArmorIQ proxy's requests (via Render ingress) reached
  `/memory/mcp`; the same request_id was verified and executed as ledger entry 1.
- Invocation status + error_code: `executed`, error_code `None`, `ledger_entry_id=1`
- `_meta` present: **yes** (keys `['ledger_entry_id']`): `_meta` survives the ArmorIQ proxy.
- Ledger row id vs `_meta.ledger_entry_id`: 1 = 1
- Ledger correlation: **correlated: true; mismatches: none**. Checked with the probe's own
  `correlate_ledger` (by jti) against the Neon toolserver role: id, jti, request_id, plan_id,
  trace_id, requester_member_id (`m-maya`), step_index (0), tool (`knowledge_search_team`) all
  match; `validation_outcome=accepted`, `execution_status=executed`, workspace `workspace-primary`.
  (The probe's in-run lookup reported `PoolTimeout` because the operator's local
  `DATABASE_URL_TOOLSERVER` was stale; the correlation was re-run read-only with the current
  toolserver DSN, without a second live call.)
- D-05 `_ctx` forwarded: **yes**. Render logged `verified tool call (... request_id=e1-20261017T063718Z-d05c49c3)`:
  the tool server verified `_ctx` (signature, audience, tool, lifetime, jti claim) before the handler.
- D-06 credential path: `tool_call_attempts.credential_id = toolserver_bearer_memory`; the
  memory bearer was accepted (ArmorIQ forwards the registered credential as
  `Authorization: Bearer`). `X-Armoriq-MCP-Auth`: not inspected (not needed: the bearer path works).
- Result shape: `{_meta: {ledger_entry_id: str}, content: [ {type, text} ], isError: bool}`;
  the text is the `KnowledgeSearchResult` JSON (`tool, tier, query, hits, min_score`).
  The gateway keeps only `MCPInvocationResult.result` (SDK `status` / `verified` / `metadata` /
  `execution_time` are dropped; see the follow-up note below).
- Probe exit code: 3 at run time (only because the in-run ledger lookup failed: stale local DSN);
  the read-only re-correlation above gives the PASS-candidate condition (EXECUTED + correlated).
- **Outcome: PASS**
- Enforcement layer (DecisionSource): `armoriq_policy_decision` (allow; no governing policy, org
  membership default). Tool-server layer: `_ctx` verified, argument validation passed, team-tier
  ceiling passed.
- Evidence: this record; Neon `tool_execution_ledger` row #1 and `tool_call_attempts` row #15
  (read by jti, hashes and arguments not shown); gateway stderr lines above; the Render log
  excerpt above (POST /memory/mcp 200, verified tool call, executed entry 1).
- Next: G1 passes with this record → P3-04 real-gateway wiring and E2 (#74) are unblocked.
  Confirm the Maya account mapping before E3/E4. P1-08 authors the real policy (E5).

## E2 record (P1-07, #8)

**Status: NOT RUN.** No live E2 attempt has run. The probe is ready for the
operator procedure below. Issue #8 stays open (PR #74 says `Refs #8`) until
a live record is filled in here.

Not evidence (AGENT_HANDOFF_PROTOCOL §5): the probe
`scripts/probe_v2_e2_offplan.py` and its offline tests
`tests/api/test_e2_probe.py` (fake SDK client, a fake tool server built on
the real `verify_and_record` and argument models, a fake ledger) and
`tests/toolserver/test_e2_direct_probe.py` (the real tool-server app with
fake local secrets).

Facts from the E1 live run (17 Oct 2026) that the probe relies on:
- ArmorIQ has `memory-mcp` registered at
  `https://managerbot-armoriq-hack.onrender.com/memory/mcp` (scan found 9
  tools). The console shows "OAuth 2.0" auth, but the proxy forwards the
  memory bearer as `Authorization: Bearer`.
- The agent `proxylead` has **no governing policy**, so ArmorIQ allows by
  org membership. If a policy is attached before the run, the ArmorIQ
  outcomes below can change. Record that.
- D-05: the proxy forwards `_ctx`, and `_meta` survives the proxy.
- Maya is the configured `PERSONA_MAYA_EMAIL` account.
- `workspace-mcp` is **not registered** in ArmorIQ. E2 does not need it:
  the honeypot attempt (`send_external_message`) is an unplanned name, and
  the SDK rejects it before any network call. If the record ever shows a
  proxy request for it, the probe flags the row; do not register anything
  to "fix" that. Investigate it as a finding.

### Attempts (run order) and the expected enforcing layer

Every invoked case gets a fresh one-step `knowledge_search_team` plan for
Maya, so no step is invoked twice under one plan. Request ids start `e2-`.

| # | case | attempt | expected outcome | expected enforcing layer |
|---|---|---|---|---|
| 1 | `planned-baseline` | planned team search (replay source) | ALLOW, EXECUTED, ledger row | none (allowed): `armoriq_policy_decision` allow, the tool server executes |
| 2 | `direct-replay` | #1's signed `_ctx` POSTed again directly, within CTX_TTL_S | `replay`, `original_entry_id` = #1's ledger entry | `tool_server_rule` |
| 3 | `direct-no-bearer` | direct call without a bearer | 401 `unauthorized` (no attempt row) | `tool_server_rule` (bearer guard) |
| 4 | `direct-no-ctx` | bearer only, no `_ctx` | `invalid_context` | `tool_server_rule` |
| 5 | `direct-forged-ctx` | malformed `_ctx` | `invalid_context` | `tool_server_rule` |
| 6 | `direct-wrong-secret` | well-formed `_ctx` signed with a wrong secret | `invalid_context` (HMAC) | `tool_server_rule` |
| 7 | `direct-cross-tool` | team `_ctx` used on `knowledge_search_confidential` | `invalid_context` (tool mismatch), never executed | `tool_server_rule` |
| 8 | `direct-expired` | `_ctx` with exp 400 s ago (lifetime 300 s) | `context_expired` | `tool_server_rule` |
| 9 | `offplan-confidential` | unplanned `knowledge_search_confidential` | BLOCK before any network call | `armoriq_sdk_plan_check` |
| 10 | `offplan-honeypot` | unplanned `send_external_message` | BLOCK before any network call; no `egress_sink` row | `armoriq_sdk_plan_check` |
| 11 | `proxy-shape` | direct proxy request bypassing the SDK | not tested (no verified request shape) | not tested |
| 12 | `tamper-query` | planned step invoked with a changed query | the proxy refuses changed arguments, or the changed query executes | `armoriq_proxy_decision` with wire evidence, or none (server-side argument binding disproven) |
| 13, 15 | `validator-tier`, `validator-user_email` | key added through the PlannedToolCall validator | rejected locally | `application_rule` |
| 14, 16 | `inject-tier`, `inject-user_email` | key injected into the planned step after validation | `invalid_arguments`, a rejected ledger row, no execution | `tool_server_rule` (or `armoriq_policy_decision` if ArmorIQ blocks first, with the proxy request on the wire) |
| 17, 19, 21 | `validator-_approvals`, `validator-_delegation_id`, `validator-_policy_context` | key added through the validator | rejected locally | `application_rule` |
| 18, 20 | `inject-_approvals`, `inject-_delegation_id` | forged key injected after validation | stripped: executes exactly as planned (business-argument hash = planned) | `tool_server_rule` (strip) |
| 22 | `inject-_policy_context` | forged key injected after validation | the SDK replaces it before sending; the tool server strips it; executes as planned | `tool_server_rule` (strip) |

How the probe evidences each label (G5: the layer that actually enforced
it, never copied from the gateway label):
- **Wire:** httpx hooks on the SDK client (gateway `client_factory` seam)
  count requests to the proxy `/invoke`. For `/invoke` they also record the
  NAMES of the params sent, and whether each injected value went out
  unchanged. `armoriq_sdk_plan_check` needs zero proxy requests and a
  pre-network IntentMismatchException (no `action` attribute). A request
  where the label claims none is flagged and exits 3. A proxy 409 is named
  `armoriq_proxy_decision`.
- **Exception:** the class, the form (pre-network vs proxy response), and
  the masked message are recorded for each SDK call.
- **DB (read-only, toolserver role):** `tool_execution_ledger` and
  `tool_call_attempts` are read by time window (the run window plus or
  minus 5 s) and tool name; `egress_sink` by window and by this run's
  request_id/jti (0012). Each row is attributed to an attempt: by jti, or
  by (tool, outcome) in run order for rejected attempts (their jti and
  request_id are NULL). For executed calls the ledger's `arguments_sha256`
  (never printed) is compared with the hash of the expected business
  arguments.
- **Zero unintended executions:** the probe reports it as CONFIRMED only if
  none of these appear: an executed row for a case that must not execute,
  an unattributed executed row in the window, or any `egress_sink` row.

Gateway ICR (from this probe, not changed in PR #74):
`ArmorIQGateway.invoke_step` and `attempt_off_plan` map every
IntentMismatchException to `armoriq_sdk_plan_check`. But armoriq-sdk
0.6.10 also raises it on a proxy HTTP 409 (`client.py:976-981`), so the
gateway should tell the two apart (by `exc.action` or the HTTP status).

### Operator procedure (live run)

1. **Console.** Confirm that `memory-mcp` is registered with the URL above,
   and that agent `proxylead` still has no governing policy. Record any
   change. Registering `workspace-mcp` is not required for E2.
2. **Render.** Write down the deployed SHA (Render → Events; `82363f8` or
   later). Wake the service:
   `curl -fsS https://managerbot-armoriq-hack.onrender.com/health` until it
   returns 200. The probe checks `/health` itself and exits 2 if it is not
   200.
3. **Neon.** The P2-03 seed (the `quokka` freeze note) must exist; the
   baseline searches for it. `DATABASE_URL_TOOLSERVER` must be the
   **current** toolserver login role. E1's in-run lookup failed on a stale
   DSN. The probe pings the database before any ArmorIQ call and exits 2 if
   the ping fails.
4. **Local values** (repository root, clean `python:3.12-slim` container,
   `python -m pip install -e ".[api]"`). Put them in the repo-root `.env`,
   `apps/api/.env` or `services/toolserver/.env` (bearer and DSN), or export
   them:
   - required: `ARMORIQ_API_KEY`, `PERSONA_MAYA_EMAIL`, `CTX_SECRET` (equal
     to Render's), `TOOLSERVER_BEARER_MEMORY` (equal to Render's), and
     `DATABASE_URL_TOOLSERVER`;
   - optional: `TOOLSERVER_BASE_URL` (default
     `https://managerbot-armoriq-hack.onrender.com`) and `ARMORIQ_AGENT_ID`
     (default `proxylead`).

   Never paste a value into this file.
5. **Run.**
   `python scripts/probe_v2_e2_offplan.py > e2-record.txt 2> e2-stderr.txt; echo "exit=$?"`
   - stdout: the record (header, evidence table, attempt details). It has
     no tokens, secrets or e-mails (masked), and no result content.
   - stderr: the gateway's own lines (request_id, jti, ledger reference,
     exception class).
   - Exit codes: `0` = every attempt has an evidenced layer and zero
     unintended executions are confirmed (a PASS candidate); `1` =
     unexpected error (class only); `2` = prerequisites missing or unusable
     (names only, checked before any ArmorIQ call); `3` = completed, but an
     attempt lacks evidence, a label is contradicted, or zero executions is
     not confirmed.
   - Permanent rows (append-only) written when every attempt behaves as
     expected:
     - 7 ledger rows: 5 executed (the baseline, the changed query, and the
       three forged `_` keys) and 2 `invalid_arguments` rejections;
     - 13 attempt rows;
     - no `egress_sink` row.
   - Run it in a quiet window: other traffic on the E2 tools during the run
     makes rows the probe cannot attribute, and blocks exit 0.
6. **If ArmorIQ HOLDs a call** (not expected: there is no policy), approve
   within 5 minutes. `_ctx` expires 300 s after signing.
7. **Render log.** Capture the lines for the run window, with secrets
   removed:
   - each invoked case: `POST /memory/mcp` 200 and `verified tool call
     (tool=knowledge_search_team request_id=e2-...)`, then `executed
     knowledge_search_team (entry N)` or `rejecting knowledge_search_team:
     invalid_arguments`;
   - the direct cases:
     - `rejecting tool call: missing _ctx` (no `_ctx`);
     - `rejecting tool call: invalid context` (forged, wrong secret,
       cross-tool);
     - `rejecting tool call: context expired`;
     - `rejecting tool call: replay`;
   - the no-bearer case: only the access line `POST /memory/mcp` 401;
   - the off-plan cases: **no** line (they never leave the SDK).
8. **Decide.** Mark PASS only if all of these hold:
   - the probe exited `0`;
   - every row says `evidenced:` (`proxy-shape` is "not tested by design");
   - "Zero unintended executions: CONFIRMED";
   - the Render lines match the record.

   Otherwise record FAIL, BLOCKED ON ACCESS, or INCONCLUSIVE with the exact
   reason. A changed query that executes is a finding ("server-side
   argument binding: DISPROVEN"), not a probe failure.

   Hand-offs:
   - P3-07: the off-plan label (`armoriq_sdk_plan_check` only if the record
     shows 0 proxy requests);
   - P5-02: the evidence rows;
   - the gateway steward: the gateway ICR (and any 409 contradiction).

### Record to fill after the live run

```text
### E2 — off-plan and tampered-call enforcement
- Run at (UTC) (probe header):
- Operator:
- Deployed tool-server SHA (Render → Events):
- SDK version (probe header):
- Agent / MCP names as registered (console): proxylead / memory-mcp; governing policy: none / <name>
- Endpoint URL (no secrets): https://managerbot-armoriq-hack.onrender.com/memory/mcp
- Requester / request_id prefix (probe header): Maya (masked), e2-
- D-05 `_ctx` forwarded (probe header + Render "verified tool call"):
- Evidence table (probe output, verbatim: one row per attempt, layer + evidence verdict):
- Attempt details (probe output, verbatim: UTC window, gateway verdict + masked reason, SDK exception, wire counts, jti, tool-server reply, DB rows, flags):
- Server-side plan binding: tool names: not tested; arguments: PROVEN / DISPROVEN / NOT DETERMINED (+ evidence)
- Zero unintended executions (probe DB evidence): CONFIRMED / NOT CONFIRMED (+ reasons)
- Unattributed DB rows / flags (probe header):
- Render log lines (redacted; IST -> UTC):
- Probe exit code:
- Outcome: PASS / FAIL / BLOCKED ON ACCESS / INCONCLUSIVE
- Label for P3-07 (off-plan honeypot attempt):
- Evidence rows for P5-02 (SC-02):
- Gateway ICR filed (IntentMismatch: pre-network vs proxy 409):
- Fallback chosen / next action:
```

## Result record template

```text
### E? — <title>
- Run at (UTC):
- SDK version:
- Agent / MCP names as registered:
- Endpoint URL (no secrets):
- Plan / session IDs:
- SDK return or exception (type + message, secrets removed):
- Tool server request observed? Ledger row / jti:
- Credential path observed:
- Outcome: PASS / FAIL / BLOCKED ON ACCESS / INCONCLUSIVE
- Enforcement layer (DecisionSource):
- Evidence (screenshots, log excerpts):
- Fallback chosen / next action:
```
