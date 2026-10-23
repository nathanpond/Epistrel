"""Helpers for the belief-partition tests (#159, #160): beliefs written through #160's
``transition()`` in an operation's commit step (``believe``, ``transit``), the ``partitions``
module switched on for a test, and the story's beliefs and projection rows.

``partitions`` is #181's to deliver (``core.profiles.schema.DELIVERED``), so until then a test
that needs it on patches the delivered set (``deliver_partitions``) and passes settings with
``character_knowledge.partitions`` on (``partitioned``) to ``retrieve``."""

from __future__ import annotations

import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import BeliefConfidenceLevel, OperationKind, Provenance
from epistrel.core.epistemic import partitions, transitions
from epistrel.core.events import Operation
from epistrel.core.membership import Position
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


@contextmanager
def delivered() -> Iterator[None]:
    """``deliver_partitions`` for a block (a helper that writes beliefs outside a test's own
    ``monkeypatch``)."""
    before = schema.DELIVERED
    schema.DELIVERED = types.MappingProxyType({**before, "partitions": frozenset({schema.ON})})
    try:
        yield
    finally:
        schema.DELIVERED = before


async def believe(
    engine: AsyncEngine,
    story_id: str,
    holder: str,
    proposition: Proposition,
    *,
    level: BeliefConfidenceLevel | None = None,
    scalar: float | None = None,
    kind: OperationKind = OperationKind.TURN,
    turn: int | None = None,
    held_from: Position | None = None,
    cause: transitions.Kind = transitions.Kind.AUTHOR,
) -> partitions.Belief:
    """``holder`` comes to believe ``proposition`` in a new operation (a turn by default), the
    way #160's ``transition()`` writes it: a ``cause`` transition (``author`` by default) citing
    an author act the operation stages, positioned at the operation's turn (or ``held_from``).
    ``level`` defaults to ``believed`` (or the scalar's projection)."""
    if level is None and scalar is None:
        level = BeliefConfidenceLevel.BELIEVED
    result = await transit(
        engine,
        story_id,
        holder,
        proposition,
        confidence=transitions.Confidence(level, scalar),
        kind=kind,
        turn=turn,
        held_from=held_from,
        cause=cause,
    )
    return result.belief


async def transit(
    engine: AsyncEngine,
    story_id: str,
    holder: str,
    proposition: Proposition,
    *,
    confidence: transitions.Confidence | None = None,
    kind: OperationKind = OperationKind.TURN,
    turn: int | None = None,
    held_from: Position | None = None,
    cause: transitions.Kind = transitions.Kind.AUTHOR,
    antecedent: transitions.Antecedent
    | Callable[[Operation], transitions.Antecedent]
    | None = None,
    position: Position | Callable[[Operation], Position] | None = None,
    **options: Any,
) -> transitions.TransitionResult:
    """One transition in a new operation (see ``believe``): ``antecedent`` defaults to the
    operation's own author act, ``position`` to the operation's turn (or ``held_from``); either
    may be a function of the operation (a staged beat names its operation)."""
    with delivered():
        settings = await partitioned(engine, story_id)
        found: dict[str, Any] = {}
        async with Operation(
            engine,
            story_id=story_id,
            kind=kind,
            principal=c.PRINCIPAL,
            turn=turn,
            bumps_revision=True if kind is not OperationKind.TURN else None,
        ) as op:
            if kind is OperationKind.TURN:
                op.append("player_input", {"text": "go on"}, provenance=Provenance.PLAYER)
                op.append("message_rendered", {"text": "reply to go on"})
            op.append("belief_authored", {"holder_character_id": holder})

            async def step(conn: AsyncConnection) -> None:
                act = await conn.scalar(
                    text(
                        "SELECT id FROM events WHERE operation_id = :o AND type = 'belief_authored'"
                    ),
                    {"o": op.operation_id},
                )
                at = (
                    position(op)
                    if callable(position)
                    else position
                    or held_from
                    or Position(op.narrative_turn or 0, op.turn_variant or 0)
                )
                cited = antecedent(op) if callable(antecedent) else antecedent
                found["value"] = await transitions.transition(
                    conn,
                    op,
                    holder=holder,
                    kind=cause,
                    proposition=proposition,
                    antecedent=cited or transitions.Antecedent.record("events", str(act)),
                    position=at,
                    held_from=held_from,
                    confidence=confidence,
                    settings=settings,
                    **options,
                )

            op.add_commit_step(step)
            await op.commit()
    result = found["value"]
    assert isinstance(result, transitions.TransitionResult)
    return result


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


async def rows_of(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    return await rows(engine, sql, **params)


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


__all__ = [
    "belief_rows",
    "believe",
    "deliver_partitions",
    "delivered",
    "execute",
    "folded_rows",
    "partitioned",
    "rows_of",
    "state_rows",
    "transit",
]
