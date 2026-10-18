"""check-membership: derived records are read through ``epistrel.core.membership`` (#130).

Views and retrieval read a derived record's timeline membership only through
``core.membership.active_only()`` / ``active_only_sql()``, so a filter cannot forget dormant or
discarded rows, or read another coordinate in membership's place (FR-STORE-29, FR-BITEMP-5).
This scan reads ``src/`` by AST (it never imports it) and reports every raw membership filter
outside ``core/membership.py`` and ``ALLOWED``:

- a ``<table>.c.membership`` (or ``.c["membership"]``) reference;
- a comparison with a bare name ``membership``;
- a string constant (not a docstring) holding a SQL membership predicate (``membership =``,
  ``<>``, ``!=``, ``IN``, ``NOT IN``).

``ALLOWED`` is frozen: the M2/M3 modules that filter the membership of ``operations`` and of the
M2 projections (``transcript_messages``, ``lite_summary_segments``), which are not derived
records. A listed module that no longer filters is reported too, so the list only shrinks. A
new module that needs a derived row's membership uses the helpers.

Run: ``python -m tools.gates.membership`` (exit 0 clean, 1 findings); enforced in CI by
``tests/gates/test_membership_gate.py``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"

HELPER_MODULE = "src/epistrel/core/membership.py"
"""The registry and its helpers: the one place a derived row's membership is filtered."""

ALLOWED: dict[str, str] = {
    "src/epistrel/core/concurrency.py": "operations (read set)",
    "src/epistrel/core/erasure/redact.py": "transcript_messages",
    "src/epistrel/core/events/operation.py": "operations (chain, retire, shelve)",
    "src/epistrel/core/fork.py": "operations (selection, flattening)",
    "src/epistrel/core/history.py": "operations (corrected history)",
    "src/epistrel/core/input_versions.py": "operations",
    "src/epistrel/core/profiles/impact.py": "operations",
    "src/epistrel/core/projections/fold.py": "operations (projection fold)",
    "src/epistrel/core/projections/lite_summary.py": "lite_summary_segments",
    "src/epistrel/core/projections/transcript.py": "transcript_messages",
    "src/epistrel/core/ratings/change.py": "operations",
    "src/epistrel/core/ratings/reactivation.py": "operations",
    "src/epistrel/core/summary.py": "lite_summary_segments",
    "src/epistrel/core/tables/operations.py": "operations (active-turn index predicate)",
    "src/epistrel/core/timeline.py": "operations (truncate, untruncate, swap)",
    "src/epistrel/core/turns/__init__.py": "operations",
    "src/epistrel/core/variants.py": "operations (variant tree)",
    "src/epistrel/core/views.py": "operations (corrected history)",
    "src/epistrel/orchestrator/jobs/handlers/lite_summary.py": "lite_summary_segments",
}
"""Modules that filter non-derived membership (M2/M3), with the tables they filter."""

SQL_PREDICATE = re.compile(r"\bmembership\s*(=|<>|!=|\bnot\s+in\b|\bin\b)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    what: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: raw membership filter ({self.what}); use core.membership"


def _is_c(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "c"


def _filters(tree: ast.AST) -> Iterator[tuple[int, str]]:
    # Docstrings and other bare string statements are prose, not queries.
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "membership" and _is_c(node.value):
            yield node.lineno, ".c.membership"
        elif (
            isinstance(node, ast.Subscript)
            and _is_c(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "membership"
        ):
            yield node.lineno, '.c["membership"]'
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(isinstance(s, ast.Name) and s.id == "membership" for s in sides):
                yield node.lineno, "membership comparison"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and SQL_PREDICATE.search(node.value)
        ):
            yield node.lineno, "SQL membership predicate"


def scan(root: Path = REPO_ROOT, allowed: dict[str, str] | None = None) -> list[str]:
    """Every finding under ``root/src``: raw filters outside the allowed modules, and allowed
    modules (other than the helper module) that no longer filter."""
    allow = ALLOWED if allowed is None else allowed
    findings: list[str] = []
    filtering: set[str] = set()
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        found = sorted(set(_filters(tree)))
        if not found:
            continue
        filtering.add(rel)
        if rel == HELPER_MODULE or rel in allow:
            continue
        findings.extend(Finding(rel, line, what).render() for line, what in found)
    findings.extend(
        f"{rel}: listed in tools.gates.membership.ALLOWED but filters no membership; remove it"
        for rel in sorted(set(allow) - filtering)
    )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    findings = scan()
    for line in findings:
        print(line)
    print(f"check-membership: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
