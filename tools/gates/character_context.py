"""check-character-context: a character's prompt is built only by ``build_context`` (NFR-PRIV-4,
FR-MEM-11; #170).

``structured_generation.j2`` (the character render's template, variant ``STRUCTURED``) must be
rendered only with inputs made from a ``core.epistemic.context.CharacterContext``, which only
``core.epistemic.context`` builds. Without importing them, this scan reads by AST every module
under ``src/epistrel`` for:

- a ``CharacterContext(...)`` construction outside ``core/epistemic/context.py``;
- a call of the sealing helper ``_seal`` of ``core.retrieval.filtered`` (an already
  viewpoint-filtered set, U-31) outside the modules that apply a viewpoint filter (``SEALERS``);
- a model call that renders the character template (``render_beats(...)`` or
  ``models.prepare(...)`` with ``variant=STRUCTURED``) whose inputs are not a name the same
  function assigned from ``character_inputs(...)``;
- a ``character_inputs`` whose first parameter is not annotated ``CharacterContext``.

A repair's regeneration (#151) and a policy regeneration (#68) rebuild their inputs from the
render's own (``Rendered.character_inputs``), so they carry the same context. The age-repair
and narrator templates are out of scope: they are not a character's prompt.

Run ``python -m tools.gates check-character-context``. It exits 0 when clean and 1 on findings.
CI enforces it through ``tests/gates/test_character_context_gate.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = "src/epistrel"
CONTEXT_MODULE = "src/epistrel/core/epistemic/context.py"
SEALERS = frozenset(
    {
        "src/epistrel/core/retrieval/filtered.py",
        "src/epistrel/core/retrieval/pipeline.py",
        CONTEXT_MODULE,
    }
)
"""The modules that may seal an already viewpoint-filtered set."""
RENDERERS = frozenset({"render_beats", "prepare"})
"""The calls that render a character template when given ``variant=STRUCTURED``."""


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _structured(call: ast.Call) -> bool:
    return any(
        k.arg == "variant" and isinstance(k.value, ast.Name) and k.value.id == "STRUCTURED"
        for k in call.keywords
    )


def _inputs_arg(call: ast.Call) -> ast.AST | None:
    """The inputs a render call is given: ``render_beats(models, inputs, ...)`` or
    ``models.prepare(purpose, inputs, ...)``, the second positional argument."""
    if len(call.args) >= 2:
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "inputs"), None)


def _context_names(function: ast.AST) -> set[str]:
    """The names ``function`` assigns from a ``character_inputs(...)`` call."""
    found: set[str] = set()
    for node in ast.walk(function):
        value = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        if isinstance(value, ast.Call) and _name(value.func) == "character_inputs":
            found |= {t.id for t in targets if isinstance(t, ast.Name)}
    return found


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _findings(rel: str, tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        if name == "CharacterContext" and rel != CONTEXT_MODULE:
            yield node.lineno, "builds a CharacterContext outside core.epistemic.context"
        if name == "_seal" and rel not in SEALERS:
            yield node.lineno, "seals a viewpoint-filtered set outside the filtering modules"
    for function in _functions(tree):
        allowed = _context_names(function)
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and _name(node.func) in RENDERERS and _structured(node):
                given = _inputs_arg(node)
                if not (isinstance(given, ast.Name) and given.id in allowed):
                    yield (
                        node.lineno,
                        "renders structured_generation.j2 with inputs not built by "
                        "character_inputs",
                    )
        if function.name == "character_inputs":
            params = function.args.posonlyargs + function.args.args
            first = params[0].annotation if params else None
            if not (first is not None and _name(first) == "CharacterContext"):
                yield function.lineno, "character_inputs does not take a CharacterContext first"


def scan(root: Path = REPO_ROOT) -> list[str]:
    """Every breach of the rule under ``root`` (see the module docstring)."""
    findings: list[str] = []
    for path in sorted((root / SOURCE_ROOT).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        findings.extend(
            f"{rel}:{line}: {what}; a character's prompt comes from build_context (NFR-PRIV-4, "
            "#170)"
            for line, what in sorted(set(_findings(rel, tree)))
        )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    findings = scan()
    for line in findings:
        print(line)
    print(f"check-character-context: {len(findings)} finding(s)")
    return 1 if findings else 0


__all__ = ["CONTEXT_MODULE", "SEALERS", "main", "scan"]
