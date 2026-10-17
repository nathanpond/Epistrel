"""The structured projections against PostgreSQL (#137): docs/04 I-01 (M4)'s part (the
relationships, placement and inventory rows change in the transaction of the canon write that
changes them, and an induced failure inside the turn leaves none), the I-02 extension (the rows
``rebuild`` refolds from canon equal the incremental ones, byte for byte, over random structured
histories, and only active facts project), and the viewpoint reads of the scene and an entity's
profile (a ``latent`` placement is not shown to a character who cannot read it).

The three tables are caches over ``facts`` (``core.projections.structured``), not derived
records, so they have no TIME (M4) slice of their own: their rows follow the facts' membership,
which ``tests/property/test_time_m4.py`` covers."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import httpx2 as httpx
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon import facts as canon
from epistrel.core.canon.errors import FactInactive, FactSuperseded, KeyMismatch
from epistrel.core.db import make_engine
from epistrel.core.enums import OperationKind, RecordProvenance
from epistrel.core.events import Operation
from epistrel.core.projections import STRUCTURED, fold, rebuild, structured
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from tests.auth_support import auth_settings, bearer
from tests.canon_support import World, at, commit, run, world
from tests.canon_support import supersede as supersede_fact
from tests.event_support import execute_in_tx, rows, scalar
from tests.fork_support import fork
from tests.projection_support import swap, truncate, untruncate
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration

HOLDS = CORE_BY_NAME["holds"].id
FRIEND_OF = CORE_BY_NAME["friend_of"].id
KNOWS = CORE_BY_NAME["knows"].id
PRESENT_AT = CORE_BY_NAME["present_at"].id

TABLES = ("relationships", "placement", "scene_state")
ORDER = {
    "relationships": "source_fact_id, object_id",
    "placement": "source_fact_id",
    "scene_state": "turn, variant",
}


class Boom(Exception):
    """Induced failure after the projection write."""


def rel(subject: str, predicate: str, obj: str, lo: int | None = 0, **kw: Any) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=Interval.of(lo, None),
        **kw,
    )


async def snapshot(engine: AsyncEngine, story_id: str) -> dict[str, tuple[str, ...]]:
    """Every structured projection row of the story as canonical JSON, in primary-key order."""
    found: dict[str, tuple[str, ...]] = {}
    async with engine.connect() as conn:
        for table in TABLES:
            found[table] = tuple(
                await conn.scalars(
                    text(
                        f"SELECT to_jsonb(t)::text FROM {table} t "  # noqa: S608 - fixed names
                        f"WHERE story_id = :s ORDER BY {ORDER[table]}"
                    ),
                    {"s": story_id},
                )
            )
    return found


async def folded(engine: AsyncEngine, story_id: str) -> dict[str, int]:
    """Row counts of the fold from canon (nothing written)."""
    async with engine.connect() as conn:
        result = await fold(conn, story_id, only=STRUCTURED)
    return {name: len(r) for name, r in result.items()}


async def placements(engine: AsyncEngine, story_id: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT * FROM placement WHERE story_id = :s ORDER BY source_fact_id",
        s=story_id,
    )


async def items(engine: AsyncEngine, w: World, *names: str) -> dict[str, str]:
    async with engine.begin() as conn:
        return {n: await store.add_entity(conn, w.story_id, "item", n) for n in names}


async def scene_of(engine: AsyncEngine, story_id: str, **kw: Any) -> structured.SceneView:
    async with engine.connect() as conn:
        return await structured.scene_view(conn, story_id, **kw)


async def profile_of(
    engine: AsyncEngine, story_id: str, entity_id: str, **kw: Any
) -> structured.ProfileView:
    async with engine.connect() as conn:
        return await structured.profile_view(conn, story_id, entity_id, **kw)


def present(view: structured.SceneView) -> list[str]:
    return [e.entity_id for e in view.present]


@pytest.mark.part("M4", "FR-STORE-3")
async def test_i_01__structured_projections(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canon commit of a placement, possession or relationship predicate changes its
    projection rows in the commit's own transaction: they are visible to the transaction
    before it commits, and the turn's scene row is written with it. A supersede closes the old
    row (kept, ``closed_by_fact_id`` set) without changing what an earlier turn reads; a
    negation closes and creates nothing; a truncation re-derives the rows in its transaction and
    an untruncation restores them exactly. Complement: a failure induced after the projection
    write, inside the turn, leaves zero projection rows (and no fact) behind."""
    w = await world(engine)
    story = w.story_id
    cal, bobby, ann = w.people["Cal"], w.people["Bobby"], w.people["Ann"]
    kitchen, garden = w.places["kitchen"], w.places["garden"]
    lamp = (await items(engine, w, "lamp"))["lamp"]

    # Turn 1: Cal in the kitchen. The row is there inside the writing transaction.
    seen: dict[str, Any] = {}

    async def place_cal(conn: AsyncConnection, op: Operation) -> canon.Fact:
        fact = await canon.commit_fact(
            conn, story, at(cal, kitchen, 0, None), provenance=RecordProvenance.RENDER, operation=op
        )
        seen["rows"] = (
            await conn.execute(
                text("SELECT entity_id, location_id FROM placement WHERE story_id = :s"),
                {"s": story},
            )
        ).all()
        return fact

    _, cal_kitchen = await run(engine, story, place_cal)
    assert [tuple(r) for r in seen["rows"]] == [(cal, kitchen)]
    first = await scene_of(engine, story, viewpoint="omniscient")
    assert (first.turn, first.location and first.location.entity_id) == (1, kitchen)
    assert present(first) == [cal]
    assert await scalar(engine, "SELECT count(*) FROM scene_state WHERE story_id = :s", s=story)

    # Turn 2: Bobby joins, holding the lamp; Bobby and Ann are friends (symmetric).
    async def turn_two(conn: AsyncConnection, op: Operation) -> None:
        for proposition in (
            at(bobby, kitchen, 0, None),
            rel(bobby, HOLDS, lamp),
            rel(bobby, FRIEND_OF, ann),
            rel(ann, KNOWS, bobby),
        ):
            await canon.commit_fact(
                conn, story, proposition, provenance=RecordProvenance.RENDER, operation=op
            )

    await run(engine, story, turn_two)
    second = await scene_of(engine, story, viewpoint="omniscient")
    assert present(second) == sorted([cal, bobby])
    bobby_profile = await profile_of(engine, story, bobby, viewpoint="omniscient")
    assert bobby_profile.placement is not None
    assert bobby_profile.placement.location is not None
    assert bobby_profile.placement.location.entity_id == kitchen
    assert [i.item.entity_id for i in bobby_profile.inventory] == [lamp]
    lamp_profile = await profile_of(engine, story, lamp, viewpoint="omniscient")
    assert lamp_profile.placement is not None
    assert lamp_profile.placement.container is not None
    assert lamp_profile.placement.container.entity_id == bobby
    ann_profile = await profile_of(engine, story, ann, viewpoint="omniscient")
    by_predicate = {
        (r.predicate, r.direction, r.other.entity_id) for r in ann_profile.relationships
    }
    # friend_of is symmetric: both read it "out"; knows is Ann's, so Bobby reads it "in".
    assert by_predicate == {("friend_of", "out", bobby), ("knows", "out", bobby)}
    assert {(r.predicate, r.direction) for r in bobby_profile.relationships} == {
        ("friend_of", "out"),
        ("knows", "in"),
    }

    # Turn 3: Cal moves to the garden; the kitchen row is closed, never deleted.
    await commit(engine, story, at(cal, garden, 0, None))
    cal_rows = [r for r in await placements(engine, story) if r.entity_id == cal]
    assert len(cal_rows) == 2
    old = next(r for r in cal_rows if r.source_fact_id == cal_kitchen.id)
    assert old.closed_by_fact_id is not None
    assert old.closed_turn == 3
    third = await scene_of(engine, story, viewpoint="omniscient")
    assert (third.location and third.location.entity_id, present(third)) == (garden, [cal])
    earlier = await scene_of(engine, story, as_of=2, viewpoint="omniscient")
    assert earlier.location is not None
    assert earlier.location.entity_id == kitchen

    # Turn 4: the friendship is negated: its row is closed and no row is made for the negation.
    before = await scalar(engine, "SELECT count(*) FROM relationships WHERE story_id = :s", s=story)
    await commit(engine, story, rel(bobby, FRIEND_OF, ann, polarity=Polarity.NEGATED))
    assert (
        await scalar(engine, "SELECT count(*) FROM relationships WHERE story_id = :s", s=story)
        == before
    )
    now = await profile_of(engine, story, bobby, viewpoint="omniscient")
    assert "friend_of" not in {r.predicate for r in now.relationships}
    then = await profile_of(engine, story, bobby, as_of=3, viewpoint="omniscient")
    assert "friend_of" in {r.predicate for r in then.relationships}

    # Truncate to 2 and back: the rows follow the facts' membership in the edit's transaction.
    kept = await snapshot(engine, story)
    assert await truncate(engine, story, 2) is not None
    rewound = await snapshot(engine, story)
    assert all('"closed_by_fact_id": null' in r for r in rewound["placement"])
    assert [r for r in rewound["scene_state"] if '"turn": 3' in r or '"turn": 4' in r] == []
    assert await untruncate(engine, story) is not None
    assert await snapshot(engine, story) == kept

    # Complement: a failure after the projection write, inside the turn, leaves nothing behind.
    def boom(story_id: str) -> None:
        raise Boom(story_id)

    monkeypatch.setattr(structured, "AFTER_WRITE", [boom])
    facts_before = await scalar(engine, "SELECT count(*) FROM facts WHERE story_id = :s", s=story)
    with pytest.raises(Boom):
        await commit(engine, story, at(ann, garden, 0, None))
    monkeypatch.setattr(structured, "AFTER_WRITE", [])
    assert await snapshot(engine, story) == kept
    assert not [r for r in await placements(engine, story) if r.entity_id == ann]
    assert (
        await scalar(engine, "SELECT count(*) FROM facts WHERE story_id = :s", s=story)
        == facts_before
    )

    # The rebuild refolds the same rows from canon, and a fork refolds its own.
    await rebuild(engine, story, principal="p-operator")
    assert await snapshot(engine, story) == kept
    forked = await fork(engine, story)
    assert {k: len(v) for k, v in (await snapshot(engine, forked.story_id)).items()} == {
        k: len(v) for k, v in kept.items()
    }
    assert await folded(engine, forked.story_id) == {k: len(v) for k, v in kept.items()}


@pytest.mark.part("M4", "FR-STORE-3")
async def test_i_01__structured_viewpoint(engine: AsyncEngine, settings: Settings) -> None:
    """The scene and the profile are read for a viewpoint: Ann, placed in the kitchen by a
    ``latent`` fact only Bobby may read, is present in Bobby's scene and the omniscient one and
    has her placement in Bobby's profile of her. Complement: Cal's scene does not show her and
    Cal's profile of her has no placement, while the objective placements (Cal, Bobby) are in
    every scene; ``source_fact_id`` is returned to ``omniscient`` only. Over REST: the two routes
    answer the same, an unknown entity is 404 and a turn beyond the head is 422."""
    w = await world(engine)
    story = w.story_id
    cal, bobby, ann = w.people["Cal"], w.people["Bobby"], w.people["Ann"]
    kitchen = w.places["kitchen"]
    latent = canon.Visibility(scope="characters", characters=(w.characters["Bobby"],))

    async def turn(conn: AsyncConnection, op: Operation) -> None:
        for proposition, visibility in (
            (at(cal, kitchen, 0, None), canon.OBJECTIVE),
            (at(bobby, kitchen, 0, None), canon.OBJECTIVE),
            (at(ann, kitchen, 0, None), latent),
        ):
            await canon.commit_fact(
                conn,
                story,
                proposition,
                provenance=RecordProvenance.RENDER,
                operation=op,
                visibility=visibility,
            )

    await run(engine, story, turn)
    everyone = sorted([cal, bobby, ann])
    assert present(await scene_of(engine, story, viewpoint="omniscient")) == everyone
    assert present(await scene_of(engine, story, viewpoint=w.viewpoint("Bobby"))) == everyone
    cal_scene = await scene_of(engine, story, viewpoint=w.viewpoint("Cal"))
    assert present(cal_scene) == sorted([cal, bobby])
    assert cal_scene.location is not None
    assert cal_scene.location.entity_id == kitchen

    assert (await profile_of(engine, story, ann, viewpoint=w.viewpoint("Cal"))).placement is None
    seen_by_bobby = await profile_of(engine, story, ann, viewpoint=w.viewpoint("Bobby"))
    assert seen_by_bobby.placement is not None
    assert seen_by_bobby.placement.source_fact_id is None
    omniscient = await profile_of(engine, story, ann, viewpoint="omniscient")
    assert omniscient.placement is not None
    assert omniscient.placement.source_fact_id is not None

    async with respond_client(settings, engine) as (client, _):
        cal_view = await client.get(
            f"/stories/{story}/scene",
            params={"viewpoint": w.viewpoint("Cal")},
            headers=bearer(),
        )
        assert cal_view.status_code == 200, cal_view.text
        assert [e["entity_id"] for e in cal_view.json()["present"]] == sorted([cal, bobby])
        profile = await client.get(
            f"/stories/{story}/entities/{ann}/profile",
            params={"viewpoint": "omniscient"},
            headers=bearer(),
        )
        assert profile.status_code == 200, profile.text
        assert profile.json()["placement"]["location"]["entity_id"] == kitchen
        await _refused(client, f"/stories/{story}/entities/{'0' * 26}/profile", 404, "not_found")
        await _refused(client, f"/stories/{story}/scene?as_of=9", 422, "validation_error")


async def _refused(client: httpx.AsyncClient, url: str, status: int, code: str) -> None:
    sep = "&" if "?" in url else "?"
    answer = await client.get(f"{url}{sep}viewpoint=omniscient", headers=bearer())
    assert answer.status_code == status, answer.text
    assert answer.json()["error"]["code"] == code


# -- I-02's extension: rebuild equality over random structured histories ---------------------

OCCURRED: Counter[str] = Counter()
"""What the histories exercised, across the examples of one run."""

STEPS = (
    "place",
    "place",
    "hold",
    "relate",
    "present",
    "latent",
    "negate",
    "supersede",
    "retcon",
    "seed",
    "truncate",
    "untruncate",
    "rewind_play_and_swap",
    "regenerate",
    "plain_turn",
)

histories = st.lists(
    st.tuples(st.sampled_from(STEPS), st.integers(min_value=0, max_value=30)), max_size=12
)


async def _perform(
    engine: AsyncEngine, w: World, things: dict[str, str], written: list[str], step: str, n: int
) -> None:
    story = w.story_id
    people = sorted(w.people.values())
    places = sorted(w.places.values())
    person = people[n % len(people)]
    other = people[(n + 1) % len(people)]
    place = places[n % len(places)]
    lo = (None, 0, 10)[n % 3]
    head = int(await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story))
    if step in ("place", "latent", "seed"):
        options: dict[str, Any] = {}
        if step == "latent":
            options["visibility"] = canon.Visibility(
                scope="characters", characters=(sorted(w.characters.values())[n % 3],)
            )
        kind = OperationKind.CONTROL if step == "seed" else OperationKind.TURN
        fact = await commit(engine, story, at(person, place, lo, None), kind=kind, **options)
        written.append(fact.id)
        OCCURRED[step] += 1
    elif step == "hold":
        item = sorted(things.values())[n % len(things)]
        written.append((await commit(engine, story, rel(person, HOLDS, item, lo))).id)
    elif step == "relate":
        predicate = (FRIEND_OF, KNOWS)[n % 2]
        written.append((await commit(engine, story, rel(person, predicate, other, lo))).id)
    elif step == "present":
        written.append((await commit(engine, story, rel(person, PRESENT_AT, place, lo))).id)
    elif step == "negate":
        await commit(engine, story, at(person, place, lo, None, negated=True))
        OCCURRED[step] += 1
    elif step in ("supersede", "retcon") and written:
        target = written[n % len(written)]
        current = await _current(engine, story, target)
        if current is None:
            return  # closed or off the timeline: nothing to supersede
        replacement = current.proposition
        if current.predicate_id == CORE_BY_NAME["located_at"].id:
            replacement = at(current.subject_id, place, 0, None)
        options = {"authority": "author", "valid_time": Interval.of(None, None)}
        try:
            fact = await supersede_fact(
                engine, story, target, replacement, **(options if step == "retcon" else {})
            )
        except FactInactive, FactSuperseded, KeyMismatch:
            return
        written.append(fact.id)
        OCCURRED[step] += 1
    elif step == "truncate" and head > 1:
        done = await truncate(engine, story, n % head)
        OCCURRED[step] += done is not None and done.discarded_operations > 0
    elif step == "untruncate":
        OCCURRED[step] += await untruncate(engine, story) is not None
    elif step == "rewind_play_and_swap":
        await commit(engine, story, at(person, place, 0, None))
        head = int(
            await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story)
        )
        await truncate(engine, story, (n % (head - 1)) if head > 1 else 0)
        await commit(engine, story, at(other, place, 0, None))
        OCCURRED["swap"] += await swap(engine, story) is not None
    elif step == "regenerate" and head > 1:
        await commit(engine, story, at(person, place, 0, None), turn=head - 1)
        OCCURRED[step] += 1
    elif step == "plain_turn":
        await run(engine, story, lambda conn, op: _nothing())


async def _nothing() -> None:
    return None


async def _current(engine: AsyncEngine, story_id: str, fact_id: str) -> canon.Fact | None:
    """The fact version when it is current (active and not closed), else ``None``."""
    async with engine.connect() as conn:
        found = await canon.canon(conn, story_id)
    return next((f for f in found if f.id == fact_id), None)


async def _corrupt(engine: AsyncEngine, story_id: str) -> None:
    await execute_in_tx(
        engine,
        ("UPDATE placement SET valid_from = -99 WHERE story_id = :s", {"s": story_id}),
        ("DELETE FROM relationships WHERE story_id = :s", {"s": story_id}),
        (
            "INSERT INTO scene_state (story_id, turn, variant, location_id, present_entity_ids, "
            "source_operation_id) VALUES (:s, 99, 1, NULL, '{}', :s)",
            {"s": story_id},
        ),
    )


async def _scenario(url: str, steps: list[tuple[str, int]]) -> None:
    engine = make_engine(auth_settings(url))
    try:
        w = await world(engine)
        things = await items(engine, w, "lamp", "key")
        written: list[str] = []
        for n, (step, number) in enumerate(steps):
            await _perform(engine, w, things, written, step, number + n)
        incremental = await snapshot(engine, w.story_id)
        OCCURRED["scene"] += bool(incremental["scene_state"])
        OCCURRED["closed"] += any(
            '"closed_by_fact_id": null' not in r for r in incremental["placement"]
        )

        # Only active facts project.
        active = set(
            await rows(
                engine,
                "SELECT id FROM facts WHERE story_id = :s AND membership = 'active'",
                s=w.story_id,
            )
        )
        sources = {
            r.source_fact_id
            for table in ("relationships", "placement")
            for r in await rows(
                engine,
                f"SELECT source_fact_id FROM {table} WHERE story_id = :s",  # noqa: S608
                s=w.story_id,
            )
        }
        assert sources <= {r.id for r in active}

        await _corrupt(engine, w.story_id)
        assert await snapshot(engine, w.story_id) != incremental
        result = await rebuild(engine, w.story_id, principal="p-operator")
        assert await snapshot(engine, w.story_id) == incremental  # byte for byte
        assert {t: result.counts[t] for t in TABLES} == {t: len(incremental[t]) for t in TABLES}
    finally:
        await engine.dispose()


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=histories)
# Always run once: the example makes every kind the occurrence check names occur.
@example(
    steps=[
        ("place", 0),
        ("hold", 1),
        ("relate", 2),
        ("present", 0),
        ("latent", 1),
        ("seed", 2),
        ("supersede", 0),
        ("retcon", 1),
        ("negate", 0),
        ("truncate", 1),
        ("untruncate", 0),
        ("rewind_play_and_swap", 1),
        ("regenerate", 0),
    ]
)
def _every_history(pg_url: str, steps: list[tuple[str, int]]) -> None:
    asyncio.run(_scenario(pg_url, steps))


@pytest.mark.part("M4", "FR-STORE-3")
def test_i_01__structured_rebuild(pg_url: str) -> None:
    """For random structured histories (placements, possession, relationships, presence,
    latent and position-less writes, negations, supersedes, retcons, truncations,
    untruncations, swaps and regenerations), the rows ``rebuild`` refolds from canon equal the
    incrementally maintained ones byte for byte, and every row's source fact is active.
    Complement: rows corrupted before the rebuild differ from them, and the rebuild replaces
    them. The run must have exercised every kind of change, or the equality says nothing."""
    OCCURRED.clear()
    _every_history(pg_url)
    kinds = (
        "place",
        "latent",
        "seed",
        "negate",
        "supersede",
        "retcon",
        "truncate",
        "untruncate",
        "swap",
        "regenerate",
        "scene",
        "closed",
    )
    assert all(OCCURRED[k] > 0 for k in kinds), OCCURRED
