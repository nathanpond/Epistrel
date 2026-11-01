"""Helpers for the State-effect tests (#187): a structured story with ``states.level = on`` and
its States declared (the ``states`` module patched in by ``states_delivered``), scripted
renders that declare effects, and reads of the prompts a turn sent."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import states
from epistrel.core.enums import StateChangeSource
from epistrel.core.states import StateDefinition
from epistrel.orchestrator.models.stub import StubAdapter
from tests import state_support as ss
from tests.structured_turn_support import structured_story

ANY_CHARACTER = {"sources": ["any_character"]}
DIRECTOR_ONLY = {"sources": ["director"]}
"""#188: only the narrator's world pass may change the State."""
NARRATOR_MARKER = "You are the narrator."
"""A line only the narrator's render prompt holds."""


def gold(**kw: Any) -> StateDefinition:
    """The player's gold (``coins``, ``gp``), which any character's render may change."""
    return ss.gold(authorization=ANY_CHARACTER, **kw)


def suspicion(**kw: Any) -> StateDefinition:
    """A hidden story State only the narrator may change."""
    found: dict[str, Any] = {
        "name": "suspicion",
        "type": "number",
        "scope": "story",
        "initial_value": 0,
        "visibility": "hidden",
        "aliases": ("unease",),
        "authorization": DIRECTOR_ONLY,
    }
    found.update(kw)
    return StateDefinition.model_validate(found)


async def shop(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    *definitions: StateDefinition,
    gold_of_player: int | None = None,
    names: tuple[str, ...] = ("Cal", "Mira"),
) -> tuple[str, dict[str, str]]:
    """A structured story (``names``: Cal, the player, and Mira, the smith, by default) with
    ``states.level = on`` and ``definitions`` declared; Cal holds ``gold_of_player`` gold
    when given."""
    story, ids = await structured_story(client, *names)
    await ss.set_level(engine, story, "on")
    if definitions:
        await ss.define_all(engine, story, *definitions)
    if gold_of_player is not None:
        await ss.change(
            engine,
            story,
            ss.set_to(
                "gold",
                gold_of_player,
                scope_key=ids["Cal"],
                reason="a purse",
                source=StateChangeSource.CONSUMER,
            ),
        )
    return story, ids


def effect(state: str, *, reason: str = "a change", **fields: Any) -> dict[str, Any]:
    """A declared ``change`` effect."""
    return {"kind": "change", "state": state, "reason": reason, **fields}


def render(*beats: dict[str, Any]) -> str:
    return json.dumps({"beats": list(beats)})


def narrator(stub: StubAdapter, answer: str) -> None:
    """The narrator's renders answer ``answer``."""
    stub.when(NARRATOR_MARKER, answer, purpose="generation")


def prompts(stub: StubAdapter, schema_name: str) -> list[str]:
    """The prompts of the stub's ``generation`` calls with response schema ``schema_name``."""
    return [
        "\n".join(m.content for m in c.messages)
        for c in stub.calls
        if c.purpose == "generation" and c.response_schema == schema_name
    ]


def regenerations(stub: StubAdapter) -> list[str]:
    """The prompts of the repair ladder's ``regeneration`` calls."""
    return [
        "\n".join(m.content for m in c.messages) for c in stub.calls if c.purpose == "regeneration"
    ]


async def gold_of(engine: AsyncEngine, story: str, character: str) -> Any:
    return await ss.value(engine, story, "gold", character)


async def turn_changes(engine: AsyncEngine, story: str) -> list[Any]:
    """The story's ``turn``-sourced State changes."""
    return [r for r in await ss.change_rows(engine, story) if r.source == "turn"]


def no_value(prompt: str, *values: object) -> None:
    """``prompt`` shows none of ``values`` in its ``## Tracked values`` section."""
    section = prompt.split("## Tracked values", 1)[1].split("\n## ", 1)[0]
    for value in values:
        assert str(value) not in section, (value, section)


__all__ = [
    "ANY_CHARACTER",
    "DIRECTOR_ONLY",
    "NARRATOR_MARKER",
    "effect",
    "gold",
    "gold_of",
    "narrator",
    "no_value",
    "prompts",
    "regenerations",
    "render",
    "shop",
    "states",
    "suspicion",
    "turn_changes",
]
