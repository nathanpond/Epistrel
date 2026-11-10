"""One character's viewpoint, as a story-bound consumer purpose call sees it (FR-MODEL-16,
NFR-PRIV-4; #826).

A consumer purpose called inside a story may ask for one character's viewpoint: what that
character knows, believes and has seen. It is the character render's knowledge sections, built
by the only producer of them (``core.epistemic.context.build_context``) and rendered through the
same partials the render uses (``viewpoint_self.j2`` and ``viewpoint_scene.j2``, included by
``structured_generation.j2`` and by this module's ``viewpoint.j2``), so a purpose's template
receives the very text the character's own render would:

- ``viewpoint_inputs(context, character, *, budget)`` takes a ``CharacterContext`` and nothing
  else for its knowledge: the character's definition and deflection style (its own author
  notes), its own age, its belief state, its sense of time, its private memories, its scene
  line, its working memory, its retrieved context and its canon. The turn-shaped sections are
  left out: the player's input (``player_say``, ``player_do``, ``player_heard``), ``## Just
  now`` and the tracked values with their effect syntax. The render's prompt token budget
  (``render.prompt_token_budget``) and ``prompt.capped``'s drop order apply: retrieved items
  first, then canon, then the oldest working memory.
- ``render_viewpoint(inputs)`` renders ``viewpoint.j2`` into a ``Viewpoint``: the character's
  ``name`` and the pre-rendered ``text`` a purpose's template reads as ``viewpoint.name`` and
  ``viewpoint.text``.

``python -m tools.gates check-character-context`` holds the path: ``viewpoint_inputs`` takes a
``CharacterContext`` first and passes only its context sections and the character's definition;
a ``Viewpoint`` is built only here, by ``render_viewpoint``, from inputs ``viewpoint_inputs``
built; ``viewpoint.j2`` and what it includes never name a narrator-only section.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from epistrel.core.epistemic.context import CharacterContext
from epistrel.core.turns import CastEntry
from epistrel.orchestrator.models.prompts import (
    example_instance,
    make_environment,
    template_context,
)
from epistrel.orchestrator.models.registry import CharacterDefinition
from epistrel.orchestrator.structured.prompt import RenderSections, capped

VIEWPOINT_TEMPLATE = "viewpoint.j2"


class ViewpointInputs(BaseModel):
    """What ``viewpoint.j2`` renders: the character render's knowledge sections (their names
    are the render's ``GenerationInputs`` fields, so the shared partials read both)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    character_definitions: list[CharacterDefinition] = Field(min_length=1, max_length=1)
    own_age: str = ""
    beliefs: list[str] = Field(default_factory=list)
    time_belief: str = ""
    private_memories: list[str] = Field(default_factory=list)
    scene: str = ""
    working_memory: list[str] = Field(default_factory=list)
    just_now: list[str] = Field(default_factory=list)
    """Always empty: a call has no turn, so no staged beats (the partial reads the name)."""
    retrieved_memories: list[str] = Field(default_factory=list)
    canon_facts: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Viewpoint:
    """One character's viewpoint as a purpose's template sees it: ``viewpoint.name`` and
    ``viewpoint.text`` (built only by ``render_viewpoint``)."""

    name: str
    text: str


def viewpoint_inputs(
    context: CharacterContext, character: CastEntry, *, budget: int
) -> ViewpointInputs:
    """``character``'s viewpoint sections from its own ``context`` (see the module docstring),
    within ``budget`` prompt tokens."""
    if context.character_id != character.id:
        raise ValueError("a viewpoint takes that character's own context")
    s = capped(
        replace(
            RenderSections(),
            scene=context.scene,
            working_memory=context.working_memory,
            retrieved=context.retrieved,
            canon=context.canon,
        ),
        budget,
    )
    return ViewpointInputs(
        character_definitions=[
            CharacterDefinition(
                name=character.name,
                definition=character.definition,
                deflection_style=character.deflection_style,
            )
        ],
        own_age=context.own_age,
        beliefs=list(context.belief_lines),
        time_belief=context.time_belief or "",
        private_memories=list(context.private_memories),
        scene=s.scene,
        working_memory=list(s.working_memory),
        retrieved_memories=list(s.retrieved),
        canon_facts=list(s.canon),
    )


_TEMPLATE = make_environment().get_template(VIEWPOINT_TEMPLATE)


def _render(inputs: ViewpointInputs) -> str:
    variables: dict[str, Any] = template_context(inputs)
    return _TEMPLATE.render(**variables).strip()


# Checked at import, like every purpose's template: a partial naming a field the viewpoint does
# not carry fails the build (``StrictUndefined``), not a call.
_render(example_instance(ViewpointInputs))


def render_viewpoint(inputs: ViewpointInputs) -> Viewpoint:
    """``inputs`` rendered through ``viewpoint.j2``: the text the character render's knowledge
    sections would show."""
    return Viewpoint(name=inputs.character_definitions[0].name, text=_render(inputs))


__all__ = [
    "VIEWPOINT_TEMPLATE",
    "Viewpoint",
    "ViewpointInputs",
    "render_viewpoint",
    "viewpoint_inputs",
]
