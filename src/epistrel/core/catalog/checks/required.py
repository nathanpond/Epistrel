"""The required-content check (FR-RATE-3, FR-SCN-2,9; #108, #111): what a published scenario must
hold. Every finding is an error. A scenario needs:

- ``field_required`` at ``public_card.name``: a name;
- ``content_rating_missing`` at ``metadata.content_rating_id``: a content rating (FR-RATE-3:
  "required to publish"); ``rating_retired`` when the rating it names is unknown or retired
  (it cannot be newly assigned, #48);
- ``role_required`` at ``cast.roles``: at least one role, since a story is created with a cast;
- ``world_required`` at ``world.description``: a world description (the setting a story starts
  in);
- ``opening_required`` at ``starting_state.opening.narration``: an opening narration, which
  genesis records as turn 1 (FR-SCN-9).

The last four are #108's hand-off to #111 (its plan's "content rules"). A companion version that
a character-alone publish copies (#116; FR-CAT-10: on the first publish, "its two slots and an
empty world") waives ``world_required`` and ``opening_required`` (``Subject.waived``); a
companion published with its character or alone needs both, like any scenario.

What a published catalog character must hold is the character content check's
(``checks.character_content``, #115), which shares ``rating_unusable``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.report import Outcome
from epistrel.core.tables import content_ratings

NAME_RULE = "a published entry is named"
RATING_RULE = "a published scenario carries a content rating from the deployment's set (FR-RATE-3)"
RETIRED_RULE = "a content rating that is unknown or retired cannot be newly assigned"
ROLE_RULE = "a published scenario has at least one role"
WORLD_RULE = "a published scenario describes its world"
OPENING_RULE = "a published scenario has an opening narration (a story's turn 1)"


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


async def rating_unusable(conn: AsyncConnection, rating_id: str) -> bool:
    """Whether ``rating_id`` cannot be newly assigned: no rating has it, or it is retired."""
    row = (
        await conn.execute(
            select(content_ratings.c.retired_at).where(content_ratings.c.id == rating_id)
        )
    ).one_or_none()
    return row is None or row.retired_at is not None


class RequiredCheck(Check):
    """See the module docstring."""

    name = "required"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        found = Findings()
        draft = draft_of(subject)
        if draft.public_card.name is None:
            found.entries.append(
                subject.entry(
                    "field_required", Outcome.rejected, field="public_card.name", rule=NAME_RULE
                )
            )
        rating = draft.metadata.content_rating_id
        if rating is None:
            found.entries.append(
                subject.entry(
                    "content_rating_missing",
                    Outcome.rejected,
                    field="metadata.content_rating_id",
                    rule=RATING_RULE,
                )
            )
        elif await rating_unusable(ctx.conn, rating):
            found.entries.append(
                subject.entry(
                    "rating_retired",
                    Outcome.rejected,
                    field="metadata.content_rating_id",
                    source=rating,
                    rule=RETIRED_RULE,
                )
            )
        if not draft.cast.roles:
            found.entries.append(
                subject.entry("role_required", Outcome.rejected, field="cast.roles", rule=ROLE_RULE)
            )
        if _blank(draft.world.description) and "world_required" not in subject.waived:
            found.entries.append(
                subject.entry(
                    "world_required", Outcome.rejected, field="world.description", rule=WORLD_RULE
                )
            )
        if (
            _blank(draft.starting_state.opening.narration)
            and "opening_required" not in subject.waived
        ):
            found.entries.append(
                subject.entry(
                    "opening_required",
                    Outcome.rejected,
                    field="starting_state.opening.narration",
                    rule=OPENING_RULE,
                )
            )
        return found


CHECK = RequiredCheck()

__all__ = ["CHECK", "RequiredCheck"]
