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


def test_embed_model_must_be_the_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """EMBED_MODEL is validated at startup (fail closed), not just logged."""
    from services.knowledge.embeddings import MODEL_ID

    assert make_settings().embed_model == MODEL_ID
    for other in ("text-embedding-3-small", "BAAI/bge-base-en-v1.5", ""):
        with pytest.raises(ValidationError, match="EMBED_MODEL"):
            make_settings(embed_model=other)
    monkeypatch.setenv("EMBED_MODEL", "BAAI/bge-large-en-v1.5")
    with pytest.raises(ValidationError, match="EMBED_MODEL"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_tokens_are_not_exposed_in_repr() -> None:
    assert "test-memory-token" not in repr(make_settings())


def test_diagnostic_tool_names_never_collide_with_product_tools() -> None:
    product = {name.value for name in ToolName}
    assert {"memory_scaffold_ping", "workspace_scaffold_ping"}.isdisjoint(product)


@pytest.mark.parametrize("endpoint", list(McpEndpoint))
def test_every_product_tool_goes_through_dispatch(endpoint: McpEndpoint) -> None:
    """Catalog tools are exposed only when implemented behind guarded dispatch.

    On both endpoints: every listed ToolName is in that endpoint's dispatch
    table, and every dispatch-table tool is listed; catalog tools with no
    implementation are never listed. (Calls to ToolNames that are not in the
    table are refused, never forwarded: test_tool_dispatch.)
    """
    import anyio

    from services.contracts import tool_spec
    from services.toolserver.servers import build_server, dispatch_table
    from services.toolserver.tools_memory import PRODUCT_TOOLS

    server = build_server(endpoint, enable_diagnostic_tools=True, holder={})
    listed = {tool.name for tool in anyio.run(server.list_tools)}
    table = dispatch_table(endpoint)
    assert f"{endpoint.value.removesuffix('-mcp')}_scaffold_ping" in listed
    tool_values = {name.value for name in ToolName}
    product_listed = {ToolName(name) for name in listed if name in tool_values}
    assert product_listed == set(table), "every registered ToolName must be dispatched"
    for name in ToolName:
        served_here = tool_spec(name).endpoint is endpoint
        if served_here and name in PRODUCT_TOOLS:
            assert name in table, f"{name.value} implemented but not dispatched"
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


def test_toolserver_image_bakes_the_model_and_runs_offline() -> None:
    """Static Dockerfile checks (P2-05 review): model layer before the source,
    no runtime download, low-arena malloc, a start period for a slow warm."""
    text = (REPO_ROOT / "deploy" / "toolserver" / "Dockerfile").read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    steps = [line for line in lines if line and not line.startswith("#")]

    def first(prefix: str) -> int:
        return next(i for i, line in enumerate(steps) if line.startswith(prefix))

    warm = first('RUN python -c "from services.knowledge.embeddings import warm; warm()"')
    source = first("COPY --chown=appuser:appuser services ./services")
    assert first("COPY pyproject.toml") < first('RUN pip install --no-cache-dir ".[toolserver]"')
    assert first('RUN pip install --no-cache-dir ".[toolserver]"') < warm < source
    assert not any(line.startswith("COPY services ./services") for line in steps[:warm])
    assert first("ENV FASTEMBED_CACHE_PATH=") < warm < first("ENV HF_HUB_OFFLINE=1")
    assert "ENV MALLOC_ARENA_MAX=2" in steps
    healthcheck = steps[first("HEALTHCHECK")]
    start_period = healthcheck.split("--start-period=")[1].split("s")[0]
    assert int(start_period) >= 120
    assert "not downloaded" not in text  # stale pre-P2-05 header


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
