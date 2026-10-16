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

## 3. Event subscriptions

Features → **Event Subscriptions** → turn **Enable Events** on. With Socket
Mode enabled no Request URL is needed: Slack delivers the events over the
websocket. Under **Subscribe to bot events** add:

| Event | Purpose | Scope it needs |
|---|---|---|
| `app_mention` | Mentions of `@ProxyLead` in channels it was invited to | `app_mentions:read` |
| `message.im` | Direct messages to the bot | `im:history` |

Also enable App Home → **Messages Tab** ("Allow users to send Slash commands
and messages from the messages tab"), otherwise users cannot DM the bot
(UNVERIFIED).

Do not subscribe `message.channels` / `message.groups`, and never call
`search.messages` or `conversations.history`: PRD §4.2 forbids assuming
history access and D-15 (RESOLVED) gives the planner the current message
only.

## 4. Bot scopes (least privilege)

Requested (the minimum for the two subscriptions above and the senders):

| Scope | Needed for | Status |
|---|---|---|
| `app_mentions:read` | receiving `app_mention` events | UNVERIFIED |
| `im:history` | receiving `message.im` events (DM text) | UNVERIFIED |
| `chat:write` | thread replies, generic notices, DM posts, unmapped-user refusal | UNVERIFIED |
| `im:write` | `conversations.open` before every DM (elevated answers, HOLD DM to Maya, DM refusals) | UNVERIFIED |

Not requested (PRD §4.2 lists them "to test/document", but nothing here
needs them): `channels:history`, `groups:history` (no channel `message`
subscription, no history reads, D-15), `im:read` (DM channels are opened with
`im:write`, never listed), `users:read` (identity is the mapped Slack user
ID; no profile lookups).

Safe fallbacks: a missing `im:write` / `chat:write` surfaces as a typed
`SlackError` carrying Slack's code (`missing_scope`, `not_in_channel`, ...)
and failed delivery (`delivery_failed`, never a fallback to another
destination). Missing scopes never weaken validation: events are still
team/channel/user/dedupe-checked first, and every post is sent with
`unfurl_links=false`, `unfurl_media=false`.

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

- Placement (D-27, RESOLVED by `VTCodeCraft`, 17 Oct 2026, issue #26): the
  Socket Mode client runs INSIDE the API process, started and stopped by
  the FastAPI lifespan (same Render service, settings and database pool).
  Run a single uvicorn worker: never add `--workers N`, which would open one
  websocket per worker.
- Without tokens the API serves HTTP only (`/health` green, Socket Mode
  skipped with a log line). With tokens but no orchestrator handle (P3-04
  sets `app.state.orchestrator_handle`), Socket Mode stays parked: a bot
  that cannot dispatch would be wrong. The backend database is opened (and
  the single workspace resolved) only when Slack actually starts.
- API startup and `/health` never depend on Slack: an invalid or revoked
  token (Bolt's `auth.test` fails), an unreachable Slack, or any other start
  error is logged by exception class and the API keeps serving HTTP.
- Bolt runs listeners on its own threads; accepted events are handed to the
  orchestrator on the API's event loop, so work `handle` schedules keeps
  running after it returns.
- Shutdown: Ctrl-C stops uvicorn; the lifespan closes the Socket Mode
  handler (no leaked threads/sockets).

## 7. Testing a mention and DM (pending live access)

1. In an allowed channel: `@ProxyLead what was decided about X?` → expect a
   thread reply (team) or a DM + generic notice (elevated).
2. DM the bot directly → expect a DM reply.
3. Send the same event twice (Slack retries with the same outer `event_id`)
   → the second is dropped; `slack_events` keeps one `received` →
   `processed` row.
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

The dedupe key is Slack's real `event_id` from the OUTER `event_callback`
envelope (`body["event_id"]`, e.g. `Ev0ABC123`; the inner `event` object does
not carry one), persisted in `slack_events` (UNIQUE). A body without it is
dropped as `invalid_envelope`. The claim is the last validation step and is
atomic (`INSERT ... ON CONFLICT DO NOTHING RETURNING`): concurrent
duplicates collapse to exactly one processing attempt, losers get a
`duplicate` audit row. Events that cannot become a request (no text, no
channel, ...) are audited before the claim and never leave a row behind.
Rows move `received` → `processed` only after the orchestrator returns; a
crash or a failed dispatch leaves a `received` row for P3-04/P3-09 to notice.
A dedupe DB failure fails closed (the event is not processed).

Every refused event gets one metadata-only `slack_event_rejected` row
(`request_events`, key `rej-<event_id>`: reason + Slack event/team/channel
IDs, never text or user IDs). Those rows are not requests: the manager
console's recent-requests list never shows them. Slack retries reach the
user check before the dedupe claim, so a retried event from an unmapped user
can get the refusal twice (rare: events are acknowledged first).

## 9. Troubleshooting

- **Bot silent in channel:** channel not in `SLACK_ALLOWED_CHANNELS`, or the
  app was not invited (`/invite @ProxyLead`).
- **DMs fail:** `im:write` missing, or the App Home Messages Tab is off (see
  §3/§4). The `SlackError` names Slack's code (`missing_scope`, ...).
- **Thread replies fail with `not_in_channel`:** invite the bot to the
  channel.
- **Nothing starts:** tokens unset (by design, HTTP-only) or no orchestrator
  handle yet (P3-04) — check startup logs. `slack socket mode failed to
  start ... error=BoltError` usually means an invalid or revoked bot token.
- **Wrong-team events:** rejected before any other check; verify
  `SLACK_TEAM_ID`.
- **Impersonation ("I am Maya"):** impossible by design — identity is the
  mapped Slack user ID, never message text (SC-03, tested).
