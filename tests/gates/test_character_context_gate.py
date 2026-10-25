"""check-character-context (#170; NFR-PRIV-4): ``structured_generation.j2`` is rendered only with
inputs ``character_inputs`` built from a ``CharacterContext``, which only
``core.epistemic.context`` constructs. The repository passes. The scan catches a render from
hand-built inputs, a ``CharacterContext`` built elsewhere, a ``character_inputs`` that takes
something else first and a stray seal. A narrator render (no ``variant=STRUCTURED``) and prose
are left alone."""

from __future__ import annotations

from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.character_context import CONTEXT_MODULE, main, scan

BYPASSES = '''"""Renders structured_generation.j2 with CharacterContext( in prose only."""
from epistrel.core.epistemic.context import CharacterContext


def character_inputs(sections, character):
    return sections


async def produce(models, sections):
    inputs = GenerationInputs(scene="everything")
    await render_beats(models, inputs, schema, variant=STRUCTURED)
    models.prepare("generation", sections, variant=STRUCTURED)
    return CharacterContext("someone")
'''

FOLLOWS = '''"""Builds the inputs from a context, and renders the narrator from anything."""


async def produce(models, knowing, sections):
    inputs = prompts.character_inputs(knowing, sections)
    await render_beats(models, inputs, schema, variant=STRUCTURED)
    models.prepare("generation", inputs, variant=STRUCTURED)
    await render_beats(models, sections, schema, variant=NARRATOR_VARIANT)
'''


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_the_repository_renders_characters_only_from_their_context() -> None:
    assert scan() == []
    assert main([]) == 0
    assert gates_main(["check-character-context"]) == 0


def test_a_bypass_is_found(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "src/epistrel/orchestrator/bypass.py": BYPASSES,
            "src/epistrel/orchestrator/follows.py": FOLLOWS,
            CONTEXT_MODULE: "def build():\n    return CharacterContext('a')\n",
        },
    )
    found = scan(root)
    assert [f.split(": ", 1)[1].split(";")[0] for f in found] == [
        "character_inputs does not take a CharacterContext first",
        "renders structured_generation.j2 with inputs not built by character_inputs",
        "renders structured_generation.j2 with inputs not built by character_inputs",
        "builds a CharacterContext outside core.epistemic.context",
    ]
    assert all(f.startswith("src/epistrel/orchestrator/bypass.py:") for f in found)
