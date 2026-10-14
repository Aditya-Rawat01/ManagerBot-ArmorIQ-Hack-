# Docker development environment (P1-11)

Run ProxyLead's whole local stack — API (:8000), MCP tool server (:8100),
web dev server (:5173), local pgvector Postgres (:5432) — with one command,
without installing matching Python/Node/Postgres versions by hand.

> What Docker does **not** do: it does not prove live ArmorIQ authorization
> (experiments E1–E5/E17 need the Render HTTPS tool server + the real org),
> it does not replace the Render deployment, and a green local stack does
> not complete E1. Local Postgres is not Neon: role/grant checks must still
> pass against Neon (P2-01, CP-2).

## 1. Prerequisites

- Docker Desktop running (`docker info` works), with the Compose v2 plugin
  (`docker compose version`).
- A repository clone. Nothing else: Python, Node, and Postgres all live in
  images. Approximate first-build downloads: ~500 MB (base images + wheels +
  npm packages).

## 2. First run

```bash
cp .env.example .env          # local-only; never commit .env
docker compose up --build -d
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8100/health
# open http://localhost:5173 — the header shows the live API health
docker compose down           # stop; the pgdata volume (database) is kept
```

Expected: both `/health` endpoints return `200 {"status":"ok",...}`, the web
app loads with its API health indicator green. MCP endpoints answer `401`
without a bearer token until `TOOLSERVER_BEARER_*` are set in `.env` (the
scaffold fails closed — that is correct behavior, not an error).

## 3. Daily commands

```bash
docker compose up -d                # start everything
docker compose up --build -d api    # rebuild + restart one service
docker compose restart toolserver   # restart without rebuilding
docker compose logs -f api          # follow one service's logs
docker compose logs -f              # everything
docker compose ps                   # status
docker compose down                 # stop (keeps pgdata + images)
```

## 4. Running the checks

```bash
docker compose run --rm dev bash scripts/check.sh   # must end "all checks passed"
```

The `dev` service (profile-gated, never started by `up`) carries Python
3.12 + Node 22, the editable install, and the web dependencies, and
bind-mounts the repo — so it tests your working tree, and it works from a
clean clone with no host `.venv`/`node_modules`.

## 5. Environment: names and who receives what

Real values live only in the untracked root `.env` (placeholders in
`.env.example`). Compose maps each service **explicitly** — there is no
wholesale `env_file: .env`, because that would leak every variable into
every container:

| Variable | api | toolserver | web | db | migrate | dev |
|---|---|---|---|---|---|---|
| `APP_ENV`, `LOG_LEVEL`, `PORT` | ✔ | ✔ | — | — | — | `APP_ENV=test` only |
| `SESSION_SECRET`, `CTX_SECRET`, `DEMO_PASSCODE`, `ADMIN_TOKEN` | ✔ | `CTX_SECRET` ✔ | — | — | — | — |
| `CORS_ALLOWED_ORIGINS` | ✔ | — | — | — | — | — |
| `TOOLSERVER_BEARER_MEMORY` / `_WORKSPACE` | — | ✔ | — | — | — | — |
| `MCP_ALLOWED_HOSTS` (compose default adds `toolserver:8100`) | — | ✔ | — | — | — | — |
| `MCP_ENABLE_DIAGNOSTIC_TOOLS` (default `true` locally) | — | ✔ | — | — | — | — |
| `DATABASE_URL_BACKEND` | ✔ only | — | — | — | — | — |
| `DATABASE_URL_TOOLSERVER` | — | ✔ only | — | — | — | — |
| `DATABASE_URL_MIGRATE` | — | — | — | — | ✔ only | — |
| `POSTGRES_USER` / `PASSWORD` / `DB` (local-only) | — | — | — | ✔ | — | — |
| `VITE_API_BASE_URL` (public, no secrets) | — | — | ✔ | — | — | — |

Verify the separation any time without secret values:

```bash
docker compose config | grep -n "DATABASE_URL"   # names only in output below
```

`DATABASE_URL_MIGRATE` must appear only on `migrate`; no backend/toolserver
DSN cross-over. Never paste expanded secret values into logs or PRs.

Known deviation (flagged, not silent): the P1-11 task file §9 sketches
`env_file: .env` for `api`/`toolserver`, but that cannot satisfy the task's
own acceptance criteria (§10: MIGRATE only on `migrate`, no cross-over) or
§9.6 least privilege, so explicit mappings are used instead. Changing this
back needs a security decision with reviewer `samarth-chawla`.

## 6. Local PostgreSQL

- Image `pgvector/pgvector:0.8.0-pg16` (pinned; close to Neon semantics —
  grant behavior must still be proven on Neon itself).
- Port bound to `127.0.0.1:5432` only; data persists in the named volume
  `pgdata` across `down`.
- Default DSNs point at it:
  `postgresql://proxylead:proxylead-local-only@db:5432/proxylead`
  (override any of `POSTGRES_*` / `DATABASE_URL_*` in `.env`; keep the three
  role DSNs consistent).
- `migrate` is the only `DATABASE_URL_MIGRATE` holder. Until P2-01 adds
  `scripts/migrate.py`, `docker compose run --rm migrate` fails with "no
  such file" — pending integration, not a bug. (Follow-up for P2-01: the
  migrate container reuses the tool-server image, which does not COPY
  `scripts/` yet.)

Safe reset (destroys local dev data — never run against shared infrastructure):

```bash
docker compose down -v   # removes pgdata too
```

## 7. Pointing at Neon instead of local db

Only when authorized (never default): set `DATABASE_URL_BACKEND`,
`DATABASE_URL_TOOLSERVER`, and (operator only) `DATABASE_URL_MIGRATE` in
`.env` to the Neon role DSNs. The `db` service still starts but is simply
unused. Grant checks (`scripts/verify_db_grants.py`, P2-01) must pass on
Neon regardless of local results.

## 8. Images

| Image | Dockerfile | Installs | Render use |
|---|---|---|---|
| `proxylead-api:dev` | `deploy/api/Dockerfile` | `.[api]` only (no `mcp`) | Yes — this is the API image (ADR-029) |
| `proxylead-toolserver:dev` | `deploy/toolserver/Dockerfile` (P1-05) | `.[toolserver]` only (no `armoriq-sdk`) | Yes — the tool-server image |
| `proxylead-web:dev` | `deploy/web/Dockerfile` | `npm ci` | No — local dev server only; Vercel builds/hosts the app (ADR-030) |
| `proxylead-dev:dev` | `deploy/dev/Dockerfile` | all extras + `npm ci` + git | No — checks only |

All run as non-root (`appuser` / `node` / `devuser`; verify with
`docker exec <container> whoami`). No image contains secrets: verify with
`docker inspect <image>` (`Env` shows only non-secret defaults) and
`docker history <image>` (no `COPY .env`, no `ARG` secrets).

## 9. Windows Docker Desktop troubleshooting

- **Daemon not running:** start Docker Desktop and wait for green; `docker
  info` must show a Server section.
- **Vite HMR/file watching laggy:** compose sets `CHOKIDAR_USEPOLLING=1`
  for the `web` service (needed through Windows bind mounts). If edits still
  lag, restart just web: `docker compose up --build -d web`.
- **API reload laggy:** same bind-mount cause; `uvicorn --reload` may be slow
  to notice host edits — `docker compose restart api` as fallback.
- **`curl.exe` quoting:** PowerShell mangles inline `-d '{json}'` (server
  sees a parse error — a client artifact). Use `--data @file.json` or Git Bash.
- **Port conflicts:** another local Postgres/API already on 5432/8000/8100/
  5173 — stop it or adjust the left side of the `ports:` mapping.
- **Stale `node_modules`:** the web/dev anonymous volumes freeze the
  dependency tree at first run. After `package-lock.json` changes:
  `docker compose down -v` (or targeted volume remove) then `up --build`.
- **Two checkouts, one project:** the compose project name is fixed
  (`proxylead`), so two clones on one machine share containers, network,
  and the `pgdata` volume — `down` one checkout before `up` in another.
- **Slow first build:** base layers + wheels + npm; later builds are cached.
  `docker compose build --no-cache` only when the toolchain itself changes.

## 10. Local Docker vs public Render deployment

| Aspect | `docker compose` (this file) | Render (P1-05 / P5-03) |
|---|---|---|
| Purpose | daily development + checks | ArmorIQ experiments (E1–E5) + demo |
| Network | `localhost` ports | public HTTPS, `PORT`-assigned |
| `MCP_ALLOWED_HOSTS` | `toolserver:8100,localhost:8100,...` | public hostname only |
| Diagnostics | `true` locally (exercise the MCP path) | `false` (only product tools listed) |
| Database | local `db` service | Neon (separate roles) |
| Secrets | root `.env` (untracked) | Render secret store |

Docker proves the code runs and the checks pass. It never proves ArmorIQ
reachability, policy behavior, or approval flows — those need Render + the
real org (P1-06 and later).
