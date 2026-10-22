"""The commitment lifecycle's integration (#146; FR-COMMIT-2,7,10,13): once-only resolution,
reopening on revert, the Lite path, the inspection route, erasure, fork, the read-set key,
the open-thunk cap and the resolution exemption. U-01 and U-16 hold the requirement parts."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.commit import lifecycle, matching
from epistrel.core.commit.sketch import Candidate
from epistrel.core.commit.threads import open_threads
from epistrel.core.enums import OpenSlot
from epistrel.core.events import Operation
from epistrel.core.readset import KeyedLookup, StructuredReadSet, touched_keys
from epistrel.core.retrieval.pipeline import CONTEXT_SOURCES, SourceRequest
from epistrel.core.verify.permission import (
    Asserter,
    Basis,
    Denied,
    EntityInfo,
    PermissionFacts,
    Permitted,
    owner_of,
    permission,
)
from tests import commit_support as c
from tests import erasure_support as e
from tests.auth_support import bearer
from tests.fork_support import fork
from tests.projection_support import truncate, untruncate
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


async def _knock_turn(engine: AsyncEngine, k: c.Knock) -> str:
    """Turn 1: the salient knock (one entry), closed by the minimal commit. Its thunk's id."""

    async def step(conn: AsyncConnection, op: Operation) -> str:
        report = await c.salient(conn, op, k, k.beat(2, "narration", "A knock."), c.KNOCK)
        await lifecycle.close_turn(conn, op)
        return str(report.recorded[0].thunk_id)

    return str(await c.in_turn(engine, k.story_id, step))


async def _depend(
    engine: AsyncEngine, k: c.Knock, thunk_id: str, who: str, channel: lifecycle.Channel = "do"
) -> lifecycle.Dependence:
    async def step(conn: AsyncConnection, op: Operation) -> lifecycle.Dependence:
        found = await lifecycle.depend(
            conn, op, thunk_id, c.knocker(k, who, 0), beat=2, channel=channel
        )
        await lifecycle.close_turn(conn, op)
        return found

    found = await c.in_turn(engine, k.story_id, step)
    assert isinstance(found, lifecycle.Dependence)
    return found


async def _knocks(engine: AsyncEngine, k: c.Knock) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                text("SELECT count(*) FROM facts WHERE story_id = :s AND predicate_id = :p"),
                {"s": k.story_id, "p": k.knocked_at},
            )
            or 0
        )


async def test_a_global_thunk_resolves_once(engine: AsyncEngine) -> None:
    """FR-COMMIT-10: any character's first dependence resolves the thunk, once, globally: Jenni's
    ``think`` that it must be the mailman commits it; Bobby's later ``do`` on the same value
    reads the committed fact (``settled``) and one on another value is ``contradicted``.
    Complement: two dependences never write two facts."""
    k = await c.knock_story(engine)
    thunk_id = await _knock_turn(engine, k)
    first = await _depend(engine, k, thunk_id, k.mailman, "think")
    assert first.kind == "committed"
    assert first.fact is not None
    second = await _depend(engine, k, thunk_id, k.mailman)
    assert second.kind == "settled"
    assert second.fact is not None
    assert second.fact.id == first.fact.id
    other = await _depend(engine, k, thunk_id, k.bobby)
    assert other.kind == "contradicted"
    assert await _knocks(engine, k) == 1


async def test_reverting_the_resolving_turn_reopens_it(engine: AsyncEngine) -> None:
    """The thunk and its entry follow the turn that introduced them; the resolution and the
    discharge follow the turn that made them. Truncating the resolving turn leaves the thunk
    in the timeline, open again (re-resolvable, FR-COMMIT-5) with its entry undischarged, so
    it blocks the next turn's close, which discharges it by the minimal commit; untruncating
    restores the resolution. Complement: truncating the introducing turn takes the thunk out
    of the timeline altogether."""
    k = await c.knock_story(engine)

    async def salience_only(conn: AsyncConnection, op: Operation) -> str:
        report = await c.salient(conn, op, k, k.beat(2, "narration", "A knock."), c.KNOCK)
        return str(report.recorded[0].thunk_id)

    thunk_id = await c.in_turn(engine, k.story_id, salience_only)
    assert [b.thunk_id for b in await c.blockers(engine, k.story_id)] == [thunk_id]
    resolved = await _depend(engine, k, thunk_id, k.mailman)
    assert resolved.kind == "committed"
    assert await c.blockers(engine, k.story_id) == []

    await truncate(engine, k.story_id, 1)
    reopened = await c.thunk(engine, k.story_id, thunk_id)
    assert reopened.state.value == "open"
    assert reopened.stored_state.value == "resolved"  # the row is kept: read through
    assert [b.thunk_id for b in await c.blockers(engine, k.story_id)] == [thunk_id]
    await untruncate(engine, k.story_id)
    assert (await c.thunk(engine, k.story_id, thunk_id)).state.value == "resolved"
    assert await c.blockers(engine, k.story_id) == []

    await truncate(engine, k.story_id, 1)

    async def next_turn(conn: AsyncConnection, op: Operation) -> lifecycle.CommitReport:
        return await lifecycle.close_turn(conn, op)

    closed = await c.in_turn(engine, k.story_id, next_turn)
    assert closed.minimal_commits == 1
    assert await c.blockers(engine, k.story_id) == []

    await truncate(engine, k.story_id, 0)
    async with engine.connect() as conn:
        page = await lifecycle.inspect(conn, k.story_id)
    assert page.thunks == ()


async def test_lite_story_writes_nothing(engine: AsyncEngine) -> None:
    """Lite complement (#129): the same input on a story whose configuration does not run
    commitment writes no thunk and no ledger row, and nothing is owed: salience records
    ``inert``, an introduction answers nothing, a dependence is ``inert``, the close reports
    nothing, the context source is empty, and the inspection is 409 ``module_off``."""
    k = await c.knock_story(engine, structured=False)

    async def step(conn: AsyncConnection, op: Operation) -> dict[str, Any]:
        recorded = await c.salient(conn, op, k, k.beat(2, "narration", "A knock."), c.KNOCK)
        introduced = await lifecycle.introduce(conn, op, c.knock_sketch(k), valid_time=0)
        dependence = await lifecycle.depend(
            conn, op, "0" * 26, c.knocker(k, k.mailman, 0), beat=2, channel="do"
        )
        closed = await lifecycle.close_turn(conn, op)
        threads = await open_threads(conn, SourceRequest(k.story_id, (1, 0), "omniscient", ()))
        return {
            "recorded": recorded,
            "introduced": introduced,
            "dependence": dependence,
            "closed": closed,
            "threads": threads,
        }

    found = await c.in_turn(engine, k.story_id, step)
    assert [r.outcome for r in found["recorded"].recorded] == ["inert"]
    assert found["introduced"] is None
    assert found["dependence"].kind == "inert"
    assert found["closed"] == lifecycle.CommitReport()
    assert found["threads"] == []
    assert await c.rows(engine, "thunks", k.story_id) == []
    assert await c.rows(engine, "promissory_ledger", k.story_id) == []
    with pytest.raises(Exception, match="commitment") as refused:
        await lifecycle.inspect_ledger(engine, k.story_id)
    assert getattr(refused.value, "code", None) == "module_off"


async def test_inspect_ledger_route(engine: AsyncEngine, settings: Settings) -> None:
    """``GET /stories/{id}/inspect/ledger`` lists the thunks and the ledger to ``omniscient``
    (the minimally discharged knock, its entry with mode and discharging operation, and the
    background "why"), filters by state and discharge, pages by ``after_id``, and is 403
    ``omniscient_required`` for a character viewpoint and 409 ``module_off`` for Lite."""
    k = await c.knock_story(engine)
    thunk_id = await _knock_turn(engine, k)
    why = c.knock_sketch(k).model_copy(
        update={
            "subject_text": "the door",
            "subject_id": k.door,
            "relation_text": "knocked_for",
            "object_text": "something",
            "predicate_id": k.knocked_for,
            "object_id": None,
            "open_slot": OpenSlot.OBJECT,
        }
    )

    async def background(conn: AsyncConnection, op: Operation) -> str:
        found = await lifecycle.introduce(conn, op, why, valid_time=0, beat=2)
        assert found is not None
        return found.id

    why_id = await c.in_turn(engine, k.story_id, background)
    lite = await c.knock_story(engine, structured=False)
    path = f"/stories/{k.story_id}/inspect/ledger"
    async with respond_client(settings, engine) as (client, _):
        page = await client.get(path, params={"viewpoint": "omniscient"}, headers=bearer())
        assert page.status_code == 200, page.text
        body = page.json()
        assert [t["id"] for t in body["thunks"]] == sorted([thunk_id, why_id])
        knock = next(t for t in body["thunks"] if t["id"] == thunk_id)
        assert (knock["open_slot"], knock["state"], knock["salient"]) == ("subject", "open", True)
        assert knock["description"] == "someone knocked_at door"
        (entry,) = body["ledger"]
        assert (entry["thunk_id"], entry["kind"], entry["discharged_mode"]) == (
            thunk_id,
            "salient_reference",
            "minimal",
        )
        assert entry["discharged_by_operation"] is not None
        assert body["next_after_id"] is None

        open_only = await client.get(
            path, params={"viewpoint": "omniscient", "discharged": "false"}, headers=bearer()
        )
        assert open_only.json()["ledger"] == []
        first = await client.get(
            path, params={"viewpoint": "omniscient", "limit": 1}, headers=bearer()
        )
        assert len(first.json()["thunks"]) == 1
        cursor = first.json()["next_after_id"]
        rest = await client.get(
            path, params={"viewpoint": "omniscient", "after_id": cursor}, headers=bearer()
        )
        assert [t["id"] for t in rest.json()["thunks"]] == [max(thunk_id, why_id)]

        refused = await client.get(
            path, params={"viewpoint": f"character:{k.jenni}"}, headers=bearer()
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "omniscient_required"
        off = await client.get(
            f"/stories/{lite.story_id}/inspect/ledger",
            params={"viewpoint": "omniscient"},
            headers=bearer(),
        )
        assert off.status_code == 409, off.text
        assert off.json()["error"]["code"] == "module_off"


@pytest.fixture
def erasure_settings(pg_url: str) -> Settings:
    return e.erasure_settings(pg_url)


async def test_erasure_redacts_and_expires_thunks(
    engine: AsyncEngine, erasure_settings: Settings
) -> None:
    """An erasure of Jenni redacts every thunk sketch that names her and expires those thunks
    (no later dependence resolves them; the ledger ids stay), in its story pass; residue 0 in
    ``thunks``. Complement: a thunk that never named her is kept as it was."""
    k = await c.knock_story(engine)
    named = c.knock_sketch(k).model_copy(
        update={
            "object_text": "the door Jenni painted",
            "candidates": (Candidate(entity_id=k.jenni, text="Jenni", confidence=0.4),),
        }
    )
    other = c.knock_sketch(k).model_copy(
        update={"predicate_id": k.knocked_for, "relation_text": "knocked_for"}
    )

    async def step(conn: AsyncConnection, op: Operation) -> tuple[str, str]:
        a = await lifecycle.introduce(conn, op, named, valid_time=0, beat=2, salient=True)
        b = await lifecycle.introduce(conn, op, other, valid_time=0, beat=3)
        assert a is not None
        assert b is not None
        return a.id, b.id

    erased_id, kept_id = await c.in_turn(engine, k.story_id, step)
    before = {r.id: r for r in await c.rows(engine, "thunks", k.story_id)}
    async with respond_client(erasure_settings, engine) as (client, _):
        done = await e.erase(client, "character", k.jenni, k.story_id, confirm_all=True)
    assert done["residue"] == 0
    after = {r.id: r for r in await c.rows(engine, "thunks", k.story_id)}
    assert str(after[erased_id].state) == "expired"
    assert "Jenni" not in str(after[erased_id].proposition_sketch)
    assert after[erased_id].proposition_sketch["candidates"] == []
    assert after[kept_id].proposition_sketch == before[kept_id].proposition_sketch
    assert str(after[kept_id].state) == "open"
    residue = await e.residue(engine, k.story_id, ["Jenni"])
    assert residue.get("thunks", 0) == 0
    assert len(await c.rows(engine, "promissory_ledger", k.story_id)) == 1


async def test_fork_keeps_resolutions_through_n(engine: AsyncEngine) -> None:
    """A fork copies the thunks and entries with their operations: a resolution made at or
    before N is the fork's (its fact is the fork's copy), one made after N is not copied, so
    the thunk is open in the fork and its entry undischarged (complement)."""
    k = await c.knock_story(engine)

    async def two(conn: AsyncConnection, op: Operation) -> list[str]:
        out = []
        for sketch in (c.KNOCK, c.WHY):
            report = await c.salient(conn, op, k, k.beat(2, "narration", "A knock."), sketch)
            out.append(str(report.recorded[0].thunk_id))
        return out

    knock_id, _ = await c.in_turn(engine, k.story_id, two)
    await _depend(engine, k, knock_id, k.mailman)  # turn 2 resolves the knock
    early = await fork(engine, k.story_id, at_turn=2)
    late = await fork(engine, k.story_id, at_turn=1)

    async def states(story_id: str) -> dict[str, tuple[str, bool]]:
        async with engine.connect() as conn:
            page = await lifecycle.inspect(conn, story_id)
        done = {x.thunk_id: x.discharged for x in page.ledger}
        return {t.sketch.relation_text: (t.state.value, done.get(t.id, False)) for t in page.thunks}

    assert (await states(early.story_id))["knocked_at"] == ("resolved", True)
    assert (await states(late.story_id))["knocked_at"] == ("open", False)
    copied = await c.rows(engine, "thunks", early.story_id)
    facts = {r.id for r in await c.rows(engine, "facts", early.story_id)}
    assert {r.resolved_fact_id for r in copied if r.resolved_fact_id} <= facts


async def test_a_concurrent_resolution_touches_the_lookup(engine: AsyncEngine) -> None:
    """A turn that looked the open thunk up (``matching.dependence_target``, a query key of its
    read set) is touched when another turn commits the knock's resolution after its base
    revision, so the two can never both resolve it (FR-COMMIT-10). Scripted: the lookup, then
    the other turn's commit, then the check; no sleeps. Complement: with nothing committed
    since, the key is untouched."""
    k = await c.knock_story(engine)
    thunk_id = await _knock_turn(engine, k)
    read_set = StructuredReadSet()
    async with engine.connect() as conn:
        base = int(
            await conn.scalar(
                text("SELECT timeline_revision FROM stories WHERE id = :s"), {"s": k.story_id}
            )
            or 0
        )
        found = await matching.dependence_target(
            KeyedLookup(conn, read_set), k.story_id, c.knocker(k, k.mailman, 0)
        )
        assert found is not None
        assert found.id == thunk_id
        assert await touched_keys(conn, k.story_id, base, read_set.query_keys) == []
    await _depend(engine, k, thunk_id, k.bobby)
    async with engine.connect() as conn:
        assert await touched_keys(conn, k.story_id, base, read_set.query_keys)
        assert (
            await matching.dependence_target(
                KeyedLookup(conn, StructuredReadSet()), k.story_id, c.knocker(k, k.mailman, 0)
            )
            is None
        )


async def test_open_thunks_are_capped(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past ``thunks_max_open`` an introduction expires the oldest open thunk that is not
    salient; a salient one is never pruned (complement)."""
    k = await c.knock_story(engine)

    async def two(_: AsyncConnection) -> int:
        return 2

    monkeypatch.setattr(lifecycle, "max_open", two)
    sketches = [
        c.knock_sketch(k),
        c.knock_sketch(k).model_copy(update={"predicate_id": k.knocked_for}),
        c.knock_sketch(k).model_copy(update={"object_id": k.parcel}),
    ]

    async def step(conn: AsyncConnection, op: Operation) -> list[str]:
        out = []
        for n, sketch in enumerate(sketches):
            found = await lifecycle.introduce(conn, op, sketch, valid_time=0, salient=n == 0)
            assert found is not None
            out.append(found.id)
        await lifecycle.close_turn(conn, op)
        return out

    salient, oldest, newest = await c.in_turn(engine, k.story_id, step)
    states = {r.id: str(r.state) for r in await c.rows(engine, "thunks", k.story_id)}
    assert states == {salient: "open", oldest: "expired", newest: "open"}


def test_the_resolution_exemption() -> None:
    """#144's matrix with #146's exemption: Jenni's render binding the knock to Bobby (another
    modeled character's action) is ``other_character_secret`` as plain novelty, and permitted
    as a ``resolution`` when it resolves an open thunk; a locked fact still wins. The context
    source is registered on #141's hook."""
    bobby, door = "B" * 26, "D" * 26
    facts = PermissionFacts(
        entities={
            bobby: EntityInfo(bobby, "person", bobby),
            door: EntityInfo(door, "item"),
        }
    )
    claim = c.knocker(
        c.Knock("S" * 26, "C" * 26, bobby, "J" * 26, door, "M" * 26, "K" * 26, "F" * 26, "P" * 26),
        bobby,
        0,
    )
    jenni = Asserter("character", "J" * 26)
    plain = permission(claim, jenni, owner_of(claim, facts), facts)
    assert isinstance(plain, Denied)
    assert plain.reason.value == "other_character_secret"
    resolving = PermissionFacts(entities=facts.entities, resolves="T" * 26)
    found = permission(claim, jenni, owner_of(claim, resolving), resolving)
    assert found == Permitted(Basis.RESOLUTION)
    locked = PermissionFacts(entities=facts.entities, resolves="T" * 26, locked=True)
    assert isinstance(permission(claim, jenni, owner_of(claim, locked), locked), Denied)
    assert open_threads in CONTEXT_SOURCES
