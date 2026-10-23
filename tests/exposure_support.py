"""Helpers for the exposure tests (#164): an exposure written by a turn of its own (a ``say`` beat
event as its source, the witness exposed at the beat's step 1), its lazy resolution inside a later
turn, and the exposures and beliefs read back.

``partitions`` and ``lazy_resolution`` are #181's to deliver, so the helpers patch the delivered
set for their own operations (``tests.testimony_support.delivered``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon import facts as canon
from epistrel.core.enums import OperationKind, Provenance, RecordProvenance
from epistrel.core.epistemic import exposure
from epistrel.core.events import Operation
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Proposition
from epistrel.core.propositions.registry import load
from tests import canon_support as c
from tests import partition_support as parts
from tests import testimony_support as ts
from tests.event_support import rows

SAY_BEAT = 2


async def expose(
    engine: AsyncEngine,
    story_id: str,
    witness: str,
    proposition: Proposition,
    *,
    source: str,
    beat: int = SAY_BEAT,
    intent: str = "sincere",
    then: list[Proposition] | None = None,
) -> tuple[str, str]:
    """A turn in which ``source`` says ``proposition`` at ``beat`` and ``witness`` is exposed to it
    (unresolved), and canon commits each of ``then`` at a later beat of the same turn (the
    S-INTRA-TURN-FRAME shape). Answers ``(exposure id, the turn's operation id)``."""
    found: dict[str, Any] = {}
    with ts.delivered():
        async with Operation(
            engine, story_id=story_id, kind=OperationKind.TURN, principal=c.PRINCIPAL
        ) as op:
            op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER, beat=1)
            op.append(
                "beat_say",
                {"text": "It was said.", "intent": intent},
                character_id=source,
                beat=beat,
            )
            op.append("message_rendered", {"text": "It was said."})

            async def step(conn: AsyncConnection) -> None:
                event_id = await conn.scalar(
                    text("SELECT id FROM events WHERE operation_id = :o AND type = 'beat_say'"),
                    {"o": op.operation_id},
                )
                registry = await load(conn, story_id)
                stored = await store.write_proposition(conn, story_id, proposition, registry)
                (found["id"],) = await exposure.record_exposures(
                    conn,
                    op,
                    [
                        exposure.Exposed(
                            witness=witness,
                            proposition_id=stored,
                            proposition=proposition,
                            source=source,
                            source_event_id=str(event_id),
                            position=exposure.exposure_position(
                                op.narrative_turn or 0, op.turn_variant or 0, beat
                            ),
                        )
                    ],
                )
                for later, revealed in enumerate(then or (), start=beat + 1):
                    await canon.commit_fact(
                        conn,
                        story_id,
                        revealed,
                        provenance=RecordProvenance.DIRECTOR,
                        operation=op,
                        beat=later,
                    )

            op.add_commit_step(step)
            await op.commit()
            found["op"] = op.operation_id
    return str(found["id"]), str(found["op"])


async def resolve(
    engine: AsyncEngine, story_id: str, exposure_id: str
) -> tuple[exposure.Resolution, str]:
    """A later turn that needs the witness's belief and resolves ``exposure_id`` in its commit.
    Answers ``(the resolution, the resolving operation id)``."""
    found: dict[str, Any] = {}
    with ts.delivered():
        settings = await parts.partitioned(engine, story_id)
        async with Operation(
            engine, story_id=story_id, kind=OperationKind.TURN, principal=c.PRINCIPAL
        ) as op:
            op.append("player_input", {"text": "what do you think?"}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": "Let me think."})

            async def step(conn: AsyncConnection) -> None:
                (item,) = await exposure.exposures_of(conn, story_id, ids=[exposure_id])
                found["value"] = await exposure.resolve_exposure(conn, item, op, settings=settings)

            op.add_commit_step(step)
            await op.commit()
            found["op"] = op.operation_id
    result = found["value"]
    assert isinstance(result, exposure.Resolution)
    return result, str(found["op"])


async def plain_turn(engine: AsyncEngine, story_id: str) -> str:
    """An ordinary turn that touches no belief (to move the timeline on)."""
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=c.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "wait"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "Time passes."})
        await op.commit()
        return op.operation_id


async def exposure_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(engine, "SELECT * FROM exposures WHERE story_id = :s ORDER BY id", s=story_id)


async def state_of(engine: AsyncEngine, story_id: str, exposure_id: str) -> str:
    """The exposure's effective state (through its resolving operation's membership)."""
    async with engine.connect() as conn:
        (item,) = await exposure.exposures_of(conn, story_id, ids=[exposure_id])
    return item.state.value


__all__ = ["SAY_BEAT", "expose", "exposure_rows", "plain_turn", "resolve", "state_of"]
