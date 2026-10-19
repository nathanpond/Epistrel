"""The could-have-known soft flag's hook (FR-COMMIT-11, FR-EPI-4; #154 stubs it, M5 fills it).

A deferred resolution into an existing modeled character who would have known (the knock was
Bobby's, and Bobby was in the scene when it was discussed) is flagged, never rejected, with a
severity that grows with the story-time gap. Flagging needs per-character partitions (M5), so in
M4 ``could_have_known`` answers ``None`` for every binding: no flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from epistrel.core.propositions.model import Proposition


@dataclass(frozen=True, slots=True)
class Flag:
    """A could-have-known soft flag: the entity, and a severity in ``[0, 1]``."""

    entity_id: str
    severity: float


def could_have_known(entity_id: str, proposition: Proposition, at: int | None) -> Flag | None:
    """Whether ``entity_id`` could have known ``proposition`` before story-minute ``at`` (M5);
    ``None`` (no flag) in M4."""
    del entity_id, proposition, at
    return None


__all__ = ["Flag", "could_have_known"]
