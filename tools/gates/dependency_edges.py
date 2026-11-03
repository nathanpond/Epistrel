"""check-dependency-edges: ``core.edges.record_edges`` is the only writer of
``dependency_edges`` (FR-DIRECT-7; #220).

Every edge is written through ``record_edges`` (its checks, its derived columns, its one-edge
rule). This scan reads ``src/`` by AST (it never imports it) and reports, outside
``src/epistrel/core/edges.py``:

- ``insert(dependency_edges)`` (``sqlalchemy``'s or ``postgresql``'s ``insert``, any alias);
- ``dependency_edges.insert()``;
- ``op.bulk_insert(dependency_edges, …)``;
- a string constant (not a docstring) holding ``INSERT INTO dependency_edges``.

``migrations/`` and ``tests/`` are not scanned. SQL built at runtime from variables is not
caught: ``record_edges`` is the documented path.

Run: ``python -m tools.gates check-dependency-edges`` (exit 0 clean, 1 findings); enforced in
CI by ``tests/gates/test_dependency_edges_gate.py``.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
WRITER = "src/epistrel/core/edges.py"
TABLE = "dependency_edges"
SQL_INSERT = re.compile(r"\bINSERT\s+INTO\s+\"?dependency_edges\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what} (write edges through core.edges)"


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _names_table(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == TABLE) or (
        isinstance(node, ast.Attribute) and node.attr == TABLE
    )


def scan_source(path: str, source: str) -> list[Finding]:
    """Every write of the table in ``source``, by line."""
    return sorted(_scan(path, source), key=lambda f: (f.line, f.what))


def _scan(path: str, source: str) -> Iterator[Finding]:
    tree = ast.parse(source)
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            inserting = name == "insert" or (name.endswith("insert") and name != "bulk_insert")
            if name == "bulk_insert" and node.args and _names_table(node.args[0]):
                yield Finding(path, node.lineno, f"bulk_insert({TABLE})")
            elif inserting and node.args and _names_table(node.args[0]):
                yield Finding(path, node.lineno, f"insert({TABLE})")
            elif name == "insert" and isinstance(func, ast.Attribute) and _names_table(func.value):
                yield Finding(path, node.lineno, f"{TABLE}.insert()")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and SQL_INSERT.search(node.value)
        ):
            yield Finding(path, node.lineno, f"SQL INSERT INTO {TABLE}")


def scan(root: Path = REPO_ROOT) -> list[Finding]:
    found: list[Finding] = []
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == WRITER:
            continue
        found.extend(scan_source(rel, path.read_text(encoding="utf-8")))
    return found


def main(argv: Sequence[str]) -> int:
    root = Path(argv[0]) if argv else REPO_ROOT
    findings = scan(root)
    for finding in findings:
        print(finding)
    print(f"check-dependency-edges: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1:]))
