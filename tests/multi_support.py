"""Helpers for the multi-character tests (#174): the ``multi_character`` module delivered for a
test (#181 delivers it for real) beside #162's partitions, perception and testimony, a story
switched to per-character renders, and a scripted character render keyed by the character's
name in its prompt (``## You are <name>``), so each primary of a sequential turn gets its own
answer whatever the order."""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.profiles import schema
from epistrel.core.profiles.story import ProfileOverridesPatch, set_overrides
from epistrel.orchestrator.models.stub import StubAdapter
from tests import perception_support as ps
from tests import testimony_support as ts

PER_CHARACTER = frozenset({"per_character"})


def _delivered() -> types.MappingProxyType[str, frozenset[str]]:
    return types.MappingProxyType({**ts._delivered(), "multi_character": PER_CHARACTER})


def deliver_multi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliver ``multi_character = per_character`` (and #162's modules) for the test."""
    monkeypatch.setattr(schema, "DELIVERED", _delivered())


@contextmanager
def delivered() -> Iterator[None]:
    before = schema.DELIVERED
    schema.DELIVERED = _delivered()
    try:
        yield
    finally:
        schema.DELIVERED = before


async def per_character(
    engine: AsyncEngine, story_id: str, *, model_calls: int | None = None, **settings: Any
) -> None:
    """The story's overrides: ``multi_character.renders = per_character`` (merged over what the
    story already overrides), with ``budgets.model_calls`` when given."""
    found: dict[str, Any] = {"multi_character": {"renders": "per_character"}, **settings}
    if model_calls is not None:
        found["budgets"] = {"model_calls": model_calls}
    await set_overrides(
        engine,
        story_id,
        ProfileOverridesPatch(settings=found, ignore_warnings=True),
        principal="p-author",
        facts=ps.NO_FACTS,
    )


def renders_for(stub: StubAdapter, name: str, answer: str) -> None:
    """``name``'s character renders answer ``answer`` (a scripted ``generation`` queue, which
    answers first, must be empty)."""
    stub.when(f"## You are {name}\n", answer, purpose="generation")


def render(*beats: dict[str, Any], steps: list[dict[str, Any]] | None = None) -> str:
    found: dict[str, Any] = {"beats": list(beats)}
    if steps:
        found["steps"] = steps
    return json.dumps(found)


def director(*ids: str) -> str:
    """A scripted director-order answer."""
    return json.dumps({"order": list(ids)})


def render_prompts(stub: StubAdapter, name: str) -> list[str]:
    """The prompts of ``name``'s character renders, in call order."""
    return [
        "\n".join(m.content for m in c.messages)
        for c in stub.calls
        if c.purpose == "generation"
        and c.response_schema == "CharacterRender"
        and any(f"## You are {name}\n" in m.content for m in c.messages)
    ]


def call_index(stub: StubAdapter, purpose: str, needle: str) -> int:
    """Where the first ``purpose`` call whose messages contain ``needle`` sits in call order."""
    for n, c in enumerate(stub.calls):
        if c.purpose == purpose and any(needle in m.content for m in c.messages):
            return n
    raise AssertionError(f"no {purpose} call containing {needle!r}")


FORGET_MULTI = (
    "UPDATE story_profile_overrides SET settings = settings - 'multi_character' "
    "WHERE settings ? 'multi_character'"
)


async def forget(engine: AsyncEngine) -> None:
    """Take back the per-character overrides (and #162's) the tests switched on: the tests share
    one database, and a later validation of every story's overrides would find them
    undelivered."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(FORGET_MULTI))
    await ts.forget(engine)


__all__ = [
    "call_index",
    "deliver_multi",
    "delivered",
    "director",
    "forget",
    "per_character",
    "render",
    "render_prompts",
    "renders_for",
]
