# P3-06 — Safe delivery routing and HOLD notification

| Field | Value |
|---|---|
| Phase | 3 — Agent and Slack |
| Owner | `parthgoyal26-03-2005` |
| Size | M (2–4 h) |
| Target window | 15 Oct |
| Gate role | Delivery authorization (Arch §5.4 layer 7) |
| Status | Not started |

## 1. Task ID and title

P3-06 — Implement `apps/api/delivery.py`:

- choose the delivery destination from the highest tier of data returned to the model;
- confidential and restricted content goes to a DM or private web session only, with a generic channel notice;
- a failed DM never falls back to a channel;
- send Maya a minimal HOLD DM with an ArmorIQ console link;
- persist every delivery.

## 2. Purpose and expected outcome

Disclosure control after authorization. Outcome:

- one delivery function for Slack and web;
- one HOLD-notification function;
- every attempt and result recorded in `delivery_events`;
- no code path in which protected text or citations reach a shared channel.

## 3. Authoritative source references

- PRD §0.7, §4.2 (DM only; generic notice in the channel; no fallback), §7.7 (minimal DM to Maya with a console link), §7.12, §7.13, §10.1 (destination/recipient/status), §14.
- Arch §0.9 (generic notice must not leak titles, snippets, or citations), §3.1 (delivery classification), §4 (Slack posting is not an ArmorIQ tool; HOLD DM minimal), §5.4 layer 7, §5.6.4, §8.11–8.12, §9 (restricted only in the authenticated private web session), §13 (never in a shared channel even after approval; DM failure).

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Reviewers: `VTCodeCraft` (Slack sender, orchestrator), `samarth-chawla` (security).

## 5. Prerequisites and dependencies

- Hard: P3-01.
- Soft: P3-05 (`SlackSender`; use a fake until merged).
- Decisions: D-22 (console link target) is needed to finish; it comes from E4 (P1-10).
- Blocks: P3-09; P3-04 integration.

## 6. Exact files/modules this task owns

- `apps/api/delivery.py` (new, PROPOSED path)
- `tests/api/test_delivery_channel.py` (new; Arch §11 documented name)
- `tests/api/test_hold_notification.py` (new)

## 7. Existing files it may need to modify

- None. It uses `ARMORIQ_CONSOLE_URL` from the existing settings.

## 8. Shared interfaces and contracts

- Provides: `deliver(ctx, answer, highest_tier) -> DeliveryRecord` and `notify_hold(ctx, approval) -> DeliveryRecord` (SHARED_INTERFACES §11, PROPOSED).
- Consumes: `SlackSender` (P3-05), the audit store `record_delivery` (P3-01), `DeliveryChannel`, `DataTier` (FIXED).

## 9. Step-by-step implementation requirements

1. **Routing table:**

   | Entry point | Highest tier returned | Destination |
   |---|---|---|
   | `slack_mention` | team | thread reply (`slack_thread`) |
   | `slack_mention` | confidential / restricted | DM to the requester (`slack_dm`) + generic thread notice ("I sent you a private response.") |
   | `slack_dm` | any | DM to the requester |
   | `web` | any | `web_private` (stored for the requester's session only) |

   The recipient is always the requester's mapped identity. There is no recipient parameter from any model output.
2. **Generic notice.** A fixed string constant with no interpolation of titles, snippets, counts, or citations.
3. **DM failure** → record `delivery_failed` with the error class. Do **not** post the content anywhere else. Optionally post a generic "I couldn't deliver privately; please DM me" notice, containing no content.
4. **`notify_hold(ctx, approval)`.** DM Maya's mapped Slack user with requester persona, tool name, request ID, and the console link (D-22). **No protected content, query text, or snippets.** No approve buttons or links into the app (R-40).
5. Record every attempt and result (channel, recipient member ID, tier delivered, status, timestamps) via P3-01.
6. Web: `web_private` content is retrievable only through `GET /api/v1/requests/{id}` by the same session member (P4-01 enforces this).

## 10. Acceptance criteria

- A confidential result in a channel request → the thread gets only the constant notice, and the DM gets the content (test inspects both payloads).
- An uncited confidential result still forces DM delivery (tier from all returned data).
- A failed DM → no channel post containing content; `delivery_failed` recorded.
- The HOLD DM text has no protected fields (test against a field allowlist) and no in-app approval affordance.

## 11. Required unit, integration, and security tests

- `tests/api/test_delivery_channel.py` (`__offline_fake_slack`):
  - routing table (parametrized);
  - generic notice constant;
  - uncited elevated data;
  - DM failure has no fallback;
  - web private only.
- `tests/api/test_hold_notification.py`: content allowlist; recipient is Maya only; no approval affordance.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_delivery_channel.py tests/api/test_hold_notification.py -q
```

## 13. Explicit non-goals

- No Slack event parsing (P3-05). No approval handling. No web UI (P4-03).

## 14. Risks and unresolved questions

- D-22 (exact console link).
- Slack DM channel opening may need `im:write` (PRD §4.2). Confirm in `docs/SLACK_SETUP.md`.

## 15. Handoff deliverables for dependent agents

- P3-04: call contract and failure semantics.
- P4-01 / P4-03: how `web_private` answers are stored and fetched.
- P5-02: delivery evidence to verify.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P3-06] Implement tier-based safe delivery routing and minimal HOLD DM to Maya`
- **Body outline:** routing table; notice constant; no-fallback rule; HOLD DM allowlist; tests.
- **Labels:** `phase-3-agent-slack`, `slack`, `security`, `backend`
- **Depends on:** P3-01 (soft: P3-05, P1-10); D-22  **Blocks:** P3-09

## 17. Definition of done

Merged with tests green; reviewed by `samarth-chawla`; handoff posted to P3-04.
