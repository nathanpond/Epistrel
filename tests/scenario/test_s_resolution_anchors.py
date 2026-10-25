"""S-RESOLUTION-ANCHORS: exposure vs resolution (docs/04 §7; FR-BELIEF-12,13,14; #164), and
S-UNDO-SWAP's M5 clause (FR-STORE-29).

One turn per message, written through ``Operation`` (``tests.exposure_support``), scaled by ten
from the spec: Ann is exposed to Bobby's claim at turn 10 (the spec's Marcus at turn 10); her
belief is resolved lazily inside turn 85 (850) by the turn that needs it; the story plays on to 90
(900).

- The resolution is committed with turn 85: its belief and transitions are that operation's
  derived records, ``resolution_operation_id`` names it, and the turn's single revision increment
  covers it. Author inspection before then (``/inspect/beliefs``' read) never persists one: the
  exposure stays unresolved and Ann holds nothing.
- Viewed through turn 90 Ann has believed it since turn 10 (``held_from``, the exposure position);
  through turn 80 she holds nothing and the exposure reads unresolved (the inclusion anchor).
- A fork at 90 copies the resolution; a fork at 80 copies the exposure unresolved and no belief.
- Truncating to 80 leaves it unresolved; a swap back to the chain that resolved it brings the
  resolution back; a swap to a continuation that never resolved it discards it; a later need there
  re-resolves it.

S-UNDO-SWAP (M5): a lazy belief from turn 10 resolved during 81'-85' reverts to unresolved in the
active chain after the undo's swap, while the original chain's own resolution of another exposure
returns.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline
from epistrel.core.epistemic import exposure, partitions
from epistrel.core.membership import Position
from epistrel.core.propositions.model import Proposition
from epistrel.core.variants import ChainRef, SwapRequest
from tests import canon_support as c
from tests import exposure_support as xs
from tests.fork_support import fork
from tests.memory_support import use_structured
from tests.profile_support import store_overrides
from tests.projection_support import truncate, untruncate

pytestmark = pytest.mark.integration

EXPOSED = 10
CHECKPOINT = 80
RESOLVED = 85
HEAD = 90
PRINCIPAL = "p-player"


async def _until(engine: AsyncEngine, story: str, turn: int) -> None:
    """Plain turns until the story's last turn is ``turn``."""
    async with engine.connect() as conn:
        head = int(
            await conn.scalar(
                text("SELECT next_turn_index FROM stories WHERE id = :s"), {"s": story}
            )
            or 1
        )
    for _ in range(head, turn + 1):
        await xs.plain_turn(engine, story)


async def _held(engine: AsyncEngine, story: str, holder: str, through: int | None) -> list[str]:
    """``holder``'s first-order beliefs in the corrected view through turn ``through`` (now when
    ``None``), as their held-from turns."""
    async with engine.connect() as conn:
        found = await partitions.partition(
            conn,
            story,
            holder,
            through=None if through is None else Position.end_of(through),
        )
    return [f"{b.held_from.turn}:{b.proposition.predicate_id}" for b in found]


async def _state(
    engine: AsyncEngine, story: str, exposure_id: str, through: int | None = None
) -> str:
    async with engine.connect() as conn:
        (item,) = await exposure.exposures_of(conn, story, ids=[exposure_id], as_of_turn=through)
    return item.state.value


async def _revision(engine: AsyncEngine, story: str) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                text("SELECT timeline_revision FROM stories WHERE id = :s"), {"s": story}
            )
            or 0
        )


PARTITIONED = {
    "character_knowledge": {"partitions": True, "lazy_resolution": True, "testimony": "defaulted"},
    "perception": {"level": "co_location"},
}
"""The story's overrides: partitions with lazy resolution (#164) and co-located perception."""


async def _world(engine: AsyncEngine) -> tuple[c.World, Proposition]:
    w = await c.world(engine)
    await use_structured(engine, w.story_id)
    # #178's gate reads the story's effective configuration: partitions on, with lazy resolution.
    await store_overrides(engine, w.story_id, PARTITIONED)
    return w, c.at(w.people["Cal"], w.places["garden"], None, None)


@pytest.mark.part("M5", "FR-BELIEF-12,13,14")
async def test_s_resolution_anchors(engine: AsyncEngine) -> None:
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    await _until(engine, story, EXPOSED - 1)
    exposure_id, _ = await xs.expose(engine, story, ann, claim, source=bobby)
    await _until(engine, story, RESOLVED - 1)

    # Author inspection never persists a resolution.
    seen = await partitions.inspect(engine, story, character=ann)
    assert seen.items == ()
    assert await _state(engine, story, exposure_id) == "unresolved"
    assert await _held(engine, story, ann, None) == []

    before = await _revision(engine, story)
    resolution, resolving = await xs.resolve(engine, story, exposure_id)
    assert await _revision(engine, story) == before + 1  # the turn's one increment covers it
    belief = resolution.belief
    assert belief is not None
    assert belief.operation_id == resolving
    assert belief.resolution_operation_id == resolving
    assert belief.narrative_turn == RESOLVED
    assert belief.held_from.turn == EXPOSED
    assert {t.transition.operation_id for t in resolution.transitions} == {resolving}
    assert await _state(engine, story, exposure_id) == "resolved"
    await _until(engine, story, HEAD)

    # The two anchors: in the record from 85, believed since 10.
    assert await _held(engine, story, ann, HEAD) == [f"{EXPOSED}:{claim.predicate_id}"]
    assert await _held(engine, story, ann, CHECKPOINT) == []
    assert await _state(engine, story, exposure_id, HEAD) == "resolved"
    assert await _state(engine, story, exposure_id, CHECKPOINT) == "unresolved"

    # Forks: at 90 the resolution is copied, at 80 it is not.
    late = await fork(engine, story, at_turn=HEAD)
    early = await fork(engine, story, at_turn=CHECKPOINT)
    async with engine.connect() as conn:
        (copied,) = await exposure.exposures_of(conn, late.story_id)
        (bare,) = await exposure.exposures_of(conn, early.story_id)
        late_ann = await conn.scalar(
            text("SELECT id FROM characters WHERE story_id = :s AND name = 'Ann'"),
            {"s": late.story_id},
        )
        early_ann = await conn.scalar(
            text("SELECT id FROM characters WHERE story_id = :s AND name = 'Ann'"),
            {"s": early.story_id},
        )
    assert copied.state.value == "resolved"
    assert copied.resolution_operation_id is not None
    assert copied.resolution_operation_id != resolving  # the fork's own copy of the turn
    assert await _held(engine, late.story_id, str(late_ann), None) == [
        f"{EXPOSED}:{claim.predicate_id}"
    ]
    assert bare.state.value == "unresolved"
    assert await _held(engine, early.story_id, str(early_ann), None) == []

    # Truncate to 80: unresolved again.
    await truncate(engine, story, CHECKPOINT)
    assert await _state(engine, story, exposure_id) == "unresolved"
    assert await _held(engine, story, ann, None) == []
    # A continuation that never resolves it: 81'-82'.
    await xs.plain_turn(engine, story)
    await xs.plain_turn(engine, story)
    # Undo: the swap back to 81-90 brings the original resolution back.
    undone = await untruncate(engine, story)
    assert undone is not None
    assert undone.swapped
    assert await _state(engine, story, exposure_id) == "resolved"
    assert await _held(engine, story, ann, None) == [f"{EXPOSED}:{claim.predicate_id}"]
    # Swap to the continuation that never resolved it: discarded.
    await timeline.swap(
        engine,
        story,
        SwapRequest(to_variant_chain=ChainRef(turn=CHECKPOINT + 1, variant=2)),
        principal=PRINCIPAL,
    )
    assert await _state(engine, story, exposure_id) == "unresolved"
    assert await _held(engine, story, ann, None) == []
    # A later need re-resolves it, in its own frame.
    again, by = await xs.resolve(engine, story, exposure_id)
    assert again.belief is not None
    assert again.belief.held_from.turn == EXPOSED
    assert again.belief.resolution_operation_id == by
    assert await _state(engine, story, exposure_id) == "resolved"


@pytest.mark.part("M5", "FR-STORE-29")
async def test_s_undo_swap__beliefs(engine: AsyncEngine) -> None:
    """A lazy belief from turn 10 resolved during 81'-85' reverts to unresolved after the undo's
    swap; the original chain's own resolution (of a second exposure, in turn 85) returns."""
    w, claim = await _world(engine)
    story = w.story_id
    ann, bobby = w.characters["Ann"], w.characters["Bobby"]
    other = c.at(w.people["Bobby"], w.places["school"], None, None)
    await _until(engine, story, EXPOSED - 1)
    lazy_id, _ = await xs.expose(engine, story, ann, claim, source=bobby)
    kept_id, _ = await xs.expose(engine, story, ann, other, source=bobby)
    await _until(engine, story, RESOLVED - 1)
    await xs.resolve(engine, story, kept_id)  # the original chain's own resolution, turn 85
    await _until(engine, story, HEAD)

    await truncate(engine, story, CHECKPOINT)
    assert await _state(engine, story, kept_id) == "unresolved"
    await _until(engine, story, CHECKPOINT + 2)
    primed, _ = await xs.resolve(engine, story, lazy_id)  # resolved during the primes
    assert primed.belief is not None
    assert await _state(engine, story, lazy_id) == "resolved"

    undone = await untruncate(engine, story)
    assert undone is not None
    assert undone.swapped
    assert await _state(engine, story, lazy_id) == "unresolved"
    assert await _state(engine, story, kept_id) == "resolved"
    held = await _held(engine, story, ann, None)
    assert held == [f"{EXPOSED + 1}:{other.predicate_id}"]
