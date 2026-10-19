"""I-16, I-15 and S-RETENTION's M4 parts, and what they stand on (#147; FR-MEM-8, NFR-SCALE-2,
FR-STORE-30, NFR-DETERM-1).

Every I-16 (M4), I-15 (M4) and S-RETENTION (M4) part lives in this file (traceability counts a
test ID per milestone in one file):

- ``test_i_16__repeated_bursts``: an overflow episode's deadline is fixed at its first
  over-budget commit, a set back within budget closes it, a later burst opens a new one, and a
  continuous stream is still retired at the first deadline;
- ``test_i_16__ranking``: retirement prefers covered and below-floor records, then the lowest
  decayed importance and the oldest access; uncovered records go only at the deadline, as
  ``retired_uncovered``; vectors go unless ``retain_retired_vectors``; pins never go;
- ``test_i_16__all_non_retirable``: 200 pins and 3,000 unpinned records above the floor, none
  covered: bounded at budget + pin ceiling after the lag, never more;
- ``test_i_16__pin_admission``: the 201st pin on one character, the 2,001st across twelve, and
  the text pins sharing the story's ceiling;
- ``test_i_16__story_vectors``: the story's vector budget with the embed job stamping, the
  stub's consolidation unavailable, the overflow gauge and the reconcile;
- ``test_i_16__retired_stay_active``: history views, keyed lookups, rehydration without a
  return to the hot set, and the ledger delivering what retired records mention;
- ``test_i_15`` and ``test_s_retention__vectors``: a dormant continuation's vectors.

These run on a database of their own (``embedding_support.embedding_db``): the first real
embedding pins the deployment's dimension, a schema change the shared database must never see.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, text

from epistrel.core import mutation
from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.db import database_url
from epistrel.core.enums import OpenSlot
from epistrel.core.history import read_history
from epistrel.core.memory import pins, records, retirement
from epistrel.core.profiles.effective import effective_configuration
from epistrel.core.propositions import registry as predicate_registry
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.retrieval import pipeline
from epistrel.observability import metrics
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers.consolidation import CONSOLIDATION
from epistrel.orchestrator.jobs.handlers.reembed import EMBED
from epistrel.orchestrator.jobs.handlers.retirement import RECONCILE
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.testing.determinism import OPERATIONAL_TABLES, differences
from tests import canon_support as c
from tests import consolidation_support as cs
from tests import hot_set_support as h
from tests import retrieval_support as r
from tests.embedding_support import Scratch, embedding_db, pin_of, reset, say, vectors
from tests.event_support import new_story
from tests.fork_support import fork
from tests.memory_support import turn
from tests.profile_support import store_overrides
from tests.projection_support import swap, truncate
from tests.story_support import WRITE

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def scratch(pg_url: str) -> AsyncIterator[Scratch]:
    async with embedding_db(pg_url) as s:
        yield s


@pytest.fixture
async def s(scratch: Scratch) -> Scratch:
    await reset(scratch.db)
    async with scratch.db.begin() as conn:
        await conn.execute(text("DELETE FROM overflow_episodes"))
    scratch.lib.models.embeddings.stub.reset()
    scratch.lib.models.stub.reset()
    return scratch


def _counter(counter: Any) -> float:
    return float(counter._value.get())


def _open(found: list[retirement.Episode], scope_id: str) -> list[retirement.Episode]:
    return [e for e in found if e.scope_id == scope_id]


@pytest.mark.part("M4", "FR-MEM-8; NFR-SCALE-2")
async def test_i_16__repeated_bursts(s: Scratch) -> None:
    """I-16's repeated bursts, with Bobby's budget lowered to 10 and the lag 50: a burst of 15
    at turn 100 opens episode 1 (deadline 150); more at 120 and 140 leave the deadline at 150;
    nothing is retirable (every record above the floor, none covered), so the deadline retires
    the lowest-ranked uncovered records in turn 150's own commit and the episode closes there,
    ``forced``, with Bobby back at exactly his budget; a burst at 160 opens episode 2 with
    deadline 210. Ann's continuous stream (one more record every turn from 100 on) is never
    back within budget, so she has one episode until the deadline retires her records at 150,
    and the next one opens at 151. Complement: before the deadline the retirement job retires
    nothing and the episode stays open."""
    db = s.db
    w = await h.story(db, memories_per_character=10, retirement_max_lag=50)
    story, bobby, ann = w.story_id, w.characters["Bobby"], w.characters["Ann"]
    await h.held(db, story, ann, 10)
    await h.narrate_to(db, story, 99)
    assert await h.episodes(db, story) == []

    opened_at, _ = await h.burst(db, story, {bobby: 15, ann: 1})
    assert opened_at == 100
    (first,) = _open(await h.episodes(db, story), bobby)
    assert (first.opened_turn, first.deadline_turn, first.excess_at_open) == (100, 150, 5)
    jobs = {j.coalesce_key for j in await h.queued(db, h.RETIREMENT, story)}
    assert f"{h.RETIREMENT}:{first.id}" in jobs
    assert {str(o) for o in await h.run(db, h.RETIREMENT, story)} == {"committed"}
    assert {r.retrieval_tier for r in (await h.memory_rows(db, story)).values()} == {"hot"}
    (still,) = _open(await h.episodes(db, story), bobby)
    assert still.open
    assert still.excess == 5

    for head in range(101, 151):
        await h.burst(
            db, story, {ann: 1, **({bobby: 3 if head == 120 else 2} if head in (120, 140) else {})}
        )
        bob_episodes = _open(await h.episodes(db, story), bobby)
        assert [e.deadline_turn for e in bob_episodes] == [150]
        ann_episodes = _open(await h.episodes(db, story), ann)
        assert [e.deadline_turn for e in ann_episodes] == [150]
        if head < 150:
            assert bob_episodes[0].open
            assert ann_episodes[0].open
    (closed,) = _open(await h.episodes(db, story), bobby)
    assert (closed.closed_turn, closed.closed_reason) == (150, retirement.FORCED)
    left = await h.counts(db, story)
    assert left.characters[bobby] == 10
    assert left.characters[ann] == 10
    retired = [r for r in (await h.memory_rows(db, story)).values() if r.retired_turn]
    assert {r.retired_reason for r in retired} == {retirement.UNCOVERED}
    assert {r.retired_turn for r in retired} == {150}
    (ann_first,) = _open(await h.episodes(db, story), ann)
    assert (ann_first.opened_turn, ann_first.deadline_turn) == (100, 150)
    assert (ann_first.closed_turn, ann_first.closed_reason) == (150, retirement.FORCED)

    await h.held(db, story, ann, 1)  # turn 151: Ann's stream opens a new episode
    ann_second = _open(await h.episodes(db, story), ann)[-1]
    assert (ann_second.opened_turn, ann_second.deadline_turn) == (151, 201)
    await h.narrate_to(db, story, 159)
    await h.held(db, story, bobby, 3)
    second = _open(await h.episodes(db, story), bobby)[-1]
    assert (second.opened_turn, second.deadline_turn, second.open) == (160, 210, True)


async def _pin(s: Scratch, story: str, memory_id: str) -> Any:
    return await s.client.post(f"/stories/{story}/memories/{memory_id}/pin", headers=WRITE)


async def _set_access(s: Scratch, accessed: dict[str, str]) -> None:
    async with s.db.begin() as conn:
        for memory_id, at in accessed.items():
            await conn.execute(
                text(
                    "UPDATE memories SET last_accessed_at = CAST(:at AS timestamptz) WHERE id = :i"
                ),
                {"i": memory_id, "at": at},
            )


@pytest.mark.part("M4", "FR-MEM-8; NFR-SCALE-2")
async def test_i_16__ranking(s: Scratch) -> None:
    """Bobby's budget is 10 (floor 0.2, lag 50) and he holds 19 records: 3 pinned (importance
    0, so they would rank first), 2 that a consolidation output covers, 2 below the floor and
    12 above it. The retirement job retires exactly the covered and the below-floor ones
    (``covered`` and ``floor``) and leaves the episode open with 2 over; at the deadline the 2
    lowest-ranked uncovered records go (``uncovered``): of three at the same importance, the
    two accessed longest ago. Retired records lose their vectors; with
    ``retain_retired_vectors`` another story's keep theirs. Complement: the pinned records
    survive every run and Bobby ends at exactly his budget plus his pins."""
    db = s.db
    w = await h.story(db, memories_per_character=10, retirement_max_lag=50, importance_floor=0.2)
    story, bobby = w.story_id, w.characters["Bobby"]
    scores = [0.0, 0.0, 0.0, 0.9, 0.9, 0.05, 0.08, 0.3, 0.3, 0.3, *[0.5 + k / 20 for k in range(9)]]
    turn, ids = await h.held(db, story, bobby, len(scores), importance=scores)
    assert turn == 1
    pinned, covered, low, ties, rest = ids[:3], ids[3:5], ids[5:7], ids[7:10], ids[10:]
    for memory_id in pinned:
        answered = await _pin(s, story, memory_id)
        assert answered.status_code == 200, answered.text
        assert answered.json()["memory"]["pinned"] is True
    await h.held(db, story, bobby, 0, covering=covered)
    await h.fake_vectors(db, story)
    await _set_access(
        s, {ties[0]: "2026-01-01T00:00:00Z", ties[1]: "2026-06-01T00:00:00Z"}
    )  # ties[2] was never accessed: its creation is its last access
    (episode,) = await h.episodes(db, story)
    assert (episode.opened_turn, episode.deadline_turn, episode.excess_at_open) == (1, 51, 9)

    assert {str(o) for o in await h.run(db, h.RETIREMENT, story)} == {"committed"}
    found = await h.memory_rows(db, story)
    retired = {i: r.retired_reason for i, r in found.items() if r.retrieval_tier == "retired"}
    assert retired == {**dict.fromkeys(covered, "covered"), **dict.fromkeys(low, "floor")}
    assert all(found[i].vector is None and found[i].version is None for i in retired)
    (still,) = await h.episodes(db, story)
    assert still.open
    assert still.excess == 2

    await h.narrate_to(db, story, 51)
    found = await h.memory_rows(db, story)
    retired = {i: r.retired_reason for i, r in found.items() if r.retrieval_tier == "retired"}
    assert {i for i, reason in retired.items() if reason == "uncovered"} == set(ties[:2])
    assert {found[i].retired_turn for i in ties[:2]} == {51}
    assert all(found[i].retrieval_tier == "hot" and found[i].pinned for i in pinned)
    assert found[ties[2]].retrieval_tier == "hot"
    assert all(found[i].retrieval_tier == "hot" for i in rest)
    (closed,) = await h.episodes(db, story)
    assert (closed.closed_turn, closed.closed_reason) == (51, retirement.FORCED)
    assert (await h.counts(db, story)).characters[bobby] == 10
    hot = [r for r in found.values() if r.holder == bobby and r.retrieval_tier == "hot"]
    assert len(hot) == 10 + len(pinned)

    kept = await h.story(
        db, memories_per_character=10, importance_floor=0.2, retain_retired_vectors=True
    )
    _, kept_ids = await h.held(
        db, kept.story_id, kept.characters["Bobby"], 12, importance=[0.05, 0.05, *[0.9] * 10]
    )
    await h.fake_vectors(db, kept.story_id)
    async with db.begin() as conn:
        (opened,) = await retirement.open_episodes(conn, kept.story_id)
        await retirement.run_episode(conn, kept.story_id, opened.id)
    kept_rows = await h.memory_rows(db, kept.story_id)
    assert [kept_rows[i].retired_reason for i in kept_ids[:2]] == ["floor", "floor"]
    assert all(kept_rows[i].vector is not None for i in kept_ids[:2])


@pytest.mark.part("M4", "FR-MEM-8; NFR-SCALE-2")
async def test_i_16__all_non_retirable(s: Scratch) -> None:
    """I-16's all-non-retirable case at the shipped budgets (2,000 a character, 200 pins):
    Bobby holds 200 pinned records and 3,000 unpinned ones, every one above the importance
    floor and none consolidated. Before the lag nothing retires and nothing is refused but a
    201st pin; at the deadline the turn's own commit retires 500 (one run's batch) and the
    follow-up job the other 500, so the episode closes at the deadline turn with Bobby's hot set
    at exactly budget + pin ceiling (2,200), and later turns keep it there. Complement: every
    pinned record is still hot and pinned."""
    db = s.db
    w = await h.story(db)
    story, bobby = w.story_id, w.characters["Bobby"]
    _, ids = await h.held(db, story, bobby, 3_200)
    pins, unpinned = ids[:200], ids[200:]
    await h.pin_rows(db, pins)
    (episode,) = await h.episodes(db, story)
    assert (episode.opened_turn, episode.deadline_turn) == (1, 51)
    assert {str(o) for o in await h.run(db, h.RETIREMENT, story)} == {"committed"}
    (still,) = await h.episodes(db, story)
    assert still.open
    assert still.excess == 1_000
    refused = await _pin(s, story, unpinned[0])
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "pin_limit_exceeded"
    refusal = refused.json()["error"]["details"]
    assert (refusal["scope"], refusal["limit"], refusal["actual"]) == ("character", 200, 201)

    await h.narrate_to(db, story, 51)
    assert (await h.counts(db, story)).characters[bobby] == 2_500
    (follow_up,) = await h.queued(db, h.RETIREMENT, story)
    assert follow_up.coalesce_key == f"{h.RETIREMENT}:{episode.id}"
    assert {str(o) for o in await h.run(db, h.RETIREMENT, story)} == {"committed"}
    (closed,) = await h.episodes(db, story)
    assert (closed.closed_turn, closed.closed_reason) == (51, retirement.FORCED)
    await h.narrate(db, story, 3)
    found = await h.memory_rows(db, story)
    hot = [r for r in found.values() if r.holder == bobby and r.retrieval_tier == "hot"]
    assert len(hot) == 2_000 + 200
    assert all(found[i].pinned and found[i].retrieval_tier == "hot" for i in pins)
    assert {found[i].retired_reason for i in unpinned if found[i].retired_turn} == {"uncovered"}


@pytest.mark.part("M4", "FR-MEM-8")
async def test_i_16__pin_admission(s: Scratch) -> None:
    """Pins are admission-limited at the shipped ceilings: the 201st pin on one character is 422
    ``pin_limit_exceeded`` (``details.scope = character``); with twelve characters each under
    200, the story's 2,001st pin is refused (``details.scope = story``); unpinning admits again;
    and #60's text pins share the story's counter (a ``PUT /pinned-memories`` that would take
    it past 2,000 is refused whole). Complement: the refused calls changed nothing."""
    db = s.db
    w = await h.story(db)
    story, bobby = w.story_id, w.characters["Bobby"]
    _, ids = await h.held(db, story, bobby, 201)
    await h.pin_rows(db, ids[:199])
    assert (await _pin(s, story, ids[199])).status_code == 200
    refused = await _pin(s, story, ids[200])
    assert refused.status_code == 422, refused.text
    body = refused.json()["error"]
    assert body["code"] == "pin_limit_exceeded"
    assert body["details"]["scope"] == "character"
    assert (body["details"]["limit"], body["details"]["actual"]) == (200, 201)
    assert (await h.memory_rows(db, story))[ids[200]].pinned is False

    names = [f"Keeper {k}" for k in range(12)]
    wide, characters = await h.cast(db, names)
    plan = {who: 167 if k < 8 else 166 for k, who in enumerate(characters)}  # 2,000 pins
    _, held = await h.burst(db, wide, {who: n + 1 for who, n in plan.items()})
    spare: list[str] = []
    to_pin: list[str] = []
    at = 0
    for who in characters:
        mine = held[at : at + plan[who] + 1]
        at += plan[who] + 1
        to_pin.extend(mine[:-1])
        spare.append(mine[-1])
    await h.pin_rows(db, to_pin[:-1])
    assert (await _pin(s, wide, to_pin[-1])).status_code == 200
    story_refused = await _pin(s, wide, spare[0])
    assert story_refused.status_code == 422
    details = story_refused.json()["error"]["details"]
    assert (details["scope"], details["limit"], details["actual"]) == ("story", 2_000, 2_001)
    text_refused = await s.client.put(
        f"/stories/{wide}/pinned-memories", json={"items": [{"text": "A text pin."}]}, headers=WRITE
    )
    assert text_refused.status_code == 422
    assert text_refused.json()["error"]["code"] == "pin_limit_exceeded"
    assert (await s.client.get(f"/stories/{wide}/pinned-memories", headers=WRITE)).json()[
        "items"
    ] == []
    released = await s.client.delete(f"/stories/{wide}/memories/{to_pin[0]}/pin", headers=WRITE)
    assert released.status_code == 200
    assert released.json()["memory"]["pinned"] is False
    assert (await _pin(s, wide, spare[0])).status_code == 200
    released = await s.client.delete(f"/stories/{wide}/memories/{to_pin[1]}/pin", headers=WRITE)
    assert released.status_code == 200
    text_pin = await s.client.put(
        f"/stories/{wide}/pinned-memories", json={"items": [{"text": "A text pin."}]}, headers=WRITE
    )
    assert text_pin.status_code == 200, text_pin.text
    async with db.connect() as conn:
        counted = await pins.pin_counts(conn, wide)
    assert (counted.memory_pins, counted.text_pins, counted.story_pins) == (1_999, 1, 2_000)


def _gauge(scrape: str, name: str, scope: str) -> float:
    (line,) = [x for x in scrape.splitlines() if x.startswith(f'{name}{{scope="{scope}"}}')]
    return float(line.rsplit(" ", 1)[1])


@pytest.mark.part("M4", "FR-MEM-8; NFR-SCALE-2")
async def test_i_16__story_vectors(s: Scratch) -> None:
    """The story's vector budget (lowered to 5, lag 10): eight shared memories count nothing
    until the embed job stamps their vectors, and that job's commit opens the story's episode
    (``scope = story``) and enqueues priority consolidation and the retirement job; the
    overflow gauge reports the excess while it is open. With consolidation stalled (the stub
    model unavailable) nothing is covered, so the deadline turn retires the three lowest-ranked
    records as ``retired_uncovered``, drops their vectors, closes the episode, queues
    consolidation to cover them, and the gauge reads 0; a later consolidation run covers them
    (their reason reads ``covered``). Outside the episode the story's hot vectors are within
    budget + pins. The daily reconcile corrects counters that drifted and counts the correction.
    Complement: the eight unembedded memories opened nothing."""
    db, models = s.db, s.lib.models
    w = await h.story(db, vectors_per_story=5, retirement_max_lag=10, consolidation=True)
    story = w.story_id
    await h.narrate(db, story, 8)
    assert await h.episodes(db, story) == []
    assert (await h.counts(db, story)).story_vectors == 0
    assert [str(o) for o in await h.run(db, EMBED, story)] == ["committed"]
    (episode,) = await h.episodes(db, story)
    assert (episode.scope, episode.scope_id) == ("story", story)
    assert (episode.opened_turn, episode.deadline_turn, episode.excess_at_open) == (8, 18, 3)
    assert len(await h.queued(db, h.RETIREMENT, story)) == 1
    priority = await h.queued(db, CONSOLIDATION, story)
    assert priority
    assert min(j.due_at for j in priority) < datetime.now(UTC)
    metrics.GAUGES.bind(database_url(s.settings))
    try:
        scrape = metrics.render_metrics().decode()
        assert _gauge(scrape, "epistrel_hot_overflow", "story") == 3.0
        assert _gauge(scrape, "epistrel_hot_overflow_episodes_open", "story") == 1.0
        assert _gauge(scrape, "epistrel_hot_overflow", "character") == 0.0

        models.stub.fault(CONSOLIDATION, "http_500", n=20)
        await cs.run_jobs(db, models, story, limit=3)
        assert await cs.outputs(db, story) == []
        await h.run(db, h.RETIREMENT, story)
        (still,) = await h.episodes(db, story)
        assert still.open

        await h.narrate_to(db, story, 18)
        (closed,) = await h.episodes(db, story)
        assert (closed.closed_turn, closed.closed_reason) == (18, retirement.FORCED)
        found = await h.memory_rows(db, story)
        uncovered = [i for i, r in found.items() if r.retired_reason == "uncovered"]
        assert len(uncovered) == 3
        assert all(found[i].vector is None for i in uncovered)
        assert (await h.counts(db, story)).story_vectors == 5
        scrape = metrics.render_metrics().decode()
        assert _gauge(scrape, "epistrel_hot_overflow", "story") == 0.0
        assert _gauge(scrape, "epistrel_hot_overflow_episodes_open", "story") == 0.0
    finally:
        metrics.GAUGES.unbind()
    assert await h.queued(db, CONSOLIDATION, story)

    models.stub.reset()
    models.stub.script(
        CONSOLIDATION, [cs.answer(facts=[("It rained all week.", "observation", [0, 1, 2])])] * 5
    )
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET due_at = now() - interval '1 hour' WHERE story_id = :s "
                "AND kind = 'consolidation' AND status = 'queued'"
            ),
            {"s": story},
        )
    await cs.run_jobs(db, models, story, limit=5)
    found = await h.memory_rows(db, story)
    assert {found[i].retired_reason for i in uncovered} == {"covered"}
    assert all(found[i].retrieval_tier == "retired" for i in uncovered)

    async with db.connect() as conn:
        pinned = (await pins.pin_counts(conn, story)).memory_pins
    assert (await h.counts(db, story)).story_vectors <= 5 + pinned
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE stories SET hot_counts = "
                '\'{"characters": {}, "story_vectors": 99}\'::jsonb WHERE id = :s'
            ),
            {"s": story},
        )
    before = _counter(metrics.HOT_COUNTS_CORRECTED)
    async with db.begin() as conn:
        await queue.enqueue(conn, RECONCILE, NoPayload(), story_id=story)
    assert [str(o) for o in await h.run(db, RECONCILE, story)] == ["committed"]
    assert _counter(metrics.HOT_COUNTS_CORRECTED) == before + 1
    assert await h.stored_counts(db, story) == (await h.counts(db, story)).document()


async def _retire_floor(db: Any, story: str, budget: int) -> retirement.EpisodeRun:
    """Lower the story's vector budget to ``budget`` and run its episode's retirement now (a
    story whose head cannot take another commit)."""
    await store_overrides(db, story, h.overrides(vectors_per_story=budget))
    async with db.begin() as conn:
        current = await retirement.recount(conn, story)
        await retirement.reevaluate(conn, story, current)
        (opened,) = await retirement.open_episodes(conn, story)
        return await retirement.run_episode(conn, story, opened.id)


def _embed(q: Sequence[float]) -> Any:
    async def embed(text_: str) -> Sequence[float]:
        del text_
        return q

    return embed


@pytest.mark.part("M4", "FR-MEM-8")
async def test_i_16__retired_stay_active(s: Scratch) -> None:
    """Retired records stay active history. A memory wrapping an author fact and one that
    mentions an open thunk both retire (below the floor, the vector budget lowered): the turn's
    beat is still in the history view, both are in the corrected view of memories, the keyed
    lookup still finds the fact and the fact still finds its (retired) memory, and the ledger
    still delivers the thunk to the context (complement: the context holds the ledger item, not
    the retired memory's text). On a 5,000-turn story, a resolution query ("that game a month
    ago") rehydrates a retired episode through the time window and a retired memory through
    the lexical lookup, re-embedding the one without a vector, and neither returns to the hot
    set (``retrieval_tier`` unchanged); the re-embedded vector is not stored, unless
    ``retain_retired_vectors`` is on, when it is stored on the still-retired row."""
    db = s.db
    w = await h.story(db)
    story, people = w.story_id, w.people
    fact = await mutation.commit_fact(
        db,
        story,
        mutation.FactRequest(
            proposition=Proposition(
                subject_id=people["Bobby"],
                predicate_id=c.KNOWS,
                object_id=people["Ann"],
                valid_time=Interval.of(0, None),
            )
        ),
        principal=h.PRINCIPAL,
    )
    assert fact.memory_id is not None
    async with db.begin() as conn:
        auto = await predicate_registry.auto_register(conn, story, "sails with")
    sketch = ThunkSketch(
        subject_text="someone",
        relation_text=auto.name,
        object_text="Ann",
        predicate_id=auto.id,
        object_id=people["Ann"],
        open_slot=OpenSlot.SUBJECT,
    )

    async def owe(conn: Any, op: Any) -> None:
        await lifecycle.introduce(conn, op, sketch, valid_time=0, salient=True, on=True)

    await c.run(db, story, owe)
    line = "Someone will sail with Ann at dawn."
    _, (mentioned,) = await turn(db, story, say=None, reply=line)
    await h.narrate(db, story, 2)
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE memories SET importance = 0.01, importance_at = 1 WHERE id = ANY(:i)"),
            {"i": [fact.memory_id, mentioned.id]},
        )
    await h.fake_vectors(db, story)
    vectors = (await h.counts(db, story)).story_vectors
    run = await _retire_floor(db, story, vectors - 2)
    assert dict(run.retired) == {"floor": 2}
    found = await h.memory_rows(db, story)
    assert found[fact.memory_id].retrieval_tier == found[mentioned.id].retrieval_tier == "retired"

    history = await read_history(db, story, viewpoint="omniscient")
    assert line in repr(history)
    async with db.connect() as conn:
        view = {m.id for m in await records.memories_through(conn, story)}
        assert {fact.memory_id, mentioned.id} <= view
        keyed = await KeyedLookup(conn, StructuredReadSet()).canon_at(
            story, predicate=c.KNOWS, subjects=people["Bobby"]
        )
        assert [f.id for f in keyed] == [fact.fact.id]
        linked = await records.memories_linked(
            conn, story, proposition_ids=[fact.fact.proposition_id]
        )
        assert fact.memory_id in {m.id for m in linked}
    async with db.begin() as conn:
        result = await pipeline.retrieve(
            conn, story, pipeline.RetrievalQuery("Tell me more."), touch=False
        )
    kinds = {(i.kind, i.text) for i in result.context.items}
    assert any(kind == "ledger" and "sails_with Ann" in t for kind, t in kinds)
    assert all(line not in t for _, t in kinds)

    await reset(db)
    await r.pin(db)
    built = await r.build_story(db, turns=r.TURNS)
    game = await r.memory(
        db,
        built,
        "retired_game",
        "Bobby says: Ann, that game was the best one yet.",
        turn=r.MONTH_AGO,
        event_type="game",
        participants=("Bobby", "Ann"),
        importance=0.01,
    )
    lantern = await r.memory(
        db,
        built,
        "retired_lantern",
        "Quillon Vexby polished the brass lantern.",
        turn=300,
        importance=0.01,
    )
    for n in range(3):
        await r.memory(
            db, built, f"kept_{n}", f"Ann does: waters plant {n}.", turn=400 + n, importance=0.9
        )
    await r.set_vectors(db, built.story_id, (0.9, 0.1, 0.0, 0.0))
    run = await _retire_floor(db, built.story_id, 3)
    assert dict(run.retired) == {"floor": 2}
    batches: list[list[str]] = []

    async def embed_batch(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        batches.append(list(texts))
        return [(1.0, 0.0, 0.0, 0.0) for _ in texts]

    query = "Those guys a month ago at the game, and the lantern: you were there with Ann."
    async with db.begin() as conn:
        found_now = await pipeline.retrieve(
            conn,
            built.story_id,
            pipeline.RetrievalQuery(query),
            embed=_embed((1.0, 0.0, 0.0, 0.0)),
            embed_batch=embed_batch,
        )
    assert {game, lantern} <= set(found_now.rehydrated)
    assert batches  # both retired rows lost their vectors: re-embedded for this query
    tiers = await r.access_and_tiers(db, [game, lantern])
    assert tiers[game][1] == tiers[lantern][1] == "retired"
    assert all(
        v.vector is None
        for k, v in (await h.memory_rows(db, built.story_id)).items()
        if k in (game, lantern)
    )

    await store_overrides(
        db, built.story_id, h.overrides(vectors_per_story=3, retain_retired_vectors=True)
    )
    async with db.begin() as conn:
        await pipeline.retrieve(
            conn,
            built.story_id,
            pipeline.RetrievalQuery(query),
            embed=_embed((1.0, 0.0, 0.0, 0.0)),
            embed_batch=embed_batch,
        )
    kept = await h.memory_rows(db, built.story_id)
    assert kept[lantern].vector is not None
    assert kept[lantern].version == r.VERSION
    assert kept[lantern].retrieval_tier == "retired"


SWAP_METADATA_TABLES = frozenset(
    {
        *OPERATIONAL_TABLES,
        "operations",
        "overflow_episodes",
        "proposition_changes",
        "subject_references",
    }
)
"""I-15's comparison leaves out the operations table (the swap's own record and the
membership flips of every operation), the hot set's episodes, the read set's change index and
the subject index (the swap's and the new continuation's records' entries), and the
operational tables (jobs: the embed job the reactivation queued)."""
SWAP_METADATA_COLUMNS = frozenset(
    {
        "membership_tx",
        "timeline_revision",
        "last_control_revision",
        "last_control_operation_id",
        "chain_unchecked",
        "summary_epoch",
        "hot_counts",
        "embedding",
    }
)
"""The control and audit metadata a swap writes (its flips' transaction-times, the revision, the
story's control marker and the epochs its triggers bump) and the vector itself."""


async def _story_state(db: Any, story: str) -> dict[str, list[dict[str, Any]]]:
    """The story's non-vector state on its active timeline (see I-15): every story table but
    ``SWAP_METADATA_TABLES``, each row as stored minus ``SWAP_METADATA_COLUMNS``; a row with a
    membership only while active, a row of an operation only while that operation is an active
    turn, genesis or consolidation (never a control or maintenance one)."""
    async with db.connect() as conn:
        tables = sorted(
            set(
                await conn.scalars(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'story_id'"
                    )
                )
            )
            - SWAP_METADATA_TABLES
        )
        active_ops = set(
            await conn.scalars(
                text(
                    "SELECT operation_id FROM operations WHERE story_id = :s AND status = "
                    "'committed' AND membership = 'active' AND kind NOT IN ('control', "
                    "'maintenance')"
                ),
                {"s": story},
            )
        )
        found: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            result = await conn.execute(
                text(f'SELECT * FROM "{table}" t WHERE t.story_id = :s'),  # noqa: S608
                {"s": story},
            )
            kept = []
            for row in result.mappings():
                values = dict(row)
                if "membership" in values and str(values["membership"]) != "active":
                    continue
                if values.get("operation_id") is not None and values["operation_id"] not in (
                    active_ops
                ):
                    continue
                kept.append({k: v for k, v in values.items() if k not in SWAP_METADATA_COLUMNS})
            found[table] = sorted(kept, key=repr)
        story_row = (
            (await conn.execute(text("SELECT * FROM stories WHERE id = :s"), {"s": story}))
            .mappings()
            .one()
        )
        found["stories"] = [
            {k: v for k, v in dict(story_row).items() if k not in SWAP_METADATA_COLUMNS}
        ]
    return found


async def _continuations(s: Scratch, *, retain: bool) -> dict[str, Any]:
    """Four turns (each one memory), embedded; truncated to 2 and continued by two other turns
    (embedded too); then swapped back to the first continuation. Answers the state and vectors
    before the truncation, after it, and after the swap, and the first continuation's memories."""
    db = s.db
    w = await h.story(db, retain_dormant_vectors=retain)
    story, bobby = w.story_id, w.characters["Bobby"]
    first = [await say(db, story, bobby, f"Line {n} of the first way.") for n in range(1, 5)]
    assert [str(o) for o in await h.run(db, EMBED, story)] == ["committed"]
    before = await _story_state(db, story)
    vectors_before = {i: v.vector for i, v in (await vectors(db, story)).items()}
    await truncate(db, story, 2)
    truncated = await vectors(db, story)
    second = [await say(db, story, bobby, f"Line {n} of the other way.") for n in (3, 4)]
    await h.run(db, EMBED, story)
    dormant = await vectors(db, story)
    swapped = await swap(db, story)
    assert swapped is not None
    queued = await h.queued(db, EMBED, story)
    await h.run(db, EMBED, story)
    return {
        "story": story,
        "first": first,
        "second": second,
        "before": before,
        "after": await _story_state(db, story),
        "vectors_before": vectors_before,
        "truncated": truncated,
        "dormant": dormant,
        "queued": queued,
        "vectors_after": await vectors(db, story),
    }


@pytest.mark.part("M4", "FR-STORE-30; NFR-DETERM-1")
async def test_i_15(s: Scratch) -> None:
    """I-15 (M4): a swap reactivates a dormant continuation with ``retain_dormant_vectors``
    off: its memories had lost their vectors when it went inactive, so the swap queues the
    embed job, which gives them vectors in the story's pinned embedding-model version; every
    non-vector state of the story on its active timeline is byte-identical to before the
    continuation went inactive, apart from the swap's own control and audit metadata (its
    operation, the flips' transaction-times, the revision). With ``retain_dormant_vectors``
    on, the vectors are byte-identical too and nothing needs re-embedding."""
    off = await _continuations(s, retain=False)
    pin = await pin_of(s.db)
    assert {t: len(v) for t, v in off["before"].items() if v} == {
        t: len(v) for t, v in off["after"].items() if v
    }
    assert len(off["before"]["memories"]) == 4
    assert len(off["before"]["events"]) == 5
    assert differences(off["before"], off["after"]) == []
    assert off["queued"], "the swap queued the reactivated memories' embedding"
    for memory_id in off["first"]:
        row = off["vectors_after"][memory_id]
        assert row.membership == "active"
        assert row.vector is not None
        assert row.version == pin.version
    on = await _continuations(s, retain=True)
    assert differences(on["before"], on["after"]) == []
    assert on["queued"] == []
    assert {i: on["vectors_after"][i].vector for i in on["first"]} == {
        i: on["vectors_before"][i] for i in on["first"]
    }


@pytest.mark.part("M4", "FR-STORE-30; NFR-DETERM-1")
async def test_s_retention__vectors(s: Scratch) -> None:
    """S-RETENTION (M4): vectors exist only for active hot records. A truncation drops the
    vectors of the memories it discards, the turns written over them leave them without, a
    swap drops the vectors of the continuation it makes dormant and re-embeds the one it
    reactivates (not byte-identical by design: here the stub's are), a regenerated turn drops
    the vectors of the variant it replaces, and a retired memory has none. Complement: with
    ``retain_dormant_vectors`` the inactive memories keep theirs."""
    off = await _continuations(s, retain=False)
    assert all(off["truncated"][i].vector is None for i in off["first"][2:])
    assert all(off["truncated"][i].vector is not None for i in off["first"][:2])
    assert all(off["dormant"][i].vector is None for i in off["first"][2:])
    assert all(off["vectors_after"][i].vector is None for i in off["second"])
    assert all(off["vectors_after"][i].membership == "dormant" for i in off["second"])
    on = await _continuations(s, retain=True)
    assert all(on["truncated"][i].vector is not None for i in on["first"][2:])
    assert all(on["vectors_after"][i].vector is not None for i in on["second"])

    db = s.db
    w = await h.story(db)
    story, bobby = w.story_id, w.characters["Bobby"]
    await say(db, story, bobby, "A line to regenerate.")
    await h.run(db, EMBED, story)
    (replaced,) = list((await vectors(db, story)).values())
    assert replaced.vector is not None
    await c.run(db, story, _nothing, turn=1)
    regenerated = await vectors(db, story)
    assert regenerated[str(replaced.id)].membership == "dormant"
    assert regenerated[str(replaced.id)].vector is None
    _, (low, _high) = await h.held(db, story, bobby, 2, importance=[0.01, 0.9])
    await h.run(db, EMBED, story)
    assert (await vectors(db, story))[low].vector is not None
    await store_overrides(db, story, h.overrides(vectors_per_story=1))
    async with db.begin() as conn:
        current = await retirement.recount(conn, story)
        await retirement.reevaluate(conn, story, current)
        for opened in await retirement.open_episodes(conn, story):
            await retirement.run_episode(conn, story, opened.id)
    retired = (await vectors(db, story))[low]
    assert retired.retrieval_tier == "retired"
    assert retired.vector is None


async def _nothing(conn: Any, op: Any) -> None:
    del conn, op


# -- what the parts stand on ------------------------------------------------------------------


def _driver(statement: Any) -> str:
    """A ``text()`` statement with its ``:name`` parameters in the driver's ``%(name)s`` form."""
    return re.sub(r"(?<!:):(\w+)", r"%(\1)s", str(statement))


def _touching(seen: Any, *names: str) -> list[str]:
    return [sql for sql, _ in seen.statements if any(n in sql for n in names)]


async def test_a_lite_story_is_untouched(s: Scratch) -> None:
    """#129: a Lite story keeps no episodic memory, so its turns write no memory, no counter
    and no episode: the only hot-set statement a Lite turn's commit runs is the one read of its
    open episodes (none). Complement: a structured story's turn of the same shape writes its
    memory and reads the same one statement for the deadline check."""
    db = s.db
    lite = await new_story(db)
    with r.statements(db) as seen:
        await turn(db, lite, say=None, reply=h.NARRATION)
    assert len(_touching(seen, "overflow_episodes")) == 1
    assert _touching(seen, "hot_counts", "INSERT INTO memories") == []
    assert await h.episodes(db, lite) == []
    assert await h.stored_counts(db, lite) is None
    w = await h.story(db)
    with r.statements(db) as seen:
        await turn(db, w.story_id, say=None, reply=h.NARRATION)
    assert len(_touching(seen, "overflow_episodes")) == 1
    assert len(_touching(seen, "INSERT INTO memories")) == 1


async def test_the_hot_set_cost_does_not_grow_with_the_story(s: Scratch) -> None:
    """NFR-SCALE-2, proven by counting, never by timing: a turn's commit issues the same
    statements on a story with 10 held memories as on one with 1,500 (within budget, so no
    episode either way), and the exact recount and the deadline check read only indexes (the
    hot set's partial indexes, the open episodes' index), never the whole ``memories`` table."""
    db = s.db
    small = await h.story(db)
    large = await h.story(db)
    await h.held(db, small.story_id, small.characters["Bobby"], 10)
    await h.held(db, large.story_id, large.characters["Bobby"], 1_500)

    async def commit_statements(world: c.World) -> list[str]:
        with r.statements(db) as seen:
            await h.held(db, world.story_id, world.characters["Bobby"], 1)
        return [sql for sql, _ in seen.statements]

    assert len(await commit_statements(small)) == len(await commit_statements(large))
    params = {"story": large.story_id}
    holders = await r.plan_of(db, _driver(retirement._COUNT_BY_HOLDER), params)
    assert "ix_memories_hot_unpinned" in holders
    assert "Seq Scan on memories" not in holders
    vectors_plan = await r.plan_of(db, _driver(retirement._COUNT_VECTORS), params)
    assert "ix_memories_hot_vectors" in vectors_plan
    due = await r.plan_of(
        db,
        "SELECT id FROM overflow_episodes WHERE story_id = %(story)s AND closed_turn IS NULL "
        "AND deadline_turn <= 5",
        params,
    )
    assert "ux_overflow_episodes_open" in due or "ix_overflow_episodes_story" in due


async def test_a_truncation_closes_what_it_brings_back_within_budget(s: Scratch) -> None:
    """In the truncation's own transaction the counters are recounted and an episode whose set
    is back within budget closes ``within_budget``; one still over keeps its deadline. A fork
    copies no episode: its counters are recounted when first read, and its next commit opens
    its own. Lowering a budget opens an episode at the next commit, and an unpin above budget
    opens one at the unpin's commit."""
    db = s.db
    w = await h.story(db, memories_per_character=10, retirement_max_lag=50)
    story, bobby, ann = w.story_id, w.characters["Bobby"], w.characters["Ann"]
    await h.burst(db, story, {bobby: 8, ann: 12})  # turn 1: Ann over
    await h.narrate(db, story, 2)
    await h.held(db, story, bobby, 8)  # turn 4: Bobby over
    await h.held(db, story, ann, 5)  # turn 5
    by = {e.scope_id: e for e in await h.episodes(db, story)}
    assert (by[bobby].opened_turn, by[ann].opened_turn) == (4, 1)
    await truncate(db, story, 3)
    by = {e.scope_id: e for e in await h.episodes(db, story)}
    assert (by[bobby].closed_turn, by[bobby].closed_reason) == (3, retirement.WITHIN_BUDGET)
    assert by[ann].open
    assert by[ann].deadline_turn == 51
    assert (await h.counts(db, story)).characters == {bobby: 8, ann: 12}

    forked = await fork(db, story)
    assert await h.episodes(db, forked.story_id) == []
    assert await h.stored_counts(db, forked.story_id) is None
    await h.narrate(db, forked.story_id, 1)
    await h.held(db, forked.story_id, ann, 1)
    (own,) = await h.episodes(db, forked.story_id)
    assert own.scope_id != ann  # the fork's own cast
    assert own.opened_turn == 5

    lowered = await h.story(db, memories_per_character=10)
    l_story, l_bobby = lowered.story_id, lowered.characters["Bobby"]
    await h.held(db, l_story, l_bobby, 8)
    await store_overrides(db, l_story, h.overrides(memories_per_character=5))
    assert await h.episodes(db, l_story) == []
    await h.held(db, l_story, l_bobby, 1)
    (opened,) = await h.episodes(db, l_story)
    assert (opened.opened_turn, opened.excess_at_open) == (2, 4)

    pinned = await h.story(db, memories_per_character=3)
    p_story, p_bobby = pinned.story_id, pinned.characters["Bobby"]
    _, ids = await h.held(db, p_story, p_bobby, 4)
    assert (await _pin(s, p_story, ids[0])).status_code == 200
    (first,) = await h.episodes(db, p_story)
    assert not first.open  # pinning brought the set back within budget
    released = await s.client.delete(f"/stories/{p_story}/memories/{ids[0]}/pin", headers=WRITE)
    assert released.status_code == 200
    reopened = [e for e in await h.episodes(db, p_story) if e.open]
    assert len(reopened) == 1
    assert reopened[0].excess_at_open == 1


async def test_pins_reach_the_context_unless_quarantined(s: Scratch) -> None:
    """The pin source (#145's carried note): a pinned memory is a tier-1 ``pin`` item of every
    retrieval, its ``item_id`` the memory's id, best decayed importance first; a quarantined
    pin is left out. Pinning a retired memory brings it back to the hot set and queues its
    embedding. Complement: an unpinned memory is no pin item."""
    db = s.db
    w = await h.story(db, memories_per_character=1, importance_floor=0.5)
    story, bobby = w.story_id, w.characters["Bobby"]
    _, (low, high, other) = await h.held(db, story, bobby, 3, importance=[0.3, 0.9, 0.95])
    assert {str(o) for o in await h.run(db, h.RETIREMENT, story)} == {"committed"}
    assert (await h.memory_rows(db, story))[low].retrieval_tier == "retired"
    await h.queued(db, EMBED, story)
    async with db.begin() as conn:
        await conn.execute(
            text("DELETE FROM jobs WHERE story_id = :s AND kind = 'embed'"), {"s": story}
        )
    for memory_id in (high, low):
        assert (await _pin(s, story, memory_id)).status_code == 200
    rows_now = await h.memory_rows(db, story)
    assert rows_now[low].retrieval_tier == "hot"
    assert rows_now[low].retired_reason is None
    assert await h.queued(db, EMBED, story)

    async def pin_items() -> list[tuple[str, str]]:
        async with db.begin() as conn:
            found = await pipeline.retrieve(
                conn, story, pipeline.RetrievalQuery("Tell me more."), touch=False
            )
        return [(i.kind, i.item_id) for i in found.context.items if i.kind == "pin"]

    assert await pin_items() == [("pin", high), ("pin", low)]
    quarantined = await s.client.post(
        f"/admin/stories/{story}/memories/{high}/quarantine",
        json={"reason": "Planted."},
        headers=WRITE,
    )
    assert quarantined.status_code == 200
    assert await pin_items() == [("pin", low)]
    assert ("pin", other) not in await pin_items()
    view = await s.client.get(f"/admin/stories/{story}/hot-set", headers=WRITE)
    assert view.status_code == 200
    body = view.json()
    assert body["pins"]["memory_pins"] == 2
    assert body["counters"]["characters"] == {bobby: 1}
    assert body["budgets"]["memories_per_character"] == 1
    assert body["recent_retirements"] == []


# -- ranking in SQL (#444) -------------------------------------------------------------------------


async def _reference_ranking(
    conn: Any,
    story: str,
    scope: str,
    scope_id: str,
    budgets: retirement.Budgets,
    *,
    now: int,
    half_life: int,
) -> list[tuple[str, str | None, float]]:
    """#147's ranking as it was built, the reference the SQL ranking must equal: every
    candidate fetched and ranked in Python. Answers ``(id, retirable, decayed)`` in order."""
    where = (
        "m.holder_character_id = :x" if scope == retirement.CHARACTER else "m.embedding IS NOT NULL"
    )
    found = await conn.execute(
        text(
            "SELECT m.id, m.importance, m.importance_at, "  # noqa: S608
            "coalesce(m.last_accessed_at, m.created_at) AS accessed, EXISTS (SELECT 1 FROM "
            "memories o WHERE o.id = ANY(m.covered_by) AND o.membership = 'active' "
            "AND o.retrieval_tier = 'hot') AS covered FROM memories m WHERE m.story_id = :s "
            "AND m.retrieval_tier = 'hot' AND NOT m.pinned AND m.membership = 'active' "
            f"AND {where}"
        ),
        {"s": story, "x": scope_id},
    )
    keyed: list[tuple[tuple[int, float, datetime, str], str | None]] = []
    for row in found:
        decayed = records.decayed_importance(row.importance, row.importance_at, now, half_life)
        floor = decayed < budgets.importance_floor
        reason = retirement.COVERED if row.covered else retirement.FLOOR if floor else None
        keyed.append(((0 if reason else 1, decayed, row.accessed, str(row.id)), reason))
    keyed.sort(key=lambda k: k[0])
    return [(key[3], reason, key[1]) for key, reason in keyed]


async def test_the_sql_ranking_equals_the_python_ranking(s: Scratch) -> None:
    """#444: ``ranked`` computes the order in Postgres and keeps the first ``limit`` rows. On a
    story mixing covered records, records below the floor, unscored ones (``NULL``
    importance), ones with no ``importance_at`` or one after ``now`` (as after a truncation),
    and exact ties broken by the last access and then by id, the order, the reasons and the
    decayed importances equal #147's Python ranking, for the character's set and for the
    story's vector set, at several ``now``s; every ``limit`` answers a prefix of the whole.
    Complement: the order is not trivial (covered, below-floor and non-retirable records all
    occur, and a tie is broken by access)."""
    db = s.db
    w = await h.story(db, importance_floor=0.2)
    story, bobby, ann = w.story_id, w.characters["Bobby"], w.characters["Ann"]
    scores: list[float | None] = [0.9, 0.9, 0.9, 0.1, 0.25, None, None, 0.6, 0.4, 0.4, 0.05]
    _, ids = await h.held(db, story, bobby, len(scores), importance=scores)
    await h.held(db, story, bobby, 0, covering=ids[7:9])
    _, others = await h.held(db, story, ann, 4, importance=[0.3, 0.9, None, 0.15])
    await h.fake_vectors(db, story, [*ids[:10], *others[:3]])
    await _set_access(s, {ids[1]: "2026-01-01T00:00:00Z", ids[2]: "2026-03-01T00:00:00Z"})
    async with db.connect() as conn:
        await conn.execute(
            text(
                "UPDATE memories SET importance_at = CASE id WHEN :a THEN NULL WHEN :b THEN 400 "
                "WHEN :c THEN 0 WHEN :d THEN 7 END WHERE id IN (:a, :b, :c, :d)"
            ),
            {"a": ids[3], "b": ids[4], "c": ids[9], "d": others[0]},
        )
        settings = await effective_configuration(conn, story, validate=False)
        budgets = retirement.Budgets.of(settings.settings)
        half_life = await records.half_life_turns(conn)
        reasons: set[str | None] = set()
        for scope, scope_id in ((retirement.CHARACTER, bobby), (retirement.STORY, story)):
            for now in (1, 40, 300, 1_000):
                expected = await _reference_ranking(
                    conn, story, scope, scope_id, budgets, now=now, half_life=half_life
                )
                got = await retirement.ranked(
                    conn, story, scope, scope_id, budgets, now=now, half_life=half_life
                )
                assert [(c.id, c.retirable) for c in got] == [e[:2] for e in expected]
                assert [c.decayed for c in got] == pytest.approx([e[2] for e in expected])
                for k in (0, 1, 3, len(expected) + 5):
                    first = await retirement.ranked(
                        conn,
                        story,
                        scope,
                        scope_id,
                        budgets,
                        now=now,
                        half_life=half_life,
                        limit=k,
                    )
                    assert [(c.id, c.retirable) for c in first] == [e[:2] for e in expected[:k]]
                reasons |= {e[1] for e in expected}
        whole = await _reference_ranking(
            conn, story, retirement.CHARACTER, bobby, budgets, now=1, half_life=half_life
        )
        await conn.rollback()
    assert reasons == {retirement.COVERED, retirement.FLOOR, None}
    order = [e[0] for e in whole]
    assert order.index(ids[1]) < order.index(ids[2]) < order.index(ids[0])  # a tie, by access


async def _forced_story_run(db: Any, story: str) -> tuple[int, int, dict[str, int]]:
    """The story-scope episode forced at the head as the deadline turn's commit forces it
    (#153's measurement, rolled back): the statements the run issued, the rows its reads
    brought into Python, and what it retired."""
    statements: list[str] = []
    fetched: list[int] = []

    def after(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        del conn, args
        statements.append(statement)
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            fetched.append(max(0, int(cursor.rowcount)))

    async with db.connect() as conn:
        await retirement.reconcile(conn, story)
        await conn.execute(
            text(
                "UPDATE overflow_episodes SET deadline_turn = opened_turn "
                "WHERE story_id = :s AND closed_turn IS NULL AND scope = 'story'"
            ),
            {"s": story},
        )
        (ep,) = [e for e in await retirement.open_episodes(conn, story) if e.scope == "story"]
        event.listen(db.sync_engine, "after_cursor_execute", after)
        try:
            run = await retirement.run_episode(conn, story, ep.id)
        finally:
            event.remove(db.sync_engine, "after_cursor_execute", after)
        await conn.rollback()
    assert run.forced
    return len(statements), sum(fetched), dict(run.retired)


async def test_forced_retirement_cost_does_not_grow_with_the_vectors(s: Scratch) -> None:
    """#444 (NFR-PERF-3, NFR-SCALE-2), proven by counting, never by timing: a forced
    story-scope run that retires 5 records issues the same statements and brings the same
    number of rows into Python on a story with 25 vectors as on one with 605 (each 5 over its
    vector budget). Complement: both runs retire exactly the 5 (``uncovered``), so the equal
    counts are not two runs that did nothing."""
    db = s.db
    found = []
    for n in (25, 605):
        w = await h.story(db, vectors_per_story=n - 5)
        await h.held(db, w.story_id, w.characters["Bobby"], n)
        await h.fake_vectors(db, w.story_id)
        found.append(await _forced_story_run(db, w.story_id))
    (small_statements, small_rows, small_retired), (large_statements, large_rows, large) = found
    assert small_retired == large == {retirement.UNCOVERED: 5}
    assert small_statements == large_statements
    assert small_rows == large_rows
