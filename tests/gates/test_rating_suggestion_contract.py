"""FR-RATE-8 (#121): the rating suggestion is advisory, so nothing safety-relevant reads it.

The import-linter contract ``rating-suggestion-is-advisory`` forbids the guardrail code (the
deterministic evaluator and the Orchestrator's gates), the catalog and its publish, story
creation and rating changes from importing the suggestion modules, directly or indirectly. These
tests check that the contract exists as written, is kept by the real tree, and bites; and that
the suggestion's only importers are the Engine, its driver and the two catalog routes.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

from tests.conftest import REPO_ROOT

LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
CONTRACT = "rating-suggestion-is-advisory"
SUGGESTION = ("epistrel.core.ratings.suggest", "epistrel.orchestrator.ratings.suggest")
GUARDED = [
    "epistrel.core.policies",
    "epistrel.orchestrator.policies",
    "epistrel.core.catalog",
    "epistrel.core.genesis",
    "epistrel.core.ratings.change",
]
SRC = REPO_ROOT / "src" / "epistrel"


def _lint(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LINT_IMPORTS), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=cwd,
    )


def test_the_contract_exists_as_written() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    contracts = {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}
    contract = contracts[CONTRACT]
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == GUARDED
    assert contract["forbidden_modules"] == list(SUGGESTION)
    assert not contract.get("allow_indirect_imports", False)


def test_the_real_tree_keeps_the_contract() -> None:
    proc = _lint(REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{CONTRACT} KEPT" in proc.stdout


def test_the_contract_bites_on_an_indirect_import(tmp_path: Path) -> None:
    """A guarded module that reaches the suggestion through another module breaks it."""
    pkg = tmp_path / "fakepkg"
    for sub in ("", "catalog", "ratings", "shared"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "ratings" / "suggest.py").write_text("VALUE = 1\n")
    (pkg / "shared" / "helper.py").write_text("import fakepkg.ratings.suggest\n")
    (pkg / "catalog" / "publish.py").write_text("import fakepkg.shared.helper\n")
    (tmp_path / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "fakepkg"\n\n'
        "[[tool.importlinter.contracts]]\n"
        f'name = "{CONTRACT}"\n'
        'type = "forbidden"\n'
        'source_modules = ["fakepkg.catalog"]\n'
        'forbidden_modules = ["fakepkg.ratings.suggest"]\n'
    )
    proc = _lint(tmp_path, "--config", "importlinter.toml")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{CONTRACT} BROKEN" in proc.stdout


def _imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_only_the_engine_its_driver_and_the_routes_import_the_suggestion() -> None:
    importers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if _imports_of(path) & set(SUGGESTION)
    )
    assert importers == [
        "api/routes/catalog_characters.py",
        "api/routes/catalog_scenarios.py",
        "api/routes/rating_suggestion.py",
        "engine.py",
        "orchestrator/ratings/suggest.py",
    ]
