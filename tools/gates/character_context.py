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
- a ``character_inputs`` whose first parameter is not annotated ``CharacterContext``;
- every prompt section ``character_inputs`` passes to ``GenerationInputs`` (#544), against the
  declared map ``SECTIONS``: a section the map does not classify is a finding (a new section
  is classified before it reaches a character); a ``CONTEXT`` section must come from the
  context (the first parameter, or a ``replace`` field set from it; #572: the player's input
  among them, perception-routed, never the typed text); an ``UNSCOPED`` one (turn
  text with no viewpoint scope: the rolling summary, the text pins) must come only from
  ``replace`` fields that are empty when the context is ``partitioned``; a ``NARRATOR`` one
  (#208: ``external_data``, the provider values' fenced blocks) is never passed at all, and
  ``structured_generation.j2`` (with what it includes) never names it.

A repair's regeneration (#151) and a policy regeneration (#68) rebuild their inputs from the
render's own (``Rendered.character_inputs``), so they carry the same context. The age-repair
and narrator templates are out of scope: they are not a character's prompt.

Run ``python -m tools.gates check-character-context``. It exits 0 when clean and 1 on findings.
CI enforces it through ``tests/gates/test_character_context_gate.py``.
"""

from __future__ import annotations

import ast
import re
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

CONTEXT = "context"
"""A section built by ``build_context``: what the character could know. #572: the player's
input is one, perception-routed with its qualifiers (a whisper's non-target gets its envelope),
never the typed text."""
TURN = "turn"
"""Turn text every character may see: the guidance, the instructions and the shared scenario
text, the character's own definition, the policy and steering lines, and the turn's roles
(#176, #180)."""
UNSCOPED = "unscoped"
"""Turn text with no viewpoint scope (#544): a partitioned character never gets it."""
NARRATOR = "narrator"
"""The narrator's alone (#208): provider content, which a character learns only as a belief
through ``build_context`` (#170). ``character_inputs`` never passes it."""
SECTIONS: dict[str, str] = {
    "own_age": CONTEXT,
    "player_input": CONTEXT,
    "beliefs": CONTEXT,
    "private_memories": CONTEXT,
    "scene": CONTEXT,
    "working_memory": CONTEXT,
    "just_now": CONTEXT,
    "retrieved_memories": CONTEXT,
    "canon_facts": CONTEXT,
    "tracked_values": CONTEXT,
    "tracked_syntax": CONTEXT,
    "rating_guidance": TURN,
    "custom_instructions": TURN,
    "scenario_text": TURN,
    "character_definitions": TURN,
    "voiced_characters": TURN,
    "policy_constraints": TURN,
    "resolution_steering": TURN,
    "length_target": TURN,
    "belief_steps": TURN,
    "clock_contract": TURN,
    "others_present": TURN,
    "interjections": TURN,
    "player_requests": TURN,
    "pinned_memories": UNSCOPED,
    "summary_segments": UNSCOPED,
    "external_data": NARRATOR,
}
"""Every section a character's ``GenerationInputs`` may carry, classified (#544)."""


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


def _mentions(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _attrs(node: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _guarded(value: ast.AST, context: str) -> bool:
    """``() if <context>.partitioned else ...``: empty for a partitioned context."""
    return (
        isinstance(value, ast.IfExp)
        and any(
            isinstance(n, ast.Attribute)
            and n.attr == "partitioned"
            and isinstance(n.value, ast.Name)
            and n.value.id == context
            for n in ast.walk(value.test)
        )
        and isinstance(value.body, ast.Tuple)
        and not value.body.elts
    )


def _section_findings(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[tuple[int, str]]:
    """#544: every section ``character_inputs`` hands the character template, against
    ``SECTIONS`` (see the module docstring)."""
    params = function.args.posonlyargs + function.args.args
    if not params:
        return
    context = params[0].arg
    from_context: set[str] = set()
    guarded: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _name(node.func) == "replace":
            for k in node.keywords:
                if k.arg is None:
                    continue
                if _guarded(k.value, context):
                    guarded.add(k.arg)
                elif _mentions(k.value, context):
                    from_context.add(k.arg)
    for node in ast.walk(function):
        if not (isinstance(node, ast.Call) and _name(node.func) == "GenerationInputs"):
            continue
        for k in node.keywords:
            kind = SECTIONS.get(k.arg or "")
            used = _attrs(k.value)
            if kind is None:
                yield node.lineno, f"passes the unclassified section {k.arg!r} to a character"
            elif kind == NARRATOR:
                yield node.lineno, f"passes the narrator-only section {k.arg!r} to a character"
            elif kind == CONTEXT and not (_mentions(k.value, context) or used & from_context):
                yield node.lineno, f"passes the context section {k.arg!r} from outside the context"
            elif kind == UNSCOPED and not (used and used <= guarded):
                yield (
                    node.lineno,
                    f"passes the unscoped section {k.arg!r} to a partitioned character",
                )


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
            yield from _section_findings(function)


TEMPLATES = "src/epistrel/orchestrator/models/templates"
CHARACTER_TEMPLATE = "structured_generation.j2"
INCLUDE = re.compile(r"""{%-?\s*include\s+["']([^"']+)["']""")


def _template_findings(root: Path) -> Iterator[str]:
    """#208: the character template, and every template it includes, never names a
    ``NARRATOR`` section."""
    narrator_only = [name for name, kind in SECTIONS.items() if kind == NARRATOR]
    pending, seen = [CHARACTER_TEMPLATE], set()
    while pending:
        name = pending.pop()
        path = root / TEMPLATES / name
        if name in seen or not path.is_file():
            continue
        seen.add(name)
        source = path.read_text(encoding="utf-8")
        pending.extend(INCLUDE.findall(source))
        for section in narrator_only:
            if re.search(rf"\b{section}\b", source):
                yield (
                    f"{TEMPLATES}/{name}: names the narrator-only section {section!r}; a "
                    "character's prompt comes from build_context (NFR-PRIV-4, #170, #208)"
                )


def scan(root: Path = REPO_ROOT) -> list[str]:
    """Every breach of the rule under ``root`` (see the module docstring)."""
    findings: list[str] = list(_template_findings(root))
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


__all__ = [
    "CHARACTER_TEMPLATE",
    "CONTEXT",
    "CONTEXT_MODULE",
    "NARRATOR",
    "SEALERS",
    "SECTIONS",
    "TEMPLATES",
    "TURN",
    "UNSCOPED",
    "main",
    "scan",
]
