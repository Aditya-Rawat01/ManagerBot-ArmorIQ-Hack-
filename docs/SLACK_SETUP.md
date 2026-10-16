# Slack app setup (P3-05)

How to create and configure the ProxyLead Slack app (Socket Mode, one
workspace). **Status: UNVERIFIED** — no live workspace access was available
while writing this; every grant, scope, and behavior below needs confirming
against the real app console and a live mention/DM check (P3-05 §12) before
anyone claims setup works.

> Secrets rule: tokens live in the Render secret store (production) or a
> local untracked `.env` (development). Never commit `.env`, tokens, or
> member emails — the repository is public.

## 1. Create the app

1. https://api.slack.com/apps → Create New App → From scratch.
2. Name: `ProxyLead`; pick the team's workspace (the only workspace ever
   authorized — `SLACK_TEAM_ID`).
3. Note the **Signing Secret** (unused: Socket Mode has no HTTP Events
   endpoint, so there is nothing to verify with it).

## 2. Socket Mode + tokens

1. Settings → Socket Mode → Enable.
2. Create an **app-level token** with `connections:write` → `SLACK_APP_TOKEN`
   (`xapp-...`). This token only opens the websocket; it cannot call Web API.
3. OAuth & Permissions → install to workspace → **bot token** → `SLACK_BOT_TOKEN`
   (`xoxb-...`).

## 3. Event subscriptions (Socket Mode tab, not Event Subscriptions URL)

Subscribe the bot to:

| Event | Purpose |
|---|---|
| `app_mention` | Channel mentions of `@ProxyLead` |
| `message.im` | Direct messages to the bot |

Do NOT subscribe `search.messages` or history scopes (PRD §4.2 forbids
assuming history access; D-15 sends the planner the current message only).

## 4. Bot scopes (requested)

| Scope | Needed for | Status |
|---|---|---|
| `app_mentions:read` | receiving `app_mention` events | UNVERIFIED |
| `im:history`, `im:read`, `im:write` | reading/sending DMs (PRD §4.2 notes DM sending may need `im:write`) | UNVERIFIED |
| `chat:write` | thread replies, generic notices, HOLD DM | UNVERIFIED |
| `channels:history` (read-only, subscribed channels) | mention `text`/`ts` payload fields | UNVERIFIED |
| `users:read` | resolving display metadata if ever needed (IDs, not names, authorize) | UNVERIFIED, optional |

Safe fallbacks: a missing `im:write` surfaces as failed DM delivery
(`delivery_failed`, never a channel fallback); a missing channel scope
surfaces as failed thread posts. Missing scopes never weaken validation —
events are still team/user/dedupe-checked first.

## 5. Environment variables

| Name | Value |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` (secret store / `.env` only) |
| `SLACK_APP_TOKEN` | `xapp-...` with `connections:write` (secret store / `.env` only) |
| `SLACK_TEAM_ID` | workspace ID (`T...`); events from any other team are rejected |
| `SLACK_ALLOWED_CHANNELS` | comma-separated channel IDs (`C...`) the bot may answer in |
| `SLACK_USER_MAYA_ID`, `_SAM_ID`, `_PRIYA_ID` | member → Slack user ID mapping (`U...`); unmapped senders are refused |

## 6. Local startup / shutdown

```bash
cp .env.example .env   # fill only the SLACK_* rows needed
uvicorn apps.api.main:create_app --factory --port 8000
```

- Without tokens the API serves HTTP only (`/health` green, Socket Mode
  skipped with a log line). With tokens but no orchestrator handle (P3-04
  pending), Socket Mode stays parked — a bot that cannot dispatch would be
  wrong.
- Shutdown: Ctrl-C stops uvicorn; the lifespan closes the Socket Mode
  handler (no leaked threads/sockets). Process placement (D-27) is pending
  approval — the current in-process daemon thread is PROPOSED, not decided.

## 7. Testing a mention and DM (pending live access)

1. In an allowed channel: `@ProxyLead what was decided about X?` → expect a
   thread reply (team) or a DM + generic notice (elevated).
2. DM the bot directly → expect a DM reply.
3. Send the same event twice (Slack retries `event_id`) → second is dropped;
   `slack_events` keeps one `received` → `processed` row.
4. As an unmapped user → a fixed polite refusal (a thread reply for a
   mention in an allowed channel, a DM for a DM; your text is never echoed),
   a `slack_event_rejected` audit row, no processing. From another team or a
   non-allowed channel → no reply at all (the bot stays silent there), an
   audit row, no processing.
5. `@ProxyLead store this Friday deployment is frozen` → one
   `knowledge_save_team_note` plan, author = your mapped identity.

Record outputs in Issue #26. Fakes (see `tests/api/test_slack_events.py`)
prove the validation order only — never live delivery.

## 8. Duplicate-event handling

Dedupe key is Slack's real `event_id`, persisted in `slack_events`
(UNIQUE). The claim is atomic (`INSERT ... ON CONFLICT DO NOTHING
RETURNING`): concurrent duplicates collapse to exactly one processing
attempt, losers get a `duplicate` audit row. Rows move `received` →
`processed` only after the orchestrator returns; a crash between leaves a
`received` row for P3-04/P3-09 to notice. A dedupe DB failure fails closed
(the event is not processed).

## 9. Troubleshooting

- **Bot silent in channel:** channel not in `SLACK_ALLOWED_CHANNELS`, or the
  app was not invited (`/invite @ProxyLead`).
- **DMs fail:** `im:write` missing (see §4 fallbacks).
- **Nothing starts:** tokens unset (by design, HTTP-only) or no orchestrator
  handle yet (P3-04) — check startup logs.
- **Wrong-team events:** rejected before any other check; verify
  `SLACK_TEAM_ID`.
- **Impersonation ("I am Maya"):** impossible by design — identity is the
  mapped Slack user ID, never message text (SC-03, tested).
