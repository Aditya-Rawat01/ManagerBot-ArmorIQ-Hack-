"""Architecture guardrails that must hold from the first line of feature code."""

import ast
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
ARMORIQ_GATEWAY = APP_DIR / "integrations" / "armoriq" / "gateway.py"


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_armoriq_sdk_is_imported_only_by_the_gateway() -> None:
    offenders = [
        path.relative_to(BACKEND_DIR).as_posix()
        for path in APP_DIR.rglob("*.py")
        if path != ARMORIQ_GATEWAY and "armoriq_sdk" in _imported_top_level_modules(path)
    ]
    assert offenders == [], f"armoriq_sdk imported outside the gateway: {offenders}"


def _pinned_dependencies() -> dict[str, str]:
    pyproject = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    for requirement in pyproject["project"]["dependencies"]:
        name, _, pinned = requirement.partition("==")
        pins[name.split("[")[0].strip()] = pinned.strip()
    return pins


@pytest.mark.parametrize(("package", "pinned"), sorted(_pinned_dependencies().items()))
def test_installed_versions_match_pins(package: str, pinned: str) -> None:
    # Reads installed metadata only; does not import or execute the packages.
    assert version(package) == pinned


def test_armoriq_sdk_pin_is_the_required_version() -> None:
    assert _pinned_dependencies()["armoriq-sdk"] == "0.6.10"
