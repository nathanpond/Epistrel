"""The Bobby/Jenni structured fixture of the novelty-permission tests (#144; #151 reuses it):
a structured story with Cal (the player), Bobby and Jenni, two places, two schools, a camp and
two unmodeled strangers, Bobby's seed placement, an author-locked enrollment, and the arrival
toggle (``allow_new_arrivals``, a story setting).

A turn here is two steps, the way #150's structured turn makes them: the read phase verifies
the turn's claims on a connection with the turn's read set (``read_turn``), and the commit
canonizes the permitted ones inside a turn ``Operation``'s commit step (``commit_turn``)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.canon.canonize import canonize_all
from epistrel.core.canon.facts import Fact
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.readset import StructuredReadSet
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from epistrel.core.story_settings import update_story_settings
from epistrel.core.verify.permission import Asserter
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.verify.verifier import ClaimRef, Verifier, story_verifier
from tests.canon_support import PRINCIPAL, commit, run
from tests.memory_support import use_structured

LOCATED_AT = CORE_BY_NAME["located_at"].id
ENROLLED_AT = CORE_BY_NAME["enrolled_at"].id
ATTENDED = CORE_BY_NAME["attended_program"].id
KNOWS = CORE_BY_NAME["knows"].id
AGE = CORE_BY_NAME["age"].id

SEED_SPAN = Interval.of(0, 1_000)
PLACED_SPAN = Interval.of(100, 1_000)
NOW = Interval.of(500, 600)


@dataclass(frozen=True)
class Cast:
    """The fixture's story and ids (a character's entity has the character's id)."""

    story_id: str
    cal: str
    bobby: str
    jenni: str
    kitchen: str
    porch: str
    osu: str
    michigan: str
    camp: str
    stranger: str
    visitor: str
    seed_fact: str
    """Bobby ``located_at`` the kitchen over 100-1000 (a render's): his most recent fact."""
    locked_fact: str
    """Bobby ``enrolled_at`` OSU, the author's, locked."""

    def render(self, character: str) -> Asserter:
        return Asserter("character", character)

    @property
    def director(self) -> Asserter:
        return Asserter("director")

    @property
    def player(self) -> Asserter:
        return Asserter("player", self.cal)


def claim(subject: str, predicate: str, obj: str | None, when: Interval = NOW) -> Proposition:
    return Proposition(subject_id=subject, predicate_id=predicate, object_id=obj, valid_time=when)


async def bobby_and_jenni(engine: AsyncEngine, *, allow_new_arrivals: bool = True) -> Cast:
    spec = StorySpec(
        name="novelty",
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
        places = {n: await store.add_entity(conn, story, "place", n) for n in ("kitchen", "porch")}
        schools = {
            n: await store.add_entity(conn, story, "organization", n) for n in ("OSU", "Michigan")
        }
        camp = await store.add_entity(conn, story, "other", "chess camp")
        strangers = {
            n: await store.add_entity(conn, story, "person", n) for n in ("stranger", "visitor")
        }
    await use_structured(engine, story)
    if not allow_new_arrivals:
        await set_arrivals(engine, story, allowed=False)
    seed = await commit(
        engine, story, claim(ids["Bobby"], LOCATED_AT, places["kitchen"], PLACED_SPAN)
    )
    locked = await commit(
        engine,
        story,
        claim(ids["Bobby"], ENROLLED_AT, schools["OSU"], SEED_SPAN),
        kind=OperationKind.CONTROL,
        provenance="author",
        author_locked=True,
    )
    return Cast(
        story_id=story,
        cal=ids["Cal"],
        bobby=ids["Bobby"],
        jenni=ids["Jenni"],
        kitchen=places["kitchen"],
        porch=places["porch"],
        osu=schools["OSU"],
        michigan=schools["Michigan"],
        camp=camp,
        stranger=strangers["stranger"],
        visitor=strangers["visitor"],
        seed_fact=seed.id,
        locked_fact=locked.id,
    )


async def set_arrivals(engine: AsyncEngine, story_id: str, *, allowed: bool) -> None:
    """The arrival toggle: ``PATCH /stories/{id}/settings {allow_new_arrivals}``'s write."""
    await update_story_settings(
        engine, story_id, {"allow_new_arrivals": allowed}, principal="p-author"
    )


Asserted = tuple[Proposition, Asserter, ClaimRef]


async def verify_all(
    conn: AsyncConnection, read_set: StructuredReadSet, story_id: str, claims: Sequence[Asserted]
) -> Verifier:
    """A turn's verifier over ``claims`` (each with its asserter and where it sits)."""
    verifier = await story_verifier(conn, read_set, story_id, models=None, budget=TurnBudget())
    for found, asserter, ref in claims:
        await verifier.verify(found, ref=ref, asserter=asserter)
    return verifier


@dataclass(frozen=True)
class ReadTurn:
    """A turn's read phase: its verifier and the denied claims rendered on its connection."""

    verifier: Verifier
    denied: list[dict[str, Any]]


async def read_turn(engine: AsyncEngine, story_id: str, claims: Sequence[Asserted]) -> ReadTurn:
    """The read phase of a turn: verify ``claims`` on one connection and read set."""
    async with engine.connect() as conn:
        verifier = await verify_all(conn, StructuredReadSet(), story_id, claims)
        return ReadTurn(verifier, await verifier.denied())


async def commit_turn(
    engine: AsyncEngine, story_id: str, verifier: Verifier
) -> tuple[Operation, list[Fact]]:
    """The turn's commit: its permitted claims canonized in its commit step."""

    async def write(conn: AsyncConnection, op: Operation) -> list[Fact]:
        return await canonize_all(conn, story_id, verifier.canon_requests(), operation=op)

    op, written = await run(engine, story_id, write)
    return op, list(written)


async def facts_of(engine: AsyncEngine, story_id: str, found: Proposition) -> list[Any]:
    """Every ``facts`` row (any version) of ``found``'s subject, predicate and object."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT f.*, p.object_id FROM facts f JOIN propositions p ON p.id = "
                "f.proposition_id WHERE f.story_id = :s AND f.subject_id = :subject AND "
                "f.predicate_id = :predicate AND p.object_id IS NOT DISTINCT FROM :object"
            ),
            {
                "s": story_id,
                "subject": found.subject_id,
                "predicate": found.predicate_id,
                "object": found.object_id,
            },
        )
        return list(rows)


__all__ = [
    "AGE",
    "ATTENDED",
    "ENROLLED_AT",
    "KNOWS",
    "LOCATED_AT",
    "NOW",
    "PLACED_SPAN",
    "SEED_SPAN",
    "Asserted",
    "Cast",
    "ReadTurn",
    "bobby_and_jenni",
    "claim",
    "commit_turn",
    "facts_of",
    "read_turn",
    "set_arrivals",
    "verify_all",
]
