"""The companion check (FR-CAT-5, FR-RATE-5; #116): what a companion scenario must keep, and the
rating difference between it and its character. It runs only on a companion (a scenario whose
``companion_of`` names a catalog character), in every validation of one: its publish, its
character's publish (the version the character's publish makes or copies), and the dry run.

- ``companion_slots_fixed`` (an error, at the slot field): the cast is no longer exactly the two
  slots (``npc`` locked to the character, and ``player``; ``core.catalog.companion_slots``). A
  save refuses such a change, so only a draft written some other way can fail it.
- ``rating_looser`` (a warning, outcome ``kept``, at ``metadata.content_rating_id``): the
  companion's rating lacks a guardrail that the rating of the character version it is pinned to
  has, so the companion is looser than its character. ``source`` names the companion side
  (``scenario_id@version``, or ``@draft``). The other direction, a character looser than its
  companion, is the rating-mismatch check's ``rating_looser``, whose ``source`` names the
  character version (``catalog_id@version``). A rating difference warns either way and never
  blocks (FR-RATE-5: "a companion scenario rated differently from its character warns the same
  way"); two ratings with the same guardrails do not differ.
"""

from __future__ import annotations

from sqlalchemy import select

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import filled
from epistrel.core.catalog.checks.rating_mismatch import is_looser, ratings_of
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.companion_slots import NPC_ROLE, defaults, pin_of, slot_change
from epistrel.core.catalog.report import Outcome
from epistrel.core.tables.catalog import scenario_catalog

SLOTS_RULE = (
    "a companion scenario keeps its two slots: `npc` locked to its character and `player` "
    "(FR-CAT-5)"
)
RATING_RULE = (
    "rating: a companion whose rating's guardrails lack a policy its character's rating has is "
    "looser than its character; the difference warns (FR-RATE-5)"
)


class CompanionCheck(Check):
    """See the module docstring."""

    name = "companion"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        character_id = await ctx.conn.scalar(
            select(scenario_catalog.c.companion_of).where(scenario_catalog.c.id == subject.entry_id)
        )
        if character_id is None:
            return Findings()
        found = Findings()
        expected = defaults(
            str(character_id), name=None, rating_id=None, pin=pin_of(subject.document)
        )
        change = slot_change(expected, subject.document)
        if change is not None:
            role, field = change
            found.entries.append(
                subject.entry(
                    "companion_slots_fixed",
                    Outcome.rejected,
                    field=field,
                    role=role,
                    source=str(character_id),
                    rule=SLOTS_RULE,
                )
            )
            return found
        rating = draft_of(subject).metadata.content_rating_id
        members = [m for m in await filled(subject, ctx) if m.role.id == NPC_ROLE]
        if rating is None or not members or members[0].version is None:
            return found
        ratings = await ratings_of(ctx)
        member = members[0]
        held = await ratings.stored(ctx, member.rating_id, member.rating_version_id)
        if is_looser(ratings.effective(rating), held):
            found.entries.append(
                subject.entry(
                    "rating_looser",
                    Outcome.kept,
                    field="metadata.content_rating_id",
                    source=f"{subject.entry_id}@{subject.version}",
                    rule=RATING_RULE,
                )
            )
        return found


CHECK = CompanionCheck()

__all__ = ["CHECK", "CompanionCheck"]
