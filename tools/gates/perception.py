"""check-perception-authority: one perception decision in the codebase (FR-TURN-9,10, NFR-PRIV-3;
#161).

Who perceives a beat is decided by ``core.epistemic.perception.route_perception`` over the
authoritative scene ``core.epistemic.scene`` loads, never by a consumer's roster. This scan reads,
by AST (it never imports them), every module under ``src/epistrel`` for a read of what routing
decides from:

- an import of the ``placement`` or ``scene_state`` projection table (from ``core.tables`` or
  ``core.tables.projections``), or a ``placement.c``/``scene_state.c`` reference;
- a reference to the topology and channel predicates routing reads (``ADJACENT_TO``,
  ``ON_CHANNEL``).

Only ``ALLOWED`` modules may: the routing modules themselves, #137's projections (which write
the tables), the table and vocabulary definitions, and story deletion. Anything else needing
who is where for a perception decision goes through the exported helpers (``co_located_set``,
``scene.placed_elsewhere``: #55's ``present`` set), so a second, divergent perception rule
cannot appear unnoticed.

Run: ``python -m tools.gates check-perception-authority`` (exit 0 clean, 1 findings); enforced in
CI by ``tests/gates/test_perception_authority_gate.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = "src/epistrel"
TABLES = frozenset({"placement", "scene_state"})
TABLE_MODULES = frozenset({"epistrel.core.tables", "epistrel.core.tables.projections"})
PREDICATES = frozenset({"ADJACENT_TO", "ON_CHANNEL"})
ALLOWED = frozenset(
    {
        "src/epistrel/core/epistemic/perception.py",
        "src/epistrel/core/epistemic/scene.py",
        "src/epistrel/core/projections/structured.py",
        "src/epistrel/core/tables/__init__.py",
        "src/epistrel/core/tables/projections.py",
        "src/epistrel/core/propositions/vocabulary.py",
        "src/epistrel/core/stories.py",
    }
)
"""The modules that may read the scene and topology directly (see the module docstring)."""


def _reads(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in TABLE_MODULES:
            hit = {a.name for a in node.names} & TABLES
            if hit:
                yield node.lineno, f"imports the {', '.join(sorted(hit))} table"
        if isinstance(node, ast.ImportFrom):
            hit = {a.name for a in node.names} & PREDICATES
            if hit:
                yield node.lineno, f"imports {', '.join(sorted(hit))}"
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "c"
            and isinstance(node.value, ast.Name)
            and node.value.id in TABLES
        ):
            yield node.lineno, f"{node.value.id}.c"
        elif isinstance(node, ast.Attribute) and node.attr in PREDICATES:
            yield node.lineno, f"names {node.attr}"


def scan(root: Path = REPO_ROOT, allowed: frozenset[str] = ALLOWED) -> list[str]:
    """Every read of the scene or the topology outside ``allowed``."""
    findings: list[str] = []
    for path in sorted((root / SOURCE_ROOT).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        findings.extend(
            f"{rel}:{line}: {what}; perception is decided by core.epistemic.perception only "
            "(FR-TURN-10, #161)"
            for line, what in sorted(set(_reads(tree)))
        )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    findings = scan()
    for line in findings:
        print(line)
    print(f"check-perception-authority: {len(ALLOWED)} allowed, {len(findings)} finding(s)")
    return 1 if findings else 0


__all__ = ["ALLOWED", "PREDICATES", "TABLES", "main", "scan"]
