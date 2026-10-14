# P1-01 — ArmorIQ organization, members, and agent setup

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `samarth-chawla` |
| Size | S (≤ 2 h, mostly console work) |
| Target window | 14 Oct |
| Gate role | Prerequisite of G1 (E1) and G2 (E5) |
| Status | Not started |

## 1. Task ID and title

P1-01 — Set up the real ArmorIQ organization, invite the three demo members, and register the `proxylead` agent.

## 2. Purpose and expected outcome

Every live experiment depends on a real, active ArmorIQ organization. At the
end of this task:

- the organization is confirmed active and its quotas are recorded;
- three real member accounts (acting as Maya, Sam, Priya) are invited and active, with Maya as Org Admin outranking Sam and Priya;
- the agent `proxylead` is registered under that exact name;
- an API key exists in local `.env` files and the deployment secret store only;
- the non-secret facts are recorded in `docs/EXPERIMENTS.md`.

## 3. Authoritative source references

- PRD §0.1 (experiments first), §1 (personas, identity must resolve to invited members), §2 P0 (one agent, one policy, two endpoints, three members; verify quotas), §4.1 (mask emails), §13 (13 Oct: verify org, invite members, confirm quota and names).
- Arch §1 (demo personas), §5.1 (quotas: 2 MCP endpoints, 2 policies, 5 members; confirm in console), §5.6.8 (approver must outrank requester), §12 E5, §14 (13 Oct).

## 4. Owner and proposed contributors

- Owner: `samarth-chawla`.
- Contributors: the team members whose real accounts act as Maya, Sam, and Priya. Maya (ArmorIQ Org Admin, console approver, host) is `Aditya-Rawat01`, the project owner (confirmed 14 Oct 2026). The team picks Sam and Priya from `samarth-chawla`, `VTCodeCraft`, `parthgoyal26-03-2005`. Org Admin access and the API key were moved here from blocker issue #1.
- Reviewer: `VTCodeCraft`.

## 5. Prerequisites and dependencies

- Hard: none. Requires real ArmorIQ console access. Without it the outcome is `BLOCKED ON ACCESS`.
- Decisions: D-25 (who is who) for issue assignment only.
- Blocks: P1-02 (API key needed for live use), P1-06, P1-08, P1-10.

## 6. Exact files/modules this task owns

- `docs/EXPERIMENTS.md`: adds a **"Setup record"** section (the file's steward is `samarth-chawla`).
- Local-only, never committed: root `.env` values `ARMORIQ_API_KEY`, `ARMORIQ_AGENT_ID`, `PERSONA_MAYA_EMAIL`, `PERSONA_SAM_EMAIL`, `PERSONA_PRIYA_EMAIL`.

## 7. Existing files it may need to modify

- None besides `docs/EXPERIMENTS.md`. `.env.example` already contains every needed variable name. Do not edit it.

## 8. Shared interfaces and contracts

- Variable names (FIXED, PRD §12): `ARMORIQ_API_KEY`, `ARMORIQ_AGENT_ID=proxylead`, `ARMORIQ_CONSOLE_URL`, `PERSONA_{MAYA,SAM,PRIYA}_EMAIL`.
- Agent name `proxylead` (FIXED, PRD §9, Arch §5.1).
- Produces the facts consumed by SHARED_INTERFACES §2 (raw-email boundary).

## 9. Step-by-step implementation requirements

1. Sign in to the ArmorIQ console with the Org Admin account and confirm the organization is active.
2. Record the quotas the console actually shows (MCP endpoints, policies, members, agents). Note any difference from Arch §5.1.
3. With the team, decide which three real accounts act as Maya, Sam, and Priya. Invite them; confirm each is **active** (not pending). Confirm Maya holds the Org Admin role and outranks Sam and Priya (Arch §5.6.8). Record persona → masked email (for example `s***@d***.com`) → role. **Never record full emails in the repository.**
4. Register the agent with the exact name `proxylead`. Record the name and ID as the console shows them.
5. Create a service API key. Store it only in local `.env` files and later the deployment secret store. Record only the key *type* (test or live), never the value or a prefix long enough to identify it.
6. Do not register MCP endpoints yet: that needs the public URL (P1-05) and happens in P1-06.
7. Record, as an **unverified observation**, where the console appears to show pending delegations (input for E4/D-22).
8. Share non-secret values (agent ID, masked emails) through the team's private channel, not GitHub issues.

## 10. Acceptance criteria

- `docs/EXPERIMENTS.md` has a "Setup record" with UTC timestamp, observed quotas, the three members (persona, masked email, role, active status), agent name and ID, and who holds the Maya account.
- The Maya account is active, Org Admin, and outranks Sam and Priya. Evidence: console screenshot reference stored outside the repository, or a description.
- `git grep` finds no full email address or API key in the repository.
- If access is unavailable, the record says `BLOCKED ON ACCESS` with the reason, and the team is notified in the P1-01 issue.

## 11. Required unit, integration, and security tests

- No code tests.
- Security check: a secret and PII scan of the diff (command below) shows no emails or keys.

## 12. Commands for verification

```bash
git diff -- docs/EXPERIMENTS.md
git grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" -- docs/ || echo "no emails"
git grep -nE "ak_(live|test)_[A-Za-z0-9]{6,}" || echo "no keys"
```

## 13. Explicit non-goals

- No MCP endpoint registration (P1-06), policy (P1-08), or SDK calls (P1-02).
- No extra members, agents, or policies beyond the documented one/three.

## 14. Risks and unresolved questions

- No org access, or invites stuck in pending. Then the outcome is BLOCKED ON ACCESS and every gate slips (critical path).
- Quotas differ from Arch §5.1. Record them and re-plan (two endpoints, one policy).
- Approver ranking may not be configurable as assumed (E4 verifies).
- D-25: confirm real handles and who acts as each persona.

## 15. Handoff deliverables for dependent agents

- P1-02 / P1-06: agent registered; the API key is available to the owner locally.
- P1-08: three active member emails (shared privately) for policy keys.
- P1-10: name of the person who will approve as Maya in the console.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-01] Set up ArmorIQ organization, members, and proxylead agent`
- **Body outline:** goal; console checklist (steps 1–8); acceptance criteria; evidence = Setup record in `docs/EXPERIMENTS.md`; no secrets in the issue.
- **Labels:** `phase-1-security`, `armoriq`, `security`, `gate`
- **Depends on:** —  **Blocks:** P1-06, P1-08 (soft: P1-02 live use; P1-10 Maya approver, via P1-09)

## 17. Definition of done

Setup record merged; three members active with correct roles; `proxylead`
registered; no secrets or full emails committed; handoff comment posted on
the P1-06 and P1-08 issues.
