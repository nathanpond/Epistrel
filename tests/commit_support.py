"""The knock fixture of the commitment tests (#146): a structured story with Cal (the
player), Bobby and Jenni, a front door, an unmodeled mailman, and the auto predicates
``knocked_at`` (who knocked at what) and ``knocked_for`` (what a knock was for).

A turn's commitment work happens the way #150's structured turn makes it: inside the turn
``Operation``'s commit step (``in_turn``), salience first (``record_salience``), then the
dependences in beat order (``lifecycle.depend``), then the close (``lifecycle.close_turn``)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.enums import OpenSlot
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Granularity, Instant, Proposition
from epistrel.core.readset import StructuredReadSet
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.orchestrator.commit.salience import Beat, record_salience
from epistrel.orchestrator.models.registry import SalienceSketch
from tests.canon_support import PRINCIPAL, run
from tests.memory_support import use_structured


@dataclass(frozen=True)
class Knock:
    """The fixture's story and ids (a character's entity has the character's id)."""

    story_id: str
    cal: str
    bobby: str
    jenni: str
    door: str
    mailman: str
    knocked_at: str
    knocked_for: str
    parcel: str

    def beat(self, index: int, kind: str, text_: str, speaker: str | None = None) -> Beat:
        names = {self.bobby: "Bobby", self.jenni: "Jenni", self.cal: "Cal"}
        return beat(index, kind, text_, speaker, None if speaker is None else names[speaker])


def beat(
    index: int, kind: str, text_: str, speaker: str | None = None, name: str | None = None
) -> Beat:
    """One beat as salience reads it."""
    return Beat(index, kind, text_, speaker, name)


async def knock_story(engine: AsyncEngine, *, structured: bool = True) -> Knock:
    spec = StorySpec(
        name="knock",
        characters=[
            CharacterSpec(name="Cal", definition="The player.", true_age=30, is_player=True),
            CharacterSpec(name="Bobby", definition="A boy.", true_age=12),
            CharacterSpec(name="Jenni", definition="His friend.", true_age=12),
        ],
    )
    created = await create_story_from(engine, spec, principal=PRINCIPAL)
    story = created.story_id
    ids = {c.name: c.id for c in created.characters}
    async with engine.begin() as conn:
        for name, character in ids.items():
            await store.add_entity(conn, story, "person", name, character_id=character)
        door = await store.add_entity(conn, story, "item", "door")
        parcel = await store.add_entity(conn, story, "item", "parcel")
        mailman = await store.add_entity(conn, story, "person", "mailman")
        knocked_at = (await registry.auto_register(conn, story, "knocked_at")).id
        knocked_for = (await registry.auto_register(conn, story, "knocked_for")).id
    if structured:
        await use_structured(engine, story)
    return Knock(
        story_id=story,
        cal=ids["Cal"],
        bobby=ids["Bobby"],
        jenni=ids["Jenni"],
        door=door,
        mailman=mailman,
        knocked_at=knocked_at,
        knocked_for=knocked_for,
        parcel=parcel,
    )


KNOCK = SalienceSketch(subject="someone", relation="knocked_at", object="the door")
"""The salient proposition of "Someone knocks at the door": who knocked is open."""
WHY = SalienceSketch(subject="the door", relation="knocked_for", object="something")
"""What the knock was for: the door's knock, its reason open."""


def salience_answer(*spans: tuple[int, str, SalienceSketch], texture: Any = ()) -> str:
    """A scripted ``salience_detection`` answer: salient ``(beat, quote, sketch)`` spans and
    texture ``(beat, quote)`` spans, quoted as the model is asked to (#461)."""
    return json.dumps(
        {
            "salient": [
                {"beat": b, "span": q, "proposition_sketch": sketch.model_dump(mode="json")}
                for b, q, sketch in spans
            ],
            "texture": [{"beat": b, "span": q} for b, q in texture],
        }
    )


def knock_sketch(k: Knock) -> ThunkSketch:
    return ThunkSketch(
        subject_text="someone",
        relation_text="knocked_at",
        object_text="the door",
        predicate_id=k.knocked_at,
        object_id=k.door,
        open_slot=OpenSlot.SUBJECT,
    )


def knocker(k: Knock, who: str, at: int) -> Proposition:
    """``who`` knocked at the door at story-minute ``at``."""
    return Proposition(
        subject_id=who,
        predicate_id=k.knocked_at,
        object_id=k.door,
        valid_time=Instant.of(at, Granularity.MINUTE),
    )


Step = Callable[[AsyncConnection, Operation], Awaitable[Any]]


async def in_turn(engine: AsyncEngine, story_id: str, step: Step, *, said: str = "go on") -> Any:
    """Run ``step`` in a new turn's commit step; answers what it answered."""
    _, found = await run(engine, story_id, step, said=said)
    return found


async def salient(
    conn: AsyncConnection,
    op: Operation,
    k: Knock,
    beat: Beat,
    sketch: SalienceSketch,
    span: tuple[int, int] | None = None,
) -> Any:
    """Record one salient reference of ``beat`` in ``op``'s commit."""
    turn = op.narrative_turn or 0
    return await record_salience(
        conn,
        op,
        [(beat, sketch, span)],
        read_set=StructuredReadSet(),
        position=(turn, 0),
    )


async def rows(engine: AsyncEngine, table: str, story_id: str) -> list[Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(f"SELECT * FROM {table} WHERE story_id = :s ORDER BY id"),  # noqa: S608
            {"s": story_id},
        )
        return list(found)


async def thunk(engine: AsyncEngine, story_id: str, thunk_id: str) -> lifecycle.Thunk:
    async with engine.connect() as conn:
        found = await lifecycle.read_thunk(conn, story_id, thunk_id)
    assert found is not None
    return found


async def blockers(engine: AsyncEngine, story_id: str) -> list[lifecycle.LedgerEntry]:
    async with engine.connect() as conn:
        return await lifecycle.blockers(conn, story_id)


__all__ = [
    "KNOCK",
    "WHY",
    "Knock",
    "beat",
    "blockers",
    "in_turn",
    "knock_sketch",
    "knock_story",
    "knocker",
    "rows",
    "salience_answer",
    "salient",
    "thunk",
]
