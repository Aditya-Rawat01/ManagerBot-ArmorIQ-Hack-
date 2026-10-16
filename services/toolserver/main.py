"""MCP tool-server application factory.

Run locally (from the repository root):

    uvicorn services.toolserver.main:create_app --factory --port 8100

Endpoints:
    GET  /health          readiness (public): 200 once the embedding model is
                          loaded (lifespan warm, P2-05), else 503 `not_ready`
    POST /memory/mcp      memory-mcp    (bearer: TOOLSERVER_BEARER_MEMORY)
    POST /workspace/mcp   workspace-mcp (bearer: TOOLSERVER_BEARER_WORKSPACE)

Both MCP endpoints use the SDK's streamable HTTP transport in stateless mode
with SSE responses (ADR-006). Compatibility with ArmorIQ's hosted proxy is
unverified until experiment E1.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from services.authorization.ctx_envelope import InMemoryReplayGuard
from services.contracts import ErrorBody, ErrorResponse, HealthResponse, McpEndpoint
from services.knowledge import embeddings
from services.knowledge.embeddings import MODEL_ID
from services.toolserver import __version__, db
from services.toolserver.config import Settings, get_settings
from services.toolserver.ledger import Ledger
from services.toolserver.security import BearerTokenGuard
from services.toolserver.servers import CREDENTIAL_IDS, ToolRuntime, build_server

MCP_PATH = "/mcp"
MOUNTS: dict[McpEndpoint, str] = {
    McpEndpoint.MEMORY: "/memory",
    McpEndpoint.WORKSPACE: "/workspace",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=[],  # server-to-server only; browsers must not call MCP directly
    )
    tokens = {
        McpEndpoint.MEMORY: settings.toolserver_bearer_memory,
        McpEndpoint.WORKSPACE: settings.toolserver_bearer_workspace,
    }

    servers: dict[McpEndpoint, MCPServer] = {}
    holders: dict[McpEndpoint, dict[str, Any]] = {}
    for endpoint in MOUNTS:
        holder: dict[str, Any] = {}
        holders[endpoint] = holder
        servers[endpoint] = build_server(
            endpoint, enable_diagnostic_tools=settings.mcp_enable_diagnostic_tools, holder=holder
        )
    # streamable_http_app() must be called before session_manager is available.
    http_apps = {
        endpoint: server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            stateless_http=True,
            json_response=False,
            transport_security=transport_security,
        )
        for endpoint, server in servers.items()
    }

    @asynccontextmanager
    async def lifespan(running: FastAPI) -> AsyncIterator[None]:
        # Mounted sub-app lifespans do not run automatically; start each
        # session manager here (each can run exactly once).
        async with AsyncExitStack() as stack:
            for server in servers.values():
                await stack.enter_async_context(server.session_manager.run())
            # One pool + ledger per process (SHARED_INTERFACES §9). Without a
            # DSN the tools still register and verification still rejects,
            # but no product tool executes (ledger None → `internal`).
            pool = None
            ledger = None
            dsn = (
                settings.database_url_toolserver.get_secret_value()
                if settings.database_url_toolserver
                else ""
            )
            if not dsn:
                logging.getLogger(__name__).warning(
                    "DATABASE_URL_TOOLSERVER is not set: product tools refuse every call"
                )
            else:
                try:
                    pool = db.create_pool(dsn)
                    ledger = Ledger(pool=pool)
                    # DB warmup (Neon cold start): one cheap round trip on a
                    # throwaway connection (not the pool: startup must not
                    # consume pooled checkouts) so the first real call does
                    # not pay the wake. Failure only logs (error class, never
                    # the DSN); /health stays up and product tools fail closed
                    # per call as before.
                    try:
                        import psycopg

                        warm_conn = psycopg.connect(dsn, connect_timeout=10)
                        try:
                            warm_conn.execute("SELECT 1")
                        finally:
                            warm_conn.close()
                    except Exception as warm_exc:
                        logging.getLogger(__name__).error(
                            "toolserver db warmup failed (%s)",
                            type(warm_exc).__name__,
                        )
                except Exception as exc:
                    logging.getLogger(__name__).error(
                        "toolserver pool unavailable (%s); tools fail closed",
                        type(exc).__name__,
                    )
            ctx_secret = settings.ctx_secret.get_secret_value() if settings.ctx_secret else None
            # Embedding model, once per process (P2-05; Settings already
            # refused any EMBED_MODEL but embeddings.MODEL_ID). A failed warm
            # keeps /health at 503, and every search fails closed: the request
            # path never loads the model (no per-request download retries, no
            # hosted fallback).
            running.state.embedding_ready = False
            try:
                embeddings.warm()
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "embedding model unavailable (%s); /health 503, search fails closed",
                    type(exc).__name__,
                )
            else:
                running.state.embedding_ready = True
                logging.getLogger(__name__).info("embedding model ready (%s)", MODEL_ID)
            for endpoint, holder in holders.items():
                holder["runtime"] = ToolRuntime(
                    endpoint=endpoint,
                    ledger=ledger,
                    pool=pool,
                    ctx_secret=ctx_secret,
                    credential_id=CREDENTIAL_IDS[endpoint],
                    dev_guard=InMemoryReplayGuard(),
                )
            try:
                yield
            finally:
                if pool is not None:
                    pool.close()

    app = FastAPI(
        title="ProxyLead MCP tool server",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.embedding_ready = False

    @app.get(
        "/health",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse, "description": "Embedding model not loaded"}},
    )
    def health() -> HealthResponse | JSONResponse:
        if not app.state.embedding_ready:
            body = ErrorResponse(
                error=ErrorBody(code="not_ready", message="embedding model not loaded")
            )
            return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
        return HealthResponse(
            service=settings.service_name, version=__version__, environment=settings.app_env
        )

    for endpoint, prefix in MOUNTS.items():
        app.mount(
            prefix,
            BearerTokenGuard(http_apps[endpoint], token=tokens[endpoint], endpoint=endpoint.value),
        )

    return app
