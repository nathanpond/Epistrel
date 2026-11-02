"""check-as-recorded-readers: only the history read reaches the as-recorded view (FR-STORE-23;
#222).

``core.views.as_originally_recorded`` answers what the story held before any correction or
rewind: an audit view, never a player-facing read. This scan reads ``src/`` by AST (it never
imports it) and reports every reference to the name (an import, a call, an attribute) outside
``ALLOWED``: the history route, the Engine, ``core/history.py`` and ``core/views.py``. It does not
see a dynamic ``getattr``; the runtime spy in ``tests/scenario/test_s_audit_view.py`` covers the
player-facing paths.

Run: ``python -m tools.gates check-as-recorded-readers`` (exit 0 clean, 1 findings); enforced in
CI by ``tests/gates/test_as_recorded_gate.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
NAME = "as_originally_recorded"
ALLOWED = frozenset(
    {
        "src/epistrel/api/routes/history.py",
        "src/epistrel/engine.py",
        "src/epistrel/core/history.py",
        "src/epistrel/core/views.py",
    }
)


def scan_source(path: str, source: str) -> list[tuple[str, int]]:
    """Every reference to the view in ``source`` (``(path, line)``)."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if (
            (isinstance(node, ast.ImportFrom) and any(a.name == NAME for a in node.names))
            or (isinstance(node, ast.Name) and node.id == NAME)
            or (isinstance(node, ast.Attribute) and node.attr == NAME)
        ):
            found.append((path, node.lineno))
    return found


def check(root: Path = SOURCE_ROOT) -> list[str]:
    """The findings over ``root``, as ``path:line`` lines."""
    problems: list[str] = []
    for file in sorted(root.rglob("*.py")):
        relative = file.relative_to(REPO_ROOT).as_posix()
        if relative in ALLOWED:
            continue
        for path, line in scan_source(relative, file.read_text(encoding="utf-8")):
            problems.append(f"{path}:{line}: {NAME} outside the history read")
    return problems


def main(argv: Sequence[str] = ()) -> int:
    found = check()
    for line in found:
        print(line)
    print(f"check-as-recorded-readers: {len(found)} finding(s)")
    return 1 if found else 0


__all__ = ["ALLOWED", "NAME", "check", "main", "scan_source"]
