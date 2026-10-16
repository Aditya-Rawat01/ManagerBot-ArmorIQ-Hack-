# ArmorIQ experiment log

Procedures: `ProxyLead_Day1_Experiments.md` and PRD v2 §11.
Rule: an experiment passes only with evidence from the real ArmorIQ
organization and the deployed HTTPS MCP endpoint. Mocks never pass a gate.

## Status

| ID | Legacy ID | Question | Status | Evidence |
|---|---|---|---|---|
| E1 | E1 | Planned SDK call reaches the MCP server through ArmorIQ; credential path | **INCONCLUSIVE** (probe built, live run blocked — see §E1 record) | — |
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

### E1 — planned ArmorIQ call reaches the MCP tool server
- Run at (UTC): 2026-10-16 (probe executed on a developer machine; no live invocation)
- SDK version: 0.6.10 (pinned; probe reports the installed version at run time)
- Agent / MCP names as registered: `proxylead` / `memory-mcp` (tool
  `knowledge_search_team`; endpoint attach to the public URL still pending)
- Endpoint URL (no secrets): `https://managerbot-armoriq-hack.onrender.com`
  (P1-05 record; redeploy state with the P2-06 build unverified from here)
- Plan / session IDs: none (no plan was created)
- SDK return or exception (type + message, secrets removed): none — the probe
  refused before any SDK, network, or database use:
  `probe_v2_e1_allowed: missing required environment: ARMORIQ_API_KEY,
  PERSONA_MAYA_EMAIL, CTX_SECRET` (exit 2; names only, no values)
- Tool server request observed? Ledger row / jti: no (nothing was invoked)
- Credential path observed: none (D-06 open; bearer vs `X-Armoriq-MCP-Auth`
  still undetermined)
- Outcome: INCONCLUSIVE
- Enforcement layer (DecisionSource): none observed
- Evidence (screenshots, log excerpts): probe source
  `scripts/probe_v2_e1_allowed.py` (branch `feat/security-p1-06-e1-live-run`);
  offline harness `tests/api/test_e1_probe.py` (13 passed); refusal output
  above, captured without secrets
- Fallback chosen / next action: no fallback (nothing failed; nothing ran).
  To reach a verdict, an operator with private access runs, in order:
  1. attach `memory-mcp` with the public URL in the ArmorIQ console and
     confirm the deployed tool server runs the P2-06 build (`tools/list`
     shows `knowledge_search_team`);
  2. confirm the P2-03 seed record exists on shared Neon;
  3. set `ARMORIQ_API_KEY`, `PERSONA_MAYA_EMAIL`, and the deployed
     `CTX_SECRET` privately, then `python scripts/probe_v2_e1_allowed.py`;
  4. correlate the printed `jti` via `Ledger.get_by_jti` (or set
     `DATABASE_URL_TOOLSERVER` and let the probe do it), check the Render
     request log and `tool_call_attempts`, and record the credential path
     plus `_ctx` arrival (D-05/D-06);
  5. replace this record with PASS/FAIL per PRD §11 step 9 (2-hour
     diagnosis rule, then the documented fallback).

INCONCLUSIVE — live E1 not executed because required credentials were unavailable.

## E2 record (P1-07, #8) — NOT RUN

Probe: `scripts/probe_v2_e2_offplan.py` (branch
`feat/security-p1-07-e2-enforcement`, stacked on PR #72); offline harness
`tests/api/test_e2_probe.py`. No live attempt has run; every row below is
unfilled until an authorized operator runs the probe with private access
(same three env vars as E1, plus `TOOLSERVER_BASE_URL` +
`TOOLSERVER_BEARER_MEMORY` for the direct-call group and a prior live `jti`
for the replay row).

### E2 evidence rows (one per attempt)

| attempt | expected | observed | enforcing layer | evidence reference |
|---|---|---|---|---|
| off-plan `knowledge_search_confidential` under a team-only plan | BLOCK `armoriq_sdk_plan_check` before any network call | not run | unknown | gateway log; Render log + ledger/attempt absence by request_id/jti |
| off-plan `send_external_message` under a team-only plan | BLOCK `armoriq_sdk_plan_check` before any network call | not run | unknown | same as above |
| direct proxy off-plan request bypassing the SDK check | not tested absent a verified shape | not tested | unknown | none (no verified direct-proxy shape in-tree) |
| off-plan search with forbidden `tier` key | BLOCK before execution | not run | unknown | gateway log; server log trace_metadata |
| off-plan search with forbidden `user_email` key | BLOCK before execution | not run | unknown | same as above |
| off-plan search with forged `_approvals` / `_delegation_id` / `_policy_context` | BLOCK; forged `_` fields grant nothing | not run | unknown | same as above; stripping proven offline by P1-04 tests |
| direct call, no `_ctx` | `invalid_context` | not run | unknown | response §4 code + Render log |
| direct call, forged `_ctx` | `invalid_context` | not run | unknown | same as above |
| replayed `jti` | `replay` with `original_entry_id` | pending (needs a prior live jti, e.g. from E1) | unknown | operator replays the E1 jti after a live E1 |

- Server-side plan binding: not tested (no verified shape).
- Zero ledger executions: unproven until the operator correlates each
  request_id/jti via Render log + `Ledger.get_by_jti` + `tool_call_attempts`.
- Outcome: NOT RUN.

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
