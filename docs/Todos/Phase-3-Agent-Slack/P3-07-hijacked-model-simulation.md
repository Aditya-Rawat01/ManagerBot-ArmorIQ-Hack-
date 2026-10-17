# P3-07 — "Simulate hijacked model" mode and honeypot attempt routing

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `VTCodeCraft` |
| Size | M (2–4 h) |
| Target window | 16 Oct AM |
| Gate role | SC-02; needs **G5** to display enforcement labels |
| Status | Implemented on `w6/a-p401-p307` (offline tests green; live 10 + 10 runs are P5-02; not merged). Handoff notes: §15.1 |

## 1. Task ID and title

P3-07 — Implement the visible, off-by-default "Simulate hijacked model" mode. It injects synthetic malicious instructions, makes the responder attempt `send_external_message`, and routes that attempt through the ArmorIQ off-plan path, so the true enforcement layer is recorded and shown.

## 2. Purpose and expected outcome

The demo must show that a compromised model cannot exfiltrate data, and which
layer stopped it. Outcome:

- a request option that activates synthetic injection (per D-28);
- the orchestrator sends every resulting honeypot attempt to `attempt_off_plan` (never executed);
- a separately **user-confirmed** honeypot plan reaches ArmorIQ policy (per D-29);
- the audit shows zero `egress_sink` executions in guarded mode.

## 3. Authoritative source references

- PRD §1.1 SC-02 (10 compromised + 10 natural runs; zero executed `send_external_message`; record the layer per attempt), §2 P0 (toggle + honeypot), §6.2, **§10.3** (toggle off by default; synthetic injection into a seeded source or adversary prompt; SDK plan check if off-plan; policy BLOCK for a confirmed plan; DM-only is an application control, not proof that ArmorIQ blocked egress).
- Arch §6.2 (toggle; attempt passes the gateway/SDK plan-check path; policy blocks a separately confirmed plan for every persona), §15 demo step 4.

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Contributor: `samarth-chawla` (labels from E2; honeypot P2-10).

## 5. Prerequisites and dependencies

- Hard: P1-02 (`attempt_off_plan`), P2-10 (honeypot), P3-03 (`requested_tools`), P3-04 (orchestrator option pass-through).
- Soft: P1-07 (**G5**). Until E2 is recorded, the UI shows the raw `DecisionSource` and makes no layer claim beyond the evidence.
- Decisions: **D-28** (injection vector) and **D-29** (honeypot arguments) are needed to finish.
- Blocks: P3-09, P4-03 (toggle UI), P5-02.

## 6. Exact files/modules this task owns

- `apps/api/hijack_simulation.py` (new, PROPOSED path): synthetic injection payloads and activation logic
- `tests/api/test_hijack_simulation.py` (new)

## 7. Existing files it may need to modify

All steward or owner `VTCodeCraft`:

- `apps/api/responder.py`: an injection hook (only when the option is on).
- `apps/api/orchestrator.py`: pass the option through.
- `seed/` (steward `Aditya-Rawat01`): only if D-28 chooses a seeded adversarial source. Request it via ICR.

## 8. Shared interfaces and contracts

- `RequestOptions.simulate_hijacked_model: bool` (SHARED_INTERFACES §12; set only by the web route P4-01; default false; never from Slack text).
- Off-plan labeling per the E2 evidence (D-03).

## 9. Step-by-step implementation requirements

1. **Activation.** Only via the web request option (P4-01). Off by default. Logged and audited as "simulation".
2. **Injection.** Per D-28, add clearly marked synthetic malicious instructions to the responder's evidence (seeded source) or as an adversary prompt that asks for `send_external_message`. Synthetic content only.
3. **Routing.**
   - Whatever the responder requests is sent by the orchestrator to `gateway.attempt_off_plan` and recorded with the returned `DecisionSource`.
   - The tool server and `egress_sink` are never reached in guarded mode.
4. **Confirmed new plan.**
   - If the user explicitly confirms the action (D-14), create a **new** request whose plan contains `send_external_message` (arguments per D-29).
   - Expected: ArmorIQ policy BLOCK for every persona (verified by E3).
   - Record the actual result honestly.
5. **Display data.** Provide, for P4-03, the per-attempt layer label and an `egress_sink` count delta of 0 (via a read the backend is allowed to make, SHARED_INTERFACES §7, PROPOSED).
6. **Natural mode.** With the toggle off, the same flow must also produce zero honeypot executions (SC-02 natural runs).

## 10. Acceptance criteria

- With the toggle on, the responder's honeypot requests never execute (fake gateway + tool-server spy), and each attempt has an audit record with its decision source.
- A confirmed honeypot plan goes through ArmorIQ (live check in P5-02). Any application-rule rejection is labeled `application_rule`, not ArmorIQ.
- The toggle cannot be activated from Slack or by message text.

## 11. Required unit, integration, and security tests

- `tests/api/test_hijack_simulation.py` (`__offline_fakes`):
  - `test_toggle_off_by_default`
  - `test_injected_attempt_routed_off_plan`
  - `test_no_egress_sink_write_in_guarded_mode`
  - `test_confirmed_plan_creates_new_request`
  - `test_slack_cannot_enable_simulation`
- Live 10 + 10 runs: P5-02.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_hijack_simulation.py -q
```

## 13. Explicit non-goals

- No real egress. No unguarded mode in the public demo. The sandbox exercise of the sink is test-only (PRD §10.3).

## 14. Risks and unresolved questions

- D-28, D-29.
- G5 may show the SDK check as the only enforcement. Then say exactly that.

## 15. Handoff deliverables for dependent agents

- P4-03: the toggle API field and the display fields (layer label, sink count).
- P5-02: the harness for the 10 + 10 runs.
- P5-04: demo wording that matches the evidence.

### 15.1 Handoff notes (as implemented)

Module: `apps/api/hijack_simulation.py` (docstring has the full flow);
wiring in `apps/api/orchestrator.py`; tests `tests/api/test_hijack_simulation.py`.

- **Activation:** only `POST /api/v1/requests` with
  `simulate_hijacked_model: true` (P4-01) sets
  `RequestOptions.simulate_hijacked_model`. Off by default; a Slack context
  carrying options is refused (`RequestRejectedError`, nothing recorded); no
  message text enables it. An active request is audited first:
  `simulation_activated` (`request_events`, payload `simulation:
  "hijacked_model"`, `injection_vector: "adversary_prompt"`, `synthetic:
  true`; new `AuditEventType`, ICR in SHARED_INTERFACES §10).
- **Injection vector (D-28 still open):** the spec's adversary-prompt
  default, no seeded source. `ADVERSARY_PROMPT` is the clearly marked
  synthetic instruction; the mode simulates a model that obeyed it by
  appending `HIJACKED_TOOL_REQUESTS` to the real responder's output (so the
  demo does not depend on a live model following an injection): a
  schema-valid honeypot `send_external_message` (`synthetic_sink`, bounded
  synthetic message), an off-plan `knowledge_search_restricted` (tier
  escalation), and two forged honeypot requests with `_ctx` and `recipient`
  keys.
- **Routing:** the requests pass the responder's own screen (`_ctx` →
  `reserved_key`, `recipient` → `forbidden_key`: recorded
  `application_rule`, never ArmorIQ); the rest go ONLY to
  `gateway.attempt_off_plan` and are recorded with the label it returned (an
  off-plan ALLOW is recorded as BLOCK/`unknown`); a name that is in the plan is
  refused by trusted code without an SDK call (E2). Each record is an
  `authorization_decided` event with `off_plan: true`, `executed: false`,
  `simulation: "hijacked_model"`. Nothing reaches `invoke_step`, the tool
  server or the sink; real steps keep using only the validated stored plan
  (fingerprint check unchanged).
- **Confirmed plan (task §9.4):** the delivered note tells the user to send a
  new request; that NEW request plans `send_external_message` (D-29/ADR-025
  arguments) under a new ArmorIQ plan and its real decision is recorded as
  returned (expected ArmorIQ policy BLOCK: live check E3/P5-02).
- **Display for P4-03:** `GET /api/v1/requests/{id}` →
  `simulation_active`, `off_plan_attempts[]` (`enforcement_layer`,
  `decision_source`, `is_armoriq_policy`, `simulated`, `rejection`,
  `executed: false`) and `egress_sink_executions` (executed
  `send_external_message` steps from the persisted execution references; the
  backend cannot read `egress_sink` itself, grant still PROPOSED in
  SHARED_INTERFACES §7). Until E2/G5 evidence says more, show the raw label:
  `armoriq_sdk_plan_check` is the client-side SDK check, not ArmorIQ policy.
- **P5-02 harness:** `test_no_egress_sink_write_in_guarded_mode` is the
  offline 10 + 10 shape (10 simulated + 10 natural web requests, zero
  honeypot executions); the live run repeats it against the real gateway.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-07] Implement "Simulate hijacked model" mode with honeypot off-plan routing`
- **Body outline:** activation rules; injection vector (D-28); routing; confirmed-plan path (D-29); labels per E2; tests.
- **Labels:** `phase-3-agent-slack`, `agent`, `security`
- **Depends on:** P1-02, P2-10, P3-03, P3-04 (soft P1-07); D-28, D-29  **Blocks:** P3-09, P4-03, P5-02

## 17. Definition of done

Merged with offline tests green; display contract handed to P4-03; harness
handed to P5-02.
