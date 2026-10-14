# ProxyLead web app (`apps/web`)

React + TypeScript web app (Vite): the public web playground and, later, the
manager console. It talks only to the backend API and never holds secrets.

**Foundation status:** application shell with a typed backend health check
(loading / success / error + retry). Chat, sign-in, request evidence, and the
manager console are visibly labeled placeholders.

## Stack

React 19.3 · TypeScript 6.0.3 · Vite 8.3 · Vitest 5 + Testing Library (jsdom)
· ESLint 10 + typescript-eslint. Exact versions are pinned in `package.json`
and `package-lock.json`. TypeScript stays on 6.0.x because typescript-eslint
does not yet support 7.x (`docs/ARCHITECTURE_DECISIONS.md`, ADR-008).

## Setup

Requires Node.js ≥ 22.12.

```bash
cd apps/web
npm ci
cp .env.example .env.local   # optional; defaults to http://localhost:8000
npm run dev                  # http://localhost:5173
```

Start the API (`apps/api/README.md`) to see the health indicator turn
green. The API must list `http://localhost:5173` in `CORS_ALLOWED_ORIGINS`
(the default).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server on port 5173 |
| `npm run build` | Type-check (`tsc -b`) and production build into `dist/` |
| `npm run lint` | ESLint, zero warnings allowed |
| `npm run typecheck` | TypeScript project build without emit |
| `npm test` | Vitest once (`npm run test:watch` to watch) |
| `npm run gen:types` | Regenerate `src/types/generated/contracts.ts` from the shared JSON Schema |

## Layout

```text
src/
  App.tsx, main.tsx, index.css
  config.ts                 VITE_API_BASE_URL handling
  components/               AppHeader, HealthStatus, PlaceholderPanel, ConversationPlaceholder
  pages/PlaygroundPage.tsx  shell layout (responsive: one column under ~830px)
  hooks/useHealthCheck.ts   loading/success/error state with abort + retry
  services/apiClient.ts     fetch wrapper → ApiError (mirrors backend ErrorResponse)
  services/health.ts        typed health check with runtime contract validation
  types/                    re-exports of generated contract types (never hand-written)
  test/setup.ts             jest-dom matchers for Vitest
```

## Rules

- Only `VITE_*` variables reach the browser bundle; never put secrets in them.
- Shared shapes come from `src/types/generated/contracts.ts`; edit
  `services/contracts` instead and run `npm run gen:types`.
- Never send persona, role, or identity in request bodies; identity comes from
  the server-side session (later phase).
- Show only real, persisted outcomes. No simulated approvals.
