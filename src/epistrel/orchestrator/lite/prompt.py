"""Assembling the single Lite prompt (FR-LITE-1, FR-RATE-4, FR-RESP-3,4; #57).

``assemble`` turns the turn's inputs (``core.turns.TurnInputs``, the Engine's own records) into
the ``generation`` purpose's ``GenerationInputs``; the ``lite_generation.j2`` template renders
them as one ``system`` message (rating guidance first, then custom instructions, the actor's and
render characters' definitions as ``### Author notes: <name>``, pinned memories, the rolling
summary and the preamble), the verbatim window as alternating ``user``/``assistant`` messages,
and the player input as the final ``user`` message. Nothing from the request's scene context
is an argument here, so it cannot reach the prompt (FR-RESP-4).

``model_params`` maps the resolved generation settings onto the call: ``length`` →
``max_tokens``, ``temperature``/``top_p`` as given, and the stop sequence ``"\\n<Actor>:"`` so
the model does not speak for the actor (omitted when the actor is itself rendered).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from epistrel.core.concurrency import hash_member
from epistrel.core.settings_schema import ResolvedGeneration
from epistrel.core.turns import CastEntry, TurnInputs, WindowRow
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.registry import (
    CharacterDefinition,
    GenerationInputs,
    PlayerInput,
    TranscriptLine,
)

NARRATOR = "Narrator"
PLAYER = "Player"


def _line(row: WindowRow, names: Mapping[str, str]) -> TranscriptLine:
    if row.role == "user":
        name = names.get(row.actor_character_id or "", PLAYER)
        say, do = row.say, row.do
        if say is None and do is None:
            say = row.text
        return TranscriptLine(name=name, say=say, do=do, role="user")
    voiced = [names[c] for c in row.render_character_ids if c in names]
    name = ", ".join(voiced) or names.get(row.actor_character_id or "", NARRATOR)
    return TranscriptLine(name=name, text=row.text, role="assistant")


def assemble(
    inputs: TurnInputs,
    *,
    actor: CastEntry,
    render: Sequence[CastEntry],
    say: str | None,
    do: str | None,
) -> GenerationInputs:
    """The ``generation`` inputs of one Lite turn, every section from the Engine's records."""
    names = {c.id: c.name for c in inputs.cast}
    included = {actor.id, *(c.id for c in render)}
    return GenerationInputs(
        rating_guidance=inputs.guidance,
        custom_instructions=inputs.instructions,
        character_definitions=[
            CharacterDefinition(name=c.name, definition=c.definition)
            for c in inputs.cast
            if c.id in included
        ],
        pinned_memories=list(inputs.pins),
        summary_segments=list(inputs.segments),
        recent_messages=[_line(row, names) for row in inputs.window],
        voiced_characters=[c.name for c in render],
        player_input=PlayerInput(name=actor.name, say=say, do=do),
    )


def model_params(
    resolved: ResolvedGeneration, *, actor: CastEntry, render: Sequence[CastEntry]
) -> ModelParams:
    """The ``generation`` call's parameters for these settings (FR-RESP-3)."""
    actor_rendered = any(c.id == actor.id for c in render)
    return ModelParams(
        temperature=resolved.temperature,
        top_p=resolved.top_p,
        max_tokens=resolved.max_tokens,
        stop=None if actor_rendered else (f"\n{actor.name}:",),
    )


def prompt_hash(messages: Sequence[Message]) -> str:
    """SHA-256 of the rendered chat messages' canonical JSON (recorded on the reply)."""
    return hash_member([m.model_dump() for m in messages])


__all__ = ["assemble", "model_params", "prompt_hash"]
