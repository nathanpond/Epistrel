"""Claim detection over a real story (#148): the protected-content index and its rebase
re-check, the dependence hand-off to the commitment lifecycle, extraction's degradation and the
shadow sample's failure, the Lite complement, and ``GET /admin/evals/extraction-shadow``."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.canon.facts import Visibility
from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import Candidate
from epistrel.core.events import Operation
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.readset import StructuredReadSet
from epistrel.core.resolution.context import load_index
from epistrel.core.resolution.entities import NARRATOR
from epistrel.core.verify import protected
from epistrel.core.verify.detect import DeclaredClaim, DetectBeat
from epistrel.core.verify.protected import load_protected_index
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.jobs.handlers.retention import sweep_retention
from epistrel.orchestrator.verify import extraction
from epistrel.orchestrator.verify.extraction import (
    EXTRACTION_DEGRADED,
    ClaimDetector,
    DetectionConfig,
)
from tests import commit_support as c
from tests import detection_support as ds
from tests import verify_support as vs
from tests.auth_support import bearer
from tests.canon_support import commit
from tests.event_support import scalar
from tests.respond_support import respond_client

pytestmark = pytest.mark.integration


async def test_protected_index_and_its_rebase_recheck(engine: AsyncEngine) -> None:
    """Linda's protected index holds what she may not know: the ring of a fact only Marcus may
    read, and the knock thunk's candidate she has never heard of ("a courier"); Marcus's own
    index does not hold his fact, and an omniscient speaker has none. A hit becomes a
    ``protected_content`` claim whose permission is denied. The index is remembered for the
    read set: the rebase re-check passes while nothing changed and fails once a new fact is
    hidden from Linda."""
    t = await ds.tavern_story(engine)
    await commit(
        engine,
        t.story_id,
        Proposition(
            subject_id=t.marcus,
            predicate_id=ds.HOLDS,
            object_id=t.ring,
            valid_time=Interval.of(0, None),
        ),
        visibility=Visibility(scope="characters", characters=(t.marcus,)),
    )
    async with engine.connect() as conn:
        index = await load_index(conn, t.story_id)
        read_set = StructuredReadSet()
        linda = await load_protected_index(
            conn, t.story_id, t.linda, index=index, read_set=read_set
        )
        marcus = await load_protected_index(conn, t.story_id, t.marcus, index=index)
        omniscient = await load_protected_index(conn, t.story_id, "omniscient", index=index)
    assert linda is not None
    assert marcus is not None
    assert omniscient is None
    assert [term.folded for term in linda.terms] == ["ring"]
    assert marcus.terms == ()
    assert [(s, e) for s, e, _ in linda.match("Is that a RING?")] == [(10, 14)]

    op = SimpleNamespace(read_set_source=read_set)
    async with engine.connect() as conn:
        assert await protected.recheck(conn, op)  # type: ignore[arg-type]
    await commit(
        engine,
        t.story_id,
        Proposition(
            subject_id=t.marcus,
            predicate_id=ds.HOLDS,
            object_id=t.key,
            valid_time=Interval.of(0, None),
        ),
        visibility=Visibility(scope="engine"),
    )
    async with engine.connect() as conn:
        index = await load_index(conn, t.story_id)
        again = await load_protected_index(conn, t.story_id, t.linda, index=index)
        assert again is not None
        assert again.digest == linda.digest  # the key is known to Linda
    async with engine.begin() as conn:
        hidden_name = await _hidden_entity(conn, t.story_id)
    await commit(
        engine,
        t.story_id,
        Proposition(
            subject_id=t.marcus,
            predicate_id=ds.HOLDS,
            object_id=hidden_name,
            valid_time=Interval.of(0, None),
        ),
        visibility=Visibility(scope="characters", characters=(t.marcus,)),
    )
    async with engine.connect() as conn:
        assert not await protected.recheck(conn, op)  # type: ignore[arg-type]


async def _hidden_entity(conn: AsyncConnection, story_id: str) -> str:
    from epistrel.core.propositions import store

    return await store.add_entity(conn, story_id, "item", "brass locket")


async def test_protected_thunk_candidate_is_denied(engine: AsyncEngine) -> None:
    """An open thunk's candidate value Linda has no way to know ("the courier", an entity in
    no fact she may read) is protected for her: her ``say`` naming it is a detected claim (the
    knock with the courier as knocker), verified as novel and denied ``protected_content``."""
    k = await c.knock_story(engine)
    async with engine.begin() as conn:
        from epistrel.core.propositions import store

        courier = await store.add_entity(conn, k.story_id, "person", "the courier")
    sketch = c.knock_sketch(k).model_copy(
        update={"candidates": (Candidate(entity_id=courier, text="", confidence=0.4),)}
    )

    async def introduce(conn: AsyncConnection, op: Operation) -> Any:
        return await lifecycle.introduce(conn, op, sketch, valid_time=0, beat=2)

    await c.in_turn(engine, k.story_id, introduce)
    mal = vs.stub_models()
    beats = [DetectBeat(1, "say", "Was it the courier?", k.jenni)]
    found = await ds.detect_turn(engine, k.story_id, mal, beats)
    (claim,) = [x for x in found.checked if x.claim.category == "protected_content"]
    assert claim.claim.proposition is not None
    assert claim.claim.proposition.subject_id == courier
    assert claim.verdict == "denied"
    assert claim.result is not None
    assert claim.result.needs_repair


async def test_a_do_claim_filling_a_thunk_is_a_dependence(engine: AsyncEngine) -> None:
    """Bobby's ``do`` declares that he knocked: the declared claim fills the open knock thunk
    (``matched_thunk_id``), and ``apply_dependences`` hands it to ``lifecycle.depend``, which
    commits the value and resolves the thunk; a ``say`` with the same claim only mentions it (no
    dependence)."""
    k = await c.knock_story(engine)

    async def introduce(conn: AsyncConnection, op: Operation) -> Any:
        return await lifecycle.introduce(conn, op, c.knock_sketch(k), valid_time=0, beat=2)

    thunk = await c.in_turn(engine, k.story_id, introduce)
    knocked = c.knocker(k, k.bobby, 0)
    beats = [
        DetectBeat(
            1,
            "do",
            "Bobby was the one at the door.",
            k.bobby,
            (DeclaredClaim("d1", proposition=knocked),),
        ),
        DetectBeat(2, "say", "It was Bobby.", k.jenni, (DeclaredClaim("d2", proposition=knocked),)),
    ]
    found = await ds.detect_turn(engine, k.story_id, vs.stub_models(), beats)
    declared = [x for x in found.checked if x.claim.layer == "declared"]
    assert [x.claim.matched_thunk_id for x in declared] == [thunk.id, thunk.id]
    assert [x.claim.beat for x in extraction.dependences(found.checked)] == [1]

    async def depend(conn: AsyncConnection, op: Operation) -> Any:
        return await extraction.apply_dependences(
            conn, op, found.checked, read_set=StructuredReadSet()
        )

    (dependence,) = await c.in_turn(engine, k.story_id, depend)
    assert dependence.kind == "committed"
    resolved = await c.thunk(engine, k.story_id, thunk.id)
    assert resolved.state.value == "resolved"
    assert extraction.mentions_of(found.outcomes[0])[1][0]["entity_id"] == k.bobby


async def test_extraction_degrades_and_a_failed_sample_is_recorded(engine: AsyncEngine) -> None:
    """An extraction answer that stays invalid leaves the render on the deterministic layers
    (``extraction: failed``, ``extraction_degraded``); the turn still detects its possession
    claim. A shadow sample whose extraction fails is recorded ``failed`` and counted apart."""
    t = await ds.tavern_story(engine, mode="gated")
    mal = vs.stub_models(retries=0)
    mal.stub.script("extraction_backstop", ["not json"])
    beats = [
        DetectBeat(1, "do", "She pockets the key.", t.linda),
        DetectBeat(2, "narration", "Linda smiles at Marcus.", NARRATOR),
    ]
    found = await ds.detect_turn(
        engine, t.story_id, mal, beats, shadow_rate=1.0, scene_entity=t.tavern
    )
    (render,) = found.outcomes
    assert render.extraction == {1: "failed", 2: "skipped"}
    assert render.degradation == (EXTRACTION_DEGRADED,)
    assert found.report["degradation"] == [EXTRACTION_DEGRADED]
    assert [x.claim.category for x in found.checked if x.claim.beat == 1] == ["possession"]
    await ds.commit_turn(engine, t.story_id, found, beats)
    mal.stub.script("extraction_backstop", ["still not json"])
    await ds.run_shadow_jobs(engine, mal, t.story_id)
    (row,) = await ds.shadow_rows(engine, t.story_id)
    assert (row.status, row.extraction_claims, row.missed_count) == ("failed", 0, 0)


async def test_a_lite_story_detects_nothing_and_calls_nothing(engine: AsyncEngine) -> None:
    """Complement: a Lite story has no structured turn, so its configuration turns extraction
    off (no call is ever made) and its verification is off (nothing is verified)."""
    k = await c.knock_story(engine, structured=False)
    mal = vs.stub_models()
    async with engine.connect() as conn:
        config = await DetectionConfig.load(conn, k.story_id)
        assert not config.structured
        assert not config.extraction_on
        env = await extraction.load_env(
            conn, k.story_id, StructuredReadSet(), position=(2, 0), speakers=[k.bobby]
        )
    detector = ClaimDetector(
        replace(config, extraction_mode="always"), env, models=mal, budget=TurnBudget()
    )
    outcome = await detector.render(
        [DetectBeat(1, "do", "Bobby pockets the parcel.", k.bobby)], (2, 0), operation_id="x"
    )
    assert outcome.extraction == {1: "off"}
    assert not outcome.called
    assert mal.stub.calls == []
    found = await ds.detect_turn(
        engine, k.story_id, mal, [DetectBeat(1, "do", "He pockets the parcel.", k.bobby)]
    )
    assert all(x.result is None for x in found.checked)


async def test_extraction_shadow_route(engine: AsyncEngine, settings: Settings) -> None:
    """``GET /admin/evals/extraction-shadow`` answers the window's samples and, apart, the
    detected and checked totals; one ``story_id``; a ``from`` that is not an ISO date-time is a
    422 naming the field; and it needs the service credential."""
    t = await ds.tavern_story(engine, mode="gated")
    mal = vs.stub_models()
    mal.stub.script(
        "extraction_backstop",
        [ds.claims_answer(), ds.claims_answer({"beat": 2, "span": "smiles", "predicate": "x"})],
    )
    beats = [
        DetectBeat(1, "do", "She pockets the key.", t.linda),
        DetectBeat(2, "narration", "Linda smiles at Marcus.", NARRATOR),
    ]
    found = await ds.detect_turn(
        engine, t.story_id, mal, beats, shadow_rate=1.0, scene_entity=t.tavern
    )
    await ds.commit_turn(engine, t.story_id, found, beats)
    await ds.run_shadow_jobs(engine, mal, t.story_id)
    path = "/admin/evals/extraction-shadow"
    async with respond_client(settings, engine) as (client, _):
        answer = await client.get(path, params={"story_id": t.story_id}, headers=bearer())
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert (body["samples"], body["missed"], body["miss_rate"]) == (1, 1, 1.0)
        assert body["detected_total"] == found.report["detected"]
        assert body["checked_total"] == found.report["checked"]
        assert body["missed_by_category"] == {"extracted": 1}
        assert [i["operation_id"] for i in body["items"]] != []
        assert set(body) >= {"from", "to"}
        other = await client.get(path, params={"story_id": "01JS0000000000000000000099"})
        assert other.status_code == 401
        none = await client.get(
            path, params={"story_id": "01JS0000000000000000000099"}, headers=bearer()
        )
        assert none.json()["samples"] == 0
        bad = await client.get(path, params={"from": "yesterday"}, headers=bearer())
        assert bad.status_code == 422, bad.text
        assert bad.json()["error"]["details"]["field"] == "query.from"


async def test_shadow_samples_expire_after_their_retention(engine: AsyncEngine) -> None:
    """The retention sweep deletes an ``extraction_shadow`` row older than
    ``extraction_shadow_retention_days`` (90 by default; #455) and keeps a younger one: the
    sample, aged 89 days, survives a sweep, and aged 91 days it is gone."""
    t = await ds.tavern_story(engine, mode="gated")
    mal = vs.stub_models()
    mal.stub.script("extraction_backstop", [ds.claims_answer(), ds.claims_answer()])
    beats = [
        DetectBeat(1, "do", "She pockets the key.", t.linda),
        DetectBeat(2, "narration", "Linda smiles at Marcus.", NARRATOR),
    ]
    found = await ds.detect_turn(
        engine, t.story_id, mal, beats, shadow_rate=1.0, scene_entity=t.tavern
    )
    await ds.commit_turn(engine, t.story_id, found, beats)
    await ds.run_shadow_jobs(engine, mal, t.story_id)
    count = "SELECT count(*) FROM extraction_shadow WHERE story_id = :s"
    assert await scalar(engine, count, s=t.story_id) == 1
    for days, left in ((89, 1), (91, 0)):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE extraction_shadow SET created_at = :t WHERE story_id = :s"),
                {"t": datetime.now(UTC) - timedelta(days=days), "s": t.story_id},
            )
        swept = await sweep_retention(engine)
        assert swept["extraction_shadow"] >= 1 - left
        assert await scalar(engine, count, s=t.story_id) == left


# -- cost: the protected indexes read the canon once per snapshot (#452) ----------------------

OBJECTIVE_FACTS = 6
"""Shared facts every viewpoint reads: the canon the indexes must not decode per speaker."""


async def _canon_story(engine: AsyncEngine) -> ds.Tavern:
    """The tavern with ``OBJECTIVE_FACTS`` shared facts (Marcus holds each of as many items)
    and one fact only Marcus may read (the ring)."""
    t = await ds.tavern_story(engine)
    async with engine.begin() as conn:
        items = [
            await _store_entity(conn, t.story_id, f"trinket {n}") for n in range(OBJECTIVE_FACTS)
        ]
    for item in items:
        await commit(
            engine,
            t.story_id,
            Proposition(
                subject_id=t.marcus,
                predicate_id=ds.HOLDS,
                object_id=item,
                valid_time=Interval.of(0, None),
            ),
        )
    await commit(
        engine,
        t.story_id,
        Proposition(
            subject_id=t.marcus,
            predicate_id=ds.HOLDS,
            object_id=t.ring,
            valid_time=Interval.of(0, None),
        ),
        visibility=Visibility(scope="characters", characters=(t.marcus,)),
    )
    return t


async def _next_turn(engine: AsyncEngine, story_id: str) -> int:
    """The position a turn played now reads through: ``(next_turn_index, 0)``."""
    return int(
        await scalar(engine, "SELECT next_turn_index FROM stories WHERE id = :s", s=story_id)
    )


async def _store_entity(conn: AsyncConnection, story_id: str, name: str) -> str:
    from epistrel.core.propositions import store

    return await store.add_entity(conn, story_id, "item", name)


class _Counts:
    """Calls counted through monkeypatched module functions."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from epistrel.core.canon import facts as canon
        from epistrel.core.projections import structured as projections
        from epistrel.core.resolution import context

        self.decoded = 0
        self.relationships = 0
        self.indexes = 0
        fact_of, views, load = canon._fact_of, projections.relationships_views, context.load_index

        def counted_fact_of(row: Any, **extra: Any) -> Any:
            self.decoded += 1
            return fact_of(row, **extra)

        async def counted_views(*args: Any, **kwargs: Any) -> Any:
            self.relationships += 1
            return await views(*args, **kwargs)

        async def counted_load(*args: Any, **kwargs: Any) -> Any:
            self.indexes += 1
            return await load(*args, **kwargs)

        monkeypatch.setattr(canon, "_fact_of", counted_fact_of)
        monkeypatch.setattr(projections, "relationships_views", counted_views)
        monkeypatch.setattr(context, "load_index", counted_load)


async def test_detection_env_reads_canon_once_per_snapshot(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-PERF-2, NFR-PERF-4 (#452): a turn's detection environment builds three speakers'
    protected indexes (Linda, Marcus, the narrator) and resolution contexts from one snapshot.
    Only the versions a speaker may not read are decoded in full (the ring, for Linda and for
    the narrator: 2), never the shared canon per speaker (it was 2 x 8 versions per speaker),
    and the people's relationships are read once, not once per speaker. Each index is the one
    its own build answers (``load_protected_index`` alone, same terms)."""
    t = await _canon_story(engine)
    turn = await _next_turn(engine, t.story_id)
    counts = _Counts(monkeypatch)
    async with engine.connect() as conn:
        env = await extraction.load_env(
            conn,
            t.story_id,
            StructuredReadSet(),
            position=(turn, 0),
            speakers=[t.linda, t.marcus],
        )
    assert counts.decoded == 2
    assert counts.relationships == 1
    async with engine.connect() as conn:
        index = await load_index(conn, t.story_id)
        for speaker in (t.linda, t.marcus, NARRATOR):
            alone = await load_protected_index(
                conn, t.story_id, speaker, index=index, through_position=(turn, 0)
            )
            shared = env.protected_for(speaker)
            assert alone is not None
            assert shared is not None
            assert shared.digest == alone.digest
    linda = env.protected_for(t.linda)
    marcus = env.protected_for(t.marcus)
    assert linda is not None
    assert marcus is not None
    assert [term.folded for term in linda.terms] == ["ring"]
    assert marcus.terms == ()


async def test_rebase_recheck_rebuilds_each_index_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-PERF-3 (#452): the rebase re-check runs under the story lock. A turn that built the
    same indexes three times (its check, its composition, a repair re-check) is re-checked by
    rebuilding each distinct index once from one canon read and one entity index: 2 decoded
    versions (the ring, for Linda and the narrator), not 3 x 3 x 2 reads of the whole canon.
    It still fails once one of them changed."""
    t = await _canon_story(engine)
    turn = await _next_turn(engine, t.story_id)
    read_set = StructuredReadSet()
    for _ in range(3):
        async with engine.connect() as conn:
            await extraction.load_env(
                conn, t.story_id, read_set, position=(turn, 0), speakers=[t.linda, t.marcus]
            )
    assert len(protected.recorded(read_set)) == 9
    op = SimpleNamespace(read_set_source=read_set)
    counts = _Counts(monkeypatch)
    async with engine.connect() as conn:
        assert await protected.recheck(conn, op)  # type: ignore[arg-type]
    assert counts.decoded == 2
    assert counts.indexes == 1
    async with engine.begin() as conn:
        locket = await _hidden_entity(conn, t.story_id)
    await commit(
        engine,
        t.story_id,
        Proposition(
            subject_id=t.marcus,
            predicate_id=ds.HOLDS,
            object_id=locket,
            valid_time=Interval.of(0, None),
        ),
        visibility=Visibility(scope="characters", characters=(t.marcus,)),
    )
    async with engine.connect() as conn:
        assert not await protected.recheck(conn, op)  # type: ignore[arg-type]
