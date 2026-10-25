"""Helpers for the participation-role tests (#176): the bar (a structured story under
per-character renders with per-character partitions, testimony ``defaulted`` and perception
``full``: Cal, the player, and Bobby, Jenni, Ada, Sam (the bartender) and Dale, all in the
parlor unless sent ``away`` to the kitchen), a character's participation set through ``PATCH
/stories/{id}/characters/{cid}/participation``, scripted renders carrying ``reactions``, and the
reads the oracles use (the narrated beats, a reactor's rows)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import multi_support as ms
from tests import testimony_support as ts
from tests.event_support import rows, scalar
from tests.story_support import WRITE

CAST = ("Cal", "Bobby", "Jenni", "Ada", "Sam", "Dale")


@dataclass(frozen=True)
class Bar:
    story_id: str
    ids: dict[str, str]
    things: dict[str, str]

    def __getitem__(self, name: str) -> str:
        return self.ids[name]


async def bar(
    client: Any,
    engine: AsyncEngine,
    *,
    names: tuple[str, ...] = CAST,
    away: tuple[str, ...] = (),
    model_calls: int | None = None,
    **settings: Any,
) -> Bar:
    """The bar (see the module docstring)."""
    story, ids, things = await ts.room(client, engine, *names, away=away)
    await ts.enable_testimony(engine, story, "defaulted", level="full")
    await ms.per_character(engine, story, model_calls=model_calls, **settings)
    return Bar(story, ids, things)


async def participation(
    client: httpx.AsyncClient, story: str, character: str, role: str, *, fixed: bool = False
) -> httpx.Response:
    return await client.patch(
        f"/stories/{story}/characters/{character}/participation",
        json={"role": role, "fixed": fixed},
        headers=WRITE,
    )


async def set_role(
    client: httpx.AsyncClient, story: str, character: str, role: str, *, fixed: bool = False
) -> None:
    answer = await participation(client, story, character, role, fixed=fixed)
    assert answer.status_code == 200, answer.text


async def interjections(client: httpx.AsyncClient, story: str, on: bool) -> None:
    answer = await client.patch(
        f"/stories/{story}/settings", json={"reactive_interjections": on}, headers=WRITE
    )
    assert answer.status_code == 200, answer.text


def reaction(
    character: str, text: str, *, kind: str = "nonverbal", reacts_to: int | None = None
) -> dict[str, Any]:
    found: dict[str, Any] = {"character_id": character, "text": text, "kind": kind}
    if reacts_to is not None:
        found["reacts_to"] = reacts_to
    return found


def render(*beats: dict[str, Any], reactions: list[dict[str, Any]] | None = None) -> str:
    """A scripted character render with its ``reactions`` (#176)."""
    found: dict[str, Any] = {"beats": list(beats)}
    if reactions:
        found["reactions"] = reactions
    return json.dumps(found)


async def narrated(engine: AsyncEngine, story: str) -> list[Any]:
    """The committed narrated beats: type, beat, the reactor, the narrator and the text."""
    return await rows(
        engine,
        "SELECT e.type, e.beat, e.character_id, e.payload->>'narrated_by' AS narrated_by, "
        "e.payload->>'text' AS text FROM events e "
        "WHERE e.story_id = :s AND e.type LIKE 'beat_%' AND e.payload ? 'narrated_by' "
        "ORDER BY e.seq",
        s=story,
    )


async def rejected(engine: AsyncEngine, story: str) -> list[Any]:
    """The ``render_rejected`` records of dropped reactions."""
    return await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'render_rejected' "
        "AND payload->'constraints' @> '[{\"kind\": \"reaction\"}]'::jsonb ORDER BY seq",
        s=story,
    )


async def reactor_rows(engine: AsyncEngine, story: str, character: str) -> dict[str, int]:
    """The reactor's rows the bounds count: its ``think``/``latent`` beats, its beliefs and its
    exposures, and its episodic ``do`` memories."""

    async def count(sql: str) -> int:
        return int(await scalar(engine, sql, s=story, c=character) or 0)

    return {
        "private_beats": await count(
            "SELECT count(*) FROM events WHERE story_id = :s AND character_id = :c "
            "AND type IN ('beat_think', 'beat_latent')"
        ),
        "beliefs_at_reaction": await count(
            "SELECT count(*) FROM beliefs b WHERE b.story_id = :s AND b.holder_character_id = :c "
            "AND b.beat IN (SELECT e.beat FROM events e WHERE e.story_id = :s "
            "AND e.character_id = :c AND e.payload ? 'narrated_by')"
        ),
        "exposures": await count(
            "SELECT count(*) FROM exposures WHERE story_id = :s AND witness_character_id = :c"
        ),
    }


def character_renders(stub: Any) -> list[str]:
    """The names of the characters rendered (a ``CharacterRender`` call each), in order."""
    found: list[str] = []
    for c in stub.calls:
        if c.purpose != "generation" or c.response_schema != "CharacterRender":
            continue
        text = "\n".join(m.content for m in c.messages)
        marker = "## You are "
        at = text.find(marker)
        if at >= 0:
            found.append(text[at + len(marker) :].split("\n", 1)[0])
    return found


__all__ = [
    "CAST",
    "WRITE",
    "Bar",
    "bar",
    "character_renders",
    "interjections",
    "narrated",
    "participation",
    "reaction",
    "reactor_rows",
    "rejected",
    "render",
    "set_role",
]
