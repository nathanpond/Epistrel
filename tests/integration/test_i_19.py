"""I-19 M7 (FR-PROF-8,9,10, FR-LITE-6): backfill a Lite story's structured data from its
transcript when it is switched to Full (#228). #229-#232 append their parts to this file.

Each test plays a long Lite story by direct commits (``tests/fixtures/long_lite_story``) on the
shared database, switches it to Full through ``PUT /stories/{id}/engine-profile`` and runs the
``backfill`` job step by step through a worker (``run_jobs``), with the stub's
``extraction_backstop`` scripted per planted line.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock
from epistrel.core.seeds.profiles import FULL_PRESET_ID, LITE_PRESET_ID
from epistrel.engine import Engine
from epistrel.orchestrator.jobs.handlers import backfill as backfill_job
from epistrel.orchestrator.jobs.types import ClaimedJob
from epistrel.orchestrator.models.registry import (
    EXTRACTION_BACKSTOP,
    GENERATION,
    REGENERATION,
)
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows, scalar
from tests.fixtures import long_lite_story as lls
from tests.offboarding_support import worker
from tests.respond_support import body as respond_body
from tests.respond_support import new_story, respond_client

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

WRITE = {**bearer(), "X-Epistrel-Principal": "p-backfill"}
READ = bearer()
FULL = {"profile_id": FULL_PRESET_ID, "ignore_warnings": True}


async def ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.text)
    found: dict[str, Any] = response.json()
    return found


async def _story(
    client: httpx.AsyncClient, engine: AsyncEngine, lib: Engine, messages: int, **kw: Any
) -> tuple[str, str, dict[str, str]]:
    story, ids = await new_story(client, "Ada", "Bobby", "Cal")
    await lls.long_lite_story(engine, story, ids["Ada"], messages, **kw)
    assert lib.models.stub is not None
    lls.script(lib.models.stub)
    backfill_job.bind(lib.models)
    return story, ids["Ada"], ids


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:test:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = 'backfill' AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def run_backfill(engine: AsyncEngine, story: str, steps: int = 500) -> int:
    """Run the story's ``backfill`` job a step at a time (each run requeues itself), at most
    ``steps`` runs, through a fresh worker's ``execute``; how many ran."""
    ran = 0
    runner = worker(engine)
    while ran < steps:
        now = clock.now()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    _CLAIM, {"s": story, "now": now, "lease": now + timedelta(seconds=60)}
                )
            ).one_or_none()
        if row is None:
            break
        await runner.execute(
            ClaimedJob(
                id=row.id,
                kind=row.kind,
                story_id=row.story_id,
                payload=dict(row.payload or {}),
                attempt=int(row.attempt),
                failures=int(row.failures),
                due_at=row.due_at,
                lease_until=row.lease_until,
                worker_id=row.worker_id,
            )
        )
        ran += 1
    return ran


async def _transcript(engine: AsyncEngine, story: str) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in await rows(
            engine,
            "SELECT message_id, narrative_turn, kind, role, text, membership::text "
            "FROM transcript_messages WHERE story_id = :s ORDER BY seq",
            s=story,
        )
    ]


def _calls(lib: Engine, *purposes: str) -> int:
    assert lib.models.stub is not None
    return sum(1 for c in lib.models.stub.calls if c.purpose in purposes)


async def _counts(engine: AsyncEngine, story: str) -> tuple[int, int, int]:
    return (
        await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story),
        await scalar(engine, "SELECT count(*) FROM operations WHERE story_id = :s", s=story),
        await scalar(engine, "SELECT count(*) FROM idempotency_keys WHERE scope_id = :s", s=story),
    )


@pytest.fixture
def stub_settings(pg_url: str) -> Any:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M7", "FR-PROF-8,9")
async def test_i_19__m7_backfill(stub_settings: Any, engine: AsyncEngine) -> None:
    """A 1,000-message Lite story switched to Full with no ``upgrade_mode`` runs a backfill
    (202): while it runs, respond is 409 ``story_paused`` with the backfill's progress and
    records nothing, and the transcript and effective-config (still Lite, with
    ``pending_backfill``) read. After it completes: the transcript is byte-identical; facts,
    memories (trust tier ``backfill``) and inferred exposures carry provenance ``backfill``, in
    non-decreasing positions; there is no ``think`` record and no generation, regeneration or
    repair call; the effective configuration is Full."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, actor, _ = await _story(client, engine, lib, 1000)
        before = await _transcript(engine, story)
        started = await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        assert started["applied"] is False
        assert started["backfill"]["status"] == "queued"
        assert started["impact"]["affected_stories"][0]["upgrade_unavailable"] is False
        counts = await _counts(engine, story)
        refused = await client.post(
            f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE
        )
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["details"]["cause"]) == ("story_paused", "backfill")
        assert set(error["details"]["progress"]) == {
            "phase",
            "chunks_done",
            "chunks_total",
            "reconciled_through_turn",
            "last_turn",
        }
        assert await _counts(engine, story) == counts
        assert (await client.get(f"/stories/{story}/history", headers=READ)).status_code == 200
        pending = await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
        assert pending["profile"]["id"] != FULL_PRESET_ID
        assert pending["pending_backfill"]["id"] == started["backfill"]["id"]
        generated = _calls(lib, GENERATION.name, REGENERATION.name, "repair")
        assert await run_backfill(engine, story) > 0
        done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
        assert done["status"] == "completed", done
        assert _calls(lib, GENERATION.name, REGENERATION.name, "repair") == generated
        assert _calls(lib, EXTRACTION_BACKSTOP.name) > 0
        assert await _transcript(engine, story) == before
        facts = await rows(
            engine,
            "SELECT provenance::text AS p, narrative_turn, membership::text AS m FROM facts "
            "WHERE story_id = :s AND operation_id = :o ORDER BY id",
            s=story,
            o=await scalar(
                engine, "SELECT operation_id FROM story_backfills WHERE id = :b", b=done["id"]
            ),
        )
        assert facts
        assert {(f.p, f.m) for f in facts} == {("backfill", "active")}
        turns = [f.narrative_turn for f in facts]
        assert turns == sorted(turns)
        memories = await rows(
            engine,
            "SELECT provenance::text AS p, trust_tier::text AS tier FROM memories "
            "WHERE story_id = :s AND provenance = 'backfill'",
            s=story,
        )
        assert memories
        assert {(m.p, m.tier) for m in memories} == {("backfill", "backfill")}
        exposed = await rows(engine, "SELECT inferred FROM exposures WHERE story_id = :s", s=story)
        assert exposed
        assert all(e.inferred for e in exposed)
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM memories WHERE story_id = :s AND kind::text = 'think'",
                s=story,
            )
            == 0
        )
        after = await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
        assert after["profile"]["id"] == FULL_PRESET_ID
        assert "pending_backfill" not in after
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE
        )
        assert played.status_code == 200, played.text


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_resume(stub_settings: Any, engine: AsyncEngine) -> None:
    """A run stopped after two chunks were extracted, and again mid-reconciliation, resumes
    from its checkpoints: no chunk is extracted twice (the extraction calls of a stopped and
    resumed run equal one uninterrupted run's), the cursor continues, and the backfill
    completes with the same facts."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _, _ = await _story(client, engine, lib, 240)
        baseline = _calls(lib, EXTRACTION_BACKSTOP.name)
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        assert await run_backfill(engine, story, 3) == 3  # begin, two chunks
        extracted = await scalar(
            engine,
            "SELECT count(*) FROM backfill_chunks c JOIN story_backfills b ON b.id = c.backfill_id "
            "WHERE b.story_id = :s AND c.status = 'extracted'",
            s=story,
        )
        assert extracted == 2
        assert _calls(lib, EXTRACTION_BACKSTOP.name) - baseline == 60  # two chunks of 30 turns
        assert await run_backfill(engine, story, 4) == 4
        cursor = await scalar(
            engine, "SELECT reconcile_cursor FROM story_backfills WHERE story_id = :s", s=story
        )
        assert cursor is not None
        assert await run_backfill(engine, story) > 0
        assert (
            await scalar(engine, "SELECT status FROM story_backfills WHERE story_id = :s", s=story)
            == "completed"
        )
        # One extraction call per turn, each made once: 120 turns.
        assert _calls(lib, EXTRACTION_BACKSTOP.name) - baseline == 120
        total = await scalar(
            engine,
            "SELECT count(*) FROM backfill_chunks c JOIN story_backfills b ON b.id = c.backfill_id "
            "WHERE b.story_id = :s",
            s=story,
        )
        assert total == 4


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_cancel(stub_settings: Any, engine: AsyncEngine) -> None:
    """``DELETE`` mid-reconciliation: the operation's rows are ``discarded`` (none deleted; no
    active row of it remains), the chunks are gone, the configuration is still Lite with no
    configuration change for the target, and respond plays. Complement: ``DELETE`` after the
    backfill ended is 409 ``backfill_not_cancellable`` and changes nothing."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, actor, _ = await _story(client, engine, lib, 240)
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story, 7)  # begin, 4 chunks, the switch, a span
        assert (
            await scalar(engine, "SELECT status FROM story_backfills WHERE story_id = :s", s=story)
            == "reconciling"
        )
        operation = await scalar(
            engine, "SELECT operation_id FROM story_backfills WHERE story_id = :s", s=story
        )
        changes = await scalar(
            engine, "SELECT count(*) FROM config_changes WHERE scope_id = :s", s=story
        )
        cancelled = await ok(await client.delete(f"/stories/{story}/backfill", headers=WRITE))
        assert cancelled["backfill"]["status"] == "cancelled"
        for table in ("facts", "memories", "exposures"):
            assert (
                await scalar(
                    engine,
                    f"SELECT count(*) FROM {table} WHERE operation_id = :o "  # noqa: S608
                    "AND membership = 'active'",
                    o=operation,
                )
                == 0
            ), table
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM backfill_chunks c JOIN story_backfills b "
                "ON b.id = c.backfill_id WHERE b.story_id = :s",
                s=story,
            )
            == 0
        )
        config = await ok(await client.get(f"/stories/{story}/effective-config", headers=READ))
        assert config["profile"]["id"] != FULL_PRESET_ID
        assert (
            await scalar(engine, "SELECT count(*) FROM config_changes WHERE scope_id = :s", s=story)
            == changes
        )
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE
        )
        assert played.status_code == 200, played.text
        again = await client.delete(f"/stories/{story}/backfill", headers=WRITE)
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "backfill_not_cancellable"


async def _ages(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT f.narrative_turn, f.tx_to IS NULL AS current, p.literal FROM facts f "
        "JOIN propositions p ON p.id = f.proposition_id JOIN predicates d ON d.id = f.predicate_id "
        "WHERE f.story_id = :s AND d.name = 'age' AND f.membership = 'active' "
        "ORDER BY f.narrative_turn",
        s=story,
    )


@pytest.mark.part("M7", "FR-PROF-10")
async def test_i_19__m7_last_mention_default(stub_settings: Any, engine: AsyncEngine) -> None:
    """Bobby thirty at turn 100 and thirty-five at 400, with no change narrated, under the
    default policy: canon is thirty-five for the overlap (the turn-100 version is closed) and a
    ``backfill_conflicts`` row records both statements, resolved ``last_mention``. Complement:
    with his birthday narrated at 250, the change writes facts and no conflict."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _, _ = await _story(client, engine, lib, 2 * lls.LATE)
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story)
        ages = await _ages(engine, story)
        assert (ages[0].narrative_turn, ages[0].current) == (lls.EARLY, False)
        latest = [a for a in ages if a.current and a.narrative_turn == lls.LATE]
        assert {a.literal["value"] for a in latest} >= {35}
        conflicts = await rows(
            engine,
            "SELECT status, resolution, statements FROM backfill_conflicts WHERE story_id = :s",
            s=story,
        )
        assert len(conflicts) == 1
        assert conflicts[0].status == "resolved"
        assert conflicts[0].resolution["policy"] == "last_mention"
        assert [s["position"] for s in conflicts[0].statements] == [lls.EARLY, lls.LATE]

        changed, _, _ = await _story(client, engine, lib, 2 * lls.LATE, changed=True)
        await ok(
            await client.put(f"/stories/{changed}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, changed)
        assert [a.narrative_turn for a in await _ages(engine, changed)][:2] == [
            lls.EARLY,
            lls.CHANGE,
        ]
        assert (
            await scalar(
                engine, "SELECT count(*) FROM backfill_conflicts WHERE story_id = :s", s=changed
            )
            == 0
        )


async def _policy_story(
    client: httpx.AsyncClient, engine: AsyncEngine, lib: Engine, policy: str | None
) -> tuple[str, str]:
    """A long Lite story (Bobby thirty at turn 100, thirty-five at 400) under ``policy``,
    switched to Full: its backfill run through its pass."""
    story, actor, _ = await _story(client, engine, lib, 2 * lls.LATE)
    if policy is not None:
        await ok(
            await client.patch(
                f"/stories/{story}/settings",
                json={"backfill_conflict_policy": policy},
                headers=WRITE,
            )
        )
    await ok(await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202)
    await run_backfill(engine, story)
    return story, actor


async def _claims(engine: AsyncEngine, story: str) -> list[int]:
    """The turns of the in-world claims (false utterances) the backfill recorded."""
    found = await rows(
        engine,
        "SELECT narrative_turn FROM utterances WHERE story_id = :s "
        "AND objective_relation = 'false' AND membership = 'active' ORDER BY narrative_turn",
        s=story,
    )
    return [int(r.narrative_turn) for r in found]


def _current(ages: list[Any]) -> set[Any]:
    return {a.literal["value"] for a in ages if a.current and a.narrative_turn >= lls.EARLY}


@pytest.mark.part("M7", "FR-PROF-10")
async def test_i_19__m7_conflicts(stub_settings: Any, engine: AsyncEngine) -> None:
    """The story's ``backfill_conflict_policy`` settles Bobby thirty at turn 100 against
    thirty-five at 400 (#230): ``last_mention`` makes thirty-five canon and turn 100's line an
    in-world claim, ``first_mention`` keeps thirty and makes turn 400's the claim. Under ``ask``
    the pass ends ``paused_for_conflicts`` with the conflict listed (its recommended answer the
    turn-400 statement) and the story paused: respond is 409 ``story_paused`` (``cause:
    backfill_conflicts``) and records nothing; a second resolve is ``backfill_conflict_closed``.
    Accepting the recommendation completes the backfill and resumes the story. Resumed with
    ``ignore_warnings`` instead, the conflict is ``uncertain`` and the backfill completes. The
    transcript is untouched in every branch."""
    async with respond_client(stub_settings, engine) as (client, lib):
        last, _ = await _policy_story(client, engine, lib, None)
        assert 35 in _current(await _ages(engine, last))
        assert await _claims(engine, last) == [lls.EARLY]

        first, _ = await _policy_story(client, engine, lib, "first_mention")
        ages = await _ages(engine, first)
        assert _current(ages) == {30}
        assert await _claims(engine, first) == [lls.LATE]
        (conflict,) = await rows(
            engine, "SELECT resolution FROM backfill_conflicts WHERE story_id = :s", s=first
        )
        assert conflict.resolution["policy"] == "first_mention"

        asked, actor = await _policy_story(client, engine, lib, "ask")
        before = await _transcript(engine, asked)
        shown = await ok(await client.get(f"/stories/{asked}/backfill", headers=READ))
        assert shown["status"] == "paused_for_conflicts"
        (listed,) = shown["conflict_list"]
        assert [s["turn"] for s in listed["statements"]] == [lls.EARLY, lls.LATE]
        assert listed["recommended"]["choose"] == listed["statements"][-1]["statement_id"]
        assert listed["statements"][0]["quote"] == lls.THIRTY
        counts = await _counts(engine, asked)
        refused = await client.post(
            f"/stories/{asked}/respond", json=respond_body(actor), headers=WRITE
        )
        assert refused.status_code == 409, refused.text
        details = refused.json()["error"]["details"]
        assert (details["cause"], details["conflicts_open"]) == ("backfill_conflicts", 1)
        assert await _counts(engine, asked) == counts
        held = await ok(await client.post(f"/stories/{asked}/backfill", json={}, headers=WRITE))
        assert (held["applied"], held["conflicts_open"]) == (False, 1)
        wrong = await client.post(
            f"/stories/{asked}/backfill/conflicts/{listed['id']}",
            json={"choose": "nonesuch:0-0"},
            headers=WRITE,
        )
        assert wrong.status_code == 422, wrong.text
        assert wrong.json()["error"]["code"] == "invalid_resolution"
        done = await ok(
            await client.post(
                f"/stories/{asked}/backfill/conflicts/{listed['id']}",
                json={"accept_recommendation": True},
                headers=WRITE,
            )
        )
        assert done["backfill"] == {"status": "completed", "conflicts_open": 0}
        assert done["conflict"]["resolution"]["via"] == "recommendation"
        assert 35 in _current(await _ages(engine, asked))
        assert await _claims(engine, asked) == [lls.EARLY]
        again = await client.post(
            f"/stories/{asked}/backfill/conflicts/{listed['id']}",
            json={"accept_recommendation": True},
            headers=WRITE,
        )
        assert again.status_code == 409, again.text
        assert again.json()["error"]["code"] == "backfill_not_awaiting_conflicts"
        assert await _transcript(engine, asked) == before
        played = await client.post(
            f"/stories/{asked}/respond", json=respond_body(actor), headers=WRITE
        )
        assert played.status_code == 200, played.text

        resumed, _ = await _policy_story(client, engine, lib, "ask")
        moved = await ok(
            await client.post(
                f"/stories/{resumed}/backfill", json={"ignore_warnings": True}, headers=WRITE
            )
        )
        assert (moved["applied"], moved["conflicts_made_uncertain"]) == (True, 1)
        (row,) = await rows(
            engine, "SELECT status FROM backfill_conflicts WHERE story_id = :s", s=resumed
        )
        assert row.status == "uncertain"
        assert (await ok(await client.get(f"/stories/{resumed}/backfill", headers=READ)))[
            "status"
        ] == "completed"


@pytest.mark.part("M7", "FR-PROF-10")
async def test_i_19__m7_conflict_value(stub_settings: Any, engine: AsyncEngine) -> None:
    """#708 (#230's resolve clause): under ``ask``, a resolution with a value neither statement
    states (thirty-three) and a ``valid_from`` window is normalized against the age predicate
    and written as canon; both statements become in-world claims and the conflict records the
    value and window. Complements: a value that does not normalize (text for an int age) and a
    window that ends before it starts are 422 ``invalid_resolution``, and nothing changes."""
    async with respond_client(stub_settings, engine) as (client, lib):
        asked, _ = await _policy_story(client, engine, lib, "ask")
        (listed,) = (await ok(await client.get(f"/stories/{asked}/backfill", headers=READ)))[
            "conflict_list"
        ]
        path = f"/stories/{asked}/backfill/conflicts/{listed['id']}"
        for wrong in ({"value": "thirty-three"}, {"value": 33, "valid_from": 9, "valid_to": 3}):
            refused = await client.post(path, json=wrong, headers=WRITE)
            assert refused.status_code == 422, refused.text
            assert refused.json()["error"]["code"] == "invalid_resolution"
        done = await ok(await client.post(path, json={"value": 33, "valid_from": 0}, headers=WRITE))
        assert done["backfill"] == {"status": "completed", "conflicts_open": 0}
        assert done["conflict"]["resolution"]["via"] == "value"
        assert done["conflict"]["resolution"]["choice"] == {
            "statement_id": None,
            "value": 33,
            "valid_from": 0,
            "valid_to": None,
        }
        assert 33 in _current(await _ages(engine, asked))
        assert await _claims(engine, asked) == [lls.EARLY, lls.LATE]


@pytest.mark.part("M7", "FR-LITE-6; FR-PROF-9")
async def test_i_19__m7_author_fact_estimate(stub_settings: Any, engine: AsyncEngine) -> None:
    """#717 (#244's upgrade clause: "the estimate counts one parse call per active fact"): a
    Lite story's upgrade estimate grows by one model call per active lasting author fact, in
    the held change's report and in ``impact-preview`` alike."""
    import json

    async def estimates(client: httpx.AsyncClient, story: str) -> tuple[int, int]:
        held = await ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": FULL_PRESET_ID},
                headers=WRITE,
            )
        )
        assert held["applied"] is False, held
        (entry,) = held["impact"]["affected_stories"]
        preview = await ok(
            await client.post(
                f"/stories/{story}/impact-preview",
                json={"profile_id": FULL_PRESET_ID},
                headers=WRITE,
            )
        )
        (seen,) = preview["affected_stories"]
        return (
            int(entry["backfill"]["estimate"]["model_calls"]),
            int(seen["backfill"]["estimate"]["model_calls"]),
        )

    async with respond_client(stub_settings, engine) as (client, lib):
        story, _, _ = await _story(client, engine, lib, 2 * lls.EARLY)
        stub = lib.models.stub
        assert stub is not None
        before = await estimates(client, story)
        assert min(before) > 0
        for text_ in ("Bobby is forty years old.", "The air smells of rain."):
            stub.when(
                text_,
                json.dumps({"classification": "durable", "temporal_scope": "now", "changes": []}),
                purpose="directive_parse",
            )
            made = await client.post(
                f"/stories/{story}/directives",
                params={"viewpoint": "omniscient"},
                json={"text": text_},
                headers=WRITE,
            )
            assert made.status_code == 200, made.text
        after = await estimates(client, story)
        assert (after[0] - before[0], after[1] - before[1]) == (2, 2)


@pytest.mark.part("M7", "FR-LITE-6; FR-PROF-9")
async def test_i_19__m7_author_facts(stub_settings: Any, engine: AsyncEngine) -> None:
    """#699 (#244's upgrade clause): a Lite story holds two lasting author facts at a slot, one
    ``directive_parse`` turns into a structured change ("Bobby is forty") and one it does not
    ("The air smells of rain"). Switched to Full: the backfill converts the first into an
    author-tier fact at the fact's slot (``provenance = author``) and keeps the second as text;
    its report lists ``author_facts {converted: 1, kept_as_text: 1}``. The kept fact is in the
    narrator's author notes from its position on, and the converted one is not (it is canon
    now); a character render of the next turn carries neither."""
    import json

    async with respond_client(stub_settings, engine) as (client, lib):
        story, actor, _ = await _story(client, engine, lib, 2 * lls.EARLY)
        stub = lib.models.stub
        assert stub is not None
        forty = "Bobby is forty years old."
        rain = "The air smells of rain."
        for text_ in (forty, rain):
            stub.when(
                text_,
                json.dumps({"classification": "durable", "temporal_scope": "now", "changes": []}),
                purpose="directive_parse",
            )
            made = await client.post(
                f"/stories/{story}/directives",
                params={"viewpoint": "omniscient"},
                json={"text": text_},
                headers=WRITE,
            )
            assert made.status_code == 200, made.text
        # The upgrade's parse: forty is a structured change, the rain is not.
        stub.reset()
        lls.script(stub)
        stub.when(
            forty,
            json.dumps(
                {
                    "classification": "durable",
                    "temporal_scope": "now",
                    "changes": [
                        {
                            "kind": "proposition",
                            "subject": "Bobby",
                            "predicate": "age",
                            "literal": 40,
                        }
                    ],
                }
            ),
            purpose="directive_parse",
        )
        stub.when(
            rain,
            json.dumps({"classification": "durable", "temporal_scope": "now", "changes": []}),
            purpose="directive_parse",
        )
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story)
        done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
        assert done["status"] == "completed", done
        (report,) = await rows(
            engine, "SELECT report FROM story_backfills WHERE id = :b", b=done["id"]
        )
        summary = report.report["author_facts"]
        assert (summary["converted"], summary["kept_as_text"]) == (1, 1)
        authored = await rows(
            engine,
            "SELECT f.provenance::text AS p, f.authority::text AS a, pr.literal FROM facts f "
            "JOIN propositions pr ON pr.id = f.proposition_id "
            "WHERE f.story_id = :s AND f.authority = 'author' AND f.membership = 'active'",
            s=story,
        )
        assert [(a.p, a.a, a.literal["value"]) for a in authored] == [("author", "author", 40)]
        stub.reset()
        played = await client.post(
            f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE
        )
        assert played.status_code == 200, played.text
        prompts = [
            "\n".join(m.content for m in c.messages)
            for c in stub.calls
            if c.purpose == "generation"
        ]
        # A character render never carries the kept fact (NFR-PRIV-4: a character learns it only
        # through its context); the narrator's author notes do (``TurnRead.director``).
        assert prompts
        assert not any(rain in p for p in prompts)
        from epistrel.orchestrator.directives import kept_author_facts

        async with engine.connect() as conn:
            block = await kept_author_facts(conn, story, 10**6)
        assert rain in block
        assert forty not in block


@pytest.mark.part("M7", "FR-LITE-6; FR-PROF-9")
async def test_i_19__m7_downgrade(stub_settings: Any, engine: AsyncEngine) -> None:
    """#232: a 400-turn Lite story upgraded to Full (backfilled), then switched back to Lite:
    the downgrade applies at once (no pause, no structured backfill), reports
    ``data_kept_dormant`` and ``summary_backfill``, keeps every fact (same ids and membership)
    and closes the structured coverage at turn 400, and starts a ``resummarize`` backfill: a
    Lite reply carries ``summary_rebuilding`` until the summary job catches up, and the row
    then completes. More Lite turns, then Full again: the backfill's range is exactly the turns
    played since, and the facts from before the downgrade are still there with their ids."""
    from epistrel.orchestrator.jobs.handlers import lite_summary as lite_summary_job
    from tests.summary_support import run_due_jobs

    async with respond_client(stub_settings, engine) as (client, lib):
        story, actor, _ = await _story(client, engine, lib, 2 * lls.LATE)
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story)
        facts = [
            tuple(r)
            for r in await rows(
                engine,
                "SELECT id, membership::text FROM facts WHERE story_id = :s ORDER BY id",
                s=story,
            )
        ]
        assert facts
        down = await ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": LITE_PRESET_ID, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        assert down["applied"] is True
        (entry,) = down["impact"]["affected_stories"]
        assert entry["data_kept_dormant"] is True
        assert entry["summary_backfill"]["needed"] is True
        assert entry["summary_backfill"]["estimate"]["model_calls"] > 0
        (rebuild,) = await rows(
            engine,
            "SELECT id, kind, status, capabilities, range_from_turn, range_to_turn "
            "FROM story_backfills WHERE story_id = :s AND kind = 'resummarize'",
            s=story,
        )
        assert rebuild.status == "extracting"
        # #709: the rolling summary's coverage capability, its range the coverage's gap, and the
        # lite_summary job's rebuild mode queued for it.
        assert list(rebuild.capabilities) == ["rolling_summary"]
        assert (rebuild.range_from_turn, rebuild.range_to_turn) == (1, lls.LATE)
        (queued,) = await rows(
            engine,
            "SELECT payload FROM jobs WHERE story_id = :s AND kind = 'lite_summary' "
            "AND coalesce_key = :k",
            s=story,
            k=f"lite_summary_rebuild:{story}",
        )
        assert queued.payload["backfill_id"] == str(rebuild.id)
        kept = [
            tuple(r)
            for r in await rows(
                engine,
                "SELECT id, membership::text FROM facts WHERE story_id = :s ORDER BY id",
                s=story,
            )
        ]
        assert kept == facts
        closed = await rows(
            engine,
            "SELECT to_turn FROM capability_coverage WHERE story_id = :s "
            "AND capability = 'pipeline' AND level = 'structured'",
            s=story,
        )
        assert [r.to_turn for r in closed] == [lls.LATE]
        played = await ok(
            await client.post(f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE)
        )
        assert "summary_rebuilding" in played["degradation"]
        assert lib.models is not None
        for _ in range(40):  # each run compresses at most twenty chunks
            if not await lite_summary_job.schedule(engine, story):
                break
            await run_due_jobs(engine, lib.models, story, limit=5)
        caught_up = await ok(
            await client.post(f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE)
        )
        assert "summary_rebuilding" not in caught_up["degradation"]
        (done,) = await rows(
            engine,
            "SELECT status FROM story_backfills WHERE story_id = :s AND kind = 'resummarize'",
            s=story,
        )
        assert done.status == "completed"
        covered = await rows(
            engine,
            "SELECT from_turn, to_turn FROM capability_coverage WHERE story_id = :s "
            "AND capability = 'rolling_summary'",
            s=story,
        )
        assert [(c.from_turn, c.to_turn) for c in covered] == [(1, lls.LATE)]

        await lls.long_lite_story(engine, story, actor, 20)  # ten more turns
        up = await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        latest = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
        assert latest["kind"] == "upgrade"
        assert latest["range"] == {"from_turn": lls.LATE + 1, "to_turn": lls.LATE + 12}
        assert up["backfill"]["id"] == latest["id"]
        await run_backfill(engine, story)
        again = {
            str(r.id)
            for r in await rows(
                engine,
                "SELECT id FROM facts WHERE story_id = :s AND membership = 'active'",
                s=story,
            )
        }
        assert {f[0] for f in facts if f[1] == "active"} <= again


@pytest.mark.part("M7", "FR-LITE-6; FR-PROF-9")
async def test_i_19__m7_resummarize_cancel(stub_settings: Any, engine: AsyncEngine) -> None:
    """#709 (#232's cancel clause): a downgrade's summarization rebuild is cancelled
    (``DELETE /stories/{id}/backfill``) after some segments are written: they are discarded
    with their operations, the next Lite prompt reads none of them, and the story stays on
    Lite with no ``summary_rebuilding``. A Lite turn under the downgrade reads nothing
    structured (spies on retrieval, verification and the belief readers count zero)."""
    from epistrel.core.epistemic import partitions
    from epistrel.core.retrieval import pipeline as retrieval
    from epistrel.core.verify import rules
    from epistrel.orchestrator.jobs.handlers import lite_summary as lite_summary_job
    from tests.summary_support import run_due_jobs

    async with respond_client(stub_settings, engine) as (client, lib):
        story, actor, _ = await _story(client, engine, lib, 2 * lls.EARLY)
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story)
        await ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={"profile_id": LITE_PRESET_ID, "ignore_warnings": True},
                headers=WRITE,
            )
        )
        assert lib.models is not None
        await run_due_jobs(engine, lib.models, story, limit=1)
        written = await rows(
            engine,
            "SELECT s.segment_id FROM lite_summary_segments s JOIN operations o "
            "ON o.operation_id = s.operation_id WHERE s.story_id = :s "
            "AND o.membership = 'active'",
            s=story,
        )
        assert written, "the rebuild wrote segments before the cancel"
        cancelled = await client.delete(f"/stories/{story}/backfill", headers=WRITE)
        assert cancelled.status_code == 200, cancelled.text
        (ended,) = await rows(
            engine,
            "SELECT status FROM story_backfills WHERE story_id = :s AND kind = 'resummarize'",
            s=story,
        )
        assert ended.status == "cancelled"
        left = await rows(
            engine,
            "SELECT s.segment_id FROM lite_summary_segments s JOIN operations o "
            "ON o.operation_id = s.operation_id WHERE s.story_id = :s "
            "AND o.membership = 'active'",
            s=story,
        )
        assert left == []
        rows_left = await rows(
            engine,
            "SELECT segment_id FROM lite_summary_segments WHERE story_id = :s "
            "AND membership = 'active'",
            s=story,
        )
        assert rows_left == []  # the rows follow their operations (a later run is not dropped)

        seen: list[str] = []
        real_retrieve, real_check = retrieval.retrieve, rules.check_with

        async def spy_retrieve(*args: Any, **kwargs: Any) -> Any:
            seen.append("retrieve")
            return await real_retrieve(*args, **kwargs)

        async def spy_check(*args: Any, **kwargs: Any) -> Any:
            seen.append("check_with")
            return await real_check(*args, **kwargs)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(retrieval, "retrieve", spy_retrieve)
            patched.setattr(rules, "check_with", spy_check)
            if hasattr(partitions, "beliefs_of"):
                real_beliefs = partitions.beliefs_of

                async def spy_beliefs(*args: Any, **kwargs: Any) -> Any:
                    seen.append("beliefs")
                    return await real_beliefs(*args, **kwargs)

                patched.setattr(partitions, "beliefs_of", spy_beliefs)
            played = await ok(
                await client.post(
                    f"/stories/{story}/respond", json=respond_body(actor), headers=WRITE
                )
            )
        assert seen == []
        assert "summary_rebuilding" not in played["degradation"]
        del lite_summary_job


async def test_backfill__gap_fill(stub_settings: Any, engine: AsyncEngine) -> None:
    """``POST /stories/{id}/backfill`` without ``ignore_warnings`` answers the preview and writes
    no backfill; with it, 202 and a backfill; once it completes, the story has no gap left: 409
    ``nothing_to_backfill``."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _, _ = await _story(client, engine, lib, 60)
        await ok(
            await client.put(
                f"/stories/{story}/engine-profile",
                json={**FULL, "upgrade_mode": "backfill"},
                headers=WRITE,
            ),
            202,
        )
        await run_backfill(engine, story)
        await engine_reset_coverage(engine, story)
        preview = await ok(await client.post(f"/stories/{story}/backfill", json={}, headers=WRITE))
        assert preview["applied"] is False
        assert preview["backfill"]["needed"] is True
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM story_backfills WHERE story_id = :s AND kind = 'gap_fill'",
                s=story,
            )
            == 0
        )
        await ok(
            await client.post(
                f"/stories/{story}/backfill", json={"ignore_warnings": True}, headers=WRITE
            ),
            202,
        )
        await run_backfill(engine, story)
        none = await client.post(
            f"/stories/{story}/backfill", json={"ignore_warnings": True}, headers=WRITE
        )
        assert none.status_code == 409, none.text
        assert none.json()["error"]["code"] == "nothing_to_backfill"


async def test_backfill__erased_subject(pg_url: str, engine: AsyncEngine) -> None:
    """#228: a subject erased before the backfill is never re-derived: Bobby, planted at turn
    ``EARLY`` ("Bobby is thirty"), is erased; the backfill to Full then writes no fact whose
    subject is Bobby and no memory naming him. Complement: the backfill completes and writes
    other memories."""
    from tests import erasure_support as es

    settings = es.erasure_settings(pg_url)
    async with respond_client(settings, engine) as (client, lib):
        story, _, ids = await _story(client, engine, lib, 2 * lls.EARLY)
        await es.erase(client, "character", ids["Bobby"], story, confirm_all=True)
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story)
        done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
        assert done["status"] == "completed", done
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM facts WHERE story_id = :s AND subject_id = :b",
                s=story,
                b=ids["Bobby"],
            )
            == 0
        )
        texts = await rows(
            engine,
            "SELECT text FROM memories WHERE story_id = :s AND provenance = 'backfill'",
            s=story,
        )
        assert texts
        assert not any(es.naming(str(t.text), ["Bobby"]) for t in texts)


async def engine_reset_coverage(engine: AsyncEngine, story: str) -> None:
    """Drop verification's backfilled interval: a gap the gap fill must cover."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM capability_coverage WHERE story_id = :s "
                "AND capability = 'verification' AND source = 'backfill'"
            ),
            {"s": story},
        )


@pytest.mark.part("M7", "FR-LITE-6,8; FR-PROF-9")
async def test_i_19__m7_lite_corrections(stub_settings: Any, engine: AsyncEngine) -> None:
    """#700: a Lite rewrite whose correction states a world change ("Bobby is forty.") applies
    with a ``lite_note`` part. Switched to Full, the backfill parses the note like a lasting
    author fact (``directive_parse``) and writes it as an author-tier fact; its report counts
    it among ``author_facts.converted``, keyed ``note:<part id>``."""
    import json

    forty = "Bobby is forty."
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _, _ = await _story(client, engine, lib, 2 * lls.EARLY)
        stub = lib.models.stub
        assert stub is not None
        stub.when(
            "",
            json.dumps({"change_statement": forty, "world_change": True}),
            purpose="lite_rewrite_parse",
        )
        stub.when("", json.dumps({"hits": []}), purpose="lite_rewrite_reach")
        opened = await ok(
            await client.post(
                f"/stories/{story}/rewrites",
                params={"viewpoint": "omniscient"},
                json={"instruction": forty, "preview": "apply_directly"},
                headers=WRITE,
            ),
            201,
        )
        assert opened["state"] == "applied", opened
        (note,) = await rows(
            engine,
            "SELECT id, patch_after->>'text' AS body FROM amendment_parts "
            "WHERE story_id = :s AND kind = 'lite_note'",
            s=story,
        )
        assert note.body == f"Author correction: {forty}"
        stub.when(
            forty,
            json.dumps(
                {
                    "classification": "durable",
                    "temporal_scope": "now",
                    "changes": [
                        {
                            "kind": "proposition",
                            "subject": "Bobby",
                            "predicate": "age",
                            "literal": 40,
                        }
                    ],
                }
            ),
            purpose="directive_parse",
        )
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story)
        done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
        assert done["status"] == "completed", done
        (report,) = await rows(
            engine, "SELECT report FROM story_backfills WHERE id = :b", b=done["id"]
        )
        summary = report.report["author_facts"]
        assert f"note:{note.id}" in summary["ids"]
        authored = await rows(
            engine,
            "SELECT pr.literal FROM facts f JOIN propositions pr ON pr.id = f.proposition_id "
            "WHERE f.story_id = :s AND f.authority = 'author' AND f.membership = 'active'",
            s=story,
        )
        assert [a.literal["value"] for a in authored] == [40]


# -- #228's remaining record kinds (M7 retry 4) ---------------------------------------------------


def _age_claim(beat: int, span: str, value: str) -> str:
    import json

    return json.dumps(
        {
            "claims": [
                {
                    "beat": beat,
                    "span": span,
                    "subject": "Bobby",
                    "predicate": "age",
                    "object": value,
                    "time": None,
                    "polarity": "affirmed",
                    "confidence": 0.95,
                }
            ]
        }
    )


async def _entity(engine: AsyncEngine, story: str, kind: str, name: str) -> str:
    from epistrel.core.propositions import store

    async with engine.begin() as conn:
        return await store.add_entity(conn, story, kind, name)


async def _report(engine: AsyncEngine, backfill_id: str) -> dict[str, Any]:
    (found,) = await rows(engine, "SELECT report FROM story_backfills WHERE id = :b", b=backfill_id)
    return dict(found.report)


async def _played(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    lib: Engine,
    lines: dict[int, tuple[str, str]],
    *names: str,
    messages: int = 10,
) -> tuple[str, dict[str, str]]:
    """A Lite story of ``names`` (the first the player) with ``lines`` planted, played by
    direct commits."""
    story, ids = await new_story(client, *names)
    await lls.long_lite_story(engine, story, ids[names[0]], messages, lines=lines)
    assert lib.models.stub is not None
    backfill_job.bind(lib.models)
    return story, ids


async def _switch(client: httpx.AsyncClient, engine: AsyncEngine, story: str) -> dict[str, Any]:
    await ok(await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202)
    await run_backfill(engine, story)
    done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
    assert done["status"] == "completed", done
    return done


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_placements(stub_settings: Any, engine: AsyncEngine) -> None:
    """#228: a turn whose text names a place places every character present there, before its
    messages are routed: ``located_at`` facts with provenance ``backfill``, active after the
    run. Complements: a turn naming no place writes no placement, and a later turn naming the
    same place does not place again a character canon already places there; one naming
    another place moves only who is present."""
    async with respond_client(stub_settings, engine) as (client, lib):
        lines = {
            2: ("Let's go in.", "Bobby and Cal walk into the kitchen."),
            3: ("Nice.", "Bobby and Cal look around the kitchen."),
            4: ("Hello.", "Cal waits."),
            5: ("Come on.", "Bobby steps out onto the porch."),
        }
        story, ids = await new_story(client, "Ada", "Bobby", "Cal")
        kitchen = await _entity(engine, story, "place", "kitchen")
        porch = await _entity(engine, story, "place", "porch")
        await lls.long_lite_story(engine, story, ids["Ada"], 12, lines=lines)
        backfill_job.bind(lib.models)
        done = await _switch(client, engine, story)
        placed = await rows(
            engine,
            "SELECT f.subject_id, pr.object_id, f.narrative_turn, f.provenance::text AS p, "
            "f.membership::text AS m, f.tx_to IS NULL AND f.valid_to IS NULL AS now "
            "FROM facts f JOIN propositions pr "
            "ON pr.id = f.proposition_id WHERE f.story_id = :s AND f.predicate_id = :p "
            "ORDER BY f.narrative_turn, f.subject_id",
            s=story,
            p="01JPRED0000000000000000002",
        )
        assert {(r.p, r.m) for r in placed} == {("backfill", "active")}
        # Written at turn 2 (all three) and 5 (the two present); none at 3 (already placed
        # there) or 4 (no place named).
        assert {int(r.narrative_turn) for r in placed} == {2, 5}
        assert {(str(r.subject_id), str(r.object_id)) for r in placed if r.now} == {
            (ids["Ada"], porch),
            (ids["Bobby"], porch),
            (ids["Cal"], kitchen),
        }
        assert (await _report(engine, done["id"]))["placements"] == 5


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_testimony(stub_settings: Any, engine: AsyncEngine) -> None:
    """#228 (#162): a witness of a backfilled ``say`` whose partition already holds something
    about the claim's subject takes it as testimony through the live writers: a ``said``
    belief and its decision (Cal, who observed Bobby to be forty, rejects "fifty"), every
    transition ``inferred``, and its exposure recorded resolved. Complement: Dee, present but
    holding nothing about Bobby, gets an unresolved inferred exposure and no transition."""
    async with respond_client(stub_settings, engine) as (client, lib):
        lines = {
            2: ("Hello.", "Cal sees that Bobby is forty years old."),
            3: ("Bobby is fifty years old.", "Cal frowns at Dee."),
        }
        story, ids = await _played(client, engine, lib, lines, "Ada", "Bobby", "Cal", "Dee")
        stub = lib.models.stub
        assert stub is not None
        stub.when(
            "Cal sees that Bobby",
            _age_claim(1, "Bobby is forty years old", "40"),
            purpose=EXTRACTION_BACKSTOP.name,
        )
        stub.when(
            "Bobby is fifty years old.",
            _age_claim(0, "Bobby is fifty years old", "50"),
            purpose=EXTRACTION_BACKSTOP.name,
        )
        done = await _switch(client, engine, story)
        said = await rows(
            engine,
            "SELECT holder_character_id AS h, kind::text AS k, outcome, inferred "
            "FROM belief_transitions WHERE story_id = :s AND holder_character_id = :c "
            "AND outcome IS NOT NULL",
            s=story,
            c=ids["Cal"],
        )
        assert said, "Cal's testimony"
        assert all(r.inferred for r in said)
        assert "reject" in {r.outcome for r in said}
        cal = await rows(
            engine,
            "SELECT resolved_by_transition, inferred FROM exposures WHERE story_id = :s "
            "AND witness_character_id = :c",
            s=story,
            c=ids["Cal"],
        )
        assert cal
        assert all(e.inferred and e.resolved_by_transition is not None for e in cal)
        dee = await rows(
            engine,
            "SELECT resolved_by_transition, inferred FROM exposures WHERE story_id = :s "
            "AND witness_character_id = :d",
            s=story,
            d=ids["Dee"],
        )
        assert dee
        assert all(e.inferred and e.resolved_by_transition is None for e in dee)
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM belief_transitions WHERE story_id = :s "
                "AND holder_character_id = :d",
                s=story,
                d=ids["Dee"],
            )
            == 0
        )
        assert (await _report(engine, done["id"]))["testimonies"] >= 1


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_commitments(stub_settings: Any, engine: AsyncEngine) -> None:
    """#228 (#146): a reference ``salience_detection`` marks salient ("Someone knocks at the
    door") becomes an open thunk with a ledger entry under the backfill's operation, and a
    later narrated claim that fills it ("The mailman knocked at the door") settles it through
    ``lifecycle.depend``: its resolution fact has provenance ``backfill``. The extraction
    checkpoint keeps the reference's offsets, never its words. Complement: a second salient
    reference nothing fills stays open."""
    import json

    from epistrel.core.propositions import registry
    from epistrel.orchestrator.models.registry import SalienceSketch
    from tests import commit_support as c

    async with respond_client(stub_settings, engine) as (client, lib):
        lines = {
            2: ("Hello.", "Someone knocks at the door."),
            3: ("What was that?", "Something rattles the window."),
            4: ("Who was it?", "The mailman knocked at the door."),
        }
        story, ids = await new_story(client, "Ada", "Bobby", "Cal")
        await _entity(engine, story, "item", "door")
        await _entity(engine, story, "item", "window")
        mailman = await _entity(engine, story, "person", "mailman")
        async with engine.begin() as conn:
            await registry.auto_register(conn, story, "knocked_at")
            await registry.auto_register(conn, story, "rattles")
        await lls.long_lite_story(engine, story, ids["Ada"], 10, lines=lines)
        backfill_job.bind(lib.models)
        stub = lib.models.stub
        assert stub is not None
        stub.when(
            "Someone knocks at the door.",
            c.salience_answer((1, "Someone knocks at the door", c.KNOCK)),
            purpose="salience_detection",
        )
        stub.when(
            "Something rattles the window.",
            c.salience_answer(
                (
                    1,
                    "Something rattles the window",
                    SalienceSketch(subject="something", relation="rattles", object="the window"),
                )
            ),
            purpose="salience_detection",
        )
        stub.when(
            "The mailman knocked at the door.",
            json.dumps(
                {
                    "claims": [
                        {
                            "beat": 1,
                            "span": "The mailman knocked at the door",
                            "subject": "the mailman",
                            "predicate": "knocked_at",
                            "object": "the door",
                            "time": None,
                            "polarity": "affirmed",
                            "confidence": 0.95,
                        }
                    ]
                }
            ),
            purpose=EXTRACTION_BACKSTOP.name,
        )
        await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        # Through extraction: the checkpoint holds offsets, not the words.
        for _ in range(10):
            status = await scalar(
                engine, "SELECT status FROM story_backfills WHERE story_id = :s", s=story
            )
            if status == "reconciling":
                break
            await run_backfill(engine, story, 1)
        stored = await rows(
            engine,
            "SELECT extraction::text AS x FROM backfill_chunks c JOIN story_backfills b "
            "ON b.id = c.backfill_id WHERE b.story_id = :s",
            s=story,
        )
        assert any('"references"' in r.x and '"model"' in r.x for r in stored)
        assert not any("knocks" in r.x or "Someone" in r.x for r in stored)
        await run_backfill(engine, story)
        done = await ok(await client.get(f"/stories/{story}/backfill", headers=READ))
        assert done["status"] == "completed", done
        operation = await scalar(
            engine, "SELECT operation_id FROM story_backfills WHERE id = :b", b=done["id"]
        )
        thunks = await rows(
            engine,
            "SELECT t.id, t.resolved_fact_id, t.salient, t.membership::text AS m, "
            "p.name AS predicate FROM thunks t JOIN predicates p ON p.id = t.predicate_id "
            "WHERE t.story_id = :s AND t.operation_id = :o ORDER BY t.id",
            s=story,
            o=operation,
        )
        by = {t.predicate: t for t in thunks}
        assert set(by) == {"knocked_at", "rattles"}
        assert {t.m for t in thunks} == {"active"}
        assert by["knocked_at"].resolved_fact_id is not None
        assert by["rattles"].resolved_fact_id is None
        resolution = (
            await rows(
                engine,
                "SELECT provenance::text AS p, subject_id FROM facts WHERE id = :f",
                f=by["knocked_at"].resolved_fact_id,
            )
        )[0]
        assert (resolution.p, str(resolution.subject_id)) == ("backfill", mailman)
        entries = await scalar(
            engine,
            "SELECT count(*) FROM promissory_ledger WHERE story_id = :s AND thunk_id = :t",
            s=story,
            t=by["rattles"].id,
        )
        assert entries == 1
        report = await _report(engine, done["id"])
        assert (report["thunks"], report["settled"]) == (2, 1)


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_state_changes(
    stub_settings: Any, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#228 (#187, #188, #190): a story-owned number State the story's narration changes
    ("Ada picks up 30 gold coins", "Ada spends 10 coins") gets inferred changes: ``source =
    backfill``, ``inferred``, through ``apply_change``. Complement: a narrated change its
    bounds refuse ("Ada spends 50 coins" with 30 held, the State non-negative) is not written
    and is listed in ``report.skipped_state_changes`` with its reason; the value folds to
    20."""
    from epistrel.core.states import StateDefinition
    from tests import state_support as ss

    ss.deliver(monkeypatch)
    async with respond_client(stub_settings, engine) as (client, lib):
        lines = {
            2: ("Look!", "Ada picks up 30 gold coins."),
            3: ("Let's shop.", "Ada spends 50 coins."),
            4: ("Fine.", "Ada spends 10 coins."),
        }
        story, ids = await new_story(client, "Ada", "Bobby", "Cal")
        await ss.set_level(engine, story, "on")
        gold = await ss.define(
            engine,
            story,
            StateDefinition.model_validate(
                {
                    "name": "gold",
                    "type": "number",
                    "scope": "story",
                    "constraints": {"non_negative": True},
                    "initial_value": 0,
                    "aliases": ("coins",),
                    "unit": "gp",
                }
            ),
        )
        await ss.set_level(engine, story, None)
        await lls.long_lite_story(engine, story, ids["Ada"], 10, lines=lines)
        backfill_job.bind(lib.models)
        done = await _switch(client, engine, story)
        changes = await rows(
            engine,
            "SELECT delta, source::text AS src, inferred, narrative_turn, membership::text AS m "
            "FROM state_changes WHERE story_id = :s AND state_id = :g ORDER BY narrative_turn",
            s=story,
            g=gold.id,
        )
        assert [(int(c.delta), c.narrative_turn) for c in changes] == [(30, 2), (-10, 4)]
        assert {(c.src, c.inferred, c.m) for c in changes} == {("backfill", True, "active")}
        report = await _report(engine, done["id"])
        assert report["state_changes"] == 2
        assert report["skipped_state_changes"] == [
            {"turn": 3, "state_id": gold.id, "reason_code": "non_negative"}
        ]
        assert int(await ss.value(engine, story, "gold")) == 20


@pytest.mark.part("M7", "FR-PROF-9")
async def test_i_19__m7_end_audit(
    stub_settings: Any, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#228: a cancelled backfill writes a ``backfill_cancelled`` ``audit_log`` row (the
    cancelling principal, the backfill's id, its progress counts; no story text), and one that
    fails its last attempt a ``backfill_failed`` row naming the exception. Complement: a
    completed backfill writes neither."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, _, _ = await _story(client, engine, lib, 120)
        started = await ok(
            await client.put(f"/stories/{story}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, story, 2)
        await ok(
            await client.delete(
                f"/stories/{story}/backfill", headers={**WRITE, "X-Epistrel-Principal": "p-op"}
            )
        )
        audit = await rows(
            engine,
            "SELECT kind, principal, details FROM audit_log WHERE story_id = :s "
            "AND kind LIKE 'backfill_%'",
            s=story,
        )
        assert [(a.kind, a.principal) for a in audit] == [("backfill_cancelled", "p-op")]
        details = audit[0].details
        assert details["backfill_id"] == started["backfill"]["id"]
        assert details["chunks_done"] == 1
        assert "error" not in details

        failing, _, _ = await _story(client, engine, lib, 60)
        await ok(
            await client.put(f"/stories/{failing}/engine-profile", json=FULL, headers=WRITE), 202
        )
        await run_backfill(engine, failing, 1)  # begin

        async def broken(*_: Any, **__: Any) -> None:
            raise RuntimeError("extraction broke")

        monkeypatch.setattr(backfill_job, "_extract", broken)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET attempt = :n WHERE kind = 'backfill' AND story_id = :s "
                    "AND status = 'queued'"
                ),
                {"n": backfill_job.MAX_ATTEMPTS - 1, "s": failing},
            )
        await run_backfill(engine, failing, 1)
        assert (
            await scalar(
                engine, "SELECT status FROM story_backfills WHERE story_id = :s", s=failing
            )
            == "failed"
        )
        failed = await rows(
            engine,
            "SELECT kind, details FROM audit_log WHERE story_id = :s AND kind LIKE 'backfill_%'",
            s=failing,
        )
        assert [f.kind for f in failed] == ["backfill_failed"]
        assert failed[0].details["error"] == "RuntimeError"
        monkeypatch.undo()

        completed, _, _ = await _story(client, engine, lib, 20)
        await _switch(client, engine, completed)
        assert (
            await scalar(
                engine,
                "SELECT count(*) FROM audit_log WHERE story_id = :s AND kind LIKE 'backfill_%'",
                s=completed,
            )
            == 0
        )
