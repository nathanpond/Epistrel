"""Output viewpoint resolution (FR-RESP-12), the provisional form #57 ships; #59 extends it.

A viewpoint is ``character:<id>`` (a member of the story's cast) or ``omniscient``. Resolution:
the requested viewpoint when one is named; otherwise the request's actor character; otherwise
the story's ``default_output_viewpoint`` (which can only be a character); otherwise the request
is refused, 422 ``viewpoint_unresolved`` (fail closed). ``omniscient`` is used only when named.
"""

from __future__ import annotations

from collections.abc import Collection

from epistrel.core.errors import ViewpointUnresolved
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX

OMNISCIENT = "omniscient"


def character_viewpoint(character_id: str) -> str:
    return f"{CHARACTER_VIEWPOINT_PREFIX}{character_id}"


def resolve_viewpoint(
    requested: str | None,
    *,
    actor_character_id: str | None,
    story_default: str | None,
    cast_ids: Collection[str],
    field: str = "body.output_viewpoint",
) -> str:
    """The viewpoint a response is produced for; raises ``ViewpointUnresolved``."""
    if requested is not None:
        named = requested.strip()
        if named == OMNISCIENT:
            return OMNISCIENT
        if named.startswith(CHARACTER_VIEWPOINT_PREFIX):
            member = named.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
            if member in cast_ids:
                return character_viewpoint(member)
        raise ViewpointUnresolved(
            "the output viewpoint must be omniscient or character:<id> of a cast member",
            {"field": field, "reason": "unknown_viewpoint"},
        )
    if actor_character_id is not None and actor_character_id in cast_ids:
        return character_viewpoint(actor_character_id)
    if story_default is not None:
        member = story_default.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
        if story_default.startswith(CHARACTER_VIEWPOINT_PREFIX) and member in cast_ids:
            return character_viewpoint(member)
    raise ViewpointUnresolved(
        "no output viewpoint resolves: name one, give an actor, or set the story's default",
        {"field": field, "reason": "unresolved"},
    )


__all__ = ["OMNISCIENT", "character_viewpoint", "resolve_viewpoint"]
