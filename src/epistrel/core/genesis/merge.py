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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from epistrel.core.catalog.checks.personality import (
    RULE,
    asserts_personality,
    personality_sentences,
)
from epistrel.core.catalog.report import Entry

PERSONALITY_IN_ROLE: Final = "personality_in_role"
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


__all__ = [
    "LITE_CONFLICTS",
    "PERSONALITY_IN_ROLE",
    "RULE",
    "asserts_personality",
    "name_warnings",
    "personality_sentences",
    "yielding",
]
