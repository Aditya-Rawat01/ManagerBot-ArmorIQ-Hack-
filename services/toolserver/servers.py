"""MCP server definitions for the two registered endpoints.

Product tools (PRD v2 §6, `services.contracts.ToolName`) are NOT defined
here yet; they land in Phase 2 together with `_ctx` verification, strict
schemas, tier-filtered SQL, idempotency, and the execution ledger.
"""

from mcp.server.mcpserver import MCPServer

from services.contracts import McpEndpoint
from services.toolserver import __version__


def _register_diagnostic_ping(server: MCPServer, endpoint: McpEndpoint) -> None:
    tool_name = f"{endpoint.value.removesuffix('-mcp')}_scaffold_ping"

    @server.tool(
        name=tool_name,
        description=(
            "Scaffold-only diagnostic. Returns static server metadata and reads no data. "
            "Not part of the ProxyLead product tool contract."
        ),
    )
    def scaffold_ping() -> dict[str, object]:
        return {"ok": True, "endpoint": endpoint.value, "server_version": __version__}


def build_server(endpoint: McpEndpoint, *, enable_diagnostic_tools: bool) -> MCPServer:
    server: MCPServer = MCPServer(
        name=endpoint.value,
        version=__version__,
        instructions=f"ProxyLead {endpoint.value} tool server (foundation scaffold).",
    )
    if enable_diagnostic_tools:
        _register_diagnostic_ping(server, endpoint)
    return server
