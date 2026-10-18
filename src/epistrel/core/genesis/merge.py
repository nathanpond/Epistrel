"""The merge rule under Lite (FR-SCN-7, FR-CAT-2; #122): facts from the scenario, personality
from the character.

**Personality.** A role defines situation, not personality: whoever fills it brings their own.
Role text that asserts personality (#111's heuristic, ``catalog.checks.personality``, which owns
the term list) is kept as guidance that yields. In the prompt, a role whose seeds assert
personality gets one ``yielding`` sentence after its seeds; in the creation report every such
sentence (in the role's setup description or its seeds, the player's role included) is one
``personality_in_role`` warning (outcome ``ignored``, rule ``personality: character over
scenario``) whose ``source`` is the name of the story character whose personality applies
(``name_warnings``: #111 can name only a ``default`` role's character; at creation every filled
role has one).

**Facts.** Lite has no fact-conflict detection: the scenario's facts are rendered first, before
the characters' author notes, and the scenario text closes with the instruction that they
prevail (``render.PREVAIL``). Nothing is dropped, so ``counts.conflicts`` is always 0 under Lite
and the fact-merge check (``fact_consistency``) is listed ``unavailable``.

**Facts under ``structured``** (#149, U-27). A structured story's seeds are propositions, so the
merge is decided by the contradiction rules: ``merge_structured`` runs #140's ``merge_layers``
over every normalized proposition of the creation (the author's closed sets first, then the
world's seeds, relationships and opening placement, the roles' seeds, and each character's
definition prose) and answers what it kept and, for each dropped one, its ``Conflict``: the
winning and losing seeds (a ``SeedFact`` id names its seed, ``core.verify.seeds.seed_of``) and
the rule that decided it. ``core.genesis.structured`` writes only what was kept and reports
each loser ``merge_conflict``; ``counts.conflicts`` counts them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from epistrel.core.catalog.checks.personality import (
    RULE,
    asserts_personality,
    personality_sentences,
)
from epistrel.core.catalog.report import Entry
from epistrel.core.propositions.registry import Registry
from epistrel.core.verify.merge import (
    SAME_LAYER_ORDER,
    MergeConflict,
    MergeResult,
    SeedFact,
    SeedLayer,
    merge_layers,
)
from epistrel.core.verify.seeds import seed_of

PERSONALITY_IN_ROLE: Final = "personality_in_role"
CLOSED_SET: Final = "closed_set"
LITE_CONFLICTS: Final = 0
"""Merge conflicts a Lite creation reports: none (no fact-conflict detection, FR-SCN-7)."""


def yielding(name: str) -> str:
    """The sentence a role's personality-asserting seeds are followed by in the prompt."""
    return (
        f"Where a scenario fact describes {name}'s personality, it is guidance only: "
        f"{name}'s own author notes decide how {name} behaves."
    )


def name_warnings(entries: Iterable[Entry], names: Mapping[str, str]) -> list[Entry]:
    """``entries`` with each ``personality_in_role`` warning's ``source`` set to the name of the
    character filling its role (``names``: role id → story name); others unchanged."""
    found: list[Entry] = []
    for entry in entries:
        role = entry.location.role
        if entry.code == PERSONALITY_IN_ROLE and role is not None and role in names:
            entry = entry.model_copy(update={"source": names[role]})
        found.append(entry)
    return found


@dataclass(frozen=True, slots=True)
class Conflict:
    """A proposition the structured merge dropped: its ``SeedFact`` id (``key``), the seeds that
    won and lost, and the deciding rule."""

    key: str
    winner: str
    loser: str
    rule: str


def merge_structured(
    layers: Mapping[SeedLayer, Sequence[SeedFact]], *, registry: Registry
) -> tuple[MergeResult, list[Conflict]]:
    """The structured merge (see the module docstring): #140's result and its conflicts."""
    result = merge_layers(layers, registry=registry)
    conflicts = [
        Conflict(
            key=c.loser.seed_id,
            winner=seed_of(c.winner.seed_id),
            loser=seed_of(c.loser.seed_id),
            rule=_rule(c),
        )
        for c in result.dropped
    ]
    return result, conflicts


def _rule(conflict: MergeConflict) -> str:
    """The deciding rule. A closed set is a story rule above every seed, merged first in the
    world layer, so a seed it beats lost to the rule, not to document order."""
    if conflict.winner.closed_set is None or conflict.rule != SAME_LAYER_ORDER:
        return conflict.rule
    rules = [] if conflict.verdict is None else sorted(r.value for r in conflict.verdict.rules)
    return rules[0] if rules else CLOSED_SET


__all__ = [
    "LITE_CONFLICTS",
    "PERSONALITY_IN_ROLE",
    "RULE",
    "Conflict",
    "asserts_personality",
    "merge_structured",
    "name_warnings",
    "personality_sentences",
    "yielding",
]
