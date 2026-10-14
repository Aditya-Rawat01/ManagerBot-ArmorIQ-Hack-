# P2-10 — `workspace-mcp`: `send_external_message` honeypot and optional `create_followup_task`

| Field | Value |
|---|---|
| Phase | 2 — MCP and Knowledge |
| Owner | `samarth-chawla` |
| Size | S (≤ 2 h) |
| Target window | 15 Oct AM |
| Gate role | Required for G2/G3 (honeypot cell of E3); SC-02 |
| Status | Not started |

## 1. Task ID and title

P2-10 — Implement the non-networked `send_external_message` honeypot (writes only to `egress_sink`) and, if D-17 keeps it, `create_followup_task` (local `tasks` table) on `workspace-mcp`.

## 2. Purpose and expected outcome

The injection demo needs a real target that ArmorIQ blocks and that can never
cause egress. Outcome:

- two guarded tools in `{WORKSPACE_TOOLS_MODULE}`;
- the honeypot writes one synthetic `egress_sink` row when executed (only in an explicitly unguarded test) and makes no network call under any circumstances;
- the follow-up task tool has enum-bounded fields, an injected requester, and an idempotency key, or it is removed from the policy and UI (D-17).

## 3. Authoritative source references

- PRD §2 P0 (honeypot writes only to a local synthetic sink; blocked for all personas), §5.3 (block for Maya, Sam, Priya), §6.2, §10.3 (never network egress), §8.1 (`tasks`, `egress_sink` if those tools ship), §14.
- Arch §6.2 (both tools; remove `create_followup_task` from the policy and UI if not fully tested; keep the honeypot only if the guarded injection test is wired end to end; non-networked), §13.

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Reviewer: `Aditya-Rawat01` (tool-server steward).

## 5. Prerequisites and dependencies

- Hard: P2-01 (`tasks`, `egress_sink` tables), P2-06 (guarded dispatch and the registration pattern). Decision D-01 (module name).
- Decisions: **D-29** (honeypot arguments) is needed to finish; D-17 (ship the follow-up task?) by 16 Oct 12:00.
- Blocks: P1-08, P1-09, P3-07.

## 6. Exact files/modules this task owns

- `services/toolserver/{WORKSPACE_TOOLS_MODULE}` (new; PRD `tools_workspace.py`, Arch `tools_work.py`, D-01)
- `tests/toolserver/test_workspace_tools.py` (new)

## 7. Existing files it may need to modify

- `services/toolserver/servers.py` (steward `Aditya-Rawat01`): one registration line for the workspace tools, following the P2-06 pattern.
- `services/contracts/tool_args.py` (P2-02 owner): honeypot and task models via ICR if D-29 or D-17 change them.

## 8. Shared interfaces and contracts

- Tool names and endpoint (FIXED). Arguments per P2-02 / D-29.
- `egress_sink` and `tasks` tables (P2-01). The requester comes from the verified `_ctx`.

## 9. Step-by-step implementation requirements

1. **`send_external_message`.**
   - Validate the arguments (D-29).
   - Insert one row into `egress_sink` (synthetic destination label, bounded message, `jti`, requester, timestamp).
   - The module must not import any HTTP, SMTP, or socket client.
   - Return a result stating "synthetic sink write only".
2. **`create_followup_task`** (if D-17 keeps it).
   - Insert into `tasks` with `title`, `description`, `owner`, `priority` (enums), requester and workspace from `_ctx`, and a unique `idempotency_key` per workspace.
   - A duplicate key returns the original task (no second row).
3. Register both through `guarded(...)` on `workspace-mcp`.
4. If D-17 drops the follow-up task: do not register it, and notify P1-08 (policy) and P4-03 (UI).

## 10. Acceptance criteria

- With `socket` connections patched to raise, the honeypot still completes its sink write. Static check: no network-client imports in the module.
- Honeypot calls without a valid `_ctx` are rejected before any sink write.
- A follow-up task with a repeated idempotency key creates exactly one row.
- `tools/list` on `workspace-mcp` shows exactly the shipped tools.

## 11. Required unit, integration, and security tests

- `tests/toolserver/test_workspace_tools.py`:
  - `test_honeypot_never_uses_network`
  - `test_honeypot_requires_valid_ctx`
  - `test_honeypot_writes_only_egress_sink`
  - `test_task_idempotency`
  - `test_task_requester_from_ctx`
  - `test_task_enum_bounds`

## 12. Commands for verification

```bash
python -m pytest tests/toolserver/test_workspace_tools.py -q
```

## 13. Explicit non-goals

- No real email, Slack, ticket, or webhook egress. No policy changes (P1-08).

## 14. Risks and unresolved questions

- D-29 decides whether a confirmed honeypot plan reaches the ArmorIQ policy at all.
- D-17.

## 15. Handoff deliverables for dependent agents

- P1-08 / P1-09: the final workspace tool list.
- P3-07: honeypot argument shape and the sink query used to show "zero sink executions".

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P2-10] Implement non-networked send_external_message honeypot and create_followup_task`
- **Body outline:** honeypot rules; follow-up task fields; D-17 / D-29; tests.
- **Labels:** `phase-2-mcp-knowledge`, `mcp`, `security`
- **Depends on:** P2-01, P2-06; D-29, D-17  **Blocks:** P1-08, P3-07 (and P1-09 via P1-08)

## 17. Definition of done

Merged, deployed, tests green; final tool list announced to P1-08.
