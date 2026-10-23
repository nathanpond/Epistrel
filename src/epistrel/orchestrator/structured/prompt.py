"""Assembling a structured render's prompt (docs/02 §5's Generation and Director rows,
FR-MEM-5, FR-RATE-4, FR-LITE-3, FR-SCN-11; #150).

``character_inputs`` builds the ``generation`` inputs of one character's render: the turn's
shared sections (the rating guidance first, the custom instructions, a scenario story's scenario
text, the story's text pins (#60: author-pinned memories in every render's prompt, FR-LITE-3),
the rolling summary when the profile keeps one), the rendering character's definition and
deflection style, and every knowledge section from its ``core.epistemic.context.CharacterContext``
and nothing else (#170, NFR-PRIV-4): its own age, belief state and private memories (#172) under
per-character partitions,
the scene (#137: the place and who is present, with apparent age bands), its viewpoint-filtered
working memory (FR-MEM-11: this turn's narration and the player's input included, as it
perceived them), ``## Just now`` (#174: in a ``sequential`` turn, the verified beats the earlier
primaries rendered, as it perceived them), #141's retrieved context and the canon retrieval
found. Nothing from another character's private records is there, and the director's notes are
the narrator's alone.
``python -m tools.gates check-character-context`` keeps ``structured_generation.j2`` rendered
only from inputs this function built.

``narrator_inputs`` builds the narrator's: the same sections for the omniscient narrator, plus
the scenario's director guidance labelled apart from the facts (``director_notes``, #122; and
the structured genesis's ``director`` items, #149's ``deferred_items``), never a character's
definition. ``age_inputs`` builds the age repair's: the guidance, the custom instructions, the
scene and each introduced character with its clue.

**The cap** (``render.prompt_token_budget``, ``len // 4`` tokens): over it, retrieved items
are dropped from the end first, then canon lines, then working memory from the oldest beat
(``## Just now`` counts against the cap and is never dropped). The
belief state and the private memories sit with the definition and the pins (U-31's top tier):
they are never dropped before a retrieved item (they are bounded at the source,
``context.BELIEFS_MAX`` and ``context.PRIVATE_MAX``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from epistrel.core.epistemic.context import CharacterContext
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
    just_now: tuple[str, ...] = ()
    """#174: a ``sequential`` turn's earlier primaries' staged beats, as the character perceived
    them (``## Just now``): counted against the cap, never dropped by it."""
    retrieved: tuple[str, ...] = ()
    canon: tuple[str, ...] = ()
    constraints: tuple[str, ...] = field(default_factory=tuple)
    resolution_steering: str = ""
    """#154 (FR-COMMIT-11): ``RESOLUTION_STEERING`` when an open thunk introduced in an earlier
    turn is in the context, else empty."""
    length_target: str = ""
    """#464 (FR-RESP-3): the requested response length as a target in words
    (``render.length_line``); the pipeline also holds the render to it."""
    belief_steps: bool = False
    """#168 (FR-TURN-12): the rendering character keeps its own beliefs, so its prompt carries
    ``## How to change your mind`` (the step contract and the ``think`` cite instruction)."""


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
        *sections.just_now,
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
    context: CharacterContext,
    sections: RenderSections,
    character: CastEntry,
    actor: CastEntry,
    *,
    say: str | None,
    do: str | None,
    budget: int,
    others_present: Sequence[str] = (),
    interjections: bool = False,
) -> GenerationInputs:
    """The ``generation`` inputs of ``character``'s render (``structured_generation.j2``): the
    shared ``sections`` and ``context``'s knowledge sections (see the module docstring);
    #176: ``others_present``, the reactive characters it may narrate reactions for."""
    if context.character_id != character.id:
        raise ValueError("a character's render takes that character's own context")
    s = capped(
        replace(
            sections,
            scene=context.scene,
            working_memory=context.working_memory,
            just_now=context.just_now,
            retrieved=context.retrieved,
            canon=context.canon,
        ),
        budget,
    )
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
        own_age=context.own_age,
        beliefs=list(context.belief_lines),
        private_memories=list(context.private_memories),
        pinned_memories=list(s.pins),
        summary_segments=list(s.segments),
        voiced_characters=[character.name],
        policy_constraints=list(s.constraints),
        resolution_steering=s.resolution_steering,
        length_target=s.length_target,
        belief_steps=s.belief_steps,
        player_input=_player(actor, say, do),
        scene=s.scene,
        working_memory=list(s.working_memory),
        just_now=list(s.just_now),
        others_present=list(others_present),
        interjections=interjections,
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
