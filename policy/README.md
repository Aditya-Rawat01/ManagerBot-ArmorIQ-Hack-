# `policy/`: the ProxyLead ArmorIQ policy (P1-08)

- `proxylead-policy.yaml`: the one active policy for agent `proxylead` (Arch §5.5), with
  **placeholders only** (`<MAYA_EMAIL>`, `<SAM_EMAIL>`, `<PRIYA_EMAIL>`,
  `<registered proxylead agent id>`). This repository is public (D-26 / ADR-022).
- `CONSOLE_STEPS.md`: the operator's click-by-click steps (register `workspace-mcp`,
  create and attach the policy, find HOLD approvals).

Never commit a substituted copy. Substitute the real lowercase member e-mails in a copy
**outside the repository** (or type them directly in the console), then delete that copy.
Check before every push:

```bash
git grep -nE "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" -- policy/ || echo "no emails"
```

## Matrix (PRD §5.1 + honeypot)

| Tool | Maya | Sam | Priya |
|---|---|---|---|
| `knowledge_search_team`, `knowledge_get_source_team`, `read_source_team` | ALLOW | ALLOW | ALLOW |
| `knowledge_search_confidential`, `knowledge_get_source_confidential`, `read_source_confidential` | ALLOW | **HOLD** (Maya approves) | BLOCK |
| `knowledge_search_restricted`, `knowledge_get_source_restricted`, `read_source_restricted` | ALLOW | BLOCK | BLOCK |
| `knowledge_save_team_note` (draft notes only) | ALLOW | ALLOW | ALLOW |
| `knowledge_confirm_maya_decision`, `knowledge_mark_superseded` | ALLOW | BLOCK | BLOCK |
| `create_followup_task` (ships: D-17 / ADR-024) | ALLOW | ALLOW | ALLOW |
| `send_external_message` (honeypot) | BLOCK | BLOCK | BLOCK |
| any other member (`"*"`) / any unlisted tool | BLOCK | BLOCK | BLOCK |

The tool server enforces its own tier ceiling (Maya → restricted, Sam → confidential,
Priya → team) whatever the policy says; E3 step 5 reports it separately.

## Open schema questions (answer in `docs/EXPERIMENTS.md` §E5, U-6 / U-4)

The ArmorIQ docs (Platform → Policy Studio → Single Policy Editor, checked 17 Oct 2026)
document: target type `agent`, status `draft|active|inactive|expired`, a numeric priority,
default enforcement `allow|block|hold|allow_log`, and member rules keyed by e-mail or ID
with allowed tools and a per-member enforcement action. They do **not** document
`toolEnforcement`, the tool-name format, or whether held tools must be in `allowedTools`.
The docs also say a policy with no matching rule **allows by default**, which is why the
file sets `defaultEnforcementAction: block` and a `"*"` block rule. Record for E5:

1. Is the YAML accepted as-is (paste it into the Policy Studio YAML editor)? Copy any
   error verbatim (e-mails removed).
2. Is per-member `toolEnforcement` accepted and enforced through the proxy (U-4)? If the
   console has no per-tool action, record it: Sam's HOLD then cannot be expressed in one
   member rule, and E3 fails with the fallback recorded (never fake a HOLD in the app).
3. Must held tools also appear in `allowedTools`? The file lists them in both (template).
4. Effective default: does an unlisted tool or member get BLOCK?

## Tool-name representation (U-6)

The file uses **bare action names** (`knowledge_search_team`), the template's form. That is
what `tools/list` returns, what the console scan listed for `memory-mcp` (9 tools on
17 Oct, 12 after P2-09), and what the SDK sends to the proxy: `invoke_with_policy(mcp,
action, ...)` with `mcp = memory-mcp | workspace-mcp` and `action = <tool>`.

The other form in play is the SDK plan form `<mcp>__<action>`
(`memory-mcp__knowledge_search_team`, `workspace-mcp__send_external_message`): the gateway
builds plan steps with it and the SDK splits it back into (mcp, action). If the console
dropdown shows qualified names, or the bare-name policy does not match (E5: the per-member
ALLOW in `check_setup.py` or the E3 BLOCKs do not appear), switch every entry to the
qualified form, re-apply, re-run E5, and file an ICR against `ToolName`
(SHARED_INTERFACES §2). All 14 names are unique across both endpoints, so the bare form is
unambiguous.

## Re-applying (P5-03)

After P2-09 deploys new tools, or in a fresh organization: rescan both endpoints, re-apply
this file with the same substitutions, attach it to `proxylead`, then run
`python scripts/check_setup.py` (exit 0) and `python scripts/probe_v2_e5_setup.py`.
