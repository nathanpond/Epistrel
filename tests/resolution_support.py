"""The porch fixture of the resolution tests (#154): #146's knock story (Cal, Bobby, Jenni, an
unmodeled mailman) with two places that containment places apart (Jenni's porch on Maple
Street, the school on Hilltop), the author rule ``knocked_at(x, place) ⇒ located_at(x,
place)``, and Bobby recorded at the school around ``KNOCK_AT``.

A knock at the porch is a subject-open thunk at ``KNOCK_AT``: binding it to Bobby implies Bobby
was at the porch then, which ``located_at``'s functional key and the school fact contradict;
binding it to the mailman (no history) is free."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.enums import OpenSlot, OperationKind
from epistrel.core.events import Operation
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Granularity, Instant, Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from tests import commit_support as c
from tests.canon_support import commit

LOCATED_AT = CORE_BY_NAME["located_at"].id
LOCATED_IN = CORE_BY_NAME["located_in"].id
KNOCK_AT = 600
"""The story-minute of the knock (turn 1's narration says "someone knocks")."""
PRESENCE_RULE = {"when": "knocked_at", "then": "located_at"}


@dataclass(frozen=True)
class Porch:
    """The knock story's ids plus the porch's."""

    knock: c.Knock
    porch: str
    maple: str
    school: str
    hilltop: str
    school_fact: str

    @property
    def story_id(self) -> str:
        return self.knock.story_id


async def porch_story(engine: AsyncEngine, *, with_rule: bool = True) -> Porch:
    k = await c.knock_story(engine)
    async with engine.begin() as conn:
        porch = await store.add_entity(conn, k.story_id, "place", "Jenni's porch")
        maple = await store.add_entity(conn, k.story_id, "place", "Maple Street")
        school = await store.add_entity(conn, k.story_id, "place", "the school")
        hilltop = await store.add_entity(conn, k.story_id, "place", "Hilltop")
        if with_rule:
            await registry.register_author(
                conn, k.story_id, registry.ScenarioRules.parse({"rules": [PRESENCE_RULE]})
            )
    for inner, outer in ((porch, maple), (school, hilltop)):
        await commit(
            engine,
            k.story_id,
            Proposition(
                subject_id=inner,
                predicate_id=LOCATED_IN,
                object_id=outer,
                valid_time=Interval.of(None, None),
            ),
            provenance="author",
            kind=OperationKind.CONTROL,
        )
    fact = await commit(
        engine,
        k.story_id,
        Proposition(
            subject_id=k.bobby,
            predicate_id=LOCATED_AT,
            object_id=school,
            valid_time=Interval.of(KNOCK_AT - 120, KNOCK_AT + 120),
        ),
        provenance="author",
        kind=OperationKind.CONTROL,
    )
    return Porch(k, porch, maple, school, hilltop, fact.id)


def knock_sketch(p: Porch) -> ThunkSketch:
    """ "Someone knocks at Jenni's porch": who is open."""
    return ThunkSketch(
        subject_text="someone",
        relation_text="knocked_at",
        object_text="Jenni's porch",
        predicate_id=p.knock.knocked_at,
        object_id=p.porch,
        open_slot=OpenSlot.SUBJECT,
    )


def knocked(p: Porch, who: str, at: int) -> Proposition:
    """``who`` knocked at the porch at story-minute ``at`` (a claim of a later turn)."""
    return Proposition(
        subject_id=who,
        predicate_id=p.knock.knocked_at,
        object_id=p.porch,
        valid_time=Instant.of(at, Granularity.MINUTE),
    )


async def introduce_knock(engine: AsyncEngine, p: Porch) -> lifecycle.Thunk:
    """Turn 1: the knock, an open thunk at ``KNOCK_AT`` (no ledger entry: unforced)."""

    async def step(conn: AsyncConnection, op: Operation) -> lifecycle.Thunk | None:
        return await lifecycle.introduce(conn, op, knock_sketch(p), valid_time=KNOCK_AT, beat=2)

    found = await c.in_turn(engine, p.story_id, step)
    assert isinstance(found, lifecycle.Thunk)
    return found


__all__ = [
    "KNOCK_AT",
    "LOCATED_AT",
    "LOCATED_IN",
    "PRESENCE_RULE",
    "Porch",
    "introduce_knock",
    "knock_sketch",
    "knocked",
    "porch_story",
]
