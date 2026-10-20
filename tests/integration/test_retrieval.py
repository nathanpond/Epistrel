"""The retrieval pipeline against PostgreSQL (#141): docs/04 I-05 and I-06 (M4) and the
S-THOSEGUYS scenario, plus rehydration, the gate on the ``episodic`` module, the debugging read
and the implementation's cost structure.

#139's slice ``test_i_05__resolution`` (FR-RET-2) covers what retrieval stands on: "those guys a
month ago at the game" resolves its time window against the position's story-time, its event
type, and its participants through recent context and #137's projections; a subject's timeline
is read through #136's keyed lookup. ``test_i_05`` (FR-RET-2,3,6) runs ``retrieve`` end to end:
the window locates the episode, its participants resolve the group, and their assessments and
standing profiles come back. ``test_i_06`` (FR-RET-4) shows the lexical arm returning a proper
noun the vector arm misses, ``test_s_thoseguys`` the same recall as I-05 on the 5,000-turn
fixture among distractors.

Every test but #139's runs on a database of its own (``embedding_support.embedding_db``): the
vector arm needs a pinned dimension (``retrieval_support.pin``), and a pin retypes
``memories.embedding``. NFR-PERF-1's p95 bound is gated in #153's weekly suite over this code;
here the cost structure is proved without a clock: the same statements whatever the story's
size, and every ``memories`` read served by an index.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.canon import facts as canon_facts
from epistrel.core.context import RequestContext
from epistrel.core.enums import MemoryKind, RecordProvenance, RetrievalTier
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.projections import structured
from epistrel.core.propositions import store
from epistrel.core.propositions.model import (
    Granularity,
    Instant,
    Interval,
    Polarity,
    Proposition,
)
from epistrel.core.propositions.vocabulary import BORN, CORE_BY_NAME, LOCATED_IN
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.resolution import context as resolution
from epistrel.core.resolution.entities import Relation, resolve_mentions
from epistrel.core.resolution.events import resolve_event_type
from epistrel.core.resolution.time import (
    SubjectTimeline,
    find_time_expressions,
    resolve_time,
    story_time_at,
)
from epistrel.core.resolution.types import MentionKind, ReasonCode, ResolvedTime, Uncertain
from epistrel.core.retrieval import pipeline
from epistrel.core.retrieval.assemble import ContextItem, Tier
from tests import canon_support as c
from tests import retrieval_support as r
from tests.auth_support import bearer
from tests.embedding_support import Scratch, embedding_db, reset, vectors

pytestmark = pytest.mark.integration

DAY = 1440
CTX = RequestContext(principal=c.PRINCIPAL)


@pytest.mark.part("M4", "FR-RET-2")
async def test_i_05__resolution(engine: AsyncEngine) -> None:
    """Cal (the player) and Bobby are in the kitchen; Bobby and Ann are siblings; Bobby's
    birth is canon; the last beat says "Bobby and Ann were at the game." Cal's "Those guys a
    month ago at the game: you were there with Bobby's sister." resolves: a window around 30
    days before the position's story-time (approximate, ± 7 days), the event type ``game``,
    "those guys" a group for #141's occasion lookup, "you" Bobby (the one other present),
    "Bobby's sister" Ann (one ``relationships`` hop). "When he was seven" anchors on Bobby's
    birth read through the keyed lookup, which records the query keys. Complement: Ann has no
    birth or age in canon, so the same expression is ``uncertain(no_anchor)``; and with nothing
    resolvable the time is ``uncertain``, not a default."""
    w = await c.world(engine)
    story, cal, bobby, ann = w.story_id, w.people["Cal"], w.people["Bobby"], w.people["Ann"]
    kitchen = w.places["kitchen"]
    await c.commit(engine, story, c.at(cal, kitchen, 0, None))
    await c.commit(engine, story, c.at(bobby, kitchen, 0, None))
    sibling = Proposition(
        subject_id=bobby,
        predicate_id=CORE_BY_NAME["sibling_of"].id,
        object_id=ann,
        valid_time=Interval.of(None, None),
    )
    await c.commit(engine, story, sibling)
    birth = Instant.of(-30 * 365 * DAY, Granularity.DAY)
    await c.commit(
        engine,
        story,
        Proposition(subject_id=bobby, predicate_id=BORN, valid_time=birth),
    )
    op, _ = await c.run(
        engine, story, lambda conn, op: _nothing(), said="Bobby and Ann were at the game."
    )
    turn = int(op.narrative_turn or 0)
    position = (turn + 1, 0)
    text = "Those guys a month ago at the game: you were there with Bobby's sister."
    async with engine.connect() as conn:
        basis = await resolution.load_time_basis(conn, story)
        context = await resolution.load_context(conn, story, position=position, speaker=cal)
        read_set = StructuredReadSet()
        read_set.start_attempt()
        lookup = KeyedLookup(conn, read_set)
        bobby_timeline = await resolution.subject_timeline(lookup, story, bobby)
        ann_timeline = await resolution.subject_timeline(lookup, story, ann)
    # The time window, against the position's story-time.
    (expression,) = find_time_expressions(text)
    window = resolve_time(expression, position, basis=basis)
    assert isinstance(window, ResolvedTime)
    now = story_time_at(position, basis)
    assert window.valid_time.possible() == (now - 37 * DAY, now - 23 * DAY)
    assert window.approximate
    assert window.direction == "past"
    # The event type, then the participants.
    assert resolve_event_type(text) == "game"
    found = resolve_mentions(text, cal, context)
    assert [(m.kind, m.entity_id) for m in found] == [
        (MentionKind.GROUP, None),
        (MentionKind.SECOND_PERSON, bobby),
        (MentionKind.NAME, bobby),
        (MentionKind.DESCRIPTION, ann),
    ]
    assert context.present == frozenset({cal, bobby})
    assert any(b.text == "Bobby and Ann were at the game." for b in context.beats)
    # The subject's timeline through the keyed lookup: its reads are query keys.
    assert bobby_timeline == SubjectTimeline(born=birth)
    assert len(read_set.query_keys) == 4  # born and age, for Bobby and for Ann
    seven = resolve_time("when he was seven", position, bobby_timeline, basis=basis)
    assert isinstance(seven, ResolvedTime)
    assert seven.valid_time.possible()[0] == birth.at + 7 * 525_960
    # Complement: no anchor in canon for Ann, and an unresolvable expression.
    assert ann_timeline == SubjectTimeline()
    missing = resolve_time("when she was seven", position, ann_timeline, basis=basis)
    assert isinstance(missing, Uncertain)
    assert missing.reason.code is ReasonCode.NO_ANCHOR
    assert isinstance(resolve_time("once upon a time", position, basis=basis), Uncertain)


async def _nothing() -> None:
    return None


# -- retrieve(), on a database of its own ---------------------------------------------------------

QUERY = "Those guys a month ago at the game: you were there with Bobby's sister."
GAME_LINES = (
    ("game_bobby", "Bobby says: What a game that was, Ann!", "Bobby"),
    ("game_ann", "Ann does: cheers as Dan scores at the game.", "Ann"),
    ("game_dan", "Dan says: I nearly lost the game.", "Dan"),
)
Q_VECTOR = (1.0, 0.0, 0.0, 0.0)
NEAR = (0.9, 0.1, 0.0, 0.0)
FAR = (-1.0, 0.0, 0.0, 0.0)


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url, stub_embedding_dimension=r.DIMENSION) as s:
        await reset(s.db)
        await r.pin(s.db)
        yield s


@pytest.fixture(scope="module")
async def long_story(scratch: Scratch) -> r.Story:
    """S-THOSEGUYS's 5,000-turn fixture: 5,000 filler memories with the occasion planted."""
    story = await r.build_story(scratch.db, fillers=r.TURNS)
    await _plant_occasion(scratch.db, story)
    await r.set_vectors(scratch.db, story.story_id, NEAR)
    return story


async def _plant_occasion(db: AsyncEngine, story: r.Story) -> None:
    """The game a month ago (three beats: Bobby, Ann, Dan), and its distractors: a game in the
    window with only Cal and Dan, a game 10 days ago with Bobby and Ann; Cal's assessments of
    Dan and of Bobby, and one of Cal by Quillon, whom nobody asked about."""
    for beat, (name, line, actor) in enumerate(GAME_LINES, start=1):
        await r.memory(
            db,
            story,
            name,
            line,
            turn=r.MONTH_AGO,
            beat=beat,
            event_type="game",
            participants=(actor, *(p for p in ("Bobby", "Ann", "Dan") if p != actor)),
            importance=0.7,
        )
    await r.memory(
        db,
        story,
        "game_other",
        "Cal says: Dan, that game was rigged.",
        turn=r.MONTH_AGO + 300,
        event_type="game",
        participants=("Cal", "Dan"),
    )
    await r.memory(
        db,
        story,
        "game_recent",
        "Bobby says: Ann, the game tonight was dull.",
        turn=r.TURNS - 1_440,
        event_type="game",
        participants=("Bobby", "Ann"),
    )
    await r.memory(
        db,
        story,
        "assess_dan",
        "Cal thinks Dan cheated at the game.",
        kind=MemoryKind.SEMANTIC,
        turn=r.MONTH_AGO + 5,
        participants=("Cal", "Dan"),
    )
    await r.memory(
        db,
        story,
        "assess_bobby",
        "Cal believes Bobby is a good sport.",
        kind=MemoryKind.REFLECTION,
        turn=None,
        participants=("Cal", "Bobby"),
    )
    await r.memory(
        db,
        story,
        "assess_cal",
        "Quillon Vexby thinks Cal talks too much.",
        kind=MemoryKind.SEMANTIC,
        turn=r.MONTH_AGO + 5,
        participants=("Quillon", "Cal"),
    )


def _vector(q: Sequence[float] = Q_VECTOR) -> Callable[[str], Awaitable[Sequence[float]]]:
    async def embed(text: str) -> Sequence[float]:
        del text
        return q

    return embed


async def _retrieve(
    db: AsyncEngine, story_id: str, text: str, **kw: Any
) -> pipeline.RetrievalResult:
    async with db.begin() as conn:
        return await pipeline.retrieve(conn, story_id, pipeline.RetrievalQuery(text), **kw)


async def _settings(db: AsyncEngine, story_id: str, **episodic: Any) -> ProfileSettings:
    """The story's effective settings with ``memory.episodic`` dials changed."""
    async with db.connect() as conn:
        found = (await effective_configuration(conn, story_id)).settings
    document = found.model_dump(mode="json")
    document["memory"]["episodic"].update(episodic)
    return ProfileSettings.model_validate(document)


def _ids(found: Sequence[pipeline.RetrievedMemory]) -> list[str]:
    return [m.id for m in found]


@pytest.mark.part("M4", "FR-RET-2,3,6")
async def test_i_05(scratch: Scratch) -> None:
    """ "Those guys a month ago at the game: you were there with Bobby's sister." resolves the
    window (about 30 days back), the event type ``game`` and Bobby and Ann; the occasion query
    locates the game's three beats (not the in-window game without them, not last week's game
    with them), their participants (Bobby, Ann and Dan) resolve "those guys", and the
    participants' assessments come back with Bobby's and Ann's standing profiles (Bobby is
    Ann's sibling) and the canon about them. Complements: the same text without its time and
    occasion words skips the occasion step and locates nothing; an assessment about none of the
    participants is not returned; Cal, the speaker, gets no profile."""
    db = scratch.db
    story = await r.build_story(db)
    await _plant_occasion(db, story)
    ids, people = story.ids, story.people
    read_set = StructuredReadSet()
    read_set.start_attempt()
    found = await _retrieve(db, story.story_id, QUERY, read_set=read_set)

    resolution = found.resolution
    assert resolution.event_type == "game"
    assert set(resolution.entities) == {people["Bobby"], people["Ann"]}
    assert resolution.group
    assert found.turn_window is not None
    first, last = found.turn_window
    assert first is not None
    assert last is not None
    assert first <= r.MONTH_AGO <= last
    assert last < r.TURNS - 1_440 - 1  # last week's game is outside the window
    assert _ids(found.episodes) == [ids["game_dan"], ids["game_ann"], ids["game_bobby"]]
    assert set(found.participants) == {people["Bobby"], people["Ann"], people["Dan"]}
    assert set(_ids(found.assessments)) == {ids["assess_dan"], ids["assess_bobby"]}
    assert ids["assess_cal"] not in _ids((*found.assessments, *found.memories))
    assert ids["game_other"] not in _ids(found.episodes)
    assert ids["game_recent"] not in _ids(found.episodes)
    assert "occasion" not in found.skipped
    profiled = {p.entity.entity_id for p in found.profiles}
    assert profiled == {people["Bobby"], people["Ann"]}  # characters only; Dan is none
    bobby = next(p for p in found.profiles if p.entity.entity_id == people["Bobby"])
    assert any(
        rel.predicate == "sibling_of" and rel.other.entity_id == people["Ann"]
        for rel in bobby.relationships
    )
    assert people["Cal"] not in profiled
    assert any(f.subject_id == people["Bobby"] for f in found.canon)
    # The context: occasions, then profiles and canon, then ranked memories, within budget.
    tiers = [int(i.tier) for i in found.context.items]
    assert tiers == sorted(tiers)
    assert found.context.used <= found.context.budget == 3_000
    episode_items = [i.item_id for i in found.context.items if i.kind == "episode"]
    assert episode_items == _ids(found.episodes)
    # The read set records every returned memory, and the profiles, as context (never keys).
    retrieved = {(t, i) for t, i in read_set.retrieved}
    assert {("memories", m) for m in _ids((*found.episodes, *found.assessments))} <= retrieved
    assert ("profile", people["Bobby"]) in retrieved
    assert all(m.id not in _ids(found.memories) for m in found.episodes)
    assert set(found.timings) == {*pipeline.STAGES, "total"}
    # Complement: no time or occasion words, no occasion.
    plain = await _retrieve(db, story.story_id, "You were there with Bobby's sister.")
    assert "occasion" in plain.skipped
    assert plain.episodes == ()
    assert plain.assessments == ()


@pytest.mark.part("M4", "FR-RET-4")
async def test_i_06(scratch: Scratch) -> None:
    """A proper noun the vector arm misses is returned by the lexical arm: 60 memories sit
    nearer the query's vector than "Quillon Vexby lent Cal the lantern." (the vector arm's
    top 50 never reaches it), yet hybrid retrieval returns it, found by the lexical and alias
    arms. Complement: with ``memory.episodic.hybrid_search`` off (vector only) it is missing,
    and nothing returned was found by any arm but the vector one."""
    db = scratch.db
    story = await r.build_story(db, turns=200)
    for n in range(60):
        await r.memory(
            db,
            story,
            f"near{n}",
            f"Bobby says: line {n} about the cellar.",
            turn=n + 1,
            vector=NEAR,
        )
    target = await r.memory(
        db, story, "quillon", "Quillon Vexby lent Cal the lantern.", turn=100, vector=FAR
    )
    await r.set_vectors(db, story.story_id, NEAR)
    text = "What did Quillon Vexby lend me?"
    hybrid = await _retrieve(db, story.story_id, text, embed=_vector())
    assert target in _ids(hybrid.memories)
    found = next(m for m in hybrid.memories if m.id == target)
    assert "vector" not in found.arms
    assert {"lexical", "alias"} <= set(found.arms)
    assert hybrid.degradation == ()
    assert hybrid.model_calls == 1
    # Complement: the lexical arm off, the vector arm alone misses it.
    vector_only = await _retrieve(
        db,
        story.story_id,
        text,
        embed=_vector(),
        settings=await _settings(db, story.story_id, hybrid_search=False),
    )
    assert target not in _ids(vector_only.memories)
    assert vector_only.memories
    assert all(m.arms == ("vector",) for m in vector_only.memories)
    assert {"lexical", "alias"} <= set(vector_only.skipped)


@pytest.mark.part("M4", "FR-RET-2,3,6")
async def test_s_thoseguys(scratch: Scratch, long_story: r.Story) -> None:
    """S-THOSEGUYS on the 5,000-turn fixture (5,000 filler memories over 5,000 turns, every one
    with a vector, an occasion type and two participants): "those guys a month ago at the game"
    returns the game's episode, its participants and their assessments, as on a small story,
    with the distractors left out. The vector arm runs (the query is embedded once) without
    displacing the occasion."""
    story = long_story
    ids, people = story.ids, story.people
    found = await _retrieve(scratch.db, story.story_id, QUERY, embed=_vector())
    assert _ids(found.episodes) == [ids["game_dan"], ids["game_ann"], ids["game_bobby"]]
    assert set(found.participants) == {people["Bobby"], people["Ann"], people["Dan"]}
    assert set(_ids(found.assessments)) == {ids["assess_dan"], ids["assess_bobby"]}
    assert {p.entity.entity_id for p in found.profiles} == {people["Bobby"], people["Ann"]}
    assert ids["game_other"] not in _ids(found.episodes)
    assert ids["game_recent"] not in _ids(found.episodes)
    assert found.model_calls == 1
    assert found.degradation == ()
    assert len(found.memories) == 40  # Structured (M4)'s k
    assert all(m.narrative_turn is None or m.narrative_turn <= r.TURNS for m in found.memories)
    scores = [m.score for m in found.memories if m.score is not None]
    assert len(scores) == len(found.memories)
    assert scores == sorted(scores, reverse=True)
    assert found.context.used <= found.context.budget


async def test_rehydration_reads_retired_records_without_promoting_them(scratch: Scratch) -> None:
    """FR-MEM-8's rehydration: a retired episode in the occasion window and a retired memory
    the lexical lookup matches are returned, read from their rows; the lexically found one has
    no vector, so it is re-embedded in one batched call (``model_calls`` 2: the query and the
    batch), its similarity is ranked, and neither row's ``retrieval_tier`` changes or gains a
    vector. Complements: a dormant (truncated-away) memory is never returned, and without the
    batch embedder the retired memory is still returned lexically."""
    db = scratch.db
    story = await r.build_story(db, turns=r.TURNS)
    episode = await r.memory(
        db,
        story,
        "retired_game",
        "Bobby says: Ann, that game was the best one yet.",
        turn=r.MONTH_AGO,
        event_type="game",
        participants=("Bobby", "Ann"),
        tier=RetrievalTier.RETIRED,
    )
    lantern = await r.memory(
        db,
        story,
        "retired_lantern",
        "Quillon Vexby polished the brass lantern.",
        turn=300,
        tier=RetrievalTier.RETIRED,
    )
    dormant = await r.memory(db, story, "dormant", "Quillon Vexby buried the lantern.", turn=301)
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE memories SET membership = 'dormant' WHERE id = :i"), {"i": dormant}
        )
    await r.set_vectors(db, story.story_id, NEAR)
    batches: list[list[str]] = []

    async def embed_batch(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        batches.append(list(texts))
        return [Q_VECTOR for _ in texts]

    query = "Those guys a month ago at the game, and the lantern: you were there with Ann."
    found = await _retrieve(db, story.story_id, query, embed=_vector(), embed_batch=embed_batch)
    assert episode in _ids(found.episodes)
    assert lantern in _ids(found.memories)
    assert set(found.rehydrated) == {episode, lantern}
    assert dormant not in _ids((*found.episodes, *found.assessments, *found.memories))
    assert batches == [["Quillon Vexby polished the brass lantern."]]
    assert found.model_calls == 2
    ranked = next(m for m in found.memories if m.id == lantern)
    assert "vector" in ranked.arms
    assert ranked.similarity is not None
    rows = await r.access_and_tiers(db, [episode, lantern])
    assert rows[episode][1] == rows[lantern][1] == "retired"
    assert rows[episode][0] is not None  # a turn's retrieval touches what it returned
    stored = await vectors(db, story.story_id)
    assert stored[lantern].vector is None
    # Complement: no batch embedder, still returned (lexically).
    lexical = await _retrieve(db, story.story_id, query, embed=_vector(), touch=False)
    assert lantern in _ids(lexical.memories)
    assert lexical.model_calls == 1


async def test_a_lite_story_retrieves_nothing(scratch: Scratch) -> None:
    """The gate: a story whose ``episodic`` module is off (Lite) reads its configuration and
    nothing else: no memory, no entity, no projection, no canon; the result is empty with
    ``skipped: ["episodic"]``."""
    db = scratch.db
    w = await c.world(db)
    with r.statements(db) as seen:
        found = await _retrieve(db, w.story_id, QUERY, embed=_vector())
    assert found.skipped == ("episodic",)
    assert found.episodes == found.memories == ()
    assert found.profiles == ()
    assert found.context.items == ()
    for table in ("memories", "entities", "facts", "relationships", "placement", "events"):
        assert seen.reading(table) == [], table


async def test_retrieval_cost_is_fixed_and_indexed(scratch: Scratch, long_story: r.Story) -> None:
    """NFR-PERF-1's implementation, without timing anything: the same query on the same cast
    issues the same statements on a story of a few memories and on the 5,000-turn fixture (its
    cost follows what it resolves, not the story's size), and every statement that reads
    ``memories`` is served by an index (``EXPLAIN`` with table scans priced out shows no "Seq
    Scan" of ``memories``; the vector arm reads the HNSW index), and the GIN indexes on
    ``search`` and ``participants`` serve the lexical, alias and occasion arms' operators."""
    db = scratch.db
    small = await r.build_story(db)
    await _plant_occasion(db, small)
    await r.set_vectors(db, small.story_id, NEAR)

    async def measure(story: r.Story) -> r.Seen:
        with r.statements(db) as seen:
            await _retrieve(db, story.story_id, QUERY, embed=_vector(), touch=False)
        return seen

    few, many = await measure(small), await measure(long_story)
    assert len(few) == len(many)
    reads = many.reading("memories")
    assert len(reads) >= 5
    plans = [await r.plan_of(db, sql, params) for sql, params in reads]
    for (sql, _), plan in zip(reads, plans, strict=True):
        assert "Seq Scan on memories" not in plan, (sql, plan)
    assert "ix_memories_embedding_hnsw" in "\n".join(plans)
    # The lexical, alias and occasion arms' operators are served by their GIN indexes (on this
    # small table the planner reaches the story's rows by its b-tree first; at 50k memories a
    # story the GIN indexes are what narrows them).
    for sql, index in (
        ("SELECT id FROM memories WHERE search @@ to_tsquery('english', 'quillon')", "search"),
        (
            "SELECT id FROM memories WHERE search @@ phraseto_tsquery('english', 'Quillon Vexby')",
            "search",
        ),
        ("SELECT id FROM memories WHERE participants && ARRAY[%(p)s]::char(26)[]", "participants"),
    ):
        plan = await r.plan_of(db, sql, {"p": long_story.people["Bobby"]})
        assert f"ix_memories_{index}" in plan, plan


def _index_conditions(plan: str) -> str:
    return "\n".join(line for line in plan.splitlines() if "Index Cond" in line)


async def test_the_retired_lexical_search_reads_the_tier_index(
    scratch: Scratch, long_story: r.Story
) -> None:
    """#451 (NFR-PERF-1): rehydration's lexical search of the retired tier reaches its rows
    through the story's ``(story_id, membership, retrieval_tier)`` index: the tier is an index
    condition, so the search reads the retired rows, not every row holding one of the query's
    words and then drops the hot ones (which made it cost as much as the hot search).

    Complement asserted: the same statement with the tier cast to text (the shape before #451)
    has no tier index condition."""
    db = scratch.db
    with r.statements(db) as seen:
        await _retrieve(db, long_story.story_id, QUERY, touch=False)
    retired = [
        (sql, params)
        for sql, params in seen.reading("memories")
        if "ts_rank" in sql and params.get("tiers") == [RetrievalTier.RETIRED.value]
    ]
    assert len(retired) == 1, [p.get("tiers") for _, p in seen.reading("memories")]
    sql, params = retired[0]
    async with db.begin() as conn:  # the planner's statistics, as autovacuum keeps them
        await conn.execute(text("ANALYZE memories"))
    plan = await r.plan_of(db, sql, params)
    assert "retrieval_tier" in _index_conditions(plan), plan
    cast_text = sql.replace(
        "m.retrieval_tier = ANY(CAST(%(tiers)s AS retrieval_tier[]))",
        "m.retrieval_tier::text = ANY(CAST(%(tiers)s AS text[]))",
    )
    assert cast_text != sql
    assert "retrieval_tier" not in _index_conditions(await r.plan_of(db, cast_text, params))


async def test_the_pins_source_reads_the_pinned_index(
    scratch: Scratch, long_story: r.Story
) -> None:
    """#451 (NFR-PERF-1): the pins context source (#147), which every retrieval runs, reaches
    the story's pinned memories through the partial index ``ix_memories_pinned`` (``WHERE
    pinned``), so it reads the pins, not every memory of the story. At 50k memories the
    ``IS TRUE`` form was a 25 ms scan of every row of the story on each retrieval.

    Complement asserted: the same statement with ``pinned IS true`` cannot use the index."""
    db = scratch.db
    with r.statements(db) as seen:
        await _retrieve(db, long_story.story_id, QUERY, touch=False)
    pins = [
        (sql, params)
        for sql, params in seen.reading("memories")
        if "memories.pinned" in sql and "importance_at" in sql
    ]
    assert len(pins) == 1, len(pins)
    sql, params = pins[0]

    async def plan_of(statement: str) -> str:
        """``EXPLAIN`` with fresh statistics and table scans priced out (sorts allowed:
        the statement orders by id, and pricing sorts out would pick the primary key)."""
        async with db.connect() as conn:
            await conn.execute(text("ANALYZE memories"))
            await conn.execute(text("SET enable_seqscan = off"))
            raw = await conn.get_raw_connection()
            cursor = raw.driver_connection.cursor()  # type: ignore[union-attr]
            await cursor.execute("EXPLAIN " + statement, params)
            rows = await cursor.fetchall()
            await conn.rollback()
        return "\n".join(str(row[0]) for row in rows)

    plan = await plan_of(sql)
    assert "ix_memories_pinned" in plan, plan
    is_true = sql.replace("memories.pinned AND", "memories.pinned IS true AND")
    assert is_true != sql
    assert "ix_memories_pinned" not in await plan_of(is_true)


async def test_the_debugging_read_is_omniscient_only(scratch: Scratch) -> None:
    """``GET /stories/{id}/retrieve`` answers the library's result for ``omniscient`` and
    touches no memory; a character viewpoint is 403 ``omniscient_required``; ``as_of`` beyond
    the head is 422; an unknown story 404."""
    db, client = scratch.db, scratch.client
    story = await r.build_story(db)
    await _plant_occasion(db, story)
    headers = {**bearer(), "X-Epistrel-Principal": c.PRINCIPAL}
    path = f"/stories/{story.story_id}/retrieve"
    params = {"q": QUERY, "viewpoint": "omniscient"}
    response = await client.get(path, params=params, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["id"] for e in body["episodes"]] == [
        story.ids["game_dan"],
        story.ids["game_ann"],
        story.ids["game_bobby"],
    ]
    assert body["resolution"]["event_type"] == "game"
    assert set(body["timings"]) == {*pipeline.STAGES, "total"}
    library = await scratch.lib.retrieve(CTX, story.story_id, q=QUERY, viewpoint="omniscient")
    assert [m.id for m in library.episodes] == [e["id"] for e in body["episodes"]]
    touched = await r.access_and_tiers(db, list(story.ids.values()))
    assert all(at is None for at, _ in touched.values())
    async with db.connect() as conn:
        cast = list(
            await conn.scalars(
                text("SELECT id FROM characters WHERE story_id = :s"), {"s": story.story_id}
            )
        )
    as_character = await client.get(
        path, params={"q": QUERY, "viewpoint": f"character:{cast[0]}"}, headers=headers
    )
    assert as_character.status_code == 403
    assert as_character.json()["error"]["code"] == "omniscient_required"
    beyond = await client.get(path, params={**params, "as_of": r.TURNS + 5}, headers=headers)
    assert beyond.status_code == 422
    missing = await client.get(
        "/stories/01J9Z8Q4N5V6W7X8Y9Z0A1B2C9/retrieve", params=params, headers=headers
    )
    assert missing.status_code == 404


async def test_context_sources_fill_tier_one(scratch: Scratch) -> None:
    """The ``context_sources`` hook (#146's ledger items and #147's pins register on it): its
    items open the context, ledger items before pins, and the pins together stay within 40% of
    the budget (one over the share is dropped as ``pin_cap``); the source is asked with the
    query's position, viewpoint and resolved entities. Complement: a source answering an item
    of another tier is refused."""
    db = scratch.db
    story = await r.build_story(db)
    await _plant_occasion(db, story)
    asked: list[pipeline.SourceRequest] = []

    async def source(conn: Any, request: pipeline.SourceRequest) -> list[ContextItem]:
        del conn
        asked.append(request)
        return [
            ContextItem(Tier.LEDGER_AND_PINS, "pin", "pin-1", "p" * 4_000),  # 1,000 tokens
            ContextItem(Tier.LEDGER_AND_PINS, "pin", "pin-2", "q" * 1_200),  # 300: over 1,200
            ContextItem(Tier.LEDGER_AND_PINS, "ledger", "debt", "Cal owes Bobby a game."),
        ]

    pipeline.register_context_source(source)
    try:
        found = await _retrieve(db, story.story_id, QUERY)
    finally:
        pipeline.remove_context_source(source)
    (request,) = asked
    assert request.position == (r.TURNS + 1, 0)
    assert request.viewpoint == "omniscient"
    assert set(request.entities) == {story.people["Bobby"], story.people["Ann"]}
    kinds = [(i.kind, i.item_id) for i in found.context.items][:2]
    assert kinds == [("ledger", "debt"), ("pin", "pin-1")]
    assert ("pin-2", "pin_cap") in {(d.item_id, d.reason) for d in found.context.dropped}
    assert found.context.used <= 3_000

    async def wrong(conn: Any, request: pipeline.SourceRequest) -> list[ContextItem]:
        del conn, request
        return [ContextItem(Tier.MEMORIES, "memory", "x", "x")]

    pipeline.register_context_source(wrong)
    try:
        with pytest.raises(ValueError, match="tier-1"):
            await _retrieve(db, story.story_id, QUERY)
    finally:
        pipeline.remove_context_source(wrong)


async def _with_people(db: AsyncEngine, story: r.Story, n: int) -> None:
    """``n`` more person entities in ``story``, none named in ``QUERY``."""
    async with db.begin() as conn:
        for i in range(n):
            await store.add_entity(conn, story.story_id, "person", f"Extra Person {i:03d}")


async def _person_count(db: AsyncEngine, story_id: str) -> int:
    async with db.connect() as conn:
        found = await conn.scalar(
            text("SELECT count(*) FROM entities WHERE story_id = :s AND kind = 'person'"),
            {"s": story_id},
        )
    return int(found or 0)


async def test_resolve_cost_does_not_grow_with_the_cast(scratch: Scratch) -> None:
    """#442 (NFR-PERF-1): the resolve stage reads every person's relationships in one keyed read,
    so the same query issues the same statements over a cast of 5 people and over one of 200,
    both in ``load_context`` alone and in the whole retrieval. The registry is loaded once
    (one full read of ``predicates``) and the people's relations are one read of ``relationships``
    (the scene's ``present_at`` rows are the other), whatever the cast.

    Complement asserted: the 200-person story really has 200 people, the extra people are in
    ``load_context``'s index, and Bobby's sibling relation is still found in both."""
    db = scratch.db
    small = await r.build_story(db)
    big = await r.build_story(db)
    await _with_people(db, big, 195)
    assert await _person_count(db, small.story_id) == 5
    assert await _person_count(db, big.story_id) == 200
    for story in (small, big):
        await _plant_occasion(db, story)
        await r.set_vectors(db, story.story_id, NEAR)

    async def resolve(story: r.Story) -> tuple[r.Seen, Any]:
        with r.statements(db) as seen:
            async with db.connect() as conn:
                found = await resolution.load_context(
                    conn, story.story_id, position=(r.TURNS, 0), speaker=story.people["Cal"]
                )
        return seen, found

    (few, few_context), (many, many_context) = await resolve(small), await resolve(big)
    assert len(few) == len(many), (len(few), len(many))
    # ``relationships`` is read twice: the scene's ``present_at`` rows and the people's relations.
    assert len(many.reading("relationships")) == 2
    # The registry is loaded once (the other ``predicates`` read is the scene's predicate ids).
    loads = [q for q, _ in many.reading("predicates") if "predicates.projection =" not in q]
    assert len(loads) == 1
    assert sum(e.kind == "person" for e in many_context.index.entries.values()) == 200
    for story, context in ((small, few_context), (big, many_context)):
        bobby, ann = story.people["Bobby"], story.people["Ann"]
        assert Relation(bobby, "sibling_of", ann) in context.relations
        assert Relation(ann, "sibling_of", bobby) in context.relations

    async def measure(story: r.Story) -> r.Seen:
        with r.statements(db) as seen:
            await _retrieve(db, story.story_id, QUERY, embed=_vector(), touch=False)
        return seen

    whole_few, whole_many = await measure(small), await measure(big)
    assert len(whole_few) == len(whole_many), (len(whole_few), len(whole_many))


async def _relations_per_profile(conn: Any, story_id: str, as_of: int) -> list[Relation]:
    """The relations as ``load_context`` built them before #442: one standing profile per
    person, in the index's order, deduplicated (the reference the keyed read must equal)."""
    index = await resolution.load_index(conn, story_id)
    lookup = KeyedLookup(conn, StructuredReadSet())
    relations: list[Relation] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in index.entries.values():
        if entry.kind != "person":
            continue
        profile = await lookup.profile(story_id, entry.id, as_of=as_of, viewpoint="omniscient")
        for rel in profile.relationships:
            subject, obj = (
                (entry.id, rel.other.entity_id)
                if rel.direction == "out"
                else (rel.other.entity_id, entry.id)
            )
            if (subject, rel.predicate, obj) not in seen:
                seen.add((subject, rel.predicate, obj))
                relations.append(Relation(subject, rel.predicate, obj))
    return relations


async def test_resolve_relations_equal_the_per_profile_read(engine: AsyncEngine) -> None:
    """#442: ``load_context``'s one keyed read of the relationships answers, at every turn of
    the story, exactly the relations (and their order) that one profile per person did: a
    symmetric pair (both directions), an inverse stored as its canonical predicate, a person's
    relation to an organization, an organization's relation to a person, a relation between
    two non-people (in neither), a relation closed by a later negation (present before it,
    gone after) and one whose valid time has ended.

    Complement asserted: the comparison is not vacuous: the relations differ between turns and
    hold each of the shapes above where expected."""
    w = await c.world(engine)
    story, bobby, ann, cal = w.story_id, w.people["Bobby"], w.people["Ann"], w.people["Cal"]
    async with engine.begin() as conn:
        dan = await store.add_entity(conn, story, "person", "Dan")
        guild = await store.add_entity(conn, story, "organization", "the Guild")

    def rel(subject: str, name: str, obj: str, lo: int | None = None, hi: int | None = None) -> Any:
        return Proposition(
            subject_id=subject,
            predicate_id=CORE_BY_NAME[name].id,
            object_id=obj,
            valid_time=Interval.of(lo, hi),
        )

    await c.commit(engine, story, rel(bobby, "sibling_of", ann))
    await c.commit(engine, story, rel(bobby, "child_of", ann))
    await c.commit(engine, story, rel(dan, "member_of", guild))
    await c.commit(engine, story, rel(guild, "enemy_of", cal))
    await c.commit(engine, story, rel(guild, "enemy_of", w.places["kitchen"]))
    await c.commit(engine, story, rel(cal, "knows", dan, None, -(10**9)))
    friends = await c.commit(engine, story, rel(cal, "friend_of", bobby))
    negated = rel(cal, "friend_of", bobby)
    await c.supersede(
        engine, story, friends.id, negated.model_copy(update={"polarity": Polarity.NEGATED})
    )
    async with engine.connect() as conn:
        head = (
            int(
                await conn.scalar(
                    text("SELECT next_turn_index FROM stories WHERE id = :s"), {"s": story}
                )
            )
            - 1
        )
        by_turn: dict[int, list[Relation]] = {}
        for turn in range(head + 1):
            context = await resolution.load_context(conn, story, position=(turn, 0), speaker=cal)
            assert list(context.relations) == await _relations_per_profile(conn, story, turn)
            by_turn[turn] = list(context.relations)
    at_head = by_turn[head]
    assert Relation(bobby, "sibling_of", ann) in at_head
    assert Relation(ann, "sibling_of", bobby) in at_head
    assert Relation(ann, "parent_of", bobby) in at_head
    assert Relation(dan, "member_of", guild) in at_head
    assert Relation(guild, "enemy_of", cal) in at_head
    assert all(w.places["kitchen"] not in (x.subject, x.object) for x in at_head)
    assert all(x.predicate != "knows" for x in at_head)
    assert all(x.predicate != "friend_of" for x in at_head)
    assert any(x.predicate == "friend_of" for x in by_turn[head - 1])
    assert len({tuple(v) for v in by_turn.values()}) > 2


async def _with_canon(db: AsyncEngine, story: r.Story, n: int) -> None:
    """``n`` more people in ``story``, none named in a query, and ``2n`` relationships among
    them (each knows the next and is the friend of the one after), committed in one turn: a
    canon that grows without touching anyone a query names."""
    async with db.begin() as conn:
        extra = [
            await store.add_entity(conn, story.story_id, "person", f"Extra Person {i:03d}")
            for i in range(n)
        ]

    async def write(conn: Any, op: Any) -> None:
        for i, person in enumerate(extra):
            for name, other in (("knows", extra[(i + 1) % n]), ("friend_of", extra[(i + 2) % n])):
                proposition = Proposition(
                    subject_id=person,
                    predicate_id=CORE_BY_NAME[name].id,
                    object_id=other,
                    valid_time=Interval.of(None, None),
                )
                await canon_facts.commit_fact(
                    conn,
                    story.story_id,
                    proposition,
                    operation=op,
                    provenance=RecordProvenance.RENDER,
                )

    await c.run(db, story.story_id, write)


async def _head_of(db: AsyncEngine, story_id: str) -> int:
    async with db.connect() as conn:
        found = await conn.scalar(
            text("SELECT next_turn_index FROM stories WHERE id = :s"), {"s": story_id}
        )
    return int(found or 0) - 1


async def test_resolve_reads_only_the_relationships_its_text_needs(scratch: Scratch) -> None:
    """#451 (NFR-PERF-1): with the text to resolve, ``load_context`` reads the relationships of
    the possessors its relational descriptions name, not every person's, so neither the
    statements nor the rows read grow with the canon. A story whose canon has 400 more
    relationships among 200 more people reads exactly what the plain story reads: no
    relationship rows besides the scene's for a text with no relation noun, and only Bobby's
    for "Bobby's sister". The whole retrieval reads the same ``relationships`` rows on both.

    Complement asserted: the comparison sees the canon: without the text (every person's
    relationships, #442's read) the big story reads over 400 rows more than the small one; and
    "Bobby's sister" still resolves to Ann on both."""
    db = scratch.db
    small = await r.build_story(db)
    big = await r.build_story(db, before_head=lambda db, story: _with_canon(db, story, 200))
    for story in (small, big):
        await _plant_occasion(db, story)
        await r.set_vectors(db, story.story_id, NEAR)

    async def context_of(story: r.Story, text: str | None) -> tuple[r.Seen, Any]:
        position = (await _head_of(db, story.story_id) + 1, 0)
        with r.statements(db) as seen:
            async with db.connect() as conn:
                found = await resolution.load_context(
                    conn,
                    story.story_id,
                    position=position,
                    speaker=story.people["Cal"],
                    text=text,
                )
        return seen, found

    plain = "Those guys a month ago at the game."
    (few, _), (many, _) = await context_of(small, plain), await context_of(big, plain)
    assert len(few) == len(many), (len(few), len(many))
    assert len(many.reading("relationships")) == 1  # the scene's present_at rows only
    assert few.rows_reading("relationships") == many.rows_reading("relationships")

    (few, few_context), (many, many_context) = (
        await context_of(small, QUERY),
        await context_of(big, QUERY),
    )
    assert len(few) == len(many), (len(few), len(many))
    assert len(many.reading("relationships")) == 2  # the scene's, then Bobby's
    assert few.rows_reading("relationships") == many.rows_reading("relationships")
    for story, context in ((small, few_context), (big, many_context)):
        found = resolve_mentions(QUERY, story.people["Cal"], context)
        assert found[-1].entity_id == story.people["Ann"]

    async def whole(story: r.Story) -> r.Seen:
        with r.statements(db) as seen:
            await _retrieve(db, story.story_id, QUERY, embed=_vector(), touch=False)
        return seen

    whole_few, whole_many = await whole(small), await whole(big)
    assert len(whole_few) == len(whole_many), (len(whole_few), len(whole_many))
    assert whole_few.rows_reading("relationships") == whole_many.rows_reading("relationships")

    # Complement: every person's relationships (no text) read the big canon.
    (few, _), (many, _) = await context_of(small, None), await context_of(big, None)
    assert many.rows_reading("relationships") >= few.rows_reading("relationships") + 400


SCOPED_TEXTS = (
    "Bobby's sister was at the game.",
    "Ann's brother's friend came along.",
    "Did he see his sister there?",
    "The Guild's enemy and Dan's friend left early.",
    "Cal's wife and her mother stayed home.",
    "Bobby, Ann and Dan were at the game.",
    "His father's father was there too.",
)


async def test_scoped_relations_resolve_as_every_persons_do(engine: AsyncEngine) -> None:
    """#451: resolving a text against ``load_context(text=…)`` (the relationships of the
    possessors it names, closed over chained possessors) answers exactly the mentions it
    answers against every person's relationships, at every turn: a possessive name, a chained
    possessor ("Ann's brother's friend"), a pronoun possessor, an organization possessor (its
    relation to a person counts, its relation to a place does not), a missing relation, a text
    with no relation noun.

    Complement asserted: the comparison is not vacuous: the chained description resolves only
    through its second hop, and the scoped contexts hold fewer relations than the full one."""
    w = await c.world(engine)
    story, bobby, ann, cal = w.story_id, w.people["Bobby"], w.people["Ann"], w.people["Cal"]
    async with engine.begin() as conn:
        dan = await store.add_entity(conn, story, "person", "Dan")
        guild = await store.add_entity(conn, story, "organization", "the Guild")

    def rel(subject: str, name: str, obj: str) -> Any:
        return Proposition(
            subject_id=subject,
            predicate_id=CORE_BY_NAME[name].id,
            object_id=obj,
            valid_time=Interval.of(None, None),
        )

    await c.commit(engine, story, rel(bobby, "sibling_of", ann))
    await c.commit(engine, story, rel(bobby, "friend_of", dan))
    await c.commit(engine, story, rel(guild, "enemy_of", cal))
    await c.commit(engine, story, rel(guild, "enemy_of", w.places["kitchen"]))
    await c.commit(engine, story, rel(dan, "friend_of", cal))
    await c.run(engine, story, lambda conn, op: _nothing(), said="Bobby waved.")
    head = await _head_of(engine, story)
    chained = None
    async with engine.connect() as conn:
        for turn in range(head + 2):
            full = await resolution.load_context(conn, story, position=(turn, 0), speaker=cal)
            for said in SCOPED_TEXTS:
                scoped = await resolution.load_context(
                    conn, story, position=(turn, 0), speaker=cal, text=said
                )
                assert resolve_mentions(said, cal, scoped) == resolve_mentions(said, cal, full), (
                    turn,
                    said,
                )
                assert set(scoped.relations) <= set(full.relations)
                if turn == head + 1 and said == SCOPED_TEXTS[1]:
                    chained = (scoped, full)
    assert chained is not None
    scoped, full = chained
    mentions = resolve_mentions(SCOPED_TEXTS[1], cal, scoped)
    assert [m.entity_id for m in mentions] == [ann, bobby, dan]
    assert len(scoped.relations) < len(full.relations)


async def _profile_world(engine: AsyncEngine) -> tuple[c.World, dict[str, str]]:
    """Cal, Bobby, Ann, Dan and Eve in the kitchen, which is in the school; Bobby holds the
    lamp; Bobby and Ann are siblings, Dan knows Eve, the Guild is Cal's enemy. Then Ann moves to
    the garden and the sibling relation is negated."""
    w = await c.world(engine)
    story = w.story_id
    async with engine.begin() as conn:
        more = {
            "Dan": await store.add_entity(conn, story, "person", "Dan"),
            "Eve": await store.add_entity(conn, story, "person", "Eve"),
            "lamp": await store.add_entity(conn, story, "item", "lamp"),
            "Guild": await store.add_entity(conn, story, "organization", "the Guild"),
        }
    people = {**w.people, **more}
    kitchen, school, garden = w.places["kitchen"], w.places["school"], w.places["garden"]

    def rel(subject: str, name: str, obj: str) -> Proposition:
        return Proposition(
            subject_id=subject,
            predicate_id=CORE_BY_NAME[name].id,
            object_id=obj,
            valid_time=Interval.of(None, None),
        )

    await c.commit(engine, story, c.at(kitchen, school, None, None, predicate=LOCATED_IN))
    for who in ("Cal", "Bobby", "Ann", "Dan", "Eve"):
        await c.commit(engine, story, c.at(people[who], kitchen, 0, None))
    await c.commit(engine, story, rel(people["Bobby"], "holds", more["lamp"]))
    sibling = await c.commit(engine, story, rel(people["Bobby"], "sibling_of", people["Ann"]))
    await c.commit(engine, story, rel(more["Dan"], "knows", more["Eve"]))
    await c.commit(engine, story, rel(more["Guild"], "enemy_of", people["Cal"]))
    await c.commit(engine, story, c.at(people["Ann"], garden, 0, None))
    negated = rel(people["Bobby"], "sibling_of", people["Ann"])
    await c.supersede(
        engine, story, sibling.id, negated.model_copy(update={"polarity": Polarity.NEGATED})
    )
    return w, people


async def test_profiles_equal_one_profile_each(engine: AsyncEngine) -> None:
    """#451: ``profile_views`` (the batched read the retrieval's profiles stage uses) answers,
    at every turn, exactly what ``profile_view`` answers for each entity: relationships both
    ways, a placement with its containing places, an inventory, a holder that moved, a
    relation that was negated; an id that is not the story's is left out. ``KeyedLookup.profiles``
    records each profile in the read set exactly as ``KeyedLookup.profile`` does.

    Complement asserted: the comparison is not vacuous: the profiles differ between turns, and
    one holds a placement with a containing place and an inventory."""
    w, people = await _profile_world(engine)
    story = w.story_id
    ids = [*people.values(), *w.places.values(), "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"]
    head = await _head_of(engine, story)
    by_turn = []
    async with engine.connect() as conn:
        for turn in range(head + 1):
            batched = await structured.profile_views(
                conn, story, ids, as_of=turn, viewpoint="omniscient"
            )
            one_each = {
                e: await structured.profile_view(conn, story, e, as_of=turn, viewpoint="omniscient")
                for e in ids[:-1]
            }
            assert batched == one_each, turn
            by_turn.append(batched)
        batch_set, single_set = StructuredReadSet(), StructuredReadSet()
        found = await KeyedLookup(conn, batch_set).profiles(story, ids, as_of=head)
        for e in ids[:-1]:
            await KeyedLookup(conn, single_set).profile(story, e, as_of=head)
    assert [p.entity.entity_id for p in found] == ids[:-1]
    assert batch_set.retrieved == single_set.retrieved
    assert len({repr(v) for v in by_turn}) > 2
    bobby = by_turn[head][people["Bobby"]]
    assert bobby.placement is not None
    assert [p.entity_id for p in bobby.placement.within] == [w.places["school"]]
    assert [i.item.entity_id for i in bobby.inventory] == [people["lamp"]]


async def test_profiles_cost_does_not_grow_with_the_subjects(engine: AsyncEngine) -> None:
    """#451 (NFR-PERF-1): the profiles of five people in one place cost the statements one
    person's does (the read context, the entities, the registry once, the relationships and
    placement rows, the place's containing places, the refs), where one profile each cost
    five times as many.

    Complement asserted: one profile each really does grow with the subjects."""
    w, people = await _profile_world(engine)
    story = w.story_id
    one = [people["Cal"]]
    five = [people[n] for n in ("Cal", "Bobby", "Dan", "Eve", "Guild")]

    async def batched(ids: list[str]) -> r.Seen:
        with r.statements(engine) as seen:
            async with engine.connect() as conn:
                await KeyedLookup(conn, StructuredReadSet()).profiles(story, ids)
        return seen

    async def one_each(ids: list[str]) -> r.Seen:
        with r.statements(engine) as seen:
            async with engine.connect() as conn:
                lookup = KeyedLookup(conn, StructuredReadSet())
                for e in ids:
                    await lookup.profile(story, e)
        return seen

    few, many = await batched(one), await batched(five)
    assert len(few) == len(many), (len(few), len(many))
    # The registry is loaded once (the other ``predicates`` reads are the canon's joins).
    loads = [
        q for q, _ in many.reading("predicates") if q.lstrip().startswith("SELECT predicates.")
    ]
    assert len(loads) == 1
    assert len(await one_each(five)) >= 4 * len(await one_each(one))
