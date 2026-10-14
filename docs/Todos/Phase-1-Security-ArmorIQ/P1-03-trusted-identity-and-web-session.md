# P1-03 — Trusted identity mapping and web demo session

| Field | Value |
|---|---|
| Phase | 1 — Security and ArmorIQ |
| Owner | `VTCodeCraft` |
| Size | M (2–5 h) |
| Target window | 14 Oct |
| Gate role | Authentication layer (Arch §5.4 layer 1); SC-03 |
| Status | Not started |

## 1. Task ID and title

P1-03 — Implement trusted requester identity (Slack user ID mapping and signed web session) in `apps/api/identity.py`.

## 2. Purpose and expected outcome

Every request carries an `AuthenticatedUser` resolved by trusted server code,
never from message text or request bodies. Outcome:

- Slack events resolve through `SLACK_TEAM_ID` + `SLACK_USER_{MAYA,SAM,PRIYA}_ID`.
- Web sign-in checks `DEMO_PASSCODE` and sets a signed HTTP-only session cookie. Later requests read identity only from that session.
- The gateway can obtain the member's ArmorIQ email; nothing else can.
- Emails are masked everywhere else.

## 3. Authoritative source references

- PRD §1 (identity must resolve to the exact invited member; display names and persona text are not identity), §1.1 SC-03, §4.1 (web persona + `DEMO_PASSCODE` → signed HTTP-only session; Slack ID mapping; mask emails; chat text cannot change identity), §4.2 (`SLACK_TEAM_ID`), §12.
- Arch §0.5, §3.1 (identity row), §4 (trusted mapping; never trust identity in text), §5.2 (email from trusted mapping only), §5.4 layer 1, §9 (public demo safety: session determines identity; no persona per chat payload; secrets and emails off the client), §13 (impersonation test).

## 4. Owner and proposed contributors

- Owner: `VTCodeCraft`.
- Security reviewer: `samarth-chawla`. Consumer: `parthgoyal26-03-2005` (P4-02).

## 5. Prerequisites and dependencies

- Hard: none.
- Decisions: D-20 (member and workspace IDs), needed to **finish**.
- Blocks: P3-02 (soft), P3-04, P3-05, P4-01, P4-02.

## 6. Exact files/modules this task owns

- `apps/api/identity.py` (new)
- `apps/api/routes/session.py` (new)
- `services/contracts/api_session.py` (new: `SessionCreateRequest`, `SessionInfo`)
- `tests/api/test_identity.py` (new; Arch §11 documented name, placed per M-02)
- `tests/api/test_session.py` (new)

## 7. Existing files it may need to modify

All steward `VTCodeCraft` (same owner):

- `apps/api/routes/router.py`: include the session router.
- `apps/api/main.py`: CORS `allow_credentials=True` for the cookie, with explicit origins only.
- `apps/api/core/config.py`: no new variables expected (`SESSION_SECRET`, `DEMO_PASSCODE`, `PERSONA_*`, `SLACK_USER_*`, `SLACK_TEAM_ID` exist).
- `services/contracts/__init__.py`, `services/contracts/schema_export.py`: register the session models. Regenerate `services/contracts/schemas/proxylead-contracts.schema.json` and `apps/web/src/types/generated/contracts.ts`.

## 8. Shared interfaces and contracts

- Produces: `AuthenticatedUser` (FIXED); resolver functions (SHARED_INTERFACES §2, PROPOSED); `POST`/`GET`/`DELETE /api/v1/session` (§1.1, PROPOSED); `SessionInfo` (new contract).
- `armoriq_email_for(member_id)` is backend-internal. Only `armoriq_gateway.py` may call it (test-enforced by an import-site scan).
- Error responses use `ErrorResponse` (FIXED).

## 9. Step-by-step implementation requirements

1. Build an immutable member table at startup from settings: persona → (member ID per D-20, display name, app role, Slack user ID, ArmorIQ email). App roles follow `AppRole`: Maya=manager, Sam=senior, Priya=junior. Missing values make that member unavailable; the app still starts (health must work without secrets).
2. `resolve_slack_user(team_id, slack_user_id)`: return the member only if `team_id == SLACK_TEAM_ID` and the user ID matches exactly one configured member. Otherwise return `None`. Text, display names, and profile emails are never inputs.
3. `POST /api/v1/session` with `{persona, passcode}`:
   - Constant-time compare against `DEMO_PASSCODE`.
   - If `DEMO_PASSCODE` or `SESSION_SECRET` is unset, sign-in is disabled (`503`, code `sign_in_unavailable`).
   - Wrong passcode: `401`, generic message.
   - Success: sign a session cookie with `SESSION_SECRET` (HTTP-only; `SameSite=Lax` or stricter; `Secure` unless `APP_ENV=development`/`test`; limited lifetime). Record the cookie library choice (stdlib HMAC or an added dependency) in an ADR.
4. `resolve_web_session(cookie)`: verify the signature and expiry, then return the member. Tampered or expired → `None` → `401`.
5. Masking helper `mask_email(...)`: keep the first character of the local part and of the domain label, for example `s***@e***.com`. Every `AuthenticatedUser.masked_email` uses it. Raw emails never appear in responses or logs.
6. `GET /api/v1/session` returns `SessionInfo`. `DELETE` clears the cookie.
7. Never read persona, role, member ID, or email from any later request body. P4-01 enforces this on its routes using this module.

## 10. Acceptance criteria

- A Slack user ID not in the mapping, or the wrong team ID, resolves to `None`.
- Sign-in with the correct passcode sets a cookie with the required flags. A tampered cookie is rejected.
- No response body, log line, or exception message contains a raw configured email (tested with `caplog` and response scans).
- With all secrets unset, the app starts, `/health` is `200`, and sign-in returns `503`.
- Only `services/authorization/armoriq_gateway.py` imports or calls `armoriq_email_for` (test).
- Session contract models exported. JSON Schema and TypeScript regenerated with no drift.

## 11. Required unit, integration, and security tests

- `tests/api/test_identity.py`:
  - unknown Slack user, wrong team, and a valid mapping for each persona;
  - text claiming to be Maya cannot change the resolution (the resolver has no text parameter; a property test over messages);
  - masked email format;
  - email never exposed.
- `tests/api/test_session.py`:
  - correct and wrong passcode;
  - disabled when unset;
  - cookie flags;
  - tampered and expired cookie;
  - sign-out;
  - `SessionInfo` has no raw email.
- Guardrail test: call sites of `armoriq_email_for`.

## 12. Commands for verification

```bash
python -m pytest tests/api/test_identity.py tests/api/test_session.py tests/api/test_guardrails.py -q
python -m services.contracts.schema_export --check
cd apps/web && npm run gen:types && git diff --exit-code -- src/types/generated
bash scripts/check.sh
```

## 13. Explicit non-goals

- No SSO, OAuth, sign-up, or password store (PRD §2 not-in-MVP).
- No ArmorIQ calls. No Slack event handling (P3-05). No web UI (P4-02).
- No rate limiting beyond the constant-time compare (record it as a known limitation).

## 14. Risks and unresolved questions

- D-20: member and workspace ID scheme must match P2-03 seed rows and ledger `requester_member_id`.
- Web hosting (ADR-030): the web app is on Vercel and reaches the API through a same-origin `/api` rewrite, so `SameSite=Lax` works. Do not switch to `SameSite=None`. P5-03 verifies the cookie on the deployed URLs.
- Passcode brute force on a public demo. Document the limitation; consider a small delay.

## 15. Handoff deliverables for dependent agents

- P3-04 / P3-05: `resolve_slack_user`; how to build `AuthenticatedUser` / `RequestContext`.
- P4-01 / P4-02: session endpoints, cookie behavior, `SessionInfo` TypeScript type, `401` / `503` semantics.
- P1-02: `armoriq_email_for`.

## 16. GitHub issue title, body outline, labels, and dependencies

- **Title:** `[P1-03] Implement trusted identity mapping and signed web demo session`
- **Body outline:** deliverables; endpoints; acceptance criteria; spoofing tests; evidence = passing tests + no-email scan.
- **Labels:** `phase-1-security`, `security`, `backend`
- **Depends on:** —  **Blocks:** P3-04, P3-05, P4-01, P4-02

## 17. Definition of done

Merged with tests green; contracts regenerated; reviewed by `samarth-chawla`;
handoff posted to the P3-04, P3-05, P4-01, and P4-02 issues.
