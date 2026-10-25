"""check-character-context (#170; NFR-PRIV-4): ``structured_generation.j2`` is rendered only with
inputs ``character_inputs`` built from a ``CharacterContext``, which only
``core.epistemic.context`` constructs. The repository passes. The scan catches a render from
hand-built inputs, a ``CharacterContext`` built elsewhere, a ``character_inputs`` that takes
something else first and a stray seal. A narrator render (no ``variant=STRUCTURED``) and prose
are left alone. #544: every section ``character_inputs`` passes the template is classified
(``SECTIONS``), and a partitioned character gets no unscoped turn text (the rolling summary, the
text pins), statically and at run time."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.character_context import (
    CONTEXT_MODULE,
    SECTIONS,
    TURN,
    UNSCOPED,
    main,
    scan,
)

from epistrel.core.epistemic.context import CharacterContext
from epistrel.core.turns import CastEntry
from epistrel.orchestrator.structured.prompt import RenderSections, character_inputs

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


# -- every section passed to the character template (#544) ---------------------------------------

SECTION_BYPASSES = '''"""Hands the template unscoped and unclassified sections."""


def character_inputs(context: CharacterContext, sections):
    s = replace(sections, scene=context.scene, pins=sections.pins)
    return GenerationInputs(
        scene=s.scene,
        working_memory=list(sections.working_memory),
        pinned_memories=list(s.pins),
        summary_segments=list(sections.segments),
        recent_messages=[],
    )
'''

SECTION_FOLLOWS = '''"""The shape prompt.character_inputs keeps."""


def character_inputs(context: CharacterContext, sections):
    s = replace(
        sections,
        pins=() if context.partitioned else sections.pins,
        segments=() if context.partitioned else sections.segments,
        scene=context.scene,
    )
    return GenerationInputs(
        own_age=context.own_age,
        scene=s.scene,
        pinned_memories=list(s.pins),
        summary_segments=list(s.segments),
        rating_guidance=s.guidance,
    )
'''


def test_every_section_given_a_character_is_classified(tmp_path: Path) -> None:
    """#544: a section the map does not classify, a context section read from the turn's
    sections, and an unscoped section not emptied for a partitioned context are findings; the
    shape ``character_inputs`` keeps is not."""
    root = _tree(
        tmp_path,
        {
            "src/epistrel/orchestrator/bypass.py": SECTION_BYPASSES,
            "src/epistrel/orchestrator/follows.py": SECTION_FOLLOWS,
        },
    )
    found = scan(root)
    assert sorted(f.split(": ", 1)[1].split(";")[0] for f in found) == [
        "passes the context section 'working_memory' from outside the context",
        "passes the unclassified section 'recent_messages' to a character",
        "passes the unscoped section 'pinned_memories' to a partitioned character",
        "passes the unscoped section 'summary_segments' to a partitioned character",
    ]
    assert all(f.startswith("src/epistrel/orchestrator/bypass.py:") for f in found)


def _sections() -> RenderSections:
    """Every text section of the turn a sentinel naming it."""
    values: dict[str, object] = {}
    for f in fields(RenderSections):
        if f.type == "str":
            values[f.name] = f"SECTION-{f.name}"
        elif f.type == "tuple[str, ...]":
            values[f.name] = (f"SECTION-{f.name}",)
    return RenderSections(**values)  # type: ignore[arg-type]


def _given(partitioned: bool) -> dict[str, object]:
    character = CastEntry(id="c-b", name="Bobby", definition="DEF", is_player=False)
    actor = CastEntry(id="c-w", name="Wren", definition="", is_player=True)
    context = CharacterContext(
        character_id="c-b",
        beliefs=("CONTEXT-belief",),
        working_memory=("CONTEXT-memory",),
        scene="CONTEXT-scene",
        partitioned=partitioned,
    )
    inputs = character_inputs(
        context, _sections(), character, actor, say="hi", do=None, budget=1_000_000
    )
    given: dict[str, object] = inputs.model_dump(exclude_defaults=True)
    return given


def test_a_partitioned_character_gets_only_its_context_and_the_turn_text() -> None:
    """#544, the run-time half: every section ``character_inputs`` fills is one ``SECTIONS``
    classifies, and a partitioned character's carries a turn sentinel only in a ``TURN``
    section (no rolling summary, no text pins). Complement: with shared character knowledge
    the summary and the text pins are there."""
    given = _given(partitioned=True)
    assert set(given) <= set(SECTIONS)
    assert {n for n in given if "SECTION-" in repr(given[n])} == {
        n for n in given if SECTIONS[n] == TURN and "SECTION-" in repr(given[n])
    }
    assert "CONTEXT-memory" in repr(given["working_memory"])
    assert "pinned_memories" not in given
    assert "summary_segments" not in given
    shared = _given(partitioned=False)
    assert shared["pinned_memories"] == ["SECTION-pins"]
    assert shared["summary_segments"] == ["SECTION-segments"]
    assert {SECTIONS[n] for n in shared if "SECTION-" in repr(shared[n])} == {TURN, UNSCOPED}
