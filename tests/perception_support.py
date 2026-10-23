"""Helpers for the perception-routing tests (#161): the ``partitions`` module and the perception
levels delivered for a test (#181 delivers them for real), a structured story switched to
per-character partitions at a perception level, the authoritative topology written as the
author's canon (placements, adjacency with barriers, channels, containment), the languages a
character understands, and the routed memories read back."""

from __future__ import annotations

import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.profiles import schema
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from epistrel.core.propositions.vocabulary import (
    ADJACENT_TO,
    LOCATED_AT,
    LOCATED_IN,
    ON_CHANNEL,
)
from tests.canon_support import commit
from tests.event_support import rows

NO_FACTS = RuntimeFacts(models={}, templates={})
LEVELS = frozenset({"co_location", "full"})


def _delivered() -> types.MappingProxyType[str, frozenset[str]]:
    return types.MappingProxyType(
        {**schema.DELIVERED, "partitions": frozenset({schema.ON}), "perception": LEVELS}
    )


def deliver_perception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver ``partitions`` and perception ``co_location``/``full`` for the test."""
    monkeypatch.setattr(schema, "DELIVERED", _delivered())


@contextmanager
def delivered() -> Iterator[None]:
    """``deliver_perception`` for a block (a Hypothesis run outside a test's ``monkeypatch``)."""
    before = schema.DELIVERED
    schema.DELIVERED = _delivered()
    try:
        yield
    finally:
        schema.DELIVERED = before


async def perceive(engine: AsyncEngine, story_id: str, level: str = "full") -> None:
    """Switch the story to per-character partitions at perception ``level`` (``none`` turns
    both off), as the story's own profile overrides."""
    on = level != "none"
    await set_overrides(
        engine,
        story_id,
        ProfileOverridesPatch(
            settings={"character_knowledge": {"partitions": on}, "perception": {"level": level}},
            ignore_warnings=True,
        ),
        principal="p-author",
        facts=NO_FACTS,
    )


FORGET = text(
    "UPDATE story_profile_overrides SET settings = settings - 'perception' - 'character_knowledge' "
    "WHERE settings ? 'perception' OR settings ? 'character_knowledge'"
)


async def forget_perception(engine: AsyncEngine) -> None:
    """Take back every story override of ``character_knowledge`` and ``perception``
    (``perceive``'s), so no later test of the shared database finds an undelivered module
    named in a story's overrides (a mention alone is undelivered, even switched off)."""
    async with engine.begin() as conn:
        await conn.execute(FORGET)


async def place_entity(engine: AsyncEngine, story_id: str, name: str) -> str:
    async with engine.begin() as conn:
        return await store.add_entity(conn, story_id, "place", name)


async def _author(engine: AsyncEngine, story_id: str, proposition: Proposition) -> Any:
    return await commit(
        engine, story_id, proposition, kind=OperationKind.CONTROL, provenance="author"
    )


async def put(engine: AsyncEngine, story_id: str, character_id: str, place_id: str) -> Any:
    """The author's canon: ``character_id`` is at ``place_id`` (for all story-time)."""
    return await _author(
        engine,
        story_id,
        Proposition(
            subject_id=character_id,
            predicate_id=LOCATED_AT,
            object_id=place_id,
            valid_time=Interval.of(None, None),
        ),
    )


async def adjoin(
    engine: AsyncEngine, story_id: str, a: str, b: str, barrier: str | None = None
) -> Any:
    """The author's canon: places ``a`` and ``b`` adjoin, through ``barrier``."""
    lo, hi = sorted((a, b))
    return await _author(
        engine,
        story_id,
        Proposition(
            subject_id=lo,
            predicate_id=ADJACENT_TO,
            object_id=hi,
            qualifiers={} if barrier is None else {"barrier": barrier},
            valid_time=Interval.of(None, None),
        ),
    )


async def inside(engine: AsyncEngine, story_id: str, inner: str, outer: str) -> Any:
    return await _author(
        engine,
        story_id,
        Proposition(
            subject_id=inner,
            predicate_id=LOCATED_IN,
            object_id=outer,
            valid_time=Interval.of(None, None),
        ),
    )


async def tune_in(engine: AsyncEngine, story_id: str, character_id: str, channel: str) -> Any:
    """The author's canon: ``character_id`` is on remote channel ``channel``."""
    return await _author(
        engine,
        story_id,
        Proposition(
            subject_id=character_id,
            predicate_id=ON_CHANNEL,
            literal=TypedLiteral(kind=LiteralKind.STR, value=channel),
            valid_time=Interval.of(None, None),
        ),
    )


async def speaks(engine: AsyncEngine, character_id: str, *languages: str) -> None:
    """``character_id`` understands only ``languages``."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE characters SET languages = :l WHERE id = :c"),
            {"l": list(languages), "c": character_id},
        )


async def memories_of(engine: AsyncEngine, story_id: str, holder: str) -> list[Any]:
    """``holder``'s routed memories (active), in turn and beat order."""
    return await rows(
        engine,
        "SELECT text, fidelity, channel, beat, narrative_turn, visibility, proposition_ids "
        "FROM memories WHERE story_id = :s AND holder_character_id = :h "
        "AND membership = 'active' ORDER BY narrative_turn, beat, id",
        s=story_id,
        h=holder,
    )


async def routed_memories(engine: AsyncEngine, story_id: str) -> list[Any]:
    """Every routed memory of the story (one per perceiver), by turn, beat and holder."""
    return await rows(
        engine,
        "SELECT holder_character_id, text, fidelity, channel, beat, narrative_turn "
        "FROM memories WHERE story_id = :s AND fidelity IS NOT NULL "
        "AND membership = 'active' ORDER BY narrative_turn, beat, holder_character_id",
        s=story_id,
    )


__all__ = [
    "LEVELS",
    "NO_FACTS",
    "adjoin",
    "deliver_perception",
    "delivered",
    "forget_perception",
    "inside",
    "memories_of",
    "perceive",
    "place_entity",
    "put",
    "routed_memories",
    "speaks",
    "tune_in",
]
