"""Who acts and who answers in a Lite turn (FR-AUTH-3, FR-RESP-5, FR-LITE-5; #62).

``resolve_cast`` checks a respond request's characters against the story's cast and settles
the order the turn uses everywhere: the prompt's author notes, the voicing line, the reply's
``render_character_ids`` and the idempotency hash. It runs first in body validation, before
the archived check and before anything is written, so a refusal has no side effects.

- **The actor** may be any character of the story, a player's own character or not: who acts
  as whom is the consumer's decision (FR-AUTH-3 a). The input is only ever the actor's own
  speech and action; the request has no field for anyone else's words (FR-AUTH-3 b).
- **The render characters** are the ones the story lets the scene trigger. In M2 that is any
  cast member. Omitted, they are every cast member except the actor, in cast order, or the
  actor alone when no one else is in the cast. Listed, they keep the request's order with
  duplicates dropped, and the actor may be one of them. An explicit empty list is refused by
  the request model.
- **Scene context**'s ``characters_present`` must be cast members too (then it is ignored).

An unknown actor and unknown render or present ids are reported together in one 422
``validation_error`` (``details.unknown_actor`` / ``unknown_render_ids`` /
``unknown_present_ids``). #58 reuses this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from epistrel.core.errors import InvalidRequest
from epistrel.core.turns import CastEntry, StoryCast
from epistrel.orchestrator.lite.schemas import RespondRequest


@dataclass(frozen=True, slots=True)
class ResolvedCast:
    """The actor and the render characters, in the order they are voiced."""

    actor: CastEntry
    render: tuple[CastEntry, ...]

    @property
    def render_ids(self) -> list[str]:
        return [c.id for c in self.render]

    @property
    def actor_rendered(self) -> bool:
        return any(c.id == self.actor.id for c in self.render)

    @property
    def noted(self) -> tuple[CastEntry, ...]:
        """Whose author notes the prompt carries, in request order: the actor, then each render
        character not already listed."""
        return (self.actor, *(c for c in self.render if c.id != self.actor.id))


def default_render_ids(story: StoryCast, actor_id: str) -> list[str]:
    """Every cast member except the actor, in cast order; the actor when no one else is left."""
    others = [c.id for c in story.cast if c.id != actor_id]
    return others or [actor_id]


def resolve_cast(story: StoryCast, request: RespondRequest) -> ResolvedCast:
    """The request's characters against the story's cast; 422 naming every unknown id."""
    members = story.by_id()
    problems: dict[str, Any] = {}
    actor = members.get(request.actor_character_id)
    if actor is None:
        problems["unknown_actor"] = request.actor_character_id
    if request.render_character_ids is None:
        ids = default_render_ids(story, request.actor_character_id)
    else:
        ids = list(dict.fromkeys(request.render_character_ids))
        unknown = [i for i in ids if i not in members]
        if unknown:
            problems["unknown_render_ids"] = unknown
    if request.scene_context is not None:
        absent = [i for i in request.scene_context.characters_present if i not in members]
        if absent:
            problems["unknown_present_ids"] = absent
    if problems or actor is None:
        raise InvalidRequest("the request names characters that are not in the story", problems)
    return ResolvedCast(actor=actor, render=tuple(members[i] for i in ids))


__all__ = ["ResolvedCast", "default_render_ids", "resolve_cast"]
