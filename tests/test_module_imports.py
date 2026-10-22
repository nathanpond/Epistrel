"""Every ``epistrel`` module imports on its own, first, in a fresh interpreter (#499).

The app and the test suite import modules in a fixed order, which hides an import cycle: a module
that only imports cleanly once some other module has been imported crashes the first entry point,
script or test that imports it first (``orchestrator.verify.extraction`` did, through the job
handler registry). Each module is imported in its own subprocess: clearing ``sys.modules`` in one
interpreter is not a fresh start (C extensions, import-time registries and ``sys.path`` hooks
survive it). The subprocesses run in parallel.
"""

from __future__ import annotations

import os
import pkgutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import epistrel

TIMEOUT_SECONDS = 120
# The repository root, as the working directory: ``epistrel.tools.capability_map`` and
# ``guarantees`` import the repository's ``tools.gates`` (they run from a checkout).
ROOT = Path(__file__).resolve().parents[1]


def _modules() -> list[str]:
    found = [
        info.name
        for info in pkgutil.walk_packages(epistrel.__path__, prefix=f"{epistrel.__name__}.")
    ]
    return [epistrel.__name__, *sorted(found)]


def _import_alone(module: str) -> tuple[str, int, str]:
    """Import ``module`` first thing in a new interpreter, from the repository root."""
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    lines = done.stderr.strip().splitlines()
    return module, done.returncode, lines[-1] if lines else ""


def test_every_module_imports_alone_in_a_fresh_interpreter() -> None:
    modules = _modules()
    assert len(modules) > 400, modules  # the walk found the package, not an empty path
    workers = max(2, os.process_cpu_count() or 2)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_import_alone, modules))
    failed = {module: error for module, code, error in results if code != 0}
    assert failed == {}, "\n".join(f"{m}: {e}" for m, e in failed.items())
