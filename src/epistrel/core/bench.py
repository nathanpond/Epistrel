"""Benched story characters: the reads (FR-MULTI-13; #831).

A story character can be **benched** and later **reactivated**
(``POST /stories/{id}/characters/{cid}/bench`` and ``…/reactivate``; ``core.stories``' operations).
While benched it keeps its partition and memories, takes no part in any turn (no render, no role,
no place in another character's cast; a turn request naming it is 409 ``character_benched``) and
does not count toward ``max_story_cast``. In ``sequestered`` mode it also perceives nothing:
``core.epistemic.scene.load_scene`` leaves it out of the scene (``SceneState.absent``), so routing
never delivers it a beat, and no exposure, belief or perceived memory is written for it.
Deliberate writes still reach it: a purpose call from its viewpoint (and ``store_as_memory``) and
the operator's belief, fact and memory writes.

**The state is positional.** A bench and a reactivation are inter-turn slot operations recorded
by ``character_benched`` / ``character_reactivated`` events; each writes one version row of
``character_bench`` (``core.tables.bench``), a registered derived table. A character's state at a
position is its latest active version there:

- ``bench_states(conn, story_id)``: every character benched at the story's head (after the
  last completed turn and its slots);
- ``bench_states(conn, story_id, before_turn=N)``: as turn ``N`` starts (versions positioned at
  turn ``N - 1``'s slots or earlier), which is what turn ``N`` (and a regenerate of it) reads;
- ``absent_from_scene(conn, story_id, turn)``: the characters a scene at the start of ``turn``
  leaves out (the ``sequestered`` ones; #832's ``present`` ones stay in it).

So a truncation before the bench un-benches the character, an untruncation re-applies it, a swap
to another variant of the bench's turn drops it, and a fork after it keeps it.

**Modes.** ``MODES`` is every mode the table admits, ``SUPPORTED_MODES`` the ones a request may
choose today; #832 adds ``present`` to it and a mode switch (another ``benched`` version in the
new mode). ``ABSENT_MODES`` names the modes that take a character out of the scene.

Nothing here logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.membership import active_only
from epistrel.core.tables.bench import BENCH_MODES, character_bench

SEQUESTERED = "sequestered"
"""The benched character perceives nothing (#831)."""
PRESENT = "present"
"""The benched character stays in the scene and hears, but never speaks (#832)."""
MODES: tuple[str, ...] = BENCH_MODES
"""Every mode ``character_bench`` admits."""
SUPPORTED_MODES: frozenset[str] = frozenset({SEQUESTERED})
"""The modes a bench request may choose (#832 adds ``present``)."""
ABSENT_MODES: frozenset[str] = frozenset({SEQUESTERED})
"""The modes that leave a benched character out of the scene: it perceives nothing."""


@dataclass(frozen=True, slots=True)
class Bench:
    """A benched character: its mode, the turn its bench follows (``since_turn``: the head turn
    when it was benched, 0 before any turn) and the participation role a reactivation restores.
    """

    character_id: str
    mode: str
    since_turn: int
    previous_role: str | None = None
    previous_fixed: bool | None = None

    def document(self) -> dict[str, Any]:
        """``{mode, since_turn}``: the ``bench`` of a character read."""
        return {"mode": self.mode, "since_turn": self.since_turn}


async def bench_states(
    conn: AsyncConnection,
    story_id: str,
    *,
    before_turn: int | None = None,
    character_ids: list[str] | None = None,
) -> dict[str, Bench]:
    """The story's benched characters by id: each character's latest active version (positioned
    before turn ``before_turn`` when given, else at the head), kept when it is a bench."""
    t = character_bench
    query = select(
        t.c.character_id,
        t.c.mode,
        t.c.benched,
        t.c.previous_role,
        t.c.previous_fixed,
        t.c.narrative_turn,
    ).where(t.c.story_id == story_id, active_only(t))
    if before_turn is not None:
        query = query.where(t.c.narrative_turn < before_turn)
    if character_ids is not None:
        query = query.where(t.c.character_id.in_(character_ids))
    query = query.order_by(t.c.narrative_turn, t.c.slot_k, t.c.beat, t.c.step, t.c.id)
    latest: dict[str, Any] = {}
    for row in await conn.execute(query):
        latest[str(row.character_id)] = row
    return {
        character_id: Bench(
            character_id=character_id,
            mode=str(row.mode),
            since_turn=int(row.narrative_turn),
            previous_role=row.previous_role,
            previous_fixed=row.previous_fixed,
        )
        for character_id, row in latest.items()
        if row.benched
    }


async def bench_of(conn: AsyncConnection, story_id: str, character_id: str) -> Bench | None:
    """``character_id``'s bench at the story's head (``None``: not benched)."""
    found = await bench_states(conn, story_id, character_ids=[character_id])
    return found.get(character_id)


async def absent_from_scene(conn: AsyncConnection, story_id: str, turn: int) -> frozenset[str]:
    """The characters a scene at the start of ``turn`` leaves out: those benched then in a mode
    of ``ABSENT_MODES``."""
    found = await bench_states(conn, story_id, before_turn=turn)
    return frozenset(c for c, b in found.items() if b.mode in ABSENT_MODES)


def benched_named(benched: Mapping[str, Any], named: list[str]) -> str | None:
    """The first of ``named`` that ``benched`` holds (``None``: none)."""
    return next((c for c in named if c in benched), None)


__all__ = [
    "ABSENT_MODES",
    "MODES",
    "PRESENT",
    "SEQUESTERED",
    "SUPPORTED_MODES",
    "Bench",
    "absent_from_scene",
    "bench_of",
    "bench_states",
    "benched_named",
]
