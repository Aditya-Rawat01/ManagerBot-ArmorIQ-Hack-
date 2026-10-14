# P4-04 — Manager console and audit view (API + UI)

| Field | Value |
|---|---|
| Phase | 4 — Frontend |
| Owner | `parthgoyal26-03-2005` |
| Size | L (5–7 h) |
| Target window | 16 Oct AM |
| Gate role | SC-06 visibility; no hidden direct-query path (Arch §7.5) |
| Status | Not started |

## 1. Task ID and title

P4-04 — Implement Maya-only manager endpoints and the console UI:

- recent requests and execution timeline;
- pending holds with delegation status and ArmorIQ console link (no approve action);
- unanswered questions;
- topic-map editor;
- real health checks and connector labels;
- the draft review queue and authoritative decisions **only through the path decided in D-12**.

## 2. Purpose and expected outcome

Maya needs visibility and control without a backdoor around ArmorIQ.
Outcome:

- `/api/v1/manager/*` endpoints reading only backend-permitted data;
- a console UI showing persisted events correlated with ledger references;
- decision confirmation submitted as a normal ArmorIQ-authorized request (`knowledge_confirm_maya_decision`).

## 3. Authoritative source references

- PRD §5.2 (topic map configured by Maya), §8.4 (confirmation bound to exact record/version), **§10.2 manager view**, §1.1 SC-06.
- Arch §5.3.1 (topic-map editor separate from policy; map grants nothing), §5.6 (no in-app approval), **§7.5** (console accesses protected knowledge only through authorized tool-server operations; no hidden direct-query path), **§9 manager view** (timeline; holds with delegation ID/status + console link; unanswered; review queue; decisions with provenance; health from real checks; connector labels), §10.

## 4. Owner and proposed contributors

- Owner: `parthgoyal26-03-2005`.
- Contributors: `VTCodeCraft` (topic map module, orchestrator for confirmations), `samarth-chawla` (security review).

## 5. Prerequisites and dependencies

- Hard: P4-02, P3-01 (queries), P3-02 (`topic_map`).
- Soft: P1-10 (console link, D-22), P2-09 (confirm tool).
- Decisions: **D-12** blocks finishing the review-queue and decisions panels. The other panels can ship without it.
- Blocks: P4-05, P5-01.

## 6. Exact files/modules this task owns

- `apps/api/routes/manager.py` (new)
- `services/contracts/api_manager.py` (new)
- `tests/api/test_manager_api.py` (new)
- `apps/web/src/pages/ManagerConsolePage.tsx` (new) and `apps/web/src/components/manager/*` (new: `RequestTimeline`, `HoldsPanel`, `UnansweredPanel`, `TopicRulesEditor`, `HealthPanel`, `ReviewQueuePanel`, `DecisionsPanel`)
- `apps/web/src/services/manager.ts` (new) + tests

## 7. Existing files it may need to modify

Steward `VTCodeCraft`; review required:

- `apps/api/routes/router.py`: include the router.
- `services/contracts/__init__.py`, `schema_export.py`: register the models; regenerate the schema and TypeScript.

## 8. Shared interfaces and contracts

- Manager endpoints in SHARED_INTERFACES §1.1 (PROPOSED → FIXED on merge).
- Uses: P3-01 queries, P3-02 `topic_map`, the P1-03 session (role check), the P4-01 request flow for confirmations.

## 9. Step-by-step implementation requirements

1. **Authorization.** Every manager endpoint requires a session whose member has `app_role=manager`. Otherwise `403`. Server-side only; hiding the UI is not enough.
2. **Timeline.** Audit events for a request in order, with decision sources, `jti`, ledger entry references, delivery records, and tier delivered. No protected content.
3. **Holds.** Pending approvals with requester persona, tool, delegation ID/status, and a link to `ARMORIQ_CONSOLE_URL` (D-22 target). **No approve or reject buttons** (R-40).
4. **Unanswered.** Question, requester, reason, status. No protected snippets.
5. **Topic rules.** List and edit through `topic_map` (P3-02). The UI states that rules configure retrieval only and grant no access.
6. **Health.** Real checks: tool-server `/health`, backend DB connectivity, Slack socket status. Connector labels "Connected and tested" / "Unavailable" / "Planned" (Gmail, Drive, Meet = Planned).
7. **Review queue and decisions** (D-12): build them **only** after D-12 is decided, through the decided path. Until then the panels say "Not available yet". That is a temporary state, not an acceptable final state (see D-12). Confirming a draft creates a normal request whose plan is `knowledge_confirm_maya_decision` with `record_id` + `version`. It goes through ArmorIQ like any other request.

## 10. Acceptance criteria

- Sam and Priya sessions get `403` on every manager endpoint (parametrized test).
- No manager endpoint query touches protected tables (code review + backend-role test).
- The holds panel has a console link and no approval control (UI test).
- Health shows real failures (stop the tool server → "Unavailable").
- Confirmation goes through the orchestrator (test with fakes asserts a plan with `knowledge_confirm_maya_decision`).

## 11. Required unit, integration, and security tests

- `tests/api/test_manager_api.py`: role checks; no protected reads; timeline correlation; topic-rule validation.
- Web tests:
  - holds panel without approve control;
  - connector labels;
  - unavailable states;
  - "Not available yet" placeholders while D-12 is open.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_manager_api.py -q
cd apps/web && npm run lint && npm run typecheck && npm test && npm run build
```

## 13. Explicit non-goals

- No in-app approval. No policy editing. No fake connector health. No direct protected reads.

## 14. Risks and unresolved questions

- **D-12** (review queue / decisions access). D-22 (console link). D-16 (reset).

## 15. Handoff deliverables for dependent agents

- P4-05: access-control surfaces to test.
- P5-01 / P5-04: the console walkthrough for the demo (timeline row → ledger reference).

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P4-04] Build Maya-only manager console: timeline, holds, unanswered, topic map, health`
- **Body outline:** endpoints; role check; panels; D-12-gated panels; confirmation flow; tests.
- **Labels:** `phase-4-frontend`, `frontend`, `backend`, `security`
- **Depends on:** P4-02, P3-01, P3-02 (soft: P1-10, P2-09); D-12 (review/decisions panels)  **Blocks:** P4-05, P5-01

## 17. Definition of done

Merged with API and web tests green; D-12 panels built through the decided
path, or an explicit P0 descope recorded in the README limitations (P5-04)
and agreed by the team; handoff posted.
