"""check-keyed-readers: verdict-bearing proposition reads go through ``KeyedLookup`` (#136).

A structured turn's verdicts, preconditions and blockers must rest on keyed lookups
(FR-CONC-11): ``epistrel.core.readset.KeyedLookup`` records each query key and the hash of its
result, so a matching change at commit regenerates the turn (FR-CONC-12). The modules that make
such lookups are listed in ``epistrel.core.readset.KEYED_READERS`` (#140's ``verify``, #144's
permission check and #146's ledger lookups add themselves when they land). This scan reads them
by AST (it never imports them) and reports, in each, every direct read of ``facts`` or
``propositions``:

- an import of a canon read (``canon``, ``canon_at``, ``canon_as_recorded``, ``hidden_from``) or
  of the ``core.canon.facts`` module;
- an import of the ``facts`` or ``propositions`` table;
- a ``facts.c`` or ``propositions.c`` reference;
- a string constant (not a docstring) holding SQL that reads either table (``FROM``/``JOIN``);
- likewise for the structured projections over canon (#137, carried by #141): an import of
  ``profile_view``/``scene_view`` (``core.projections.structured``) or of the
  ``relationships``, ``placement`` or ``scene_state`` table, a ``.c`` reference to one, or SQL
  reading one. A turn reads them through ``KeyedLookup.profile``/``scene``.

A listed module that does not exist is reported, and so is a listed module of ``EXEMPT``: the
store and ``KeyedLookup`` itself read the tables directly by design, and are never keyed readers.

Run: ``python -m tools.gates check-keyed-readers`` (exit 0 clean, 1 findings); enforced in CI by
``tests/gates/test_keyed_readers.py``, which also checks the test-only keyed reader
(``tests/readset_support.py``).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
READSET_MODULE = "src/epistrel/core/readset.py"
LIST_NAME = "KEYED_READERS"

EXEMPT: dict[str, str] = {
    READSET_MODULE: "KeyedLookup itself",
    "src/epistrel/core/canon/facts.py": "the canon store (#133)",
    "src/epistrel/core/propositions/store.py": "the proposition store (#131)",
}
"""Modules that read the tables directly by design and are never keyed readers."""

CANON_MODULE = "epistrel.core.canon.facts"
CANON_READS = frozenset({"canon", "canon_at", "canon_as_recorded", "hidden_from"})
PROJECTION_MODULE = "epistrel.core.projections.structured"
PROJECTION_READS = frozenset({"profile_view", "scene_view"})
TABLES = frozenset({"facts", "propositions", "relationships", "placement", "scene_state"})
TABLE_MODULES = frozenset(
    {
        "epistrel.core.tables",
        "epistrel.core.tables.facts",
        "epistrel.core.tables.propositions",
        "epistrel.core.tables.projections",
    }
)
SQL_READ = re.compile(
    r"\b(from|join)\s+(facts|propositions|relationships|placement|scene_state)\b",
    re.IGNORECASE,
)


def listed(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """The dotted module names ``KEYED_READERS`` holds (a literal tuple or list of strings)."""
    tree = ast.parse((root / READSET_MODULE).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            target, value = (first.id if isinstance(first, ast.Name) else None), node.value
        if target == LIST_NAME and isinstance(value, ast.Tuple | ast.List):
            return tuple(
                e.value
                for e in value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    raise ValueError(f"{READSET_MODULE} defines no literal {LIST_NAME}")


def path_of(module: str) -> str:
    """``epistrel.core.verify.rules`` → ``src/epistrel/core/verify/rules.py``; a ``tests.``
    module stays under ``tests/``."""
    parts = module.split(".")
    base = "" if parts[0] == "tests" else "src/"
    return base + "/".join(parts) + ".py"


def _reads(tree: ast.AST) -> Iterator[tuple[int, str]]:
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            names = {a.name for a in node.names}
            if node.module == CANON_MODULE and names & CANON_READS:
                yield node.lineno, f"imports {', '.join(sorted(names & CANON_READS))}"
            elif node.module == PROJECTION_MODULE and names & PROJECTION_READS:
                yield node.lineno, f"imports {', '.join(sorted(names & PROJECTION_READS))}"
            elif node.module == "epistrel.core.canon" and "facts" in names:
                yield node.lineno, "imports core.canon.facts"
            elif node.module in TABLE_MODULES and names & TABLES:
                yield node.lineno, f"imports the {', '.join(sorted(names & TABLES))} table"
        elif isinstance(node, ast.Import):
            if any(a.name == CANON_MODULE for a in node.names):
                yield node.lineno, "imports core.canon.facts"
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "c"
            and isinstance(node.value, ast.Name)
            and node.value.id in TABLES
        ):
            yield node.lineno, f"{node.value.id}.c"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and SQL_READ.search(node.value)
        ):
            yield node.lineno, "SQL read of facts/propositions or a structured projection"


def scan(root: Path = REPO_ROOT, readers: Sequence[str] | None = None) -> list[str]:
    """Every finding over ``readers`` (dotted names; default: ``KEYED_READERS``)."""
    modules = listed(root) if readers is None else tuple(readers)
    findings: list[str] = []
    for module in modules:
        rel = path_of(module)
        if rel in EXEMPT:
            findings.append(f"{rel}: listed in {LIST_NAME} but exempt ({EXEMPT[rel]})")
            continue
        path = root / rel
        if not path.is_file():
            findings.append(f"{rel}: listed in {LIST_NAME} but does not exist")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        findings.extend(
            f"{rel}:{line}: {what}; a keyed reader reads facts only through KeyedLookup"
            for line, what in sorted(set(_reads(tree)))
        )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    findings = scan()
    for line in findings:
        print(line)
    print(f"check-keyed-readers: {len(listed())} reader(s), {len(findings)} finding(s)")
    return 1 if findings else 0


__all__ = ["EXEMPT", "listed", "main", "path_of", "scan"]
