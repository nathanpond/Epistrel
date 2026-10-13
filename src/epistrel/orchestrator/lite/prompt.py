"""Assembling the single Lite prompt (FR-LITE-1, FR-RATE-4, FR-RESP-3,4; #57).

``assemble`` turns the turn's inputs (``core.turns.TurnInputs``, the Engine's own records) into
the ``generation`` purpose's ``GenerationInputs``; the ``lite_generation.j2`` template renders
them as one ``system`` message (rating guidance first, then custom instructions, the actor's
and render characters' definitions as ``### Author notes: <name>`` in request order, each with
its deflection style when it has one (FR-GUARD-7), pinned memories, the rolling summary and the
preamble), the verbatim window as alternating ``user``/``assistant`` messages, and the player
input as the final ``user`` message. Nothing from the request's scene context is an argument
here, so it cannot reach the prompt (FR-RESP-4).

Several render characters share this one generation (FR-RESP-5, FR-LITE-5). Their knowledge is
separated only by instruction: each one's notes are labelled with its name and the preamble says
each character knows only what its notes and the scene show. The notes all sit in one shared
prompt, so a secret in one character's notes is in the context the others are voiced from.

On a guardrail regeneration (#68) the system block ends with the output gate's constraint
lines, one per output policy an earlier draft broke; the deflection styles in the notes let the
regenerated reply refuse or change the subject in character (FR-GUARD-7; #115).

``model_params`` maps the resolved generation settings onto the call: ``length`` →
``max_tokens``, ``temperature``/``top_p`` as given, and the stop sequence ``"\\n<Actor>:"`` so
the model does not speak for the actor (omitted when the actor is itself rendered).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from epistrel.core.concurrency import hash_member
from epistrel.core.settings_schema import ResolvedGeneration
from epistrel.core.turns import TurnInputs, WindowRow
from epistrel.orchestrator.lite.cast import ResolvedCast
from epistrel.orchestrator.models.adapter import Message, ModelParams
from epistrel.orchestrator.models.registry import (
    CharacterDefinition,
    GenerationInputs,
    PlayerInput,
    TranscriptLine,
)

NARRATOR = "Narrator"
PLAYER = "Player"


def transcript_line(row: WindowRow, names: Mapping[str, str]) -> TranscriptLine:
    """One transcript row as a ``TranscriptLine`` (the window, and #72's summary chunks)."""
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
    cast: ResolvedCast,
    *,
    say: str | None,
    do: str | None,
    constraints: Sequence[str] = (),
) -> GenerationInputs:
    """The ``generation`` inputs of one Lite turn, every section from the Engine's records.

    The author notes are the actor's and the render characters', in request order; the
    definitions are the ones read in the turn's snapshot (``inputs.cast``). ``constraints``
    are the output gate's lines on a guardrail regeneration (#68)."""
    names = {c.id: c.name for c in inputs.cast}
    current = {c.id: c for c in inputs.cast}
    return GenerationInputs(
        rating_guidance=inputs.guidance,
        custom_instructions=inputs.instructions,
        character_definitions=[
            CharacterDefinition(
                name=c.name, definition=c.definition, deflection_style=c.deflection_style
            )
            for c in (current.get(entry.id, entry) for entry in cast.noted)
        ],
        pinned_memories=list(inputs.pins),
        summary_segments=list(inputs.segments),
        recent_messages=[transcript_line(row, names) for row in inputs.window],
        voiced_characters=[c.name for c in cast.render],
        policy_constraints=list(constraints),
        player_input=PlayerInput(name=cast.actor.name, say=say, do=do),
    )


def model_params(resolved: ResolvedGeneration, cast: ResolvedCast) -> ModelParams:
    """The ``generation`` call's parameters for these settings (FR-RESP-3)."""
    return ModelParams(
        temperature=resolved.temperature,
        top_p=resolved.top_p,
        max_tokens=resolved.max_tokens,
        stop=None if cast.actor_rendered else (f"\n{cast.actor.name}:",),
    )


def prompt_hash(messages: Sequence[Message]) -> str:
    """SHA-256 of the rendered chat messages' canonical JSON (recorded on the reply)."""
    return hash_member([m.model_dump() for m in messages])


__all__ = ["assemble", "model_params", "prompt_hash", "transcript_line"]
