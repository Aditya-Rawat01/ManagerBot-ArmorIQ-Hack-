"""Import boundary: services.knowledge is toolserver-only (P2-04 §9-step-4).

Every Python file under the scanned trees is parsed; any import that resolves
to `services.knowledge` (absolute, `from services import knowledge`, or a
relative import resolved against the file's own package, plus
`importlib.import_module` / `__import__` string literals) outside the allowed
trees fails. `scripts/` is scanned too: nothing there imports knowledge (the
seed job is `python -m services.knowledge.ingest`, inside the allowed tree).
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED = (REPO_ROOT / "apps", REPO_ROOT / "services", REPO_ROOT / "scripts")
ALLOWED_PREFIXES = ("services/toolserver/", "services/knowledge/")
TARGET = "services.knowledge"


def _is_target(name: str) -> bool:
    return name == TARGET or name.startswith(TARGET + ".")


def _package_of(rel_path: str) -> list[str]:
    """Package parts a module at `rel_path` resolves relative imports against."""
    parts = rel_path.removesuffix(".py").split("/")
    return parts[:-1]  # `pkg/__init__.py` and `pkg/mod.py` both live in `pkg`


def imported_names(source: str, rel_path: str) -> list[str]:
    """Every fully-qualified module name `source` may import."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source, filename=rel_path)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = _package_of(rel_path)
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue  # beyond the top-level package: not importable
                base = package[:keep]
            else:
                base = []
            module = ".".join([*base, *([node.module] if node.module else [])])
            if module:
                names.append(module)
            names.extend(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.append(first.value)
    return names


def offenders_in(source: str, rel_path: str) -> bool:
    return any(_is_target(name) for name in imported_names(source, rel_path))


@pytest.mark.parametrize(
    ("rel_path", "source"),
    [
        ("apps/api/x.py", "import services.knowledge"),
        ("apps/api/x.py", "import services.knowledge.retrieval as r"),
        ("apps/api/x.py", "from services.knowledge import retrieval"),
        ("apps/api/x.py", "from services import knowledge"),
        ("apps/api/x.py", "from services import contracts, knowledge"),
        ("services/audit/x.py", "from .. import knowledge"),
        ("services/audit/x.py", "from ..knowledge import retrieval"),
        ("services/audit/sub/__init__.py", "from ...knowledge.retrieval import search"),
        ("apps/api/x.py", "importlib.import_module('services.knowledge.retrieval')"),
        ("apps/api/x.py", "__import__('services.knowledge')"),
        ("scripts/tool.py", "def f():\n    from services.knowledge import ingest\n"),
    ],
)
def test_checker_catches_every_import_form(rel_path: str, source: str) -> None:
    assert offenders_in(source, rel_path)


@pytest.mark.parametrize(
    ("rel_path", "source"),
    [
        ("apps/api/x.py", "from services import contracts"),
        ("apps/api/x.py", "import services.knowledgebase"),
        ("services/audit/x.py", "from . import knowledge"),  # services.audit.knowledge
        ("services/audit/x.py", "from ..toolserver import db"),
        ("apps/api/x.py", "from services.contracts import knowledge_models"),
    ],
)
def test_checker_allows_unrelated_imports(rel_path: str, source: str) -> None:
    assert not offenders_in(source, rel_path)


def test_knowledge_imported_only_by_toolserver() -> None:
    for base in SCANNED:
        assert base.is_dir(), f"scanned tree missing: {base}"
    offenders = []
    scanned = 0
    for base in SCANNED:
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(ALLOWED_PREFIXES):
                continue
            scanned += 1
            if offenders_in(path.read_text(encoding="utf-8"), rel):
                offenders.append(rel)
    assert scanned > 20, "the scan must actually cover apps/, services/, scripts/"
    assert offenders == [], f"services.knowledge imported outside the tool server: {offenders}"
