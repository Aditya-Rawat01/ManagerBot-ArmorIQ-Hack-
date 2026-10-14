import ast
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from proxylead_contracts import ToolName

from .conftest import make_settings

SERVER_DIR = Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"


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


def test_mcp_server_never_imports_armoriq_sdk() -> None:
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == "armoriq_sdk" for name in names):
                offenders.append(path.relative_to(SERVER_DIR).as_posix())
    assert offenders == []


def _pins() -> dict[str, str]:
    pyproject = tomllib.loads((SERVER_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    result = {}
    for requirement in pyproject["project"]["dependencies"]:
        name, _, pinned = requirement.partition("==")
        result[name.split("[")[0].strip()] = pinned.split("#")[0].strip()
    return result


@pytest.mark.parametrize(("package", "pinned"), sorted(_pins().items()))
def test_installed_versions_match_pins(package: str, pinned: str) -> None:
    assert version(package) == pinned
