"""Architecture guardrails that must hold from the first line of feature code."""

import ast
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# All Python source of the API and every shared service (apps/web is not Python).
# ADR-018: probes and setup scripts must call ArmorIQGateway, never armoriq_sdk,
# so scripts/ is scanned too.
PYTHON_SOURCE_DIRS = (
    REPO_ROOT / "apps" / "api",
    REPO_ROOT / "services",
    REPO_ROOT / "scripts",
)
ARMORIQ_GATEWAY = REPO_ROOT / "services" / "authorization" / "armoriq_gateway.py"


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def _imported_modules(path: Path) -> set[str]:
    """Dotted names imported by ``path``; ``from a import b`` yields ``a`` and ``a.b``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


# ADR-032: Gemini through google-genai is the only LLM SDK, imported only by
# apps/api/llm.py. The retired SDK's name is spelled in parts so a repository
# grep for it stays empty.
LLM_MODULE = REPO_ROOT / "apps" / "api" / "llm.py"
RETIRED_LLM_SDK = "an" + "thropic"


def _uses_genai(path: Path) -> bool:
    return any(
        name == "google.genai" or name.startswith("google.genai.")
        for name in _imported_modules(path)
    )


def test_google_genai_is_imported_only_by_the_llm_module() -> None:
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (p for d in PYTHON_SOURCE_DIRS for p in d.rglob("*.py"))
        if path != LLM_MODULE and _uses_genai(path)
    ]
    assert offenders == [], f"google.genai imported outside apps/api/llm.py: {offenders}"
    assert _uses_genai(LLM_MODULE)


def test_llm_module_imports_the_sdk_lazily() -> None:
    tree = ast.parse(LLM_MODULE.read_text(encoding="utf-8"))
    top_level = {
        alias.name if isinstance(node, ast.Import) else str(node.module)
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not any(name.split(".")[0] == "google" for name in top_level), top_level


def test_importing_the_api_never_loads_the_llm_sdk() -> None:
    code = (
        "import sys\n"
        "import apps.api.main, apps.api.orchestrator, apps.api.planner, apps.api.responder\n"
        "print(sorted(m for m in sys.modules if m.startswith('google.genai')))\n"
    )
    result = subprocess.run(  # noqa: S603 -- this interpreter, a constant script
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_retired_llm_sdk_is_gone() -> None:
    scanned = (*PYTHON_SOURCE_DIRS, REPO_ROOT / "tests")
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (p for d in scanned for p in d.rglob("*.py"))
        if RETIRED_LLM_SDK in {name.split(".")[0] for name in _imported_modules(path)}
    ]
    assert offenders == [], f"retired LLM SDK imported: {offenders}"
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    requirements = [*project["dependencies"]]
    for extra in project["optional-dependencies"].values():
        requirements.extend(extra)
    assert not [req for req in requirements if req.lower().startswith(RETIRED_LLM_SDK)]
    from apps.api.core.config import Settings

    assert not [name for name in Settings.model_fields if RETIRED_LLM_SDK in name]


def test_armoriq_sdk_is_imported_only_by_the_gateway() -> None:
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (p for d in PYTHON_SOURCE_DIRS for p in d.rglob("*.py"))
        if path != ARMORIQ_GATEWAY and "armoriq_sdk" in _imported_top_level_modules(path)
    ]
    assert offenders == [], f"armoriq_sdk imported outside the gateway: {offenders}"


def _pinned_dependencies() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    requirements = project["dependencies"] + project["optional-dependencies"]["api"]
    pins: dict[str, str] = {}
    for requirement in requirements:
        name, _, pinned = requirement.partition("==")
        pins[name.split("[")[0].strip()] = pinned.strip()
    return pins


@pytest.mark.parametrize(("package", "pinned"), sorted(_pinned_dependencies().items()))
def test_installed_versions_match_pins(package: str, pinned: str) -> None:
    # Reads installed metadata only; does not import or execute the packages.
    assert version(package) == pinned


def test_armoriq_sdk_pin_is_the_required_version() -> None:
    assert _pinned_dependencies()["armoriq-sdk"] == "0.6.10"


def test_google_genai_is_pinned_for_the_api_only() -> None:
    assert _pinned_dependencies()["google-genai"] == "2.31.0"
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    toolserver = pyproject["project"]["optional-dependencies"]["toolserver"]
    assert not [req for req in toolserver if req.startswith("google-genai")]


def test_offline_fakes_never_reach_production_code() -> None:
    # FakeArmorIQGateway labels its decisions with ArmorIQ sources, so production
    # code must not be able to construct one and record fake "ArmorIQ" evidence.
    # It lives in tests/fakes/, which is neither installed nor copied into images.
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (p for d in PYTHON_SOURCE_DIRS for p in d.rglob("*.py"))
        if "tests" in _imported_top_level_modules(path)
        or "FakeArmorIQGateway" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"test doubles referenced from production code: {offenders}"


def test_armoriq_email_for_is_called_only_by_the_gateway() -> None:
    """Raw member emails leave `apps/api/identity.py` only through the gateway.

    The definition lives in `apps/api/identity.py`; the only non-test caller
    allowed is `services/authorization/armoriq_gateway.py` (lands in P1-02).
    """
    gateway = ARMORIQ_GATEWAY.relative_to(REPO_ROOT).as_posix()
    allowed = {
        "apps/api/identity.py",  # definition site
        gateway,  # the single permitted caller (P1-02)
    }
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in (p for d in PYTHON_SOURCE_DIRS for p in d.rglob("*.py"))
        if path.relative_to(REPO_ROOT).as_posix() not in allowed
        and "tests" not in path.parts
        and "armoriq_email_for" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"armoriq_email_for referenced outside the gateway: {offenders}"
