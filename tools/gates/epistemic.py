"""check-epistemic-separation: objective canon and belief partitions never read each other's
table (FR-EPI-1; #159).

Canon is one table (``facts``) with one holder; a character's beliefs another (``beliefs``, with
its projection ``belief_state``). FR-EPI-1 says they are represented separately, so a belief is
never read as canon and canon never as a belief. This scan reads, by AST (it never imports them):

- every module of the canon store (``src/epistrel/core/canon/``) for a read of the belief tables:
  an import of ``beliefs``/``belief_state`` or of ``core.epistemic``, a ``beliefs.c`` or
  ``belief_state.c`` reference, or a string constant (not a docstring) holding SQL that reads one
  (``FROM``/``JOIN``);
- every module of the epistemic model (``src/epistrel/core/epistemic/``) for the same reads of
  ``facts``: an import of the ``facts`` table or of ``core.canon.facts``, a ``facts.c``
  reference, or SQL reading ``facts``.

A verifier that combines the two (#140's ``check`` over ``belief(A)``: A's partition plus the
canon A may read) does so through ``KeyedLookup``, outside both packages.

Run: ``python -m tools.gates check-epistemic-separation`` (exit 0 clean, 1 findings); enforced
in CI by ``tests/gates/test_epistemic_separation.py``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Side:
    """One side of the separation: its package and what it must never read."""

    package: str
    tables: frozenset[str]
    modules: frozenset[str]
    """Modules whose import is a read of the other side."""


CANON_SIDE = Side(
    "src/epistrel/core/canon",
    frozenset({"beliefs", "belief_state"}),
    frozenset({"epistrel.core.epistemic", "epistrel.core.epistemic.partitions"}),
)
EPISTEMIC_SIDE = Side(
    "src/epistrel/core/epistemic",
    frozenset({"facts"}),
    frozenset({"epistrel.core.canon.facts"}),
)
SIDES: tuple[Side, ...] = (CANON_SIDE, EPISTEMIC_SIDE)
TABLE_MODULES = frozenset(
    {"epistrel.core.tables", "epistrel.core.tables.facts", "epistrel.core.tables.beliefs"}
)


def _sql(tables: frozenset[str]) -> re.Pattern[str]:
    return re.compile(
        rf"\b(from|join)\s+({'|'.join(sorted(tables))})\b",
        re.IGNORECASE,
    )


def _reads(tree: ast.AST, side: Side) -> Iterator[tuple[int, str]]:
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    sql = _sql(side.tables)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names = {a.name for a in node.names}
            if node.module in side.modules or any(
                f"{node.module}.{n}" in side.modules for n in names
            ):
                yield node.lineno, f"imports {node.module}"
            elif node.module in TABLE_MODULES and names & side.tables:
                yield node.lineno, f"imports the {', '.join(sorted(names & side.tables))} table"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in side.modules:
                    yield node.lineno, f"imports {alias.name}"
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "c"
            and isinstance(node.value, ast.Name)
            and node.value.id in side.tables
        ):
            yield node.lineno, f"{node.value.id}.c"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and sql.search(node.value)
        ):
            yield node.lineno, f"SQL read of {', '.join(sorted(side.tables))}"


def scan(root: Path = REPO_ROOT, sides: Sequence[Side] = SIDES) -> list[str]:
    """Every finding over the packages of ``sides``."""
    findings: list[str] = []
    for side in sides:
        package = root / side.package
        for path in sorted(package.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            findings.extend(
                f"{rel}:{line}: {what}; canon and belief partitions are separate (FR-EPI-1)"
                for line, what in sorted(set(_reads(tree, side)))
            )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    findings = scan()
    for line in findings:
        print(line)
    print(f"check-epistemic-separation: {len(SIDES)} side(s), {len(findings)} finding(s)")
    return 1 if findings else 0


__all__ = ["CANON_SIDE", "EPISTEMIC_SIDE", "SIDES", "Side", "main", "scan"]
