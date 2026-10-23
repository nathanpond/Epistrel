"""Helpers for the belief-partition tests (#159): beliefs written the way #160's ``transition()``
will write them (``partitions._insert_belief`` in an operation's commit step), the ``partitions``
module switched on for a test, and the story's beliefs and projection rows.

``partitions`` is #181's to deliver (``core.profiles.schema.DELIVERED``), so until then a test
that needs it on patches the delivered set (``deliver_partitions``) and passes settings with
``character_knowledge.partitions`` on (``partitioned``) to ``retrieve``."""

from __future__ import annotations

import types
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel, OperationKind
from epistrel.core.epistemic import partitions
from epistrel.core.events import Operation
from epistrel.core.profiles import schema
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Proposition
from tests import canon_support as c
from tests.event_support import rows


def deliver_partitions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver the ``partitions`` module for the test (#181 delivers it for real)."""
    monkeypatch.setattr(
        schema,
        "DELIVERED",
        types.MappingProxyType({**schema.DELIVERED, "partitions": frozenset({schema.ON})}),
    )


async def partitioned(engine: AsyncEngine, story_id: str, on: bool = True) -> ProfileSettings:
    """The story's effective settings with ``character_knowledge.partitions`` set to ``on``."""
    async with engine.connect() as conn:
        found = (await effective_configuration(conn, story_id, validate=False)).settings
    document = found.model_dump(mode="json")
    document["character_knowledge"]["partitions"] = on
    return ProfileSettings.model_validate(document)


async def believe(
    engine: AsyncEngine,
    story_id: str,
    holder: str,
    proposition: Proposition,
    *,
    level: BeliefConfidenceLevel = BeliefConfidenceLevel.BELIEVED,
    kind: OperationKind = OperationKind.TURN,
    turn: int | None = None,
    **options: Any,
) -> partitions.Belief:
    """``holder`` comes to believe ``proposition`` in a new operation (a turn by default)."""

    async def write(conn: AsyncConnection, op: Operation) -> partitions.Belief:
        return await partitions._insert_belief(
            conn, story_id, holder, proposition, operation=op, level=level, **options
        )

    _, found = await c.run(engine, story_id, write, kind=kind, turn=turn)
    assert isinstance(found, partitions.Belief)
    return found


async def belief_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(engine, "SELECT * FROM beliefs WHERE story_id = :s ORDER BY id", s=story_id)


async def state_rows(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """``belief_state``'s rows of the story, comparable with a fold's."""
    found = await rows(
        engine,
        "SELECT * FROM belief_state WHERE story_id = :s "
        "ORDER BY holder_character_id, proposition_id",
        s=story_id,
    )
    return [_comparable(dict(r._mapping)) for r in found]


async def folded_rows(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The fold of the story's beliefs (nothing written), ordered like ``state_rows``."""
    async with engine.connect() as conn:
        found = await partitions.fold_state(conn, story_id)
    return sorted(
        (_comparable(r) for r in found),
        key=lambda r: (r["holder_character_id"], r["proposition_id"]),
    )


def _comparable(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: (str(v) if isinstance(v, str) else v) for k, v in row.items()}
    if out.get("current_scalar") is not None:
        out["current_scalar"] = float(out["current_scalar"])
    return out


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


__all__ = [
    "belief_rows",
    "believe",
    "deliver_partitions",
    "execute",
    "folded_rows",
    "partitioned",
    "state_rows",
]
