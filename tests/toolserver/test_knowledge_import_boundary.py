"""Import boundary: services.knowledge is toolserver-only (P2-04 §9-step-4)."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED = (REPO_ROOT / "apps", REPO_ROOT / "services")
ALLOWED_PREFIXES = ("services/toolserver", "services/knowledge", "tests/")


def test_knowledge_imported_only_by_toolserver() -> None:
    offenders = []
    for base in SCANNED:
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel.startswith(ALLOWED_PREFIXES):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                if any(
                    name == "services.knowledge" or name.startswith("services.knowledge.")
                    for name in names
                ):
                    offenders.append(rel)
                    break
    assert offenders == [], f"services.knowledge imported outside the tool server: {offenders}"
