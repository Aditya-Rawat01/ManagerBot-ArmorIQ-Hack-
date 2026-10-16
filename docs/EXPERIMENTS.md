# ArmorIQ experiment log

Procedures: `ProxyLead_Day1_Experiments.md` and PRD v2 §11.
Rule: an experiment passes only with evidence from the real ArmorIQ
organization and the deployed HTTPS MCP endpoint. Mocks never pass a gate.

## Status

| ID | Legacy ID | Question | Status | Evidence |
|---|---|---|---|---|
| E1 | E1 | Planned SDK call reaches the MCP server through ArmorIQ; credential path | **NOT RUN** (probe ready; live run pending an operator with ArmorIQ access — see §E1 record) | — |
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

**Status: NOT RUN.** The probe has never called ArmorIQ, Render, or Neon, so
there is no E1 evidence and no outcome (PASS / FAIL / BLOCKED ON ACCESS /
INCONCLUSIVE) yet. When the probe was written (16 Oct 2026), its author did
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
