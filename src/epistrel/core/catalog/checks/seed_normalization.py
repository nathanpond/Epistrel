"""The seed-normalization check (FR-SCN-5, FR-PROP-12; #111 registered it, #149 implements it):
every seed normalizes to registered predicates with resolved entities, and the story rules
declare predicates structured genesis can register.

It needs the ``structured`` pipeline: a deployment without it lists the check ``unavailable``
(``check_unavailable``), never passes a seed silently. It reads the scenario against the
ephemeral world of ``seed_world`` (the registry of the core and declared predicates, the world
and role entities):

- a story-rule declaration genesis would refuse (a name a core predicate has, a predicate
  declared twice, a reference to an undeclared non-core predicate, a rule cycle, a field the
  typed declaration does not have) is ``seed_unnormalizable`` at its field (#131's rule, refused
  at publish since #149);
- a ``canon`` seed (the default kind) naming an entity nothing resolves ("Lin", an alias the
  character's new version dropped) is ``seed_unnormalizable`` at the seed's text, with its
  ``seed_id`` and the resolver's reason in ``rule``; when a non-player role waits for its
  character until creation, the name may be that character's, so it is the warning
  ``seed_unresolved_cast`` (outcome ``kept``) instead, and genesis checks it against the real
  cast;
- an unregistered relation in free text is tolerated (the seed becomes a memory, not canon),
  and the other kinds (``belief``, ``nested``, ``scheduled``) are read for their shape only.

The companion's validation inside a character's publish (``companion.copy_companion``) runs it
too, so a character version that drops an alias a companion seed names fails the pair (I-20).
"""

from __future__ import annotations

from typing import Final

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.seed_world import (
    UNNORMALIZABLE,
    is_canon,
    seed_field,
    world_of,
)
from epistrel.core.catalog.report import Outcome

UNRESOLVED_CAST: Final = "seed_unresolved_cast"
RULE_SEED: Final = (
    "a canon seed normalizes to registered predicates with resolved entities (FR-PROP-12)"
)
RULE_CAST: Final = (
    "a name no role, character or world entity has may be the name of a character chosen at "
    "creation; the story's creation checks it against its cast (FR-PROP-12)"
)
RULE_DECLARATION: Final = (
    "a story rule declares a predicate structured genesis can register (FR-PROP-4)"
)


class SeedNormalizationCheck(Check):
    """See the module docstring."""

    name = "seed_normalization"
    capability = "structured"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        world = await world_of(subject, ctx)
        found = Findings()
        for problem in world.rule_problems:
            found.entries.append(
                subject.entry(
                    UNNORMALIZABLE,
                    Outcome.rejected,
                    field=problem.field,
                    source=problem.reason,
                    rule=RULE_DECLARATION,
                )
            )
        roles = world.draft.cast.roles
        for loc, seed, reading in world.readings:
            if not reading.unresolved or not is_canon(seed):
                continue
            role = (
                roles[loc[2]].id
                if loc[:2] == ("cast", "roles") and isinstance(loc[2], int)
                else None
            )
            cast = world.open_npc
            found.entries.append(
                subject.entry(
                    UNRESOLVED_CAST if cast else UNNORMALIZABLE,
                    Outcome.kept if cast else Outcome.rejected,
                    field=f"{seed_field(loc)}.text",
                    role=role,
                    seed_id=seed.id,
                    source=reading.unresolved[0],
                    rule=RULE_CAST if cast else RULE_SEED,
                )
            )
        return found


CHECK = SeedNormalizationCheck()

__all__ = ["CHECK", "UNRESOLVED_CAST", "SeedNormalizationCheck"]
