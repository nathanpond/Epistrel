"""Assembling a structured render's prompt (docs/02 §5's Generation and Director rows,
FR-MEM-5, FR-RATE-4, FR-LITE-3, FR-SCN-11; #150).

``character_inputs`` builds the ``generation`` inputs of one character's render from what the
turn read: the rating guidance first, the custom instructions, a scenario story's scenario text,
the rendering character's definition (``core.turns.CastEntry`` carries no true age), its
deflection style, the story's text pins (#60: author-pinned memories in every render's prompt,
FR-LITE-3), the rolling summary when the profile keeps one, the scene (#137: the place and who is
present), its working memory (the latest ``render.working_memory_beats`` beats as it perceived
them, this turn's narration and the player's input included), #141's retrieved context and the
canon retrieval found, without its own true age (M5 adds the character's own age). Nothing from
another character's private records is there: working memory and retrieval are read for the
rendering character's viewpoint, and the director's notes are the narrator's alone.

``narrator_inputs`` builds the narrator's: the same sections for the omniscient narrator, plus
the scenario's director guidance labelled apart from the facts (``director_notes``, #122; and
the structured genesis's ``director`` items, #149's ``deferred_items``), never a character's
definition. ``age_inputs`` builds the age repair's: the guidance, the custom instructions, the
scene and each introduced character with its clue.

**The cap** (``render.prompt_token_budget``, ``len // 4`` tokens): over it, retrieved items
are dropped from the end first, then canon lines, then working memory from the oldest beat.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from epistrel.core.turns import CastEntry
from epistrel.orchestrator.models.registry import (
    AgeClue,
    CharacterDefinition,
    GenerationInputs,
    PlayerInput,
)

PLAYER = "Player"


@dataclass(frozen=True, slots=True)
class RenderSections:
    """What one render's prompt is made of, as the turn read it."""

    guidance: str = ""
    instructions: str = ""
    scenario_text: str = ""
    director_notes: str = ""
    pins: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()
    scene: str = ""
    working_memory: tuple[str, ...] = ()
    retrieved: tuple[str, ...] = ()
    canon: tuple[str, ...] = ()
    constraints: tuple[str, ...] = field(default_factory=tuple)
    resolution_steering: str = ""
    """#154 (FR-COMMIT-11): ``RESOLUTION_STEERING`` when an open thunk introduced in an earlier
    turn is in the context, else empty."""
    length_target: str = ""
    """#464 (FR-RESP-3): the requested response length as a target in words
    (``render.length_line``); the pipeline also holds the render to it."""


RESOLUTION_STEERING = (
    "If this turn settles something an earlier turn left open (who knocked, what was in the "
    "box), resolve toward insignificance; prefer entities with no history here."
)
"""The deferral bias (FR-COMMIT-11, #154): generation steering, not a hard check."""

KNOWLEDGE_STEERING = (
    "An earlier turn settled something on a character who would plausibly have known it all "
    "along; keep what that character says and does consistent with having known it."
)
"""The could-have-known steer (FR-COMMIT-11, #165): the render after a soft flag only."""


def tokens(text: str) -> int:
    return len(text) // 4


def _size(sections: RenderSections) -> int:
    parts = (
        sections.guidance,
        sections.instructions,
        sections.scenario_text,
        sections.director_notes,
        sections.scene,
        *sections.pins,
        *sections.segments,
        *sections.working_memory,
        *sections.retrieved,
        *sections.canon,
    )
    return sum(tokens(p) for p in parts)


def capped(sections: RenderSections, budget: int) -> RenderSections:
    """``sections`` within ``budget`` tokens: retrieved items dropped from the end first, then
    canon lines, then working memory from the oldest (see the module docstring)."""
    out = sections
    while _size(out) > budget and out.retrieved:
        out = replace(out, retrieved=out.retrieved[:-1])
    while _size(out) > budget and out.canon:
        out = replace(out, canon=out.canon[:-1])
    while _size(out) > budget and out.working_memory:
        out = replace(out, working_memory=out.working_memory[1:])
    return out


def _player(actor: CastEntry, say: str | None, do: str | None) -> PlayerInput:
    return PlayerInput(name=actor.name, say=say, do=do)


def character_inputs(
    sections: RenderSections,
    character: CastEntry,
    actor: CastEntry,
    *,
    say: str | None,
    do: str | None,
    budget: int,
) -> GenerationInputs:
    """The ``generation`` inputs of ``character``'s render (``structured_generation.j2``)."""
    s = capped(sections, budget)
    return GenerationInputs(
        rating_guidance=s.guidance,
        custom_instructions=s.instructions,
        scenario_text=s.scenario_text,
        character_definitions=[
            CharacterDefinition(
                name=character.name,
                definition=character.definition,
                deflection_style=character.deflection_style,
            )
        ],
        pinned_memories=list(s.pins),
        summary_segments=list(s.segments),
        voiced_characters=[character.name],
        policy_constraints=list(s.constraints),
        resolution_steering=s.resolution_steering,
        length_target=s.length_target,
        player_input=_player(actor, say, do),
        scene=s.scene,
        working_memory=list(s.working_memory),
        retrieved_memories=list(s.retrieved),
        canon_facts=list(s.canon),
    )


def narrator_inputs(
    sections: RenderSections,
    actor: CastEntry,
    *,
    say: str | None,
    do: str | None,
    budget: int,
    open_references: Sequence[str] = (),
) -> GenerationInputs:
    """The narrator's ``generation`` inputs (``narrator_generation.j2``); a follow-up render's
    (#146, FR-COMMIT-7) name the salient references it must settle."""
    s = capped(sections, budget)
    return GenerationInputs(
        rating_guidance=s.guidance,
        custom_instructions=s.instructions,
        scenario_text=s.scenario_text,
        director_notes=s.director_notes,
        pinned_memories=list(s.pins),
        policy_constraints=list(s.constraints),
        resolution_steering=s.resolution_steering,
        player_input=_player(actor, say, do),
        scene=s.scene,
        working_memory=list(s.working_memory),
        retrieved_memories=list(s.retrieved),
        canon_facts=list(s.canon),
        open_references=list(open_references),
    )


def age_inputs(
    sections: RenderSections,
    actor: CastEntry,
    clues: Sequence[tuple[str, str]],
) -> GenerationInputs:
    """The age repair's ``generation`` inputs (``age_repair.j2``): ``clues`` are each
    introduced character's name and what the render said about it."""
    return GenerationInputs(
        rating_guidance=sections.guidance,
        custom_instructions=sections.instructions,
        player_input=PlayerInput(name=actor.name),
        scene=sections.scene,
        introductions=[AgeClue(name=name, clue=clue) for name, clue in clues],
    )


def working_line(kind: str, name: str | None, text: str, *, own: bool) -> str:
    """One working-memory beat as a prompt line: ``Bobby says: …``, ``Bobby does: …``, a
    narration verbatim, and the rendering character's own thought as ``(You think) …``."""
    body = text.strip()
    if kind == "say" and name:
        return f"{name} says: {body}"
    if kind == "do" and name:
        return f"{name} does: {body}"
    if kind == "think" and own:
        return f"(You think) {body}"
    if kind == "think" and name:
        return f"({name} thinks) {body}"
    return body


__all__ = [
    "KNOWLEDGE_STEERING",
    "PLAYER",
    "RESOLUTION_STEERING",
    "RenderSections",
    "age_inputs",
    "capped",
    "character_inputs",
    "narrator_inputs",
    "tokens",
    "working_line",
]
