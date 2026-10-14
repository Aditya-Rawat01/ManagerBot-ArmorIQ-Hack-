# P1-08 — ArmorIQ policy and E5 setup/policy conformance

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | M (3–4 h) |
| Target window | Policy draft 14 Oct; console application + E5 on 15 Oct |
| Gate role | **G2** (policy accepted; names verified). Prerequisite of G3 |
| Status | Not started — E5 = NOT RUN |

## 1. Task ID and title

P1-08 — Author `policy/proxylead-policy.yaml`, apply it in the ArmorIQ console, register `workspace-mcp`, implement `scripts/check_setup.py`, and run E5 (names, member resolution, policy schema, mismatch fails closed).

## 2. Purpose and expected outcome

Per-member outcomes are meaningless if names do not match exactly or the
policy is not accepted (Arch §5.1, §5.5). Outcome:

- one active policy for agent `proxylead`, accepted by the live console;
- two endpoints registered with their product tools;
- a setup-check script that validates names and member resolution against reality;
- an E5 record proving a deliberate name mismatch fails closed.

## 3. Authoritative source references

- PRD §2 P0 (one agent, one policy, two endpoints, three members), §5.3 (one active policy keyed by exact lowercase emails; default block; `send_external_message` blocked for all; Maya-only maintenance tools; not working until E3/E5 show it; verify schema), §6, §9 (names must match exactly), §11 (E5 row), §3.1 (`policy/proxylead-policy.yaml`, `scripts/check_setup.py`).
- Arch §5.1 (naming mismatch can mean no policy applies; setup-check script), **§5.5 template and "Policy acceptance is a gate"** (verify schema; whether held tools must also be in `allowedTools`; tool-name representation for two endpoints; per-member `toolEnforcement` through the proxy), §6 (names unique across endpoints if bare names are used), §6.2 (remove `create_followup_task` if not shipped), §12 E5.

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributors: `Aditya-Rawat01` (tool registry / `tools/list`), `parthgoyal26-03-2005` (deployment).

## 5. Prerequisites and dependencies

- Hard: P1-01; P1-06 (E1 path known); **P2-08, P2-10** (tier search/source tools and honeypot deployed: what E5/E3 exercise).
- Soft: P2-09 (maintenance tools). When it deploys, re-register, re-apply the policy if needed, and re-run `check_setup.py`. The E3/UI gate does not wait for it (Arch §12 build gate).
- Decisions: D-26 (placeholders in a public repo; blocks finish), D-17 (`create_followup_task` in or out), D-12 (option (a) adds a tool to the policy), D-02 (`probe_v2_e5_setup.py` name), D-18.
- Note: drafting the YAML can start immediately. Only the console steps wait.
- Blocks: P1-09.

## 6. Exact files/modules this task owns

- `policy/proxylead-policy.yaml` (new; **placeholders only**, per D-26)
- `scripts/check_setup.py` (new)
- `scripts/probe_v2_e5_setup.py` (new; name per ADR-017)
- `docs/EXPERIMENTS.md` § E5
- `docs/SECURITY.md` § "Policy" (what was accepted and the verified schema facts)

## 7. Existing files it may need to modify

- `.env.example` (steward `parthgoyal26-03-2005`): only if E5 proves a documented variable needs a comment change.

## 8. Shared interfaces and contracts

- Tool names and endpoints (FIXED, `TOOL_CATALOG`). Registry representation becomes FIXED here (U-6). File an ICR if names must be qualified.
- Policy outcome matrix (FIXED, PRD §5.1).

## 9. Step-by-step implementation requirements

1. Write `policy/proxylead-policy.yaml` from the Arch §5.5 template using placeholders `<MAYA_EMAIL>`, `<SAM_EMAIL>`, `<PRIYA_EMAIL>` and `<registered proxylead agent id>`. Never commit real emails (D-26).
2. Register `workspace-mcp` (`/workspace/mcp`) in the console. Confirm both endpoints list exactly the product tools (diagnostic tools disabled, M-04).
3. Substitute real lowercase emails outside the repository and apply the policy. Record acceptance or rejection messages verbatim (secrets and emails removed).
4. Verify and record:
   - whether HOLD tools must also appear in `allowedTools`;
   - how tool names are represented across the two endpoints;
   - whether `toolEnforcement` per member is accepted;
   - the effective default block;
   - current quotas.
   If D-17 removes `create_followup_task`, remove it from the policy. If the console rejects names of tools not yet deployed (for example P2-09's), apply without them, record it, and re-apply after P2-09.
5. `scripts/check_setup.py` checks, without printing secrets:
   - required env vars are present;
   - `ARMORIQ_AGENT_ID == "proxylead"`;
   - each endpoint's live `tools/list` equals the `TOOL_CATALOG` subset for that endpoint;
   - the three persona emails are configured and distinct;
   - one real ALLOW decision per member on `knowledge_search_team` (through the gateway or SDK per D-18).
   It exits non-zero on any mismatch.
6. `probe_v2_e5_setup.py`: deliberately invoke a misnamed tool (for example a registered tool name with a suffix) and confirm the outcome is BLOCK or error, never ALLOW. Record the enforcing layer.
7. Record E5 using the template.

## 10. Acceptance criteria

- The live console shows one **active** policy for `proxylead`. The repository copy contains no real emails.
- `python scripts/check_setup.py` exits `0` against the live setup and non-zero when one env name is altered (demonstrated).
- The name-mismatch attempt fails closed, with evidence, and the record names the enforcing layer.
- **ArmorIQ evidence rule.** Policy evidence and fail-closed evidence are recorded separately.
  - Any decision cited as proof that the ArmorIQ policy applies (the per-member decisions from `check_setup.py`, or a policy BLOCK/HOLD) must carry `armoriq_policy_decision`, `armoriq_proxy_decision`, or `armoriq_issuance` plus an ArmorIQ evidence reference. A BLOCK/HOLD must also show that the tool server was not reached.
  - A mismatch stopped by `armoriq_sdk_plan_check`, `tool_server_rule`, `application_rule`, or `unknown` still proves fail-closed behavior, but is never cited as proof that the ArmorIQ policy applies.
- E5 record complete; U-6 answered; ICR filed if tool-name representation differs from `ToolName`.

## 11. Required unit, integration, and security tests

- Live: `check_setup.py` + `probe_v2_e5_setup.py`.
- Offline unit test for the comparison logic in `check_setup.py` (pure function over two name sets): `tests/api/test_check_setup_logic.py` (owned by this task).

## 12. Commands for verification

```bash
python scripts/check_setup.py
python scripts/probe_v2_e5_setup.py
python -m pytest tests/api/test_check_setup_logic.py -q
git grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" -- policy/ || echo "no emails"
```

## 13. Explicit non-goals

- No matrix outcome verification beyond one ALLOW per member (E3).
- No second policy or extra agent. No PII tools.

## 14. Risks and unresolved questions

- The template schema may be rejected. Record the exact error and adjust only to the verified schema (R-02).
- Quotas may count differently (E1 observation).
- D-26, D-17.

## 15. Handoff deliverables for dependent agents

- P1-09: active policy, verified tool-name representation, `check_setup.py` passing.
- P5-03: the steps to re-apply the policy in the final environment.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-08] Author and apply the ArmorIQ policy; E5 setup and policy conformance`
- **Body outline:** policy file (placeholders); console steps; check script spec; mismatch probe; evidence.
- **Labels:** `phase-1-security`, `armoriq`, `experiment`, `gate`, `security`
- **Depends on:** P1-01, P1-06, P2-08, P2-10 (soft: P2-09); D-26, D-17, D-02  **Blocks:** P1-09

## 17. Definition of done

Policy active and accepted; repository copy placeholder-only; check script
and E5 record merged; G2 status announced.
