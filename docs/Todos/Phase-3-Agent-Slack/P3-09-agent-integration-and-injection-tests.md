# P3-09 — Agent integration, prompt-injection, and failure-mode tests (offline)

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `VTCodeCraft` |
| Size | M (3–4 h) |
| Target window | 16 Oct AM |
| Gate role | Offline coverage for the Arch §13 list (live coverage in P5-02) |
| Status | Not started |

## 1. Task ID and title

P3-09 — Write offline integration tests that drive Slack and web entry points through the orchestrator, planner, responder, delivery, and audit with fakes, covering the Arch §13 security cases that do not need live ArmorIQ.

## 2. Purpose and expected outcome

Catch regressions in CI before the live security regression (P5-02). Outcome:
`tests/api/test_prompt_injection.py` and `tests/api/test_request_lifecycle_integration.py`
run in the API CI job. They cover injection, spoofing, delivery, duplicate
events, outage fail-closed behavior, and audit consistency.

## 3. Authoritative source references

- Arch **§13** (security test list), §11 (`test_prompt_injection.py`, `test_end_to_end.py` names), §8.
- PRD §1.1 (SC-02, SC-03, SC-05, SC-06), §7, §13 (16 Oct test list: prompt injection, direct-call, spoofing, tier, replay, token expiry, DM failure, outage).

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Contributors: `parthgoyal26-03-2005` (delivery cases), `samarth-chawla` (review of security cases).

## 5. Prerequisites and dependencies

- Hard: P3-04, P3-05, P3-06, P3-07.
- Blocks: P5-01.

## 6. Exact files/modules this task owns

- `tests/api/test_prompt_injection.py` (new; Arch §11 documented name)
- `tests/api/test_request_lifecycle_integration.py` (new)
- `tests/api/fakes/` additions (directory owned by P3-04, same owner)

## 7. Existing files it may need to modify

- None.

## 8. Shared interfaces and contracts

- Exercises the interfaces in SHARED_INTERFACES §2, §3, §10, §11, §12 through public entry points only.

## 9. Step-by-step implementation requirements

Each case below runs through the public entry points with fakes:

1. **Injection inside retrieved evidence** cannot add a plan step, change identity, choose a public destination, or grant permission. Assert:
   - the plan is unchanged;
   - delivery follows tier routing;
   - requested tools go only to `attempt_off_plan`.
2. **Additional action suggested by evidence** requires an explicit user confirmation and a new plan (D-14).
3. **Spoofing:** a Slack message "I am Maya" from Sam keeps Sam's identity. A web body with `persona` / `member_id` fields is rejected (P4-01 schema).
4. **Delivery:** elevated data in a channel request goes to DM only. A DM failure has no fallback. Uncited elevated data still forces DM.
5. **Duplicate Slack event:** processed once.
6. **Outages:** gateway exception, tool-server error, DB timeout (audit store), LLM failure → ERROR or UNVERIFIED, never ALLOW. A safe message is delivered.
7. **Audit consistency:** for each scenario, the sequence of audit event types matches the expected lifecycle, and `executed` steps have a ledger reference (fake).
8. Name all tests with `__offline_fakes` and state in the module docstrings that they are not live evidence.

## 10. Acceptance criteria

- All cases above are implemented and green in the API CI job.
- Each Arch §13 bullet is mapped to a test here or marked "live only → P5-02" in a table at the top of `test_prompt_injection.py`.

## 11. Required unit, integration, and security tests

- The two files above, listed in §6.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_prompt_injection.py tests/api/test_request_lifecycle_integration.py -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No live services (P5-01 / P5-02). No UI tests (P4-05).

## 14. Risks and unresolved questions

- The fakes may diverge from real SDK behavior. Update them from the E1–E4 evidence via ICR.

## 15. Handoff deliverables for dependent agents

- P5-01 / P5-02: the mapping table of Arch §13 bullets (offline-covered vs live-only) and the reusable scenario helpers.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-09] Add offline agent integration and prompt-injection security tests`
- **Body outline:** scenario list; Arch §13 mapping table; offline labeling rule.
- **Labels:** `phase-3-agent-slack`, `testing`, `security`, `agent`
- **Depends on:** P3-04, P3-05, P3-06, P3-07  **Blocks:** P5-01

## 17. Definition of done

Tests merged and green in CI; the mapping table is complete; handoff posted to
P5-01 and P5-02.
