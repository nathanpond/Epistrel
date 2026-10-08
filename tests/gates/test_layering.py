"""docs/02 layering: Core imports neither Orchestrator nor API, and the contract bites."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO_ROOT

LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LINT_IMPORTS), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=cwd,
    )


def _fake_tree(root: Path, core_imports_orchestrator: bool) -> None:
    pkg = root / "fakepkg"
    for sub in ("", "core", "orchestrator", "api"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    body = "import fakepkg.orchestrator\n" if core_imports_orchestrator else "VALUE = 1\n"
    (pkg / "core" / "x.py").write_text(body)
    (root / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "fakepkg"\n\n'
        "[[tool.importlinter.contracts]]\n"
        'name = "core-does-not-import-orchestrator-or-api"\n'
        'type = "forbidden"\n'
        'source_modules = ["fakepkg.core"]\n'
        'forbidden_modules = ["fakepkg.orchestrator", "fakepkg.api"]\n'
    )


def test_contract_fails_when_core_imports_the_orchestrator(tmp_path: Path) -> None:
    _fake_tree(tmp_path, core_imports_orchestrator=True)
    proc = _run(tmp_path, "--config", "importlinter.toml")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "fakepkg.core.x -> fakepkg.orchestrator" in proc.stdout


def test_contract_passes_when_the_import_is_removed(tmp_path: Path) -> None:
    _fake_tree(tmp_path, core_imports_orchestrator=False)
    proc = _run(tmp_path, "--config", "importlinter.toml")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_real_tree_respects_the_layering() -> None:
    proc = _run(REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "core-does-not-import-orchestrator-or-api" in proc.stdout
    assert "KEPT" in proc.stdout
