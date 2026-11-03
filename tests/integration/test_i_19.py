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
            "SELECT kind, status FROM story_backfills WHERE story_id = :s AND kind = 'resummarize'",
            s=story,
        )
        assert rebuild.status == "extracting"
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
