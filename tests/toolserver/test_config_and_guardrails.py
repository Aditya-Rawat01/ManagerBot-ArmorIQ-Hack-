import ast
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts import McpEndpoint, ToolName
from services.toolserver.config import Settings

from .conftest import make_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
# Tool-server code plus the retrieval package only it may call (import boundary).
TOOLSERVER_DIRS = (REPO_ROOT / "services" / "toolserver", REPO_ROOT / "services" / "knowledge")


def test_defaults_need_no_secrets() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.toolserver_bearer_memory is None
    assert settings.toolserver_bearer_workspace is None
    assert settings.mcp_allowed_hosts == ["localhost:8100", "127.0.0.1:8100"]


def test_allowed_hosts_parse_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com, mcp.example.com:443")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.mcp_allowed_hosts == ["mcp.example.com", "mcp.example.com:443"]


def test_empty_allowed_hosts_are_rejected() -> None:
    with pytest.raises(ValidationError, match="MCP_ALLOWED_HOSTS"):
        make_settings(mcp_allowed_hosts="")


def test_tokens_are_not_exposed_in_repr() -> None:
    assert "test-memory-token" not in repr(make_settings())


def test_diagnostic_tool_names_never_collide_with_product_tools() -> None:
    product = {name.value for name in ToolName}
    assert {"memory_scaffold_ping", "workspace_scaffold_ping"}.isdisjoint(product)


def test_every_product_tool_goes_through_dispatch() -> None:
    """Catalog tools are exposed only when implemented behind guarded dispatch.

    Implemented product tools (PRODUCT_TOOLS) appear on their endpoint's
    tool list; catalog tools with no implementation are never listed, so no
    product tool exists outside guarded dispatch.
    """
    from services.contracts import tool_spec
    from services.toolserver.servers import build_server
    from services.toolserver.tools_memory import PRODUCT_TOOLS

    server = build_server(McpEndpoint.MEMORY, enable_diagnostic_tools=True, holder={})
    listed = set(server._tool_manager._tools)
    assert "memory_scaffold_ping" in listed
    for name in ToolName:
        if tool_spec(name).endpoint is not McpEndpoint.MEMORY:
            continue
        if name in PRODUCT_TOOLS:
            assert name.value in listed, f"{name.value} implemented but not listed"
        else:
            assert name.value not in listed, f"{name.value} listed without dispatch"


def test_mcp_server_never_imports_armoriq_sdk() -> None:
    offenders = []
    for path in (p for d in TOOLSERVER_DIRS for p in d.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == "armoriq_sdk" for name in names):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


def _pins() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    requirements = project["dependencies"] + project["optional-dependencies"]["toolserver"]
    result = {}
    for requirement in requirements:
        name, _, pinned = requirement.partition("==")
        result[name.split("[")[0].strip()] = pinned.split("#")[0].strip()
    return result


@pytest.mark.parametrize(("package", "pinned"), sorted(_pins().items()))
def test_installed_versions_match_pins(package: str, pinned: str) -> None:
    assert version(package) == pinned
