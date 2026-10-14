"""FR-FB-3 (#126): feedback is flag-only, so nothing in the engine reads it to change a turn, a
profile or a guardrail.

The import-linter contract ``feedback-is-flag-only`` forbids the Orchestrator (the Lite pipeline,
its gates and jobs), story creation, the guardrail evaluator, rating changes, profiles and the
projections from importing the feedback modules, directly or indirectly; only erasure's two
edges are ignored (it indexes and redacts a rating's text, FR-ADMIN-6). These tests check that
the contract exists as written, is kept by the real tree and bites; that no guarded module
imports a feedback module by name; and that none names the feedback tables in its text, but the
retention sweep, which expires them (#112).
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

from tests.conftest import REPO_ROOT

LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
CONTRACT = "feedback-is-flag-only"
FEEDBACK = (
    "epistrel.core.feedback",
    "epistrel.core.feedback_reporting",
    "epistrel.core.feedback_export",
)
GUARDED = [
    "epistrel.orchestrator",
    "epistrel.core.genesis",
    "epistrel.core.policies",
    "epistrel.core.ratings",
    "epistrel.core.profiles",
    "epistrel.core.projections",
]
IGNORED = [
    "epistrel.core.erasure.records -> epistrel.core.feedback",
    "epistrel.core.erasure.erase -> epistrel.core.feedback",
]
TABLES = ("message_feedback", "feedback_history", "feedback_export_log")
TABLE_READERS = {"orchestrator/jobs/handlers/retention.py"}
"""Guarded modules allowed to name a feedback table: the retention sweep expires ratings (#112)."""
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


def _guarded_files(root: Path = SRC) -> list[Path]:
    found: list[Path] = []
    for module in GUARDED:
        relative = Path(*module.split(".")[1:])
        package = root / relative
        found += sorted(package.rglob("*.py")) if package.is_dir() else [package.with_suffix(".py")]
    return found


def _imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_the_contract_exists_as_written() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    contracts = {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}
    contract = contracts[CONTRACT]
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == GUARDED
    assert contract["forbidden_modules"] == list(FEEDBACK)
    assert contract["ignore_imports"] == IGNORED
    assert not contract.get("allow_indirect_imports", False)


def test_the_real_tree_keeps_the_contract() -> None:
    proc = _lint(REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{CONTRACT} KEPT" in proc.stdout


def test_the_contract_bites_on_an_indirect_import(tmp_path: Path) -> None:
    """A guarded module that reaches a feedback module through another module breaks it."""
    pkg = tmp_path / "fakepkg"
    for sub in ("", "core", "orchestrator", "shared"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "core" / "feedback_reporting.py").write_text("VALUE = 1\n")
    (pkg / "shared" / "helper.py").write_text("import fakepkg.core.feedback_reporting\n")
    (pkg / "orchestrator" / "turn.py").write_text("import fakepkg.shared.helper\n")
    (tmp_path / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "fakepkg"\n\n'
        "[[tool.importlinter.contracts]]\n"
        f'name = "{CONTRACT}"\n'
        'type = "forbidden"\n'
        'source_modules = ["fakepkg.orchestrator"]\n'
        'forbidden_modules = ["fakepkg.core.feedback_reporting"]\n'
    )
    proc = _lint(tmp_path, "--config", "importlinter.toml")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{CONTRACT} BROKEN" in proc.stdout


def test_no_guarded_module_imports_or_names_feedback(tmp_path: Path) -> None:
    """No module under the guarded packages imports a feedback module (by any spelling of
    ``import``), and none names a feedback table in its text but the retention sweep.
    Complement: the same scans find a planted importer and a planted reader."""

    def importers(root: Path) -> list[str]:
        return sorted(
            path.relative_to(root).as_posix()
            for path in _guarded_files(root)
            if path.is_file()
            and any(
                name == m or name.startswith(f"{m}.")
                for name in _imports_of(path)
                for m in FEEDBACK
            )
        )

    def readers(root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in _guarded_files(root)
            if path.is_file() and any(table in path.read_text(encoding="utf-8") for table in TABLES)
        }

    assert _guarded_files()
    assert importers(SRC) == []
    assert readers(SRC) == TABLE_READERS

    planted = tmp_path / "src"
    (planted / "orchestrator" / "lite").mkdir(parents=True)
    (planted / "core" / "policies").mkdir(parents=True)
    (planted / "orchestrator" / "lite" / "prompt.py").write_text(
        "from epistrel.core.feedback_reporting import aggregates\n", encoding="utf-8"
    )
    (planted / "core" / "policies" / "evaluate.py").write_text(
        'QUERY = "SELECT tags FROM message_feedback"\n', encoding="utf-8"
    )
    assert importers(planted) == ["orchestrator/lite/prompt.py"]
    assert readers(planted) == {"core/policies/evaluate.py"}
