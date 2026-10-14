# ProxyLead

A Slack-first AI representative of a busy engineering manager, built for the
ArmorIQ Flow State hackathon (Oct 10–17, 2026). Team members ask what was
decided and why, retrieve authorized team knowledge, and save notes, while
every protected read or action is authorized per requester through ArmorIQ.

> **Status:** project foundation only. No ArmorIQ experiment has been run yet
> (see `docs/EXPERIMENTS.md`). Nothing in this repository should be read as a
> claim that an ArmorIQ control has been verified.

## Repository layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI API: trusted identity, orchestration, ArmorIQ gateway, delivery routing |
| `frontend/` | React + TypeScript (Vite) web playground and manager console |
| `mcp-server/` | Separately deployed MCP tool server (`memory-mcp`, `workspace-mcp`) |
| `contracts/` | Shared data contracts (Pydantic source of truth → JSON Schema → TypeScript) |
| `docs/` | Implementation contract, architecture decisions, workflow, experiments |
| `ProxyLead_PRD_v2.md` | Authoritative product requirements |
| `ProxyLead_Architecture_v2.1.md` | Authoritative architecture / implementation contract |
| `ProxyLead_Day1_Experiments.md` | ArmorIQ experiment procedures (E1–E5, E17) |

Setup instructions are added as each application is scaffolded.
