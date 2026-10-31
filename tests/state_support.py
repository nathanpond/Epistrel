"""Helpers for the State tests (#185): the ``states`` module delivered for a test and switched on
for a story, declarations and changes written in an operation's commit step, and the story's
State rows and projection.

``states`` is #203's to deliver (``core.profiles.schema.DELIVERED``), so until then a test that
needs it patches the delivered set (``deliver``, or ``delivered()`` where no fixture reaches)
and sets the story's ``states.level`` override (``set_level``)."""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import states
from epistrel.core.enums import OperationKind, StateChangeKind, StateChangeSource
from epistrel.core.events import Operation
from epistrel.core.membership import Position
from epistrel.core.profiles import schema
from epistrel.core.states import StateChange, StateDefinition
from tests import canon_support as c
from tests.event_support import rows

LEVELS = frozenset({"prompt_only", schema.ON})
"""The levels #203 delivers."""


def _with_states() -> types.MappingProxyType[str, frozenset[str]]:
    return types.MappingProxyType({**schema.DELIVERED, "states": LEVELS})


def deliver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver the ``states`` module for the test (#203 delivers it for real)."""
    monkeypatch.setattr(schema, "DELIVERED", _with_states())


@contextmanager
def delivered() -> Iterator[None]:
    """``deliver`` for a block (a helper no fixture reaches)."""
    with mock.patch.object(schema, "DELIVERED", _with_states()):
        yield


async def set_level(engine: AsyncEngine, story_id: str, level: str | None) -> None:
    """Set the story's ``states.level`` override (``None``: drop it, the profile's ``off``),
    keeping its other overrides."""
    async with engine.begin() as conn:
        if level is None:
            await conn.execute(
                text(
                    "UPDATE story_profile_overrides SET settings = settings - 'states', "
                    "revision = revision + 1 WHERE story_id = :s"
                ),
                {"s": story_id},
            )
            return
        await conn.execute(
            text(
                "INSERT INTO story_profile_overrides (story_id, settings, revision) "
                "VALUES (:s, CAST(:o AS jsonb), 1) ON CONFLICT (story_id) DO UPDATE "
                "SET settings = story_profile_overrides.settings || excluded.settings, "
                "revision = story_profile_overrides.revision + 1"
            ),
            {"s": story_id, "o": json.dumps({"states": {"level": level}})},
        )


def gold(**kw: Any) -> StateDefinition:
    """A per-character, non-negative number State: the player's gold (``coins``, ``gp``)."""
    found: dict[str, Any] = {
        "name": "gold",
        "type": "number",
        "scope": "character",
        "constraints": {"non_negative": True},
        "initial_value": 0,
        "aliases": ("coins",),
        "unit": "gp",
    }
    found.update(kw)
    return StateDefinition.model_validate(found)


async def define(
    engine: AsyncEngine,
    story_id: str,
    definition: StateDefinition,
    *,
    kind: OperationKind = OperationKind.CONTROL,
) -> StateDefinition:
    """Declare ``definition`` in a new operation (a position-less ``control`` one by default)."""

    async def write(conn: AsyncConnection, op: Operation) -> StateDefinition:
        return await states.define_state(conn, op, story_id, definition)

    _, stored = await c.run(engine, story_id, write, kind=kind)
    assert isinstance(stored, StateDefinition)
    return stored


async def define_all(
    engine: AsyncEngine,
    story_id: str,
    *definitions: StateDefinition,
    kind: OperationKind = OperationKind.TURN,
) -> list[StateDefinition]:
    """Declare ``definitions`` in one new operation (a turn by default: an operation the fork
    copies records an event, and a position-less ``control`` operation records none)."""

    async def write(conn: AsyncConnection, op: Operation) -> list[StateDefinition]:
        return [await states.define_state(conn, op, story_id, d) for d in definitions]

    _, stored = await c.run(engine, story_id, write, kind=kind)
    return list(stored)


def delta(
    state: str,
    amount: int | Decimal,
    *,
    scope_key: str | None = None,
    reason: str = "a change",
    source: StateChangeSource = StateChangeSource.TURN,
    **kw: Any,
) -> StateChange:
    return StateChange(
        state=state,
        reason=reason,
        source=source,
        kind=StateChangeKind.DELTA,
        delta=Decimal(amount),
        scope_key=scope_key,
        **kw,
    )


def set_to(
    state: str,
    value: Any,
    *,
    scope_key: str | None = None,
    reason: str = "a change",
    source: StateChangeSource = StateChangeSource.TURN,
    **kw: Any,
) -> StateChange:
    return StateChange(
        state=state,
        reason=reason,
        source=source,
        kind=StateChangeKind.SET,
        value=value,
        scope_key=scope_key,
        **kw,
    )


async def change(
    engine: AsyncEngine,
    story_id: str,
    *changes: StateChange,
    kind: OperationKind = OperationKind.TURN,
    turn: int | None = None,
) -> tuple[Operation, list[states.Applied | states.Violation]]:
    """Apply ``changes`` in one new operation (a turn by default)."""

    async def write(conn: AsyncConnection, op: Operation) -> list[Any]:
        return [await states.apply_change(conn, op, one) for one in changes]

    op, found = await c.run(engine, story_id, write, kind=kind, turn=turn)
    return op, list(found)


async def value(engine: AsyncEngine, story_id: str, name: str, *args: Any, **kw: Any) -> Any:
    """``value_of``'s answer: the bare value (``None`` for ``Undefined``)."""
    async with engine.connect() as conn:
        found = await states.value_of(conn, story_id, name, *args, **kw)
    return None if isinstance(found, states.UndefinedType) else found.value


async def stored_values(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The story's ``state_values`` rows, in key order."""
    found = await rows(
        engine,
        "SELECT * FROM state_values WHERE story_id = :s ORDER BY state_id, scope_key",
        s=story_id,
    )
    return [_canonical(dict(r._mapping)) for r in found]


async def folded_values(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The ``state_values`` rows the fold gives now (``core.states.fold_state``)."""
    async with engine.connect() as conn:
        found = await states.fold_state(conn, story_id)
    return [_canonical(r) for r in sorted(found, key=lambda r: (r["state_id"], r["scope_key"]))]


def _canonical(row: dict[str, Any]) -> dict[str, Any]:
    found = dict(row)
    raw = found.get("value")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        found["value"] = str(Decimal(str(raw)).normalize())
    return found


async def change_rows(engine: AsyncEngine, story_id: str) -> list[Any]:
    """The story's ``state_changes`` rows, in position order."""
    return list(
        await rows(
            engine,
            "SELECT * FROM state_changes WHERE story_id = :s "
            "ORDER BY narrative_turn, slot_k, beat, step, id",
            s=story_id,
        )
    )


def at(turn: int, slot_k: int = 0) -> Position:
    """Checkpoint ``turn`` (``N``), or its slot ``N⁺k``."""
    return Position.end_of(turn, slot_k)
