# ProxyLead MCP tool server (`services/toolserver`)

Separately deployed MCP server exposing the two endpoints registered in
ArmorIQ: `memory-mcp` and `workspace-mcp`. In the product it is the only
component allowed to read protected knowledge (`docs/IMPLEMENTATION_CONTRACT.md`).

**Foundation status:** transport, endpoint authentication, and one
scaffold-only diagnostic tool per endpoint. **No product tool exists yet**,
no `_ctx` verification, no database, no ledger. It has **not** been
registered in ArmorIQ, and remote connectivity through ArmorIQ's proxy is
unverified (experiment E1, `docs/EXPERIMENTS.md`).

## Design

- Official MCP Python SDK `mcp==2.3.0` (`mcp.server.mcpserver.MCPServer`,
  formerly `FastMCP`), streamable HTTP in **stateless** mode with SSE
  responses (ADR-006).
- Two `MCPServer` instances mounted in one FastAPI app; their session managers
  run in the FastAPI lifespan.
- Each endpoint requires `Authorization: Bearer <token>`; a missing token
  configuration makes that endpoint answer **503 to everything** (fail closed).
- DNS-rebinding protection: only `MCP_ALLOWED_HOSTS` Host values are accepted
  (421 otherwise); any browser `Origin` header is rejected (403).

| Path | Endpoint | Credential env var |
|---|---|---|
| `GET /health` | liveness (public) | — |
| `POST /memory/mcp` | `memory-mcp` | `TOOLSERVER_BEARER_MEMORY` |
| `POST /workspace/mcp` | `workspace-mcp` | `TOOLSERVER_BEARER_WORKSPACE` |

Scaffold-only tools: `memory_scaffold_ping`, `workspace_scaffold_ping` (static
metadata, no data access; disable with `MCP_ENABLE_DIAGNOSTIC_TOOLS=false`).

## Setup

From the repository root:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[toolserver,dev]"   # macOS/Linux: .venv/bin/python
```

A deployment installs only `pip install ".[toolserver]"` (no `armoriq-sdk`,
`anthropic`, or `slack-bolt`).

Set at least the bearer tokens in the root `.env` (see `../.env.example`).

## Run independently

```bash
uvicorn services.toolserver.main:create_app --factory --port 8100
```

Smoke test (the request shape from ArmorIQ's MCP format docs):

```bash
curl -s http://localhost:8100/health
curl -s -X POST http://localhost:8100/memory/mcp \
  -H "Authorization: Bearer $TOOLSERVER_BEARER_MEMORY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

When deployed, set `MCP_ALLOWED_HOSTS` to the public hostname and serve over
HTTPS (ArmorIQ requires a public HTTPS endpoint whose URL ends in `/mcp`).

## Checks

```bash
ruff check . && ruff format --check .
python -m pytest tests/toolserver
```

Tests cover health, endpoint authentication (missing / wrong / cross-endpoint
/ unconfigured credentials), the MCP handshake with protocol version
`2024-11-05`, SSE framing, `tools/list` and `tools/call` content shape,
Host/Origin protection, configuration, and the "never import armoriq_sdk"
guardrail.

## Known E1 risks (recorded by tests)

- The SDK transport answers **406** unless the client accepts both
  `application/json` and `text/event-stream`. If ArmorIQ's proxy sends only
  `application/json`, switch to the hand-written JSON-RPC/SSE fallback (ADR-006).
- SSE frames use CRLF line endings (valid SSE); confirm the proxy parses them.

## Phase 2 (not implemented)

`_ctx` identity-envelope verification, underscore-key stripping, strict
per-tool schemas, tier-filtered hybrid retrieval (`services/knowledge/`), idempotency
by `jti`, the hash-chained execution ledger (`services/toolserver/ledger.py`), and the
`send_external_message` honeypot writing only to a local sink.
