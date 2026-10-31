"""The could-have-known soft flag (FR-COMMIT-11, FR-EPI-4; #154 stubbed it, #165 fills it).

A deferred resolution into an existing modeled character who would plausibly have known is
flagged, never rejected, with a severity that grows with the story-time gap. Reconciliation
(``core.commit.reconcile``) asks ``could_have_known`` for a binding that has **no conflict**
(a conflicting one is rejected before any flag) when the caller names the resolving position:
``core.commit.lifecycle.depend``, in the commit of the turn that resolves the thunk.

The flag fires when all of these hold:

- the bound character C was modeled **before** the thunk was introduced (``characters.modeled_*``;
  the genesis cast at position 0). A character modeled later still gets its knowledge backdated
  (``reconcile.Knowledge``) but is no existing character the thunk could have been hiding;
- the resolution is **not in the thunk's own turn** (a same-turn resolution is no deferral);
- C was **co-located** with the thunk's event at its introduction, per #137's scene state
  (``core.epistemic.scene.load_scene_state`` at the start of that turn): C's placement is the
  event's place (the thunk's object when that is a place, else the scene's location), or lies
  inside it or contains it;
- ``core.epistemic.replay.could_have_known(C, binding, now)`` finds **no path**: nothing C
  acquired by now records it acting on, or knowing, what the binding says it did.

Severity bands, over the gap from the thunk's ``valid_time`` (fallback: the story-time of its
introduction) to the resolving turn's story-time: ``mild`` under one story-day, ``moderate`` under
30, ``strong`` beyond. Weight does not adjust it. The flag is a warning in
``turn_report.commit.could_have_known_flags`` and the turn's ``could_have_known_flag``
degradation, is listed on ``/inspect/ledger`` with its thunk, and steers the next render only
(``orchestrator.structured.prompt.KNOWLEDGE_STEERING``). Without per-character partitions nothing
is flagged (``replay`` answers ``NotApplicable``). Ids and counts only (invariant 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import Registry
from epistrel.core.readset import StructuredReadSet
from epistrel.core.tables import characters
from epistrel.observability.metrics import COULD_HAVE_KNOWN_FLAGS

if TYPE_CHECKING:
    from epistrel.core.commit.lifecycle import Thunk

Severity = Literal["mild", "moderate", "strong"]
STORY_DAY = 24 * 60
"""One story-day in story-minutes."""
SEVERITY_BANDS: tuple[tuple[int, Severity], ...] = (
    (STORY_DAY, "mild"),
    (30 * STORY_DAY, "moderate"),
)
"""The gap below each bound (story-minutes) and its severity; beyond the last: ``strong``."""
FLAG_DEGRADATION = "could_have_known_flag"


@dataclass(frozen=True, slots=True)
class Flag:
    """A could-have-known soft flag: the character, the thunk, the severity and the story-time
    gap (minutes) it was measured over."""

    entity_id: str
    severity: Severity
    thunk_id: str = ""
    gap_minutes: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "character_id": self.entity_id,
            "thunk_id": self.thunk_id,
            "severity": self.severity,
            "gap_minutes": self.gap_minutes,
        }


def severity_of(gap_minutes: int) -> Severity:
    """The severity of a flag over a story-time gap (see the module docstring)."""
    for below, severity in SEVERITY_BANDS:
        if gap_minutes < below:
            return severity
    return "strong"


async def _modeled_at(conn: AsyncConnection, story_id: str, character_id: str) -> Position | None:
    row = (
        await conn.execute(
            select(characters).where(
                characters.c.story_id == story_id, characters.c.id == character_id
            )
        )
    ).one_or_none()
    return None if row is None else Position.of_columns(dict(row._mapping), "modeled")


async def co_located(
    conn: AsyncConnection,
    story_id: str,
    thunk: Thunk,
    character_id: str,
    *,
    read_set: StructuredReadSet | None = None,
) -> bool:
    """Whether ``character_id`` was where the thunk's event happened when it was introduced
    (see the module docstring)."""
    from epistrel.core.epistemic.scene import load_scene_state

    if thunk.narrative_turn is None:
        return False
    scene = await load_scene_state(conn, story_id, thunk.narrative_turn, read_set=read_set)
    place = thunk.sketch.object_id if thunk.sketch.object_id in scene.places else scene.location
    where = scene.at(character_id)
    if place is None or where is None:
        return False
    return place in scene.ancestry(where) or where in scene.ancestry(place)


async def could_have_known(
    conn: AsyncConnection,
    story_id: str,
    thunk: Thunk,
    binding: Proposition,
    character_id: str,
    *,
    now: Position,
    settings: Any = None,
    registry: Registry | None = None,
    read_set: StructuredReadSet | None = None,
) -> Flag | None:
    """The soft flag on binding ``thunk`` to ``character_id`` at ``now`` (``binding`` reconciled
    with no conflict), or ``None``; see the module docstring."""
    from epistrel.core import world_clock
    from epistrel.core.commit.reconcile import introduced_at
    from epistrel.core.epistemic import replay

    introduced = introduced_at(thunk)
    if thunk.narrative_turn is None or now.turn == thunk.narrative_turn:
        return None
    modeled = await _modeled_at(conn, story_id, character_id)
    if modeled is None or not modeled.before(introduced):
        return None
    if not await co_located(conn, story_id, thunk, character_id, read_set=read_set):
        return None
    answer = await replay.could_have_known(
        conn,
        story_id,
        character_id,
        binding,
        now,
        settings=settings,
        registry=registry,
        read_set=read_set,
    )
    if not isinstance(answer, replay.NoPath):
        return None
    basis = await world_clock.time_basis(conn, story_id)
    since = thunk.valid_time
    if since is None:
        since = basis.at(introduced)
    gap = max(0, basis.at(now) - since)
    severity = severity_of(gap)
    COULD_HAVE_KNOWN_FLAGS.labels(severity=severity).inc()
    return Flag(character_id, severity, thunk.id, gap)


__all__ = [
    "FLAG_DEGRADATION",
    "SEVERITY_BANDS",
    "STORY_DAY",
    "Flag",
    "Severity",
    "co_located",
    "could_have_known",
    "severity_of",
]
