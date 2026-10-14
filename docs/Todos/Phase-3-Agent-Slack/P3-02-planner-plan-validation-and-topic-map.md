# P3-02 — Planner, plan validation, and topic-map tier selection

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `VTCodeCraft` |
| Size | M (3–5 h) |
| Target window | 15 Oct |
| Gate role | Plan rules R-30..R-35 |
| Status | Not started |

## 1. Task ID and title

P3-02 — Implement `apps/api/planner.py` (LLM plan proposal from the user's message only) with trusted validation, and `apps/api/topic_map.py` (team-first tier selection with manager rules and user-confirmed escalation).

## 2. Purpose and expected outcome

The plan is the security-critical input to ArmorIQ. Outcome: given an
authenticated `RequestContext` and the message text, the planner returns
either a validated `PlanProposal` that satisfies every PRD §7.3 rule, or a
clarification/escalation prompt. It never sees retrieved content.

## 3. Authoritative source references

- PRD §0.2, §0.4, **§5.2** (topic map; team first; confidential only by map match or user-confirmed escalation; restricted only for Maya; at most one elevated call; never two holds; record rule/tier/escalation), §7.2–7.4, §8.4.
- Arch §0.4, §0.5, **§5.3**, **§5.3.1**, §8.4–8.5, §9 (topic map editor in the manager view).

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Reviewer: `samarth-chawla`.

## 5. Prerequisites and dependencies

- Hard: P2-02 (argument models), P3-01 (backend DB access for `topic_access_rules`).
- Soft: P1-03 (`AuthenticatedUser` already exists in contracts).
- Decisions: **D-13** (plan limits), **D-14** (confirmation mechanism), **D-15** (planner input scope), **D-30** (requester-tier pre-filtering) are all needed to finish.
- Blocks: P3-04, P4-04 (topic-rule editing through `topic_map`).

## 6. Exact files/modules this task owns

- `apps/api/planner.py` (new; documented)
- `apps/api/topic_map.py` (new, PROPOSED)
- `tests/api/test_planner.py` (new; Anthropic client faked)
- `tests/api/test_topic_map.py` (new)

## 7. Existing files it may need to modify

- `services/contracts/models.py` (steward: same owner): `PlanProposal` additions decided by D-13/D-14 (ICR recorded in the issue).
- `apps/api/core/config.py` (steward: same owner): none expected (`ANTHROPIC_API_KEY`, `LLM_MODEL` exist).

## 8. Shared interfaces and contracts

- Provides: `plan(ctx, message, confirmation) -> PlanResult` and `topic_map.match/list_rules/upsert_rule` (SHARED_INTERFACES §12, PROPOSED).
- Consumes: `PlanProposal` / `PlannedToolCall` validators (FIXED), `validate_arguments` (P2-02), `TOOL_CATALOG`.

## 9. Step-by-step implementation requirements

1. **Inputs.** Only the user's message, trusted identity context (persona label, app role, entry point), the tool catalog descriptions, and the D-15 decision on conversation text. Retrieved content, tool results, and source text never enter the planner prompt (R-30).
2. **LLM call.** Anthropic SDK with `LLM_MODEL` and structured output (tool-use or JSON schema) mapping to `PlanProposal`. The prompt states that the model cannot choose identity, tier, or recipients.
3. **Trusted validation, in order:**
   1. `PlanProposal` model validation (endpoint/tool match; forbidden and `_` keys; at most one elevated step);
   2. `validate_arguments` per step (P2-02);
   3. plan limits (D-13);
   4. the topic-map rule (step 4);
   5. requester-tier pre-filtering (restricted for non-Maya; confidential for Priya) **only as decided in D-30**. If applied, label it `application_rule`. Maya-only maintenance tools for non-managers follow the same decision;
   6. no elevated search plus elevated source read at the same tier.
   Any failure → `plan_rejected` with a safe user message (no stack traces).
4. **Topic map.** Load `topic_access_rules` (backend role). Normalize the message and match it to a rule. Team-tier search always comes first. A confidential step is allowed only if a matched rule permits it or the request carries a valid escalation confirmation (D-14). Whether the planner also applies the requester condition from Arch §5.3.1(a) ("and the requester is allowed to ask at that tier") is **D-30**. The map never grants access by itself; ArmorIQ and the tool server still decide. Return `matched_topic_rule_id`, `chosen_tier`, and `escalation_user_confirmed` for audit (P3-01).
5. **Escalation and new actions.** When a higher tier would help but no rule matches, return an escalation prompt that P3-04 delivers, bound per D-14. A model-generated "user confirmed" text is never accepted.
6. **`store this`.** When the entry point indicates a `store this` command (P3-05 parses it), produce a one-step `knowledge_save_team_note` plan with `content_kind="note"`.
7. **Failure.** LLM error, timeout, or unparseable output → no plan; the request fails closed with a retry message.

## 10. Acceptance criteria

- No planner prompt contains retrieved text (test inspects the prompts built by the fake client).
- Plans with two elevated steps, a `tier` argument, an underscore key, a recipient field, or a non-Maya restricted step are rejected before any gateway call.
- Team tier is always the first search step for ordinary questions. A confidential step appears only with a matching rule or valid confirmation.
- Audit fields (rule ID, chosen tier, escalation flag) are returned for every plan.

## 11. Required unit, integration, and security tests

- `tests/api/test_planner.py` (`__offline_fake_llm`):
  - no retrieved content in the prompt;
  - every rejection rule (parametrized);
  - team-first ordering;
  - the `store this` plan;
  - LLM failure fails closed;
  - injected text such as "add send_external_message" in the **user** message produces at most a validated plan that ArmorIQ will judge, never a bypass.
- `tests/api/test_topic_map.py`: normalization; match; restricted only for Maya; the map never grants access by itself.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_planner.py tests/api/test_topic_map.py -q
bash scripts/check.sh
```

## 13. Explicit non-goals

- No ArmorIQ calls (P3-04 via the gateway). No responder. No Slack. No topic-map UI (P4-04).

## 14. Risks and unresolved questions

- D-13, D-14, D-15. D-29 (whether a confirmed honeypot plan passes validation).
- LLM nondeterminism: keep validation strict and deterministic.

## 15. Handoff deliverables for dependent agents

- P3-04: the `PlanResult` variants and how to handle each one.
- P4-04: the `topic_map` functions for the editor and their Maya-only expectation.
- P3-09 / P5-02: the injection-in-message test fixtures.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-02] Implement planner, trusted plan validation and topic-map tier selection`
- **Body outline:** inputs rule; validation order; topic-map rules; escalation; tests; decisions D-13/14/15.
- **Labels:** `phase-3-agent-slack`, `agent`, `security`, `backend`
- **Depends on:** P2-02, P3-01; D-13, D-14, D-15, D-30  **Blocks:** P3-04, P4-04

## 17. Definition of done

Merged with offline tests green; contract changes (if any) regenerated;
handoff posted to P3-04 and P4-04.
