# Testing checkpoints

Work is merged in eight waves (see [README.md § PR merge order](README.md#pr-merge-order)).
After **every** wave there is a testing checkpoint, CP-1 to CP-8. Each one runs on the
integrated `main`, not on a single branch.

**Rule:** the next wave's PRs are not merged until the previous checkpoint passes.
People may keep developing on their branches meanwhile. The last checkpoint, CP-8, is
the final delivery check.

Each checkpoint has a GitHub issue (`[CP-n] …`). It closes only when the checkpoint
passes.

| Checkpoint | Issue | Runs after | Unlocks |
|---|---|---|---|
| CP-1 Phase 1 foundation | [#40](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/40) | Wave 1 merged | Merging Wave 2 |
| CP-2 Contracts and database | [#41](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/41) | Wave 2 merged | Merging Wave 3 |
| CP-3 Data layer | [#42](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/42) | Wave 3 merged | Merging Wave 4 |
| CP-4 First tool, ready for E1 | [#43](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/43) | Wave 4 merged | Merging Wave 5 |
| CP-5 E1 gate and remaining tools | [#44](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/44) | Wave 5 merged | Merging Wave 6 |
| CP-6 Security gates (E5, E3, E4, E17) | [#45](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/45) | Wave 6 merged | Merging Wave 7 |
| CP-7 Web UI and offline security suites | [#46](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/46) | Wave 7 merged | Merging Wave 8 |
| CP-8 Final delivery | [#47](https://github.com/Aditya-Rawat01/ManagerBot-ArmorIQ-Hack-/issues/47) | Wave 8 merged | Submission |

## How to run any checkpoint

1. **Entry:** every issue of the wave is closed by a merged PR (check the issues on GitHub). Then:

   ```bash
   git checkout main && git pull origin main
   python -m pip install -e ".[api,toolserver,dev]"
   cd apps/web && npm ci && cd ../..
   ```

2. **Automated suite, run every time** (the newly merged tests are included automatically):

   ```bash
   bash scripts/check.sh          # Ruff, schema drift, all pytest, web gen:types/lint/typecheck/test/build
   ```

   CI's per-extra isolation, each in a fresh virtual environment:

   ```bash
   python -m pip install -e ".[api,dev]" && python -m pip check && python -m pytest tests/api
   python -m pip install -e ".[toolserver,dev]" && python -m pip check && python -m pytest tests/toolserver
   ```

3. **Secret scan, run every time:**

   ```bash
   git grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" -- docs/ seed/ policy/ || echo "no emails"
   git grep -nE "ak_(live|test)_[A-Za-z0-9]{6,}|xox[bpa]-[0-9]|xapp-[0-9]" || echo "no keys"
   ```

4. **The checkpoint's own list below.** Every listed test file must **exist** (its task
   creates it) and pass. Live items need the real environment: `.env` with real values,
   the Neon database, the ArmorIQ organization, and the Render deployment.
5. **Record** on the checkpoint issue:
   - the tested commit SHA;
   - each command and its result line;
   - links to any `docs/EXPERIMENTS.md` records;
   - an overall **PASS** or **FAIL**.

   Close the issue only on PASS.

**On failure:**
- Fix it in a PR that says `Refs #<CP issue>`, or open a new issue.
- Merge the fix, then **re-run the whole checkpoint**, not just the failed test.
- For a live ArmorIQ gate, spend at most 2 hours on diagnosis, then apply the documented fallback (Arch §12). The checkpoint passes only once that fallback is recorded in `docs/EXPERIMENTS.md` and the team agrees to it.
- A mocked or offline result never passes a live item.

---

## CP-1: Phase 1 foundation (after Wave 1)

**Issues:** #2 (P1-01), #5 (P1-04), #4 (P1-03), #3 (P1-02), #6 (P1-05), #48 (P1-11)

**Automated** (each file must exist and pass):
- `tests/api/test_ctx_envelope.py`
- `tests/toolserver/test_security_ctx.py`
- `tests/toolserver/test_auth.py`
- `tests/api/test_identity.py`
- `tests/api/test_session.py`
- `tests/api/test_armoriq_gateway.py`
- `tests/api/test_guardrails.py`: only the gateway imports `armoriq_sdk`, including under `scripts/` (ADR-018)
- `tests/contracts/test_contracts.py`: includes `ARMORIQ_POLICY_SOURCES` (ADR-026)

Also run:

```bash
python -m services.contracts.schema_export --check
grep -n "ctx_envelope" services/authorization/armoriq_gateway.py   # gateway uses the real sign_ctx from P1-04
```

**Live:**
- **P1-01:** `docs/EXPERIMENTS.md` has the Setup record: org active, quotas, three members active, `proxylead` agent registered, Maya (`Aditya-Rawat01`) is Org Admin. The API key is stored privately; confirm it exists, never paste it.
- **P1-05:** Render smoke test, with the bearer token from the environment:

  ```bash
  curl -fsS https://<toolserver-host>/health                                          # 200, status ok
  curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<toolserver-host>/memory/mcp  # 401
  ```

  An authenticated `initialize` returns an SSE `event: message`. `tools/list` shows **no diagnostic tools and no product tools** (nothing may be deployed before G0).

- **P1-11 (Docker, ADR-029):** on a clean clone, `cp .env.example .env && docker compose up --build -d`. Both `/health` endpoints return 200 and the web app loads on :5173. `docker compose run --rm dev bash scripts/check.sh` passes. `docker compose config` shows `DATABASE_URL_MIGRATE` only on `migrate`.

**Pass:** all of the above. **Unlocks:** merging Wave 2.

## CP-2: Contracts and database (after Wave 2)

**Issues:** #12 (P2-02), #13 (P2-01)

**Automated:**
- `tests/contracts/test_tool_args.py`
- `tests/toolserver/test_db_grants.py` (live database; skipped without DSNs, so it must be run with them here)

**Live:**

```bash
python scripts/migrate.py            # operator machine, DATABASE_URL_MIGRATE; re-run applies nothing
python scripts/verify_db_grants.py   # exit 0: backend role denied on every protected table/view
```

**Pass:** all of the above. **G0 is complete** (P1-04 + P2-01). **Unlocks:** Wave 3.

## CP-3: Data layer (after Wave 3)

**Issues:** #14 (P2-07), #15 (P2-03), #16 (P3-01), #17 (P3-03)

**Automated:**
- `tests/toolserver/test_ledger_chain.py` (the concurrent-append test needs the DSN)
- `tests/toolserver/test_idempotency.py`
- `tests/toolserver/test_ingest.py`
- `tests/api/test_audit_store.py`
- `tests/api/test_audit_redaction.py`
- `tests/api/test_responder_citations.py`
- `tests/api/test_abstention.py`

**Live:**

```bash
python -m services.knowledge.ingest --source seed/     # migration role; re-run creates no duplicates
```

The E1 seed record (one team-tier record with its known search phrase) is present in Neon. `seed/` contains no emails or real people.

**Pass:** all of the above. **Unlocks:** Wave 4.

## CP-4: First tool, ready for E1 (after Wave 4)

**Issues:** #18 (P2-04), #19 (P2-06), #20 (P3-02), #21 (P2-05)

**Automated:**
- `tests/toolserver/test_tier_filters.py`
- `tests/toolserver/test_retrieval_lexical.py`
- `tests/toolserver/test_knowledge_import_boundary.py`
- `tests/toolserver/test_tool_dispatch.py`
- `tests/toolserver/test_search_team_tool.py`
- `tests/toolserver/test_protocol.py`
- `tests/toolserver/test_config_and_guardrails.py`
- `tests/toolserver/test_retrieval_fusion.py`
- `tests/api/test_planner.py`
- `tests/api/test_topic_map.py`

**Live:**
- Redeploy the tool server to Render at the merged SHA (P1-05 procedure). Record the memory use with the embedding model loaded (P2-05).
- `tools/list` on `memory-mcp` shows `knowledge_search_team`.
- A direct call without a valid `_ctx` is rejected before any handler runs.

**Pass:** all of the above. The deployed build is **ready for E1**. **Unlocks:** Wave 5.

## CP-5: E1 gate and remaining tools (after Wave 5)

**Issues:** #7 (P1-06, E1), #22 (P2-08), #23 (P2-10), #24 (P2-09), #25 (P3-04), #26 (P3-05), #27 (P3-06), #8 (P1-07, E2)

**Live gates:**
- **G1: E1** recorded in `docs/EXPERIMENTS.md` as `PASS`, or as a documented fallback the team agreed to. The real gateway is wired into the orchestrator (#25) only because G1 is recorded.
- **G5: E2** recorded. Every attempt is labeled with the layer that actually enforced it.

**Automated:**
- `tests/toolserver/test_elevated_tier_tools.py`
- `tests/toolserver/test_source_leakage.py`
- `tests/toolserver/test_workspace_tools.py` (the honeypot never uses the network)
- `tests/toolserver/test_maintenance_tools.py`
- `tests/api/test_orchestrator.py`
- `tests/api/test_slack_events.py`
- `tests/api/test_slack_dedupe.py`
- `tests/api/test_delivery_channel.py`
- `tests/api/test_hold_notification.py`

**Live:**

```bash
python scripts/verify_db_grants.py    # now including the approved views
```

- Redeploy with all tools. `tools/list` matches `TOOL_CATALOG`.
- Slack smoke in the real workspace: a mention and a DM each get a reply, a duplicate event is processed once, and a confidential answer goes to DM only.

**Pass:** all of the above. **Unlocks:** Wave 6.

## CP-6: Security gates (after Wave 6)

**Issues:** #9 (P1-08, E5), #10 (P1-09, E3), #11 (P1-10, E4), #28 (P3-07), #29 (P4-01), #30 (P3-08, E17)

**Live gates:**

```bash
python scripts/check_setup.py         # exit 0 against the live setup
```

- **G2: E5** `PASS`. The policy is active, names are verified, and a name mismatch fails closed. The record names the enforcing layer.
- **G3: E3** `PASS` under the **ArmorIQ evidence rule**: every BLOCK/HOLD cell has an ArmorIQ source, an ArmorIQ evidence reference, and no tool-server contact. The tool-server ceiling check is reported separately.
- **G4: E4** recorded: approve/reject/timeout/expiry/duplicate, `PASS` or the documented fallback ("pending, resume unverified").
- **G6: E17** recorded, and `MIN_SCORE` calibrated and configured.

**Automated:**
- `tests/api/test_check_setup_logic.py`
- `tests/api/test_hijack_simulation.py`
- `tests/api/test_requests_api.py`
- `tests/toolserver/test_e17_metrics.py`

**Pass:** all of the above. **UI (Wave 7) may start only if G1 and G3 passed or have recorded fallbacks.** **Unlocks:** Wave 7.

## CP-7: Web UI and offline security suites (after Wave 7)

**Issues:** #31 (P4-02), #32 (P4-03), #33 (P4-04), #34 (P4-05), #35 (P3-09)

**Automated:**
- Web: `cd apps/web && npm run lint && npm run typecheck && npm test && npm run build`
- Web tests in `apps/web/src/`: sign-in/session, `OutcomeBadge`, `CitationList`, `PlaygroundPage`, manager console, and `test/accessControl.test.tsx`, `test/noSimulatedApproval.test.tsx`, `test/noIdentityInPayloads.test.ts`
- `tests/api/test_manager_api.py`
- `tests/api/test_web_access_control.py`
- `tests/api/test_prompt_injection.py`
- `tests/api/test_request_lifecycle_integration.py`
- Bundle scan: `grep -RIEo "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" apps/web/dist || echo "no emails in bundle"`

**Manual against staging:**
- Sign in and ask a team question: you get an answer with a citation.
- A HOLD shows "pending", with **no approve button**.
- The manager console is Maya-only. Sam and Priya get `403`.
- The hijack toggle is off by default.

**Pass:** all of the above. **Unlocks:** Wave 8.

## CP-8: Final delivery (after Wave 8)

**Issues:** #36 (P5-01), #37 (P5-03), #38 (P5-02), #39 (P5-04)

**Live:**

```bash
PROXYLEAD_E2E=1 python -m pytest tests/e2e -m e2e -q                      # flows 1-5 (P5-01)
PROXYLEAD_E2E=1 python -m pytest tests/e2e/test_security_regression.py -m e2e -q
python scripts/run_injection_trials.py --compromised 10 --natural 10      # SC-02: zero honeypot executions
python scripts/verify_db_grants.py
python scripts/check_setup.py                                             # against production
curl -fsS https://<api-host>/health && curl -fsS https://<toolserver-host>/health
git grep -nE "tamper-proof|zero hallucination|perfect" -- README.md docs/DEMO.md || echo "no forbidden claims"
```

**Pass:**
- **G7:** every Arch §13 item and SC-01..SC-06 recorded in `docs/SECURITY.md`. Core items are due by 16 Oct 12:00.
- The README lists only verified controls.
- Three clean rehearsals, and the video is 3:00 or less (SC-07).
- Submission confirmed before the verified deadline.
- This is the project Definition of Done (PRD §14, Arch §16).
