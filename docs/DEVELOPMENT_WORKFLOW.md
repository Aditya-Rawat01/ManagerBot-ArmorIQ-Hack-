# Development workflow

For a four-person team shipping by 17 October 2026. Keep it simple, keep
`main` green.

## Phases

| Phase | Content | Gate to start |
|---|---|---|
| 0 — Foundation | This scaffold: structure, contracts, health endpoints, CI | — |
| 1–4 | Detailed plan delivered separately (ArmorIQ experiments first, then the vertical slice, policy/HOLD/manager view, hardening) | Phase 0 merged |

Rules that hold in every phase (PRD v2 §0, Architecture v2.1 §0):

- **Experiments before UI.** E1 (real call reaches MCP) and E3 (per-member
  outcomes) must pass, or a documented fallback be chosen, before building the
  full product. Record raw evidence in `docs/EXPERIMENTS.md`.
- Never mark an experiment passed from a mock. Unit tests may mock the SDK;
  their names and docs must not imply live verification.
- If an ArmorIQ behavior is uncertain, run the mapped experiment instead of
  guessing an SDK call.

## Branches and pull requests

- `main` is always deployable; no direct pushes once the team starts merging.
- Branch names: `feat/<area>-<short-desc>`, `fix/...`, `chore/...`, `docs/...`,
  `exp/e1-...` for experiment probes.
- Small PRs (one logical change). Use Conventional Commit messages
  (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `ci:`, `refactor:`).
- A PR needs: green CI, one reviewer, and an updated doc/contract when it
  changes behavior or a shared shape.

## PR checklist

- [ ] No secrets, tokens, real emails, or `.env` files in the diff.
- [ ] Shared shape changed? Updated `contracts/`, regenerated JSON Schema and TS types.
- [ ] No `armoriq_sdk` import outside `services/authorization/armoriq_gateway.py`.
- [ ] Protected data is read only through the MCP tool server.
- [ ] UI shows only persisted, real outcomes; no simulated approval.
- [ ] Security claim added to docs? It cites a passing test or experiment.

## Local checks (run before every PR)

From the repository root:

```bash
bash scripts/check.sh            # everything below in one go (Git Bash / macOS / Linux)
```

Or individually:

```bash
# Python (repository root, root .venv activated)
ruff check . && ruff format --check .
python -m services.contracts.schema_export --check
python -m pytest                      # tests/contracts, tests/api, tests/toolserver
python -m pytest tests/api            # one component at a time

# web app
cd apps/web && npm run lint && npm run typecheck && npm test && npm run build
```

Activate the root venv first: `.venv\Scripts\activate` (Windows) or
`source .venv/bin/activate` (macOS/Linux).

## Updating contracts

1. Edit `services/contracts/`.
2. `python -m services.contracts.schema_export --write`
3. `cd apps/web && npm run gen:types`
4. Commit all three together; CI rejects drift.

## Secrets

- Real values live only in local `.env` files and the deployment platform's
  secret store.
- Never paste tokens into issues, PRs, logs, or LLM prompts.
- Mask member emails in UI and logs.
