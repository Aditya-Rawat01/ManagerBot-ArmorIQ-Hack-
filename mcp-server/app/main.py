"""MCP tool-server application factory.

Run locally (from `mcp-server/`):

    uvicorn app.main:create_app --factory --port 8100

Endpoints:
    GET  /health          liveness (public)
    POST /memory/mcp      memory-mcp    (bearer: TOOLSERVER_BEARER_MEMORY)
    POST /workspace/mcp   workspace-mcp (bearer: TOOLSERVER_BEARER_WORKSPACE)

Both MCP endpoints use the SDK's streamable HTTP transport in stateless mode
with SSE responses (ADR-006). Compatibility with ArmorIQ's hosted proxy is
unverified until experiment E1.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app import __version__
from app.config import Settings, get_settings
from app.security import BearerTokenGuard
from app.servers import build_server
from proxylead_contracts import HealthResponse, McpEndpoint

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

    servers: dict[McpEndpoint, MCPServer] = {
        endpoint: build_server(
            endpoint, enable_diagnostic_tools=settings.mcp_enable_diagnostic_tools
        )
        for endpoint in MOUNTS
    }
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
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Mounted sub-app lifespans do not run automatically; start each
        # session manager here (each can run exactly once).
        async with AsyncExitStack() as stack:
            for server in servers.values():
                await stack.enter_async_context(server.session_manager.run())
            yield

    app = FastAPI(
        title="ProxyLead MCP tool server",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            service=settings.service_name, version=__version__, environment=settings.app_env
        )

    for endpoint, prefix in MOUNTS.items():
        app.mount(
            prefix,
            BearerTokenGuard(http_apps[endpoint], token=tokens[endpoint], endpoint=endpoint.value),
        )

    return app
