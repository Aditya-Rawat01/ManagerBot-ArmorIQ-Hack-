# P4-03 — Team member web playground

| Field | Value |
|---|---|
| Phase | 4 — Frontend |
| Owner | `parthgoyal26-03-2005` |
| Size | M (3–5 h) |
| Target window | 16 Oct AM |
| Gate role | Shows only persisted, real outcomes (R-72) |
| Status | Not started |

## 1. Task ID and title

P4-03 — Replace the placeholder playground with the team-member view:

- chat composer and conversation;
- honest ALLOW/BLOCK/HOLD/ABSTAIN/ERROR labels with their decision source;
- validated citations;
- pending-HOLD state with a console note;
- private answer display;
- the clearly labeled "Simulate hijacked model" toggle.

## 2. Purpose and expected outcome

The web demo must tell the truth about what happened. Outcome:

- the playground submits messages and polls status;
- it renders only server-reported state;
- it separates assistant prose from authoritative system records;
- it never shows an approve button;
- it shows the hijack toggle off by default with the enforcement label and sink count from the server.

## 3. Authoritative source references

- PRD §10.2 team view (chat, citations, honest outcome labels, pending HOLD state, prose vs authoritative record distinction, safe private delivery), §10.3 (toggle named exactly "Simulate hijacked model", off by default; show the exact enforcement layer).
- Arch §1 required outcomes, §6.3 (UI distinguishes a tool-server rejection from an ArmorIQ decision), **§9 team member view**, §15 demo steps.

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Reviewer: `VTCodeCraft`.

## 5. Prerequisites and dependencies

- Hard: P4-02.
- Soft: P3-07 (toggle display fields); P1-07 / G5 (which labels may be shown); P1-10 / G4 (whether the HOLD UI says "approval verified" or "pending/unverified").
- Blocks: P4-05, P5-01.

## 6. Exact files/modules this task owns

All under `apps/web/src/`:

- `pages/PlaygroundPage.tsx` (existing; rewritten)
- `components/ConversationPlaceholder.tsx` (existing) → replaced by `components/Conversation.tsx` (new)
- `components/MessageComposer.tsx`, `components/OutcomeBadge.tsx`, `components/CitationList.tsx`, `components/HoldNotice.tsx`, `components/HijackToggle.tsx` (new)
- `hooks/useRequestPolling.ts` (new)
- Tests: `components/OutcomeBadge.test.tsx`, `components/CitationList.test.tsx`, `pages/PlaygroundPage.test.tsx` (new)

## 7. Existing files it may need to modify

- `src/components/PlaceholderPanel.tsx`: remove or keep for planned connectors only.
- `src/index.css`: styles.

## 8. Shared interfaces and contracts

- Consumes: `RequestStatusView`, `StepStatusView`, `SourceCitation`, `DecisionSource`, `ApprovalState`, `ToolExecutionStatus` (generated TypeScript).
- Label text rules: SHARED_INTERFACES §3 / ARCHITECTURE_RULES R-03 (no "ArmorIQ blocked" unless the source is an ArmorIQ label).

## 9. Step-by-step implementation requirements

1. Composer → `submitRequest`. Poll `getRequestStatus` until a terminal state. HOLD pending is non-terminal with slow polling.
2. `OutcomeBadge` maps the outcome **and** source to text, for example "Blocked by ArmorIQ policy" only for `ARMORIQ_POLICY_SOURCES` (ADR-026), "ArmorIQ SDK plan check (client-side)" for `armoriq_sdk_plan_check`, "Rejected by tool server" for `tool_server_rule`, "Blocked by application rule", "Authorization could not be verified".
3. `HoldNotice`: "Waiting for manager approval in ArmorIQ". No approve or reject controls. Wording reflects G4 (verified vs unverified resume).
4. `CitationList`: only the citations in the status view (already validated server-side). Show title, author, version, review status (draft vs authoritative), supersession.
5. Show answers only when the status view includes them for this session (`web_private`). Visually separate assistant prose from system records (decision and citation metadata).
6. `HijackToggle`: labeled exactly "Simulate hijacked model", off by default, sends `simulateHijackedModel` per request. Shows the attempt's decision source and "synthetic sink writes: 0" from the server.
7. Accessibility basics: labels, focus states, ARIA live region for status updates.

## 10. Acceptance criteria

- Each outcome fixture (ALLOW, BLOCK by policy, BLOCK by SDK plan check, tool-server rejection, HOLD pending, ABSTAIN, ERROR) renders its correct, distinct label (tests).
- There is no approve button anywhere in the playground (test queries buttons).
- The toggle is off by default and labeled exactly as the PRD specifies.
- The UI never renders state that the status view did not return (tests use mocked API responses only).

## 11. Required unit, integration, and security tests

- `OutcomeBadge.test.tsx`: label matrix.
- `CitationList.test.tsx`: draft and authoritative markers; superseded marker.
- `PlaygroundPage.test.tsx`: submit → poll → render; HOLD pending without approve control; toggle default off.

## 12. Commands for verification

```bash
cd apps/web && npm run lint && npm run typecheck && npm test && npm run build
```

## 13. Explicit non-goals

- No manager features (P4-04). No in-app approval. No client-side tier logic or filtering (server decides).

## 14. Risks and unresolved questions

- The label set depends on E2 / E4 evidence (G5 / G4). Keep labels data-driven.

## 15. Handoff deliverables for dependent agents

- P4-05: components and test IDs.
- P5-04: screenshots of each outcome for the demo script.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P4-03] Build the team web playground with honest outcome labels and hijack toggle`
- **Body outline:** components; label matrix; HOLD notice; toggle spec; tests.
- **Labels:** `phase-4-frontend`, `frontend`
- **Depends on:** P4-02 (soft P3-07, P1-07, P1-10)  **Blocks:** P4-05, P5-01

## 17. Definition of done

Merged with web CI green; demo screenshots captured; handoff posted.
