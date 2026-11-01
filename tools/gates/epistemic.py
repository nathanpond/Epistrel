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

Two more rules hold the epistemic model's writes (#160):

- **one writer of beliefs**: ``partitions._insert_belief`` is named nowhere under ``src/``
  except ``core/epistemic/partitions.py`` (its definition) and ``core/epistemic/transitions.py``
  (``transition()``, every belief's typed cause); #164's resolver, #167's nested beliefs and
  #173's seeds all route through ``transition()``;
- **a belief never writes a fact**: no module of ``core/epistemic/`` names ``commit_fact`` or
  ``supersede`` (canon's writers; reading canon through ``canon()`` elsewhere is not a write),
  so no false belief can silently become canon (FR-EPI-3, X-02).

And one read rule (#167, FR-BELIEF-15, NFR-PRIV-1):

- **the objective relation is a label**: ``nested.attribution_objective_relation`` (whether an
  attributed holder actually believes what a speaker attributed to it) is imported or called
  only by ``core/epistemic/nested.py`` (its definition) and the verifier that labels utterances
  (``orchestrator/verify/verifier.py``), so it can never reach a speaker's context;
- **the stored label is read only where it labels** (#563): the relation once computed is
  stored on ``utterances``/``divergences`` rows (``attribution_objective_relation``, migrations
  0074 and 0082), carried on ``AssertedClaim.attribution_objective`` and on
  ``AttributionCheck.objective``. An attribute read of either column name on any object, of
  ``.objective`` on an ``AttributionCheck`` (the class, a name annotated as or assigned from
  one, or any receiver named ``attribution*``: by AST, so a name, not a type), or a ``getattr``
  of them, is a finding outside ``LABEL_READERS``: the modules (or the functions within them)
  that compute the label, write the utterance label or record the divergences. ``.objective``
  on anything else (an eval's gold label, ``beliefs.objective()``) is not this relation.

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


SOURCE_ROOT = "src/epistrel"
BELIEF_WRITER = "_insert_belief"
WRITER_MODULES = frozenset(
    {"src/epistrel/core/epistemic/partitions.py", "src/epistrel/core/epistemic/transitions.py"}
)
CANON_WRITERS = frozenset({"commit_fact", "supersede"})
OBJECTIVE_READER = "attribution_objective_relation"
OBJECTIVE_MODULES = frozenset(
    {"src/epistrel/core/epistemic/nested.py", "src/epistrel/orchestrator/verify/verifier.py"}
)


def _objective_reads(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Imports of ``attribution_objective_relation`` and calls through ``nested.``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == OBJECTIVE_READER:
                    yield node.lineno, alias.name
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == OBJECTIVE_READER
            and isinstance(node.value, ast.Name)
            and node.value.id == "nested"
        ):
            yield node.lineno, f"nested.{node.attr}"


STORED_LABELS = frozenset({"attribution_objective_relation", "attribution_objective"})
"""Attribute names that carry the stored label: the ``utterances``/``divergences`` column
(migrations 0074, 0082) and ``AssertedClaim.attribution_objective`` (#563)."""
CHECK_CLASS = "AttributionCheck"
CHECK_FIELD = "objective"
LABEL_READERS: dict[str, frozenset[str] | None] = {
    # module -> the functions in it that may read the label (None: the whole module)
    "src/epistrel/core/epistemic/nested.py": None,  # computes the relation
    "src/epistrel/orchestrator/verify/verifier.py": None,  # AttributionCheck, the verifier
    "src/epistrel/core/epistemic/divergences.py": None,  # the divergence records
    "src/epistrel/core/turns/beats.py": frozenset(
        {"attribution_objective_of", "write_utterances"}  # the utterance label
    ),
    "src/epistrel/orchestrator/structured/pipeline.py": frozenset({"_asserted"}),
    "src/epistrel/orchestrator/epistemic/speech.py": frozenset({"speech_committed"}),
}


def _mentions_check(annotation: ast.expr | None) -> bool:
    return annotation is not None and any(
        (isinstance(n, ast.Name) and n.id == CHECK_CLASS)
        or (isinstance(n, ast.Attribute) and n.attr == CHECK_CLASS)
        or (isinstance(n, ast.Constant) and isinstance(n.value, str) and CHECK_CLASS in n.value)
        for n in ast.walk(annotation)
    )


def _checks(tree: ast.AST) -> frozenset[str]:
    """Names the module binds to an ``AttributionCheck``: annotated as one (a parameter, an
    annotated assignment) or assigned from a call of it. Flow-insensitive, per module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and _mentions_check(node.annotation):
            bound.add(node.arg)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and _mentions_check(node.annotation)
        ):
            bound.add(node.target.id)
        elif isinstance(node, ast.Assign | ast.NamedExpr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if (isinstance(func, ast.Name) and func.id == CHECK_CLASS) or (
                isinstance(func, ast.Attribute) and func.attr == CHECK_CLASS
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                bound.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(bound)


def _is_check(receiver: ast.expr, checks: frozenset[str]) -> bool:
    """Whether ``receiver.objective`` reads an ``AttributionCheck``'s objective relation: the
    class itself, a name bound to one, or anything named ``attribution*`` (``result.attribution``,
    ``attribution``; the verifier's ``CheckResult.attribution`` is the one carrier)."""
    if isinstance(receiver, ast.Name):
        name = receiver.id
    elif isinstance(receiver, ast.Attribute):
        name = receiver.attr
    else:
        return False
    return name in checks or name == CHECK_CLASS or name.lower().startswith("attribution")


def _label_reads(tree: ast.AST) -> Iterator[tuple[int, str, tuple[str, ...]]]:
    """Reads of the stored objective relation (#563), with their enclosing functions:
    ``x.attribution_objective_relation`` / ``x.attribution_objective`` on anything,
    ``.objective`` on an ``AttributionCheck`` (see ``_is_check``), and ``getattr`` of either."""
    checks = _checks(tree)

    def visit(node: ast.AST, scope: tuple[str, ...]) -> Iterator[tuple[int, str, tuple[str, ...]]]:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scope = (*scope, node.name)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr in STORED_LABELS:
                if not (isinstance(node.value, ast.Name) and node.value.id == "nested"):
                    yield node.lineno, f".{node.attr}", scope  # nested.: the rule above
            elif node.attr == CHECK_FIELD and _is_check(node.value, checks):
                yield node.lineno, f"{CHECK_CLASS}.{CHECK_FIELD}", scope
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
        ):
            attr = node.args[1].value
            if attr in STORED_LABELS:
                yield node.lineno, f"getattr(..., {attr!r})", scope
            elif attr == CHECK_FIELD and _is_check(node.args[0], checks):
                yield node.lineno, f"getattr({CHECK_CLASS}, {attr!r})", scope
        for child in ast.iter_child_nodes(node):
            yield from visit(child, scope)

    yield from visit(tree, ())


def _label_allowed(rel: str, scope: tuple[str, ...]) -> bool:
    if rel not in LABEL_READERS:
        return False
    functions = LABEL_READERS[rel]
    return functions is None or bool(functions & set(scope))


def _names(tree: ast.AST, wanted: frozenset[str]) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in wanted:
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute) and node.attr in wanted:
            yield node.lineno, node.attr
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in wanted:
                    yield node.lineno, alias.name


def writes(root: Path = REPO_ROOT) -> list[str]:
    """The findings of the two write rules and the objective-relation rule (see the module
    docstring)."""
    findings: list[str] = []
    for path in sorted((root / SOURCE_ROOT).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if rel not in WRITER_MODULES:
            findings.extend(
                f"{rel}:{line}: names {name}; transition() is the only writer of beliefs (#160)"
                for line, name in sorted(set(_names(tree, frozenset({BELIEF_WRITER}))))
            )
        if rel not in OBJECTIVE_MODULES:
            findings.extend(
                f"{rel}:{line}: reads {name}; an attribution's objective relation is a label "
                "for the verifier only (FR-BELIEF-15, #167)"
                for line, name in sorted(set(_objective_reads(tree)))
            )
        findings.extend(
            f"{rel}:{line}: reads the stored {name}; an attribution's objective relation is "
            "for labelling and divergence only (FR-BELIEF-15, NFR-PRIV-1, #563)"
            for line, name in sorted(
                {
                    (line, name)
                    for line, name, scope in _label_reads(tree)
                    if not _label_allowed(rel, scope)
                }
            )
        )
        if rel.startswith(EPISTEMIC_SIDE.package + "/"):
            findings.extend(
                f"{rel}:{line}: names {name}; a belief never writes canon (FR-EPI-3, #160)"
                for line, name in sorted(set(_names(tree, CANON_WRITERS)))
            )
    return findings


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
    findings = [*scan(), *writes()]
    for line in findings:
        print(line)
    print(f"check-epistemic-separation: {len(SIDES)} side(s), {len(findings)} finding(s)")
    return 1 if findings else 0


__all__ = ["CANON_SIDE", "EPISTEMIC_SIDE", "SIDES", "Side", "main", "scan", "writes"]
