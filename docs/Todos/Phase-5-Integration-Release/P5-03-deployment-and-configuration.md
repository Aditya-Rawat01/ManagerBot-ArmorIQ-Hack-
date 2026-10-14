# P5-03 — Public demo deployment and configuration

| Field | Value |
|---|---|
| Phase | 5 — Integration and Release |
| Owner | `parthgoyal26-03-2005` |
| Size | M (3–5 h) |
| Target window | Prepare 15 Oct; deploy the verified build 16 Oct PM; verify 17 Oct |
| Gate role | Public HTTPS demo (PRD §2 P0) |
| Status | Not started |

## 1. Task ID and title

P5-03 — Deploy the API, tool server, and web app for the public demo:

- all secrets in platform stores;
- the final MCP credential path (from E1);
- diagnostic tools off;
- `MIN_SCORE` set;
- the Slack app connected;
- health verified.

Also define the demo reset procedure (D-16) and update `.env.example`, CI, and `scripts/check.sh` where release configuration requires it.

## 2. Purpose and expected outcome

A stable, public, synthetic-data-only demo that runs the verified build.
Outcome:

- public URLs for web and API, and an HTTPS tool server registered in ArmorIQ;
- a repeatable deploy and reset procedure;
- the configuration matching E1/E4/E17 results;
- three clean demo rehearsals possible (Arch §14).

## 3. Authoritative source references

- PRD §0.9 ("synthetic data only. No live customer data, PII tool, external email send, production deployment, or actual external egress"), §2 P0 (public demo), §3 (tool server separately deployed; ~1 GB RAM), §7.1 (token values), §12 (environment variables), §13 (16 Oct public deployment; 17 Oct verify public URL, Slack flow, audit trail).
- Arch §2 deployment shape, §7.1 (Neon cold start), §9 (public demo safety; reset restricted to Maya + server secret; no public sign-up), §14 (16–17 Oct), §17.

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Contributors: `samarth-chawla` (ArmorIQ registration and policy re-apply), `Aditya-Rawat01` (migrations, seed), `VTCodeCraft` (Slack app).

## 5. Prerequisites and dependencies

- Hard: P1-05 (procedure), P5-01 (verified build SHA).
- Decisions: D-07 (hosting), **D-16** (reset scope) needed to finish, D-06 (credential path from E1), D-27 (Socket Mode placement).
- Blocks: P5-04.

## 6. Exact files/modules this task owns

- `deploy/` (from P1-05; extended for API and web)
- `.env.example` (steward: this owner): comments for the calibrated `MIN_SCORE`, the final credential path, and production toggles (no secrets)
- `.github/workflows/ci.yml`, `scripts/check.sh` (steward: this owner): only release-required changes (for example excluding `tests/e2e` from default runs if needed)
- Reset procedure documentation: `deploy/RESET.md` (new) per D-16

## 7. Existing files it may need to modify

- `README.md` (steward: this owner): the deployment section (final README content is P5-04).

## 8. Shared interfaces and contracts

- Variable names (FIXED, PRD §12 / Arch §17). `ARMORIQ_MCP_CREDENTIALS` only if E1 proved it is needed.
- Endpoint names and URLs registered in ArmorIQ must match exactly (R-82).

## 9. Step-by-step implementation requirements

1. Deploy the tool server (P1-05 procedure) at the verified SHA:
   - `MCP_ENABLE_DIAGNOSTIC_TOOLS=false`; `APP_ENV` value per the note in §14;
   - enough memory for FastEmbed;
   - `MIN_SCORE` from E17;
   - the credential path from E1.
2. Deploy the API: all secrets; `TOKEN_VALIDITY_S` / `APPROVAL_TIMEOUT_MS` as verified by E4; `CORS_ALLOWED_ORIGINS` = the web origin; Socket Mode running per D-27; interactive API docs disabled (the scaffold disables them only when `APP_ENV=production`; see §14).
3. Deploy the web app with `VITE_*` pointing to the API. Cookie settings compatible with the origins (`SameSite`/`Secure`).
4. Run migrations and the seed with the migration role from an operator machine. Never from the runtime.
5. In ArmorIQ, ensure the endpoints point to the production URLs and the policy is active (with `samarth-chawla`). Run `scripts/check_setup.py`.
6. Reset (D-16): document and implement the decided scope. Respect PRD §8.1 (no runtime use of the migration role).
7. Smoke tests: health on all three; a Slack mention; web sign-in; one team-tier question; audit and ledger correlation.
8. Record the URLs (no secrets) for P5-04.

## 10. Acceptance criteria

- All three components are reachable over HTTPS. `check_setup.py` exits `0` against production.
- Smoke tests pass and are recorded. Diagnostic tools are not listed. The API docs are disabled.
- No secret in the repository, logs, or client bundle (scan).
- The reset procedure is documented and tested once.

## 11. Required unit, integration, and security tests

- Smoke run (recorded). `check_setup.py`. Bundle secret scan. CI remains green after any `ci.yml` / `check.sh` change.

## 12. Commands for verification

```bash
curl -fsS https://<api-host>/health && curl -fsS https://<toolserver-host>/health
python scripts/check_setup.py
bash scripts/check.sh
```

## 13. Explicit non-goals

- No production data. No multi-region or autoscaling work. No stretch features.

## 14. Risks and unresolved questions

- D-07, D-16, D-27. Neon cold starts during the demo (warm up before recording).
- PRD §0.9 rules out a "production deployment", while PRD §2 requires a public demo. The public demo is a synthetic-data demo environment. If `APP_ENV=production` is used, it is only a configuration flag (for example to disable API docs); record this in the deploy README. It is not a production deployment.

## 15. Handoff deliverables for dependent agents

- P5-04: public URLs, reset procedure, known operational limits.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P5-03] Deploy the public demo (API, tool server, web) with verified configuration`
- **Body outline:** per-component configuration; ArmorIQ alignment; reset (D-16); smoke checklist; secret scan.
- **Labels:** `phase-5-release`, `deployment`
- **Depends on:** P1-05, P5-01; D-07, D-16  **Blocks:** P5-04

## 17. Definition of done

Public deployment verified by smoke tests and `check_setup.py`; reset
documented; URLs handed to P5-04.
