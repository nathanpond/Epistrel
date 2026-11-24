"""A ``think``'s copied cites resolve to the records its prompt showed (#994, FR-TURN-8).

``prompt.normalize_cite`` collapses whitespace, strips, drops one leading ``- `` and one pair
of surrounding quotes, and nothing else; ``prompt.cite_lines`` maps the lines a character's
prompt showed after the cap to their records; ``prompt.resolve_cites`` turns a cite copying a
shown canon line into its fact id, and nothing else into one.
"""

from __future__ import annotations

from typing import Any

import pytest

from epistrel.core.epistemic.context import CharacterContext
from epistrel.core.turns import CastEntry
from epistrel.orchestrator.structured.prompt import (
    RenderSections,
    character_inputs,
    cite_lines,
    normalize_cite,
    resolve_cites,
)

ACTOR = CastEntry(id="c-w", name="Wren", definition="", is_player=True)
BOBBY = CastEntry(id="c-b", name="Bobby", definition="DEF", is_player=False)
HALL = "Dana located_at the front hall"
KITCHEN = "Dana located_at the kitchen"
SAID = "Dana says: I'm fine. Just tired tonight."
MEMORY = "Dana was quiet at dinner."


def _lines(context: CharacterContext, budget: int = 1_000_000) -> dict[str, frozenset[str]]:
    inputs = character_inputs(context, RenderSections(), BOBBY, ACTOR, budget=budget)
    return cite_lines(context, inputs)


def _context(**fields: Any) -> CharacterContext:
    return CharacterContext(character_id="c-b", **fields)


@pytest.mark.parametrize(
    ("cite", "expected"),
    [
        (HALL, HALL),
        (f"- {HALL}", HALL),
        ("  Dana   located_at\tthe front\nhall  ", HALL),
        (f'"{HALL}"', HALL),
        (f"'{HALL}'", HALL),
        (f"“{HALL}”", HALL),
        (f'- "{HALL}"', HALL),
        (HALL.upper(), HALL.upper()),
        (f"{HALL}.", f"{HALL}."),
        (f'""{HALL}""', f'"{HALL}"'),
    ],
)
def test_normalisation_collapses_whitespace_and_one_marker_and_nothing_else(
    cite: str, expected: str
) -> None:
    assert normalize_cite(cite) == expected


def test_a_copied_canon_line_resolves_to_its_fact() -> None:
    lines = _lines(_context(canon=(HALL, KITCHEN), canon_ids=("f-hall", "f-kitchen")))
    assert resolve_cites([f"- {HALL}"], lines) == ["f-hall"]
    assert resolve_cites([f'"{KITCHEN}"', KITCHEN], lines) == ["f-kitchen"]


def test_case_differences_do_not_match() -> None:
    lines = _lines(_context(canon=(HALL,), canon_ids=("f-hall",)))
    assert resolve_cites([HALL.lower()], lines) == [HALL.lower()]  # no fact id: dropped later


def test_a_step_number_or_paraphrase_resolves_to_nothing_new() -> None:
    lines = _lines(_context(canon=(HALL,), canon_ids=("f-hall",)))
    cites = ["1", "Dana is in the front hall"]
    assert resolve_cites(cites, lines) == cites  # passed through; fact_edges drops non-ids


def test_a_record_id_keeps_its_acceptance() -> None:
    lines = _lines(_context(canon=(HALL,), canon_ids=("f-hall",)))
    assert resolve_cites(["f-other"], lines) == ["f-other"]


def test_a_line_the_cap_dropped_is_not_citable() -> None:
    long = "x" * 4_000
    context = _context(canon=(long, HALL), canon_ids=("f-long", "f-hall"))
    shown = character_inputs(context, RenderSections(), BOBBY, ACTOR, budget=1_000)
    assert HALL not in shown.canon_facts  # the cap dropped it from the end
    lines = cite_lines(context, shown)
    assert HALL not in lines
    assert resolve_cites([HALL], lines) == [HALL]  # not a fact id: no edge


def test_a_line_never_shown_is_not_citable() -> None:
    lines = _lines(_context(canon=(HALL,), canon_ids=("f-hall",)))
    assert resolve_cites([KITCHEN], lines) == [KITCHEN]


def test_the_working_memory_cap_keeps_the_newest_lines_with_their_ids() -> None:
    old = "y" * 4_000
    context = _context(working_memory=(old, SAID), working_ids=("e-old", "e-said"))
    shown = character_inputs(context, RenderSections(), BOBBY, ACTOR, budget=1_000)
    assert shown.working_memory == [SAID]
    assert cite_lines(context, shown) == {SAID: frozenset({"event:e-said"})}


def test_a_memory_line_resolves_to_its_memory_and_records_no_edge() -> None:
    lines = _lines(
        _context(
            working_memory=(SAID,),
            working_ids=("e-said",),
            retrieved=(MEMORY,),
            retrieved_ids=("memory:m-1",),
        )
    )
    assert lines == {SAID: frozenset({"event:e-said"}), MEMORY: frozenset({"memory:m-1"})}
    assert resolve_cites([f'"{SAID}"', MEMORY], lines) == []


def test_a_line_shared_by_two_records_resolves_to_none() -> None:
    two_facts = _lines(_context(canon=(HALL, HALL), canon_ids=("f-1", "f-2")))
    assert resolve_cites([HALL], two_facts) == []
    across = _lines(
        _context(
            retrieved=(HALL,), retrieved_ids=("memory:m-1",), canon=(HALL,), canon_ids=("f-1",)
        )
    )
    assert resolve_cites([HALL], across) == []


def test_the_same_record_shown_twice_counts_once() -> None:
    lines = _lines(_context(canon=(HALL, HALL), canon_ids=("f-1", "f-1")))
    assert resolve_cites([HALL], lines) == ["f-1"]


def test_lines_given_without_ids_add_nothing() -> None:
    assert _lines(_context(canon=(HALL,), retrieved=(MEMORY,))) == {}
