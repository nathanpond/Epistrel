"""Output viewpoints (FR-RESP-12, FR-AUTH-2, NFR-SEC-5's parity half; #59).

A viewpoint is the string ``character:<id>`` (a member of the story's cast) or ``omniscient``.
Every respond, history read and returned beat list is produced for exactly one, resolved here
for both surfaces:

1. **Named.** REST's body ``output_viewpoint`` or ``?viewpoint=``, the library's
   ``output_viewpoint=``/``viewpoint=`` keyword: each fills ``RequestContext.viewpoint``
   (``with_requested``). Two different named values are 422 ``validation_error``. A named
   viewpoint that is malformed or names no cast member is 422 ``viewpoint_unresolved``.
2. Otherwise the request's **actor** character.
3. Otherwise the story's ``default_output_viewpoint``, which may only be a character
   (``validate_default_viewpoint``; ``StorySettings`` refuses ``omniscient``).
4. Otherwise the request is refused, 422 ``viewpoint_unresolved`` (fail closed).

``omniscient`` is used only when named. A character viewpoint never receives ``think`` or
``latent`` content (``visible_to``); with perception ``none`` (every Lite story) every other
beat is returned in full and the result says ``audience_filter: "none"``. Private reads (audit
records) need ``omniscient`` by name (``require_omniscient``, 403 ``omniscient_required``).

**Stored results keep their viewpoint.** A turn records its resolved viewpoint on
``operations.viewpoint``; reusing its ``Idempotency-Key`` for a request that resolves to
another viewpoint is 409 ``idempotency_conflict`` (``check_key_viewpoint``). #61's store folds
the resolved viewpoint into its request hash and replaces the interim check.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.context import RequestContext
from epistrel.core.errors import (
    IdempotencyConflict,
    InvalidRequest,
    OmniscientRequired,
    ViewpointUnresolved,
)
from epistrel.core.settings_schema import CHARACTER_VIEWPOINT_PREFIX, OMNISCIENT
from epistrel.core.tables import operations

PRIVATE_CHANNELS = frozenset({"think", "latent"})
"""Channels no character viewpoint ever receives, under every profile."""

AudienceFilter = Literal["none", "co_location", "full"]
AUDIENCE_FILTER: AudienceFilter = "none"
"""Perception is ``none`` for every M2 story, so no audience filtering applies (FR-RESP-13 is
M5's); results state it."""


def character_viewpoint(character_id: str) -> str:
    return f"{CHARACTER_VIEWPOINT_PREFIX}{character_id}"


def with_requested(
    ctx: RequestContext, *named: str | None, field: str = "body.output_viewpoint"
) -> RequestContext:
    """``ctx`` with every named viewpoint folded into ``ctx.viewpoint``, the single carrier.

    ``named`` are the call's other ways of naming one (a keyword argument, a body field);
    ``None`` means "not named". Two different values are 422 ``validation_error``."""
    values = [v.strip() for v in (*named, ctx.viewpoint) if v is not None]
    if len(set(values)) > 1:
        raise InvalidRequest(
            "the request names two different output viewpoints",
            {"field": field, "reason": "conflicting_viewpoints"},
        )
    viewpoint = values[0] if values else None
    return ctx if viewpoint == ctx.viewpoint else dataclasses.replace(ctx, viewpoint=viewpoint)


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


def try_resolve(resolve: Callable[[], str]) -> str | None:
    """``resolve()`` (a bound ``resolve_viewpoint``), or ``None`` where it would refuse."""
    try:
        return resolve()
    except ViewpointUnresolved:
        return None


def validate_default_viewpoint(
    value: str | None,
    cast_ids: Collection[str],
    *,
    field: str = "body.settings.default_output_viewpoint",
) -> str | None:
    """A story default as stored (``character:<id>`` or ``None``): ``omniscient`` is 422
    ``validation_error`` (never a default, FR-RESP-12); anything but a cast member is 422
    ``viewpoint_unresolved``. Every settings write calls it (#63's ``PATCH``)."""
    if value is None:
        return None
    named = value.strip()
    if named.casefold() == OMNISCIENT:
        raise InvalidRequest(
            "a story's default output viewpoint must be a character, never omniscient",
            {"field": field, "reason": "omniscient"},
        )
    member = named.removeprefix(CHARACTER_VIEWPOINT_PREFIX)
    if not named.startswith(CHARACTER_VIEWPOINT_PREFIX) or member not in cast_ids:
        raise ViewpointUnresolved(
            "the default output viewpoint names no character of the cast",
            {"field": field, "reason": "unknown_character"},
        )
    return character_viewpoint(member)


def is_omniscient(viewpoint: str) -> bool:
    return viewpoint == OMNISCIENT


def visible_to(channel: str, viewpoint: str) -> bool:
    """Whether a beat on ``channel`` is returned to ``viewpoint`` (perception ``none``)."""
    return is_omniscient(viewpoint) or channel not in PRIVATE_CHANNELS


def require_omniscient(viewpoint: str, *, field: str) -> None:
    """A private read names ``omniscient`` or is 403 ``omniscient_required``."""
    if not is_omniscient(viewpoint):
        raise OmniscientRequired(
            "this read returns private records and needs the omniscient viewpoint by name",
            {"field": field, "output_viewpoint": viewpoint},
        )


async def check_key_viewpoint(
    db: AsyncEngine, story_id: str, idempotency_key: str | None, viewpoint: str | None
) -> None:
    """409 ``idempotency_conflict`` when ``idempotency_key`` already names an operation of the
    story made for a viewpoint other than ``viewpoint`` (``None``: the request resolves to no
    viewpoint, so it cannot be the same request). No stored result is returned.

    Interim (#59): #61's ``idempotency_keys`` store hashes the resolved viewpoint with the rest
    of the request and replaces this lookup; until then a same-viewpoint reuse is not replayed.
    """
    if idempotency_key is None:
        return
    async with db.connect() as conn:
        stored = (
            await conn.execute(
                select(operations.c.operation_id, operations.c.viewpoint)
                .where(
                    operations.c.story_id == story_id,
                    operations.c.idempotency_key == idempotency_key,
                )
                .order_by(operations.c.started_at)
                .limit(1)
            )
        ).one_or_none()
    if stored is not None and (viewpoint is None or stored.viewpoint != viewpoint):
        raise IdempotencyConflict(
            "this Idempotency-Key was used for a request with another output viewpoint",
            {"reason": "viewpoint"},
        )


__all__ = [
    "AUDIENCE_FILTER",
    "OMNISCIENT",
    "PRIVATE_CHANNELS",
    "AudienceFilter",
    "character_viewpoint",
    "check_key_viewpoint",
    "is_omniscient",
    "require_omniscient",
    "resolve_viewpoint",
    "try_resolve",
    "validate_default_viewpoint",
    "visible_to",
    "with_requested",
]
