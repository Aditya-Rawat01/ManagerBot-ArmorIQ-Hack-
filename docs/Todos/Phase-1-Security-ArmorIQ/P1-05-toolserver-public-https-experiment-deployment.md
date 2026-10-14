# P1-05 — Tool-server public HTTPS deployment for experiments

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `parthgoyal26-03-2005` |
| Size | S–M (2–4 h including platform setup) |
| Target window | 14 Oct (scaffold build), redeploy when P2-06 merges |
| Gate role | Prerequisite of G1 (E1 needs a public HTTPS MCP endpoint) |
| Status | Not started |

## 1. Task ID and title

P1-05 — Deploy `services/toolserver` to a public HTTPS URL for ArmorIQ experiments, with secrets configured and diagnostic tools disabled.

## 2. Purpose and expected outcome

ArmorIQ can only reach a public HTTPS MCP endpoint (Arch §5.1). E1 needs one
running before any other gate. Outcome:

- a public HTTPS tool-server URL serving `/health`, `/memory/mcp`, and `/workspace/mcp` from the same code that runs locally;
- bearer credentials, `CTX_SECRET`, allowed hosts, and later `DATABASE_URL_TOOLSERVER` set in the platform's secret store;
- a repeatable deploy procedure for redeploying when P2-06 / P2-08..P2-10 merge.

This is the **experiment environment**. Final demo deployment of all three
components is P5-03.

## 3. Authoritative source references

- PRD §3 (separately deployed tool server; reserve ~1 GB RAM for FastEmbed; verify deployment limits), §9 (public HTTPS, JSON-RPC 2.0 + SSE), §11 E1 step 2 ("Deploy the MCP server with public HTTPS and its bearer credential"), §12, §13 (13 Oct: deploy minimal MCP protocol).
- Arch §2 deployment shape, §5.1 (public HTTPS; exact URL and names), §14 (13 Oct), §17.
- `ProxyLead_Day1_Experiments.md` gate order step 2.

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Contributors: `Aditya-Rawat01` (tool-server steward; review), `samarth-chawla` (uses the URL in P1-06).

## 5. Prerequisites and dependencies

- Hard: none for the scaffold build.
- Decisions: D-07 resolved: **Render** (ADR-016). Verify the ADR-016 requirements (RAM, no idle sleep, HTTPS, secrets in Render settings) when provisioning.
- Soft: P2-06 (redeploy with the first product tool before E1); P2-01 (`DATABASE_URL_TOOLSERVER`); P2-05 (FastEmbed memory).
- Blocks: P1-06, P1-11 (compose reuses the tool-server Dockerfile), P5-03 (reuses the procedure).

## 6. Exact files/modules this task owns

- `deploy/toolserver/Dockerfile` (new; ADR-029): `python:3.12-slim`, `pip install ".[toolserver]"` only, non-root user, FastEmbed model downloaded at build time once P2-05 needs it, `CMD uvicorn services.toolserver.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8100}`. Render runs it as a Docker web service, and P1-11's compose reuses it. No secrets in the image.
- `deploy/toolserver/README.md` (new): the Render deploy and redeploy procedure.

## 7. Existing files it may need to modify

- `.env.example` (steward: this owner): comments only, if the deployment requires clarifying `MCP_ALLOWED_HOSTS` or `TOOLSERVER_BASE_URL`. No new variable without an ICR.
- `docs/EXPERIMENTS.md` (steward `samarth-chawla`): submit the deployed URL and version for the Setup record through that owner.

## 8. Shared interfaces and contracts

- Run command (FIXED by scaffold): `uvicorn services.toolserver.main:create_app --factory --host 0.0.0.0 --port <PORT>`, installed with `pip install ".[toolserver]"`.
- Endpoint paths (FIXED): `/health`, `/memory/mcp`, `/workspace/mcp`.
- Settings: `TOOLSERVER_BEARER_MEMORY`, `TOOLSERVER_BEARER_WORKSPACE`, `MCP_ALLOWED_HOSTS` (= public hostname), `MCP_ENABLE_DIAGNOSTIC_TOOLS=false` (M-04), `CTX_SECRET`, `DATABASE_URL_TOOLSERVER`, `APP_ENV=staging`, `EMBED_MODEL`.

## 9. Step-by-step implementation requirements

1. With D-07 decided, create the service on the chosen platform from the repository root. Install only `.[toolserver]` (the API extra must not be installed, matching CI isolation).
2. Configure secrets in the platform store only:
   - generate long random bearer tokens;
   - share `CTX_SECRET` with the backend owner securely;
   - set `MCP_ALLOWED_HOSTS` to the public hostname;
   - set `MCP_ENABLE_DIAGNOSTIC_TOOLS=false` so only product tools are listed when ArmorIQ registers the endpoint.
3. Confirm the platform terminates TLS and that the app sees the expected `Host` header. A `421` means the host allowlist is wrong.
4. Record the platform's memory and timeout limits. Note whether ~1 GB is available for FastEmbed (needed once P2-05 lands).
5. Write `deploy/toolserver/README.md`: prerequisites, the deploy command or button path, the environment variable list (names only), the redeploy procedure, and the health/MCP smoke commands below.
6. Redeploy when P2-06 merges. Hand the URL and deployed commit SHA to `samarth-chawla` for E1.

## 10. Acceptance criteria

- `GET https://<host>/health` returns `200` with `status=ok`.
- `POST https://<host>/memory/mcp` without a bearer token returns `401`. With the correct bearer and `Accept: application/json, text/event-stream`, `initialize` returns an SSE `event: message` frame.
- `tools/list` on each endpoint returns **no diagnostic tools**, and after P2-06 returns only product tools.
- No secrets in the repository. The deploy README lists variable names only.
- URL, commit SHA, and platform limits are handed to P1-06 and recorded in the EXPERIMENTS Setup record.

## 11. Required unit, integration, and security tests

- Manual smoke (commands below), recorded in the issue: health, unauthenticated rejection, authenticated `initialize` / `tools/list`, wrong `Host` rejected.
- No CI change in this task.

## 12. Commands for verification

```bash
curl -fsS https://<host>/health
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<host>/memory/mcp   # expect 401
curl -sN -X POST https://<host>/memory/mcp \
  -H "Authorization: Bearer $TOOLSERVER_BEARER_MEMORY" \
  -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

## 13. Explicit non-goals

- No API or web deployment (P5-03). No production hardening beyond what the experiments need.
- No ArmorIQ registration (P1-06). No database provisioning (P2-01).

## 14. Risks and unresolved questions

- Render instance limits (memory, idle behavior) must meet ADR-016; record them. A shortfall is recorded, never worked around by weakening checks.
- Memory limits may not fit FastEmbed. Record them; the fallback needs a documented decision (PRD §8.2 forbids silently sending confidential text to a hosted embedder).
- Cold starts or idle sleep may cause E1 timeouts. Record the platform behavior.
- Proxy-compatible transport (D-06) is verified in E1, not here.

## 15. Handoff deliverables for dependent agents

- P1-06: public URL, deployed commit SHA, bearer tokens (via secure channel), how to read tool-server logs on the platform.
- P5-03: the deploy procedure, reusable for the final environment.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-05] Deploy the MCP tool server to public HTTPS for ArmorIQ experiments`
- **Body outline:** platform (D-07); env names; smoke checks; acceptance criteria; evidence = smoke output (no secrets) + URL in Setup record.
- **Labels:** `phase-1-security`, `deployment`, `mcp`
- **Depends on:** D-07  **Blocks:** P1-06, P1-11, P5-03

## 17. Definition of done

Public HTTPS URL passes the smoke checks; deploy README merged; URL and SHA
handed to P1-06; no secrets committed.
