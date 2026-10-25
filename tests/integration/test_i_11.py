"""I-11 (M5): viewpoint-scoped retrieval (docs/04 §4; FR-RET-1; #159).

A retrieval for character A returns only A's partition plus the records A may read, across every
memory tier and both retrieval channels: the occasion and assessment reads, the vector arm, the
lexical arm over the hot and the retired tier (a rehydrated row included), the alias arm, canon
and the ``beliefs`` arm. The story runs on a database of its own (the vector arm needs a pinned
dimension, as #141's retrieval tests do), with the ``partitions`` module on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import MemoryKind, RetrievalTier
from epistrel.core.membership import insert_derived
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.retrieval import pipeline
from epistrel.core.tables import characters
from epistrel.core.tables.memories import memories
from epistrel.core.visibility import Visibility
from epistrel.testing.fixtures import scale
from tests import canon_support as c
from tests import partition_support as ps
from tests import retrieval_support as r
from tests.embedding_support import Scratch, embedding_db, reset

pytestmark = pytest.mark.integration

NEAR = (0.9, 0.1, 0.0, 0.0)
QUERY_VECTOR = (1.0, 0.0, 0.0, 0.0)
QUERY = "Bobby, where did the lantern go after the game?"


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url, stub_embedding_dimension=r.DIMENSION) as s:
        await reset(s.db)
        await r.pin(s.db)
        yield s


def _embed() -> Callable[[str], Awaitable[Sequence[float]]]:
    async def embed(text: str) -> Sequence[float]:
        del text
        return QUERY_VECTOR

    return embed


async def _memory(
    db: AsyncEngine,
    story: r.Story,
    line: str,
    *,
    visibility: Visibility,
    turn: int,
    kind: MemoryKind = MemoryKind.EPISODIC,
    event_type: str | None = None,
    participants: Sequence[str] = (),
    tier: RetrievalTier = RetrievalTier.HOT,
) -> str:
    """One memory with its visibility (a hot one with a vector near the query's; a retired one
    with none, so only the lexical arm's rehydration finds it)."""
    hot = tier is RetrievalTier.HOT
    row = scale.memory_row(
        story_id=story.story_id,
        operation_id=story.operation_id,
        line=line,
        turn=turn,
        kind=kind,
        event_type=event_type,
        participants=[story.people[p] for p in participants],
        vector=NEAR if hot else None,
        version=r.VERSION if hot else None,
    )
    row["visibility"] = visibility.document()
    row["retrieval_tier"] = tier.value
    async with db.begin() as conn:
        await insert_derived(conn, memories, [row])
    return str(row["id"])


async def _cast(db: AsyncEngine, story_id: str) -> dict[str, str]:
    async with db.connect() as conn:
        found = await conn.execute(
            select(characters.c.name, characters.c.id).where(characters.c.story_id == story_id)
        )
        return {str(n): str(i) for n, i in found}


def _returned(found: pipeline.RetrievalResult) -> set[str]:
    return {m.id for m in (*found.episodes, *found.assessments, *found.memories)}


@pytest.mark.part("M5", "FR-RET-1")
async def test_i_11(scratch: Scratch) -> None:
    """Ann asks Bobby about the lantern after the game. Bobby's private ``think`` memories (an
    occasion episode, a hot memory near the query's vector, a retired one only the lexical arm
    can rehydrate), his private assessment of the game, the ``latent`` fact canon tells him
    alone and his own beliefs are never in Ann's result, by any arm; the shared memories, her own
    private one, the public canon and her own beliefs are. Complements: the same query as
    ``omniscient`` returns Bobby's private rows (the rehydrated one too) and no beliefs; with
    the ``partitions`` module off, memories are not filtered (M4's shared knowledge) while the
    ``latent`` fact stays canon's to filter; and the vector arm alone, run as Ann, still leaves
    Bobby's near memory out."""
    db = scratch.db
    found_ids: dict[str, str] = {}

    async def canon_and_beliefs(engine: AsyncEngine, story: r.Story) -> None:
        cast = await _cast(engine, story.story_id)
        people = story.people

        def knows(who: str, whom: str) -> Proposition:
            return Proposition(
                subject_id=people[who],
                predicate_id=c.KNOWS,
                object_id=people[whom],
                valid_time=Interval.of(None, None),
            )

        public = await c.commit(engine, story.story_id, knows("Bobby", "Dan"))
        latent = await c.commit(
            engine,
            story.story_id,
            knows("Bobby", "Quillon"),
            visibility=Visibility(scope="characters", characters=(cast["Bobby"],)),
        )
        hers = await ps.believe(engine, story.story_id, cast["Ann"], knows("Bobby", "Cal"))
        his = await ps.believe(engine, story.story_id, cast["Bobby"], knows("Bobby", "Ann"))
        found_ids.update(public=public.id, latent=latent.id, hers=hers.id, his=his.id)

    story = await r.build_story(db, turns=200, before_head=canon_and_beliefs)
    cast = await _cast(db, story.story_id)
    ann, bobby = cast["Ann"], cast["Bobby"]
    shared, public = Visibility(scope="public"), Visibility(scope="objective")
    his_only, hers_only = (
        Visibility(scope="characters", characters=(bobby,)),
        Visibility(scope="characters", characters=(ann,)),
    )
    ids = {
        "game": await _memory(
            db,
            story,
            "Bobby says: The lantern stayed by the bench at the game.",
            visibility=shared,
            turn=150,
            event_type="game",
            participants=("Bobby", "Ann"),
        ),
        "his_game": await _memory(
            db,
            story,
            "Bobby thinks: I hid the lantern under the bench at the game.",
            visibility=his_only,
            turn=151,
            event_type="game",
            participants=("Bobby", "Ann"),
        ),
        "his_assessment": await _memory(
            db,
            story,
            "Bobby thinks Ann would never find the lantern.",
            visibility=his_only,
            turn=152,
            kind=MemoryKind.SEMANTIC,
            participants=("Bobby", "Ann"),
        ),
        "his_near": await _memory(
            db, story, "Bobby thinks: the lantern is mine now.", visibility=his_only, turn=160
        ),
        "his_retired": await _memory(
            db,
            story,
            "Bobby thinks: the lantern was always a secret.",
            visibility=his_only,
            turn=20,
            tier=RetrievalTier.RETIRED,
        ),
        "hers": await _memory(
            db,
            story,
            "Ann thinks: Bobby took my lantern.",
            visibility=hers_only,
            turn=161,
        ),
        "narration": await _memory(
            db, story, "The lantern flickers on the porch.", visibility=public, turn=162
        ),
    }
    private = {ids["his_game"], ids["his_assessment"], ids["his_near"], ids["his_retired"]}
    on = await ps.partitioned(db, story.story_id)
    off = await ps.partitioned(db, story.story_id, on=False)

    async def ask(viewpoint: str, settings: ProfileSettings, **kw: Any) -> pipeline.RetrievalResult:
        async with db.begin() as conn:
            return await pipeline.retrieve(
                conn,
                story.story_id,
                pipeline.RetrievalQuery(QUERY, actor=ann),
                viewpoint=viewpoint,
                embed=_embed(),
                settings=settings,
                touch=False,
                **kw,
            )

    as_ann = await ask(f"character:{ann}", on)
    returned = _returned(as_ann)
    assert returned.isdisjoint(private), returned & private
    assert {ids["game"], ids["hers"], ids["narration"]} <= returned
    assert as_ann.model_calls >= 1
    assert as_ann.degradation == ()
    assert any("vector" in m.arms for m in as_ann.memories)
    seen = {f.id for f in as_ann.canon}
    assert found_ids["public"] in seen
    assert found_ids["latent"] not in seen
    assert [b.id for b in as_ann.beliefs] == [found_ids["hers"]]
    assert all(b.holder_character_id == ann for b in as_ann.beliefs)
    assert ids["game"] in {m.id for m in as_ann.episodes}

    # Complement: omniscient reads everything (the rehydrated row too) and holds no beliefs.
    everything = await ask("omniscient", on)
    assert private <= _returned(everything)
    assert ids["his_retired"] in everything.rehydrated
    assert {found_ids["public"], found_ids["latent"]} <= {f.id for f in everything.canon}
    assert everything.beliefs == ()

    # Complement: with partitions off, M4's shared knowledge: memories unfiltered, no beliefs,
    # and canon still keeps the latent fact from Ann.
    shared_knowledge = await ask(f"character:{ann}", off)
    assert private & _returned(shared_knowledge)
    assert shared_knowledge.beliefs == ()
    assert found_ids["latent"] not in {f.id for f in shared_knowledge.canon}

    # The vector arm alone: as Ann it never reaches Bobby's near memory; omniscient, it does.
    vector_only = ProfileSettings.model_validate(
        {
            **on.model_dump(mode="json"),
            "memory": {
                **on.model_dump(mode="json")["memory"],
                "episodic": {
                    **on.model_dump(mode="json")["memory"]["episodic"],
                    "hybrid_search": False,
                },
            },
        }
    )
    alone = await ask(f"character:{ann}", vector_only)
    assert alone.memories
    assert all(m.arms == ("vector",) for m in alone.memories)
    assert ids["his_near"] not in _returned(alone)
    assert ids["his_near"] in _returned(await ask("omniscient", vector_only))
