"""The fact-consistency check (FR-SCN-5,7, FR-PROP-10; #111 registered it, #149 implements it):
the scenario's seeds are mutually consistent under the contradiction rules, or the conflicts
are reported.

It needs the ``structured`` pipeline (``unavailable`` without it). The canon seeds' propositions
(read by ``seed_world``, as genesis reads them) go through #140's ``merge_layers`` by layer: the
world's seeds, then the roles', then the definition prose of each character a ``locked`` or
``default`` role fills. A conflict genesis would decide by document order (two seeds of one
layer) is the error ``seed_contradiction`` at the losing seed; a conflict a higher layer wins is
the warning ``seed_contradiction`` (outcome ``dropped``: genesis drops the loser and reports it
``merge_conflict``). Each names the winning seed in ``source`` and the deciding rule in ``rule``.
"""

from __future__ import annotations

from typing import Final

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.seed_world import World, is_canon, seed_field, world_of
from epistrel.core.catalog.report import Outcome
from epistrel.core.catalog.scenario_models import WORLD_HOLDER
from epistrel.core.verify.merge import SAME_LAYER_ORDER, SeedFact, SeedLayer, merge_layers
from epistrel.core.verify.seeds import seed_facts, seed_of

CONTRADICTION: Final = "seed_contradiction"


def _layers(
    world: World,
) -> tuple[dict[SeedLayer, list[SeedFact]], dict[str, tuple[str, str | None, str | None]]]:
    """The merge's input and, by seed key, ``(field, role, seed id)`` for the report."""
    layers: dict[SeedLayer, list[SeedFact]] = {layer: [] for layer in SeedLayer}
    where: dict[str, tuple[str, str | None, str | None]] = {}
    roles = world.draft.cast.roles
    for loc, seed, reading in world.readings:
        if not is_canon(seed) or not reading.propositions or loc[0] == "drivers":
            continue
        key = seed.id or seed_field(loc)
        in_role = loc[:2] == ("cast", "roles") and isinstance(loc[2], int)
        role = roles[loc[2]].id if in_role and isinstance(loc[2], int) else None
        layer = (
            SeedLayer.ROLE
            if in_role or seed.holder not in (None, WORLD_HOLDER)
            else SeedLayer.WORLD
        )
        layers[layer].extend(seed_facts(key, layer, reading.propositions))
        where[key] = (f"{seed_field(loc)}.text", role, seed.id)
    for index, name, reading in world.backgrounds:
        if not reading.propositions:
            continue
        role = roles[index].id
        key = f"{role}:{name}"
        layers[SeedLayer.CHARACTER].extend(
            seed_facts(key, SeedLayer.CHARACTER, reading.propositions)
        )
        where[key] = (f"cast.roles[{index}].character", role, None)
    return layers, where


class FactConsistencyCheck(Check):
    """See the module docstring."""

    name = "fact_consistency"
    capability = "structured"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        world = await world_of(subject, ctx)
        layers, where = _layers(world)
        found = Findings()
        merged = merge_layers(layers, registry=world.seed_world.registry)
        reported: set[str] = set()
        for conflict in merged.dropped:
            loser = seed_of(conflict.loser.seed_id)
            if loser in reported:
                continue
            reported.add(loser)
            field, role, seed_id = where[loser]
            same = conflict.rule == SAME_LAYER_ORDER
            found.entries.append(
                subject.entry(
                    CONTRADICTION,
                    Outcome.rejected if same else Outcome.dropped,
                    field=field,
                    role=role,
                    seed_id=seed_id,
                    source=seed_of(conflict.winner.seed_id),
                    rule=conflict.rule,
                )
            )
        return found


CHECK = FactConsistencyCheck()

__all__ = ["CHECK", "CONTRADICTION", "FactConsistencyCheck"]
