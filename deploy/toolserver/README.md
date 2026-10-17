# Tool-server deployment (P1-05)

Docker image + Render deployment for `services/toolserver` (ADR-016, ADR-029).
This is the **experiment environment** for E1–E5: a public HTTPS URL serving
the same code that runs locally. Final demo deployment of all three
components is P5-03, which reuses this procedure.

**Scope:** scaffold only. No product tool exists yet, and none may be
deployed before P1-04 merges (G0). `tools/list` therefore returns `[]` with
diagnostics off, and product tools only after P2-06 redeploys.

> Secrets rule (R-83): this file lists variable **names only**. Real values
> live in the Render secret store (production) or a local untracked file
> (testing). Never commit `.env`, tokens, or emails — the repo is public.

## 1. Prerequisites

- Docker Desktop (daemon running, `docker info` works) for local verification.
- Python 3.12 + root `.venv` (`pip install -e ".[api,toolserver,dev]"`) for
  the repo checks; Node 22.12+ for the web checks.
- A Render account with access to create a Docker Web Service (ask
  `Aditya-Rawat01`, the project host). No Render access = deploy stops at
  §5; everything up to §4 is verifiable locally.
- The bearer tokens do not exist yet: generate them during §5 (step 3) and
  share them only through a private channel.

## 2. Build the image

From the repository root (the build context — never build from
`deploy/toolserver/` itself):

```bash
docker build -f deploy/toolserver/Dockerfile -t proxylead-toolserver:dev .
```

What the Dockerfile does (`deploy/toolserver/Dockerfile`):

- `FROM python:3.12-slim` (R-81: pinned Python 3.12).
- Three cache-friendly layers: (1) `pyproject.toml` only, then
  `pip install --no-cache-dir ".[toolserver]"` — the toolserver extra only,
  matching the CI `toolserver` job isolation (`armoriq-sdk`, `google-genai`,
  `slack-bolt` are **not** installed; verified in §4, which predates ADR-032
  and checked the LLM SDK used then); (2) the FastEmbed model
  (see below); (3) `services/` (no API or web source). A source change
  rebuilds only layer 3.
- `USER appuser` (non-root, ADR-029). `EXPOSE 8100` (documentation only).
- `CMD` is the task-fixed run command in **shell form** so Render's `PORT`
  expands: `exec uvicorn services.toolserver.main:create_app --factory
  --host 0.0.0.0 --port ${PORT:-8100}` (`exec` preserves signal forwarding;
  the `JSONArgsRecommended` build warning is accepted deliberately — exec-form
  `CMD` cannot expand `PORT`).
- Bakes in no secrets: image `Env` is only `PATH`/`LANG`/Python-build vars
  plus non-secret defaults (`PORT=8100`, `MCP_ENABLE_DIAGNOSTIC_TOOLS=false`);
  there is no `.env` in `/app` (verified in §4). Root `.dockerignore` keeps
  `.env*` files and `node_modules/` at any depth, `.venv`, `.git/`, and local
  journals out of the context (ADR-029).
- Diagnostic ping tools are **off by default** in the image. Only local
  compose opts in (`docs/DOCKER.md`); Render keeps them off.
- The FastEmbed model (`BAAI/bge-small-en-v1.5`, ~65 MB) **is** downloaded at
  build time into `FASTEMBED_CACHE_PATH=/app/.fastembed-cache` (P2-05), in a
  layer that depends only on `services/knowledge/embeddings.py`. fastembed
  0.9.0 cannot pin a hub revision, so the build log prints the snapshot it got
  (2026-10-16: `aa8f8b060edb00e03bfdd08813a2949946c8ba55`). The runtime sets
  `HF_HUB_OFFLINE=1`: it never downloads; a missing or corrupt cache means
  `/health` 503, never a fetch.
- `MALLOC_ARENA_MAX=2` and `EMBED_THREADS=1` (in code) keep memory and CPU
  contention low on a 512 MB / 0.1 CPU instance (§4a).
- `/health` is **readiness**: `503 not_ready` until the startup warm loaded
  the model, then `200`. `HEALTHCHECK --start-period=120s` covers a ~45 s cold
  start at 0.1 CPU.

## 3. Run the container locally

Bearer values below are obviously-fake local test tokens. Pass real ones only
via `--env-file` pointing at an **untracked** file (never `-e` with a real
secret on a shared machine, never `COPY .env`):

```bash
docker run -d --name pl-toolserver-dev -p 8100:8100 \
  -e TOOLSERVER_BEARER_MEMORY=<memory-token> \
  -e TOOLSERVER_BEARER_WORKSPACE=<workspace-token> \
  -e MCP_ENABLE_DIAGNOSTIC_TOOLS=false \
  proxylead-toolserver:dev
curl -fsS http://localhost:8100/health
docker stop pl-toolserver-dev && docker rm pl-toolserver-dev
```

Non-default port (Render-compatibility check — Render assigns `PORT`):

```bash
docker run -d --name pl-toolserver-9000 -p 9000:9000 \
  -e PORT=9000 -e MCP_ALLOWED_HOSTS=localhost:9000 \
  -e TOOLSERVER_BEARER_MEMORY=<memory-token> \
  -e TOOLSERVER_BEARER_WORKSPACE=<workspace-token> \
  -e MCP_ENABLE_DIAGNOSTIC_TOOLS=false \
  proxylead-toolserver:dev
curl -fsS http://localhost:9000/health
```

Note: `MCP_ALLOWED_HOSTS` must contain the host you curl (default covers
`localhost:8100` / `127.0.0.1:8100` only).

## 4. Local verification (observed 2026-10-14, image `proxylead-toolserver:dev`)

Build: clean build from repo root ✔ (`python:3.12-slim` → 3.12.15,
`pip install ".[toolserver]"` installed 38 packages; image 756 MB on disk).
Container starts as `appuser`, uvicorn on `0.0.0.0:8100` (and on `:9000` with
`PORT=9000`) ✔. Stop / restart / remove all clean ✔.

| Check | Command (shape) | Observed |
|---|---|---|
| Health | `GET /health` | `200`, `{"status":"ok","service":"proxylead-mcp-server","version":"0.1.0"}` ✔ |
| No-auth memory | `POST /memory/mcp` (no bearer) | `401` ✔ |
| No-auth workspace | `POST /workspace/mcp` (no bearer) | `401` ✔ |
| Wrong bearer | valid shape, bad token | `401` ✔ |
| Cross-endpoint token | workspace token on `/memory/mcp` | `401` ✔ |
| Authenticated `initialize` | bearer + `Accept: application/json, text/event-stream` | SSE `event: message`, `protocolVersion: 2024-11-05`, `serverInfo.name: memory-mcp` ✔ |
| `tools/list` (both endpoints, diagnostics off) | bearer + Accept headers | `{"tools":[]}` — no diagnostic, no product tools ✔ (expected: scaffold) |
| Wrong `Host` + valid bearer | `Host: evil.example.com` | `421` ✔ |
| Browser `Origin` + valid bearer | `Origin: https://evil.example` | `403` ✔ |
| Image isolation | `pip show mcp` / `armoriq-sdk` / `anthropic` / `slack-bolt` | `mcp 2.3.0` present; other three absent ✔ |
| No baked secrets | `docker inspect` image `Env`; `/app` listing; log scan | only non-secret defaults; no `.env`; no token in logs ✔ |

One PowerShell note: inline `-d '{...json...}'` with `curl.exe` produced a
server `Parse error` (client-side quoting artifact). Payloads were sent with
`--data @file.json` instead — the failure was in the test invocation, not the
server. Prefer body files (or Git Bash) for MCP smoke tests on Windows.

## 4a. P2-05 resources (observed 2026-10-16, image built from the P2-05 branch)

`docker run --memory=512m --memory-swap=512m --cpus=0.1` (Render Free's
limits), local seeded PostgreSQL, fake local secrets, real MCP `tools/call`
of `knowledge_search_team` with a validly signed `_ctx` (each call also writes
its ledger rows):

| Measure | Observed |
|---|---|
| Image size | 710 MB (dependencies 308 MB, model layer 67 MB) |
| Container start → `Application startup complete` | 44.6 s (28.2 s Python imports, 16.4 s model warm) |
| RSS after warm (`VmHWM`) | 335 MiB |
| RSS peak after 78 searches incl. 2× 8 concurrent (`VmHWM`) | 342 MiB (cgroup `memory.peak` 299 MiB; no OOM events) |
| Headroom under 512 MiB | ~170 MiB by RSS, ~213 MiB by cgroup peak |
| Typical query, sequential (n=30, twice) | p50 0.20 s, p95 0.29–0.30 s, max 0.54 s |
| 500-character query | 0.90 s |
| 8 concurrent calls | all succeed; 2.3 s wall, slowest call 2.27 s (embeds are serialized) |
| Model cache missing, no network | startup completes in ~37 s with an error log; `/health` 503 `not_ready`; never retries a download |

So the tool server **fits Render Free (512 MB)**; the PRD's "~1 GB" was a
budget, not a measurement. Expect slower cold starts and query latency if the
host's CPU slice is slower than this machine's; re-measure on Render (§5
step 9).

## 5. Render deployment procedure (NOT YET PERFORMED — needs owner action)

Create a **Docker Web Service** (D-07 resolved → Render, ADR-016):

1. Render Dashboard → New → Web Service → build from the
   `ManagerBot-ArmorIQ-Hack-` repository (or "Existing image" only if the team
   later publishes one — default is repo build).
2. Runtime: **Docker**. Dockerfile Path: `deploy/toolserver/Dockerfile`.
   Build context: repository root.
3. Health Check Path: `/health` (readiness: `503` until the embedding model
   has loaded, so a deploy whose model fails to load never goes live).
4. Environment: set `APP_ENV=staging`. Leave `PORT` to Render (it injects it;
   the image defaults to `8100` locally).
5. Environment variables / secret files — add in Render's **Environment**
   settings (never in the repo). Generate the bearer tokens now (long random
   strings, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`
   run locally, output never committed):

   | Name | Value (names only — real values in Render, never here) |
   |---|---|
   | `APP_ENV` | `staging` |
   | `TOOLSERVER_BEARER_MEMORY` | generated token |
   | `TOOLSERVER_BEARER_WORKSPACE` | generated token (different from memory) |
   | `MCP_ALLOWED_HOSTS` | the public hostname Render assigns (e.g. `<service>.onrender.com`), added **after** step 7 |
   | `MCP_ENABLE_DIAGNOSTIC_TOOLS` | `false` |
   | `CTX_SECRET` | generated token, shared privately with the backend path (unused by the scaffold; required from P1-04/P2-06 — set now so redeploys need no secret changes) |
   | `DATABASE_URL_TOOLSERVER` | set when P2-01 provisions Neon (not yet — leave unset; nothing reads it) |
   | `EMBED_MODEL` | leave unset: the default is `BAAI/bge-small-en-v1.5`, the only accepted value (anything else fails startup) |

6. Deploy. Wait for "Live".
7. Read the public URL (`https://<service>.onrender.com`), set
   `MCP_ALLOWED_HOSTS` to that hostname, redeploy.
8. Run the public smoke tests from §6 against the public URL.
9. Verify the instance meets ADR-016 before handing off: memory for the
   FastEmbed model (measured locally under Render Free's limits: ~335–342 MiB
   RSS, so 512 MB fits with ~170 MiB headroom, §4a; confirm the RSS in Render's
   Metrics after a few searches), no idle sleep during experiments (sleeping
   breaks E1 timeouts and ArmorIQ reachability), public HTTPS with sane
   request timeouts. Startup takes ~45 s at 0.1 CPU; `/health` is 503 until
   then. **Record the
   instance type and observed limits in issue #6 and the EXPERIMENTS Setup
   record** (via `samarth-chawla`, the EXPERIMENTS steward). A shortfall is
   recorded, never worked around by weakening checks (task file §14).

## 6. Public smoke tests (run after §5; same shape as task file §12)

```bash
HOST=https://<toolserver-host>
curl -fsS $HOST/health                                                # 200, status ok
curl -s -o /dev/null -w "%{http_code}\n" -X POST $HOST/memory/mcp      # 401
curl -sN -X POST $HOST/memory/mcp \
  -H "Authorization: Bearer $TOOLSERVER_BEARER_MEMORY" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  --data @smoke-init.json                                             # SSE event: message
# tools/list on both endpoints → {"tools":[]} until P2-06 redeploys
# wrong-Host check: Render routes by Host, so test 421 locally (§4) instead
```

`smoke-init.json`:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
```

Record outputs in issue #6 with tokens redacted. `tools/list` must show **no
diagnostic tools** (`MCP_ENABLE_DIAGNOSTIC_TOOLS=false`) and — until P2-06 —
no product tools. Do not invent product-tool results.

## 7. Redeploy procedure (after P2-06, P2-08, P2-09, P2-10 merge)

1. `git checkout main && git pull origin main` (wave merged + checkpoint
   passed).
2. Render Dashboard → the tool-server service → Manual Deploy → Deploy latest
   commit (or push a commit — auto-deploy if enabled; confirm the deployed SHA
   matches `main`).
3. Re-run §6 against the public URL. After P2-06, `tools/list` on `memory-mcp`
   must show `knowledge_search_team`; after P2-08/P2-10, the full
   `TOOL_CATALOG` for the deployed wave.
4. Hand the new commit SHA to the P1-06 owner for E1 (`samarth-chawla`).

## 8. Verifying the running commit SHA

The `/health` response carries the app `version` (`0.1.0`), not the git SHA.
The source of truth for what is running is Render Dashboard → service →
**Deploys** tab (commit SHA + timestamp). Quote that SHA in the P1-06 handoff
and the EXPERIMENTS Setup record.

## 9. Inspecting deployment logs

Render Dashboard → service → **Logs** tab (runtime stdout/stderr: uvicorn
access lines, startup). Bearer tokens never appear there (`SecretStr` stays
redacted; verified locally in §4). Share log excerpts with tokens redacted.

## 10. Known limitations and infrastructure risks

- **Not deployed yet.** §5–§6 are procedure, not evidence. P1-05 is *partially
  done* (image + local verification) until a public URL passes §6.
- **Free-tier sleep risk.** If the service sleeps when idle, ArmorIQ/E1 calls
  hit cold starts or timeouts. Confirm the no-sleep requirement (ADR-016) at
  provisioning; record the behavior in #6.
- **RAM headroom.** PRD budgets ~1 GB for FastEmbed; measured with the model
  warm it needs ~335–342 MiB RSS (§4a), so Render Free's 512 MB fits with
  ~170 MiB headroom. Watch Render's memory metric after deploys; if it ever
  does not fit, file the fallback decision (never send confidential text to a
  hosted embedder silently — PRD §8.2).
- **CPU.** At 0.1 CPU a typical search takes ~0.2–0.3 s and embeds are
  serialized, so concurrent calls queue (8 at once: ~2.3 s). Long queries
  cost more (500 characters: ~0.9 s here).
- **Transport compatibility (U-2/D-06) is E1's job, not this task's.** Local
  SDK handshake is proven (§4, `tests/toolserver/test_protocol.py`); whether
  ArmorIQ's proxy accepts it is decided by P1-06.
- **`tools/list` is empty by design** until P2-06 (G0: no product tool before
  P1-04 merges). ArmorIQ registration happens in P1-06, not here.
- **Windows PowerShell quoting** breaks inline `-d '{json}'` with `curl.exe`
  (server `Parse error` — client artifact, §4). Use `--data @file.json`.

## 11. Handoff

- **P1-06 (`samarth-chawla`, E1):** needs the public URL, the deployed commit
  SHA (Render Deploys tab), the two bearer tokens (private channel only),
  and how to read logs (§9). Pending until §5–§6 are performed.
- **P5-03 (final deploy):** reuses this Dockerfile + procedure for the
  production tool server; adds `DATABASE_URL_TOOLSERVER`, `EMBED_MODEL`,
  `MCP_ENABLE_DIAGNOSTIC_TOOLS=false` unchanged.
- **P1-11 (compose):** reuses `deploy/toolserver/Dockerfile` and root
  `.dockerignore` for the local `db`/`toolserver` services.
