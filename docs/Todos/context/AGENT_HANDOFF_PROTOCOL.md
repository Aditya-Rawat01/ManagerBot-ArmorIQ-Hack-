# Agent handoff protocol

Applies to every person and coding agent working on a ProxyLead task.

## 1. Before writing code

Read, in this order:

1. [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)
2. [ARCHITECTURE_RULES.md](ARCHITECTURE_RULES.md)
3. [SHARED_INTERFACES.md](SHARED_INTERFACES.md)
4. [DECISIONS_AND_OPEN_QUESTIONS.md](DECISIONS_AND_OPEN_QUESTIONS.md). Check every `D-xx` your task cites.
5. The phase `README.md` for your task.
6. Your task file `Px-yy-*.md`, all 17 sections.
7. The sections of `ProxyLead_PRD_v2.md` / `ProxyLead_Architecture_v2.1.md` cited in your task's §3.
8. `docs/DEVELOPMENT_WORKFLOW.md` (branches, PRs, local checks).

Then confirm:

- Every **hard** prerequisite in your task's §5 is merged to `main`. Soft
  prerequisites can be replaced by the agreed interface plus a fake.
- Every decision your task is **blocked by** is resolved. If not, stop and ask
  the decision owner. Do not pick an answer.

## 2. Scope discipline

- Work only on the files listed in your task's §6 (owned) and §7 (may modify).
  A §7 file belongs to another steward: keep the change minimal and request
  that steward's review.
- Never edit another task's owned file without that owner's written agreement
  in the issue or PR.
- Do not implement non-goals (§13) or stretch scope.
- Do not refactor or restructure directories as a side effect.
- Never commit secrets, tokens, `.env`, real member emails, or real Slack IDs.
  The repository is public.

## 3. Shared files and stewards

| File(s) | Steward |
|---|---|
| `services/contracts/{__init__,enums,models,tools,schema_export}.py`, `services/contracts/schemas/*`, `apps/web/src/types/generated/*` | `VTCodeCraft` |
| `apps/api/main.py`, `apps/api/routes/router.py`, `apps/api/core/config.py`, `apps/api/deps.py` | `VTCodeCraft` |
| `pyproject.toml` | `VTCodeCraft` |
| `services/toolserver/{main,config,servers}.py`, `migrations/`, `seed/` | `Aditya-Rawat01` |
| `services/toolserver/security.py`, `services/authorization/*`, `policy/`, `docs/EXPERIMENTS.md`, `docs/SECURITY.md` | `samarth-chawla` |
| `.github/workflows/ci.yml`, `scripts/check.sh`, `.env.example`, `README.md`, `docs/DEMO.md`, `apps/web/**` (except generated types) | `parthgoyal26-03-2005` |
| `docs/ARCHITECTURE_DECISIONS.md`, `docs/IMPLEMENTATION_CONTRACT.md`, `docs/DEVELOPMENT_WORKFLOW.md`, `docs/ARCHITECTURE.md`, `docs/SLACK_SETUP.md`, `docs/Todos/**` | `VTCodeCraft` |

Rules for shared files:

- Change a steward file only in a PR that the steward reviews.
- Migrations are append-only. Never edit a merged migration; add a new numbered file.
- Generated files (JSON Schema, TypeScript) are never edited by hand. On a merge
  conflict, rebase and regenerate.
- `docs/EXPERIMENTS.md`: other owners submit their sections (for example E17) in
  a PR reviewed by `samarth-chawla`.

## 4. Interface Change Requests (ICR)

Any change to an interface marked FIXED or PROPOSED in
[SHARED_INTERFACES.md](SHARED_INTERFACES.md), or any new cross-task
interface, needs an ICR **before** implementation.

1. Comment on your task issue with:

   ```text
   ICR: <interface name>  (SHARED_INTERFACES.md §<n>)
   Current: <current definition or "none">
   Proposed: <exact new definition>
   Why: <requirement / evidence, with PRD/Arch section or experiment ID>
   Affected tasks: <IDs>
   Breaking? yes/no
   ```

2. Get agreement from the interface owner **and** the owners of the affected
   tasks.
3. Land the interface change first (a small PR touching the contract module and
   `SHARED_INTERFACES.md`), then the implementation.
4. If evidence (E1–E5, E17) contradicts an interface, the evidence wins. File
   the ICR with the experiment record attached.

Never redefine a shared shape locally (for example a private copy of a
Pydantic model or TypeScript type).

## 5. Tests and evidence

- Every task ships tests for its acceptance criteria (§11 of the task file).
- Offline tests that fake ArmorIQ, Slack, Anthropic, or Neon must say so in the
  test name or docstring, for example `test_gateway_maps_hold__offline_fake_sdk`.
- **Never claim a security control works, or a gate passed, unless the real test
  ran against the required real integration** (real ArmorIQ org, deployed HTTPS
  tool server, real Neon roles) **and you inspected the evidence.** Unit tests
  never count as gate evidence.
- Live experiment results go into `docs/EXPERIMENTS.md` using its record
  template. Outcomes: `PASS`, `FAIL`, `BLOCKED ON ACCESS`, or `INCONCLUSIVE`.
  Never record a partial pass as `PASS`.
- Live tests that need secrets are skipped automatically when the variables are
  missing. CI has no secrets by design.
- Run `bash scripts/check.sh` (or the task's §12 commands) before opening a PR.

## 6. Git workflow

Follow `docs/DEVELOPMENT_WORKFLOW.md`:

- One branch per task: `feat/<area>-<task-id-lowercase>-<short>`, for example
  `feat/toolserver-p2-06-team-search`, or `exp/e1-...` for experiments.
- One focused PR per task. Title: `[P2-06] <issue title>`. Link the issue
  (`Closes #<n>`).
- Conventional Commit messages. No direct pushes to `main`, no force-pushes to
  shared branches, no rewriting merged history.
- PR needs green CI, a review from the task's reviewer (see the task file's §4),
  and steward review for any steward file touched.

## 7. Handoff report (paste into the PR description and the issue)

```markdown
## Handoff — <Task ID> <title>

**Status:** done / partially done (what remains) / blocked (by what)

**Changed files**
- path — what changed

**Interfaces delivered / changed** (link ICRs)
- ...

**Commands run and results**
- `bash scripts/check.sh` → pass/fail (paste the summary line)
- `<task-specific command>` → result

**Live evidence** (only if this task has a live gate)
- docs/EXPERIMENTS.md § <E-id> — outcome, run time (UTC), evidence refs

**Not verified / limitations**
- ...

**Unresolved questions** (link D-xx)
- ...

**Instructions for dependent tasks**
- <task ID>: you can now ...; call ... ; do not ...
```

## 8. Definition of Ready (before starting)

- Hard prerequisites merged. Blocking decisions resolved.
- Interfaces you consume are FIXED, or PROPOSED and acknowledged by their owner.
- You can run `bash scripts/check.sh` locally.

## 9. Definition of Done (every task)

- All acceptance criteria in the task file are met and demonstrated.
- Tests added and passing locally and in CI. `bash scripts/check.sh` is green.
- No rule in ARCHITECTURE_RULES.md is violated (checklist §10 ticked in the PR).
- Docs updated where behavior or configuration changed.
- Live-gate tasks: evidence recorded in `docs/EXPERIMENTS.md`, with the outcome
  stated honestly.
- Handoff report posted. Dependent task owners notified in their issues.
