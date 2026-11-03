"""Helpers for #221's amendment tests: a long story played by direct commits, and amendments
written through ``core.amendments.store`` inside a test ``rewrite_session`` operation (the
side-channel that produces them arrives in #223/#224)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.amendments import store
from epistrel.core.enums import OperationKind, Provenance
from epistrel.core.events import Operation
from tests.event_support import rows

AUTHOR = "p-author"


async def play(engine: AsyncEngine, story_id: str, turns: int, *, start: int = 1) -> dict[int, str]:
    """``turns`` turns committed in one transaction each (input and reply), the latest index
    ``start + turns - 1``; answers each turn's reply event id by turn."""
    replies: dict[int, str] = {}
    for n in range(start, start + turns):
        async with Operation(
            engine, story_id=story_id, kind=OperationKind.TURN, principal="p-test"
        ) as op:
            op.append("player_input", {"text": f"line {n}"}, provenance=Provenance.PLAYER)
            op.append("message_rendered", {"text": f"reply {n}"})
            await op.commit()
        found = await rows(
            engine,
            "SELECT id FROM events WHERE operation_id = :o AND type = 'message_rendered'",
            o=op.operation_id,
        )
        replies[int(op.narrative_turn or 0)] = str(found[0].id)
    return replies


async def amend(
    engine: AsyncEngine,
    story_id: str,
    parts: Sequence[store.PartDraft],
    *,
    kind: str = "rewrite",
    revert: str | None = None,
    rationale: Sequence[Mapping[str, str]] = (),
    relies_on: Sequence[str] = (),
) -> tuple[Operation, store.RecordedAmendment, list[str]]:
    """One ``rewrite_session`` operation recording an amendment of ``parts`` (and, with
    ``revert``, reverting that amendment into it); ``rationale`` its rationale records and
    ``relies_on`` the facts it relies on (#239)."""
    found: dict[str, Any] = {}
    async with Operation(
        engine,
        story_id=story_id,
        kind=OperationKind.REWRITE_SESSION,
        principal=AUTHOR,
        bumps_revision=True,
    ) as op:

        async def step(conn: AsyncConnection) -> None:
            made = await store.record_amendment(
                conn,
                op,
                kind=kind,
                parts=parts,
                rationale_record_ids=rationale,
                relies_on=relies_on,
            )
            found["made"] = made
            found["inverse"] = (
                []
                if revert is None
                else await store.revert_amendment(
                    conn, op, revert, by_amendment_id=made.amendment_id
                )
            )

        op.add_commit_step(step)
        await op.commit()
    return op, found["made"], found["inverse"]


async def table_rows(engine: AsyncEngine, table: str, story_id: str) -> list[dict[str, Any]]:
    """Every row of ``table`` of the story, as dicts ordered by id (a byte-level diff)."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(f"SELECT * FROM {table} WHERE story_id = :s ORDER BY id"),  # noqa: S608
            {"s": story_id},
        )
        return [dict(r._mapping) for r in found]
