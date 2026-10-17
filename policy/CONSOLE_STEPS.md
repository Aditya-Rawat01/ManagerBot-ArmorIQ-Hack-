# ArmorIQ console steps for P1-08 (operator)

Who: the account holder of the Maya persona (Org Admin) in organization
`ArmorIQ-hackathon`. Time: about 15 minutes. Nothing here changes `memory-mcp`.

Rules for the whole procedure:
- Never paste a token, API key or real e-mail into chat, an issue, a PR or this repository.
- Record console messages verbatim in `docs/EXPERIMENTS.md` §E5 with e-mails replaced by
  `<MAYA_EMAIL>`-style placeholders and tokens removed.
- Console labels below follow the ArmorIQ docs (checked 17 Oct 2026). If a label differs
  on screen, follow the screen and note the real label in the E5 record.

## 0. Before you start

1. Wake the tool server: `curl -fsS https://managerbot-armoriq-hack.onrender.com/health`
   until it returns 200 (Render Free sleeps; the first call can take ~1 minute).
2. Have ready, privately: the value of `TOOLSERVER_BEARER_WORKSPACE` (Render → the tool
   server service → Environment; 64 characters), and the three lowercase member e-mails
   (`PERSONA_MAYA_EMAIL`, `PERSONA_SAM_EMAIL`, `PERSONA_PRIYA_EMAIL` from your local `.env`).
3. Confirm roles in the sidebar **Team** (or Organization → Members): Maya's account is
   **Org Admin**, Sam's and Priya's are **Analyst**, all **Active**. HOLD approval needs a
   higher-ranked approver; nobody can approve their own request.

## 1. Register `workspace-mcp`

Leave `memory-mcp` exactly as it is (registered 17 Oct; its saved auth works with the
proxy, E1/E2). Only add the second endpoint.

1. Optional, read-only: open **Asset Registry → MCP Servers → memory-mcp** and note which
   **Auth Method** it uses and where the token was entered. Do not click Edit or Save.
2. **Asset Registry → MCP Servers → Add MCP Server**.
3. **Selection:** choose **Manual Configuration** (Auto-Detect is fine too if it fills the
   fields below; check every field afterwards).
4. **Server Details:**
   - Server Name: `workspace-mcp` (exact, lowercase, hyphen)
   - Environment: the same as `memory-mcp` (Development unless it shows otherwise)
   - URL: `https://managerbot-armoriq-hack.onrender.com/workspace/mcp`
   - Port: `443` (if asked)
   - Transport: **HTTP / SSE** (streamable HTTP, JSON-RPC POST with SSE responses; the same
     as `memory-mcp`)
   - Protocol Version: `2024-11-05`
   - Auth Method: the same method `memory-mcp` uses; the secret is a **Bearer** token whose
     value is your `TOOLSERVER_BEARER_WORKSPACE` (paste it only into the console field;
     never the memory token: the tool server answers 401 to the other endpoint's token)
   - Version / Repository URL / Description: optional (`ProxyLead workspace tools`)
5. **Capabilities:** enable **Tools** only.
6. **Security:** defaults are fine. Finish. The first scan starts automatically.
7. When the scan finishes, open `workspace-mcp` → Tools. Expect exactly two tools:
   `create_followup_task` and `send_external_message`. Write down the names **as the console
   displays them** (bare, or prefixed with the server name): this answers U-6.
8. Open `memory-mcp` → Tools (read-only) and do the same. Expect 12 tools when P2-09 is
   deployed (9 before): the `knowledge_search_*`, `knowledge_get_source_*` and
   `read_source_*` tools for three tiers, then `knowledge_save_team_note`,
   `knowledge_confirm_maya_decision`, `knowledge_mark_superseded`. No `scaffold_ping`.
9. If the agent page (**Asset Registry → Agents → proxylead**) has an MCP server list,
   make sure both `memory-mcp` and `workspace-mcp` are linked to it.

If the console rejects the registration: copy the message verbatim (token removed) into
the E5 record and stop. Do not change `memory-mcp`.

## 2. Create the policy (Policy Studio)

1. Copy `policy/proxylead-policy.yaml` to a file **outside the repository** (for example
   your home directory). In that copy only, replace:
   - `<MAYA_EMAIL>`, `<SAM_EMAIL>`, `<PRIYA_EMAIL>` with the exact **lowercase** e-mails;
   - `<registered proxylead agent id>` with the ID shown on **Agents → proxylead**.
2. **Access Control → Policies → Policy Studio** (`/dashboard/policy-studio`), Single
   Policy Editor. If a previous draft appears, click **Clear Draft**.
3. **Metadata:** Policy Name `proxylead-team-authority`; Target Type **agent**; Target ID:
   select **proxylead** (selecting the target loads its tools into the dropdowns).
4. Paste the substituted YAML into the **YAML Editor** panel. Check that the form sections
   now show: **Scheduling & Defaults** status `active`, priority `100`, default enforcement
   `block`; **Member Rules** for three e-mails plus `*`.
   - If the YAML editor does not accept a field (most likely `toolEnforcement`, or the
     `"*"` rule), copy the error verbatim, then enter the same rules through the form
     (Member Rules → add member → allowed tools from the dropdown → enforcement action).
     Record exactly which fields the form offers.
   - If the dropdowns show tool names in a form other than the bare names in the file
     (for example `memory-mcp__knowledge_search_team`), use the console's form and record
     it (U-6; see `policy/README.md`).
5. Click **Save Policy**. Record the confirmation or error message verbatim.
6. Delete your substituted copy outside the repository.

## 3. Make it the one active policy for `proxylead`

1. **Access Control → Policies** (Policy List). Find `proxylead-team-authority`: status
   **active**, target **agent: proxylead**.
2. If any other policy targets `proxylead` (for example the starter policy created by the
   agent registration flow), set it to **inactive**. Do not delete anything. Record how
   many policies the organization now has (quota: 2) and any quota shown.
3. Open **Agents → proxylead** and confirm it lists `proxylead-team-authority` as its
   governing policy (E1/E2 ran with "no governing policy"; that must change now).

## 4. Where HOLD approvals appear (D-22, needed for E3/E4)

- **Governance → Plans Governance** (`/platform/plans` in the docs), filter **Needs you**:
  "plans held on a delegation waiting for your approval". Open the held plan to approve or
  reject it. Only the Org Admin (Maya) can approve Sam's request; Sam cannot approve his own.
- During `scripts/probe_v2_e4_hold.py` the probe prints the delegation ID as soon as the
  SDK reports the hold. Match it with the plan in the list, then approve or reject as the
  scenario says. Record the exact page path and button labels you used (D-22, P3-06 link
  text, P4-04 holds panel) and whether the page shows who approved (U-11).

## 5. Then run the checks (operator, repository root)

```bash
python scripts/check_setup.py           # exit 0 expected
python scripts/probe_v2_e5_setup.py     # misnamed tool: BLOCK/error, never ALLOW
```

Full procedures and record templates: `docs/EXPERIMENTS.md` §E5, §E3, §E4.
