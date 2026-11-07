"""FR-IMG-12 (#257): the image generation path never imports the safety context or the image
path's canonical-age reader.

The import-linter contract ``images-generation-path-has-no-ages`` forbids
``orchestrator.images.adapters`` and ``orchestrator.images.port`` from importing
``core.images.safety`` and ``core.catalog.ages`` (direct imports). These tests check that the
contract exists as written, that the real tree keeps it, and that it bites: a temporary copy of
the package whose OpenRouter adapter imports the age reader breaks it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from tests.conftest import REPO_ROOT

LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
CONTRACT = "images-generation-path-has-no-ages"
SOURCES = ["epistrel.orchestrator.images.adapters", "epistrel.orchestrator.images.port"]
FORBIDDEN = ["epistrel.core.images.safety", "epistrel.core.catalog.ages"]
SRC = REPO_ROOT / "src" / "epistrel"


def _contract() -> dict[str, object]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    contracts = {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}
    found: dict[str, object] = contracts[CONTRACT]
    return found


def _lint(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LINT_IMPORTS), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=cwd,
    )


def test_the_contract_exists_as_written() -> None:
    contract = _contract()
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == SOURCES
    assert contract["forbidden_modules"] == FORBIDDEN
    assert contract["allow_indirect_imports"] is True


def test_the_real_tree_keeps_the_contract() -> None:
    proc = _lint(REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{CONTRACT} KEPT" in proc.stdout


def test_the_contract_bites_on_a_copy_with_a_forbidden_import(tmp_path: Path) -> None:
    """A copy of the package whose OpenRouter adapter imports ``core.catalog.ages`` breaks the
    contract; the same copy untouched keeps it (the complement)."""
    package = tmp_path / "epistrel"
    shutil.copytree(SRC, package, ignore=shutil.ignore_patterns("__pycache__"))
    contract = _contract()
    listed = ", ".join(f'"{m}"' for m in contract["forbidden_modules"])  # type: ignore[attr-defined]
    sources = ", ".join(f'"{m}"' for m in contract["source_modules"])  # type: ignore[attr-defined]
    (tmp_path / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "epistrel"\n\n'
        "[[tool.importlinter.contracts]]\n"
        f'name = "{CONTRACT}"\n'
        'type = "forbidden"\n'
        f"source_modules = [{sources}]\n"
        f"forbidden_modules = [{listed}]\n"
        "allow_indirect_imports = true\n"
    )
    kept = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
    assert kept.returncode == 0, kept.stdout + kept.stderr
    adapter = package / "orchestrator" / "images" / "adapters" / "openrouter.py"
    adapter.write_text(
        adapter.read_text(encoding="utf-8") + "\nfrom epistrel.core.catalog import ages  # noqa\n",
        encoding="utf-8",
    )
    broken = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert f"{CONTRACT} BROKEN" in broken.stdout
    assert "epistrel.core.catalog.ages" in broken.stdout
