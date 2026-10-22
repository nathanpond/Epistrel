"""Memory records against PostgreSQL (#135): U-11's record shape (the episodic writer, the
``think`` complement, working memory without persistence), I-09 (M4)'s zero residue across
memories and vectors, and the registrations beyond the milestone parts (the importance job and
its stub, the guard trigger, the resurrection guard, the inert-when-off path, fork, Lite).

The TIME (M4) slices are in ``tests/property/test_time_m4.py``, U-10's importance half in
``tests/test_u_10.py`` and S-FLASHBACK-TRUNCATE's memory half in
``tests/scenario/test_s_flashback_truncate.py``."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import (
    MemoryKind,
    MemoryTrustTier,
    OperationKind,
    Provenance,
    RecordProvenance,
)
from epistrel.core.errors import ErasedSubjectReferenced
from epistrel.core.membership import DERIVED_TABLES
from epistrel.core.memory import records
from epistrel.core.tables.memories import MUTABLE_MEMORY_COLUMNS
from epistrel.orchestrator.jobs import queue
from epistrel.orchestrator.jobs.handlers import importance
from epistrel.orchestrator.jobs.recurring import registered_tasks
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.stub import digest_score
from tests import erasure_support as e
from tests.auth_support import auth_settings
from tests.canon_support import world
from tests.event_support import execute_in_tx, new_story, play_turn, rows, sqlstate
from tests.fork_support import fork
from tests.memory_support import (
    derived_memory,
    embed_all,
    event_ids,
    importance_jobs,
    memory_rows,
    run_importance,
    turn,
    use_structured,
)
from tests.respond_support import body, respond_client
from tests.respond_support import new_story as new_rest_story
from tests.respond_support import respond as respond_turn
from tests.structured_turn_support import (
    beat,
    claim,
    history,
    place,
    render,
    structured_story,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "sets" / "importance" / "cases.jsonl"

FR_MEM_4_COLUMNS: dict[str, str] = {
    # FR-MEM-4: provenance, trust tier, visibility, importance, source references,
    # created/last-accessed times, embedding-model version.
    "provenance": "record_provenance",
    "trust_tier": "memory_trust_tier",
    "visibility": "jsonb",
    "holder_character_id": "bpchar",
    "importance": "float4",
    "importance_at": "int4",
    "source_event_ids": "_bpchar",
    "created_at": "timestamptz",
    "last_accessed_at": "timestamptz",
    "embedding_model_version": "text",
    # FR-STORE-9: the operation, the narrative position and the timeline membership.
    "operation_id": "bpchar",
    "narrative_turn": "int4",
    "turn_variant": "int2",
    "membership": "membership",
    "membership_tx": "timestamptz",
    # The record itself and its retrieval.
    "kind": "memory_kind",
    "text": "text",
    "retrieval_tier": "retrieval_tier",
    "embedding": "vector",
    "proposition_ids": "_bpchar",
}
"""Every column U-11 asserts, with its ``information_schema`` udt name."""


@pytest.fixture
def models(pg_url: str, engine: AsyncEngine) -> ModelAccessLayer:
    layer = ModelAccessLayer.from_settings(auth_settings(pg_url, model_adapter="stub"))
    layer.bind(engine)
    return layer


async def _columns(engine: AsyncEngine) -> dict[str, dict[str, str]]:
    found = await rows(
        engine,
        "SELECT table_name, column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema IN ('public', 'catalog')",
    )
    tables: dict[str, dict[str, str]] = {}
    for r in found:
        tables.setdefault(str(r.table_name), {})[str(r.column_name)] = str(r.udt_name)
    return tables


async def _structured_world(engine: AsyncEngine) -> Any:
    w = await world(engine)
    await use_structured(engine, w.story_id)
    return w


# -- U-11 ------------------------------------------------------------------------------------


@pytest.mark.part("M4", "FR-MEM-1,2,4,5; FR-TURN-4")
async def test_u_11(engine: AsyncEngine) -> None:
    """The record shape (FR-MEM-1,2,4,5, FR-TURN-4): every FR-MEM-4/FR-STORE-9 column exists
    with its type, and the tiers include semantic and reflection; a structured turn's commit
    writes one episodic memory per observable beat (the player's ``say`` and ``do``, the
    reply) with its source event, operation, position, provenance, trust tier and visibility.
    Complement: the ``think`` and ``latent`` beats produce no memory (``latent`` routes to
    canon). Working memory is derived from events: what it answers matches the beats, and no
    table or column persists it."""
    tables = await _columns(engine)
    shape = tables["memories"]
    assert {c: shape.get(c) for c in FR_MEM_4_COLUMNS} == FR_MEM_4_COLUMNS
    labels = await rows(
        engine,
        "SELECT t.typname, array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS labels "
        "FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname IN ('memory_kind', 'memory_trust_tier') GROUP BY t.typname",
    )
    assert {r.typname: list(r.labels) for r in labels} == {
        "memory_kind": ["episodic", "semantic", "reflection"],
        "memory_trust_tier": ["author", "external", "engine", "in_world"],
    }
    assert DERIVED_TABLES["memories"].valid_time is None

    w = await _structured_world(engine)
    story, cal, bobby, ann = (
        w.story_id,
        w.characters["Cal"],
        w.characters["Bobby"],
        w.characters["Ann"],
    )
    op, written = await turn(
        engine,
        story,
        actor=cal,
        say="Where were you last night?",
        do="leans on the fence",
        reply="I was at the river.",
        voiced=[bobby],
        think=(bobby, "She must not find out."),
        latent=(ann, "Ann hid the key."),
    )
    beats = await event_ids(engine, op.operation_id)
    found = await memory_rows(engine, story)
    assert {m.id for m in written} == set(found)
    assert {(r.text, r.character_id, r.provenance, r.beat) for r in found.values()} == {
        ("Cal says: Where were you last night?", cal, "player", 1),
        ("Cal does: leans on the fence", cal, "player", 1),
        ("Bobby says: I was at the river.", bobby, "render", 2),
    }
    by_type = {"player": beats["player_input"], "render": beats["message_rendered"]}
    for r in found.values():
        assert r.kind == "episodic"
        assert list(r.source_event_ids) == [by_type[r.provenance]]
        assert r.operation_id == op.operation_id
        assert (r.narrative_turn, r.turn_variant) == (op.narrative_turn, op.turn_variant)
        assert str(r.membership) == "active"
        assert r.trust_tier == "in_world"
        assert r.visibility == {"scope": "public"}
        assert r.holder_character_id is None
        assert r.importance is None
        assert r.importance_at is None
        assert r.retrieval_tier == "hot"
        assert r.embedding is None
        assert r.embedding_model_version is None
        assert r.last_accessed_at is None
    # Complement: no memory names a think or latent beat's event.
    sources = {s for r in found.values() for s in r.source_event_ids}
    assert beats["think"] not in sources
    assert beats["latent"] not in sources
    assert not any("must not find out" in r.text or "hid the key" in r.text for r in found.values())
    # Narration (a reply voicing nobody) is objective, the director's.
    op2, (narrated,) = await turn(engine, story, say=None, reply="Rain falls on the farm.")
    assert (narrated.provenance, narrated.visibility.scope, narrated.character_id) == (
        RecordProvenance.DIRECTOR,
        "objective",
        None,
    )
    assert narrated.text == "Rain falls on the farm."
    assert records.trust_tier_of(RecordProvenance.RENDER) is MemoryTrustTier.IN_WORLD
    # A semantic memory (FR-MEM-2; #143 writes them) carries the same columns.
    semantic = await derived_memory(
        engine, story, "Bobby was at the river that night.", [beats["message_rendered"]]
    )
    row = (await memory_rows(engine, story))[semantic]
    assert (row.kind, row.trust_tier, row.provenance) == (
        "semantic",
        MemoryTrustTier.ENGINE.value,
        "system_consolidated",
    )

    # FR-MEM-5: working memory, derived from events on demand.
    async with engine.connect() as conn:
        bobbys = await records.working_memory(conn, story, bobby)
        anns = await records.working_memory(conn, story, ann)
        everyone = await records.working_memory(conn, story, None)
        through_one = await records.working_memory(conn, story, None, through_position=1)
        last_two = await records.working_memory(conn, story, None, n=2)
    assert [(b.kind, b.text) for b in bobbys] == [
        ("say", "Where were you last night?"),
        ("do", "leans on the fence"),
        ("say", "I was at the river."),
        ("think", "She must not find out."),
        ("narration", "Rain falls on the farm."),
    ]
    assert [b.kind for b in anns] == ["say", "do", "say", "narration"]  # not Bobby's think
    assert [b.kind for b in everyone] == ["say", "do", "say", "think", "latent", "narration"]
    assert [b.narrative_turn for b in through_one] == [1, 1, 1, 1, 1]
    assert [b.text for b in last_two] == ["Ann hid the key.", "Rain falls on the farm."]
    assert {b.event_id for b in bobbys} <= set(beats.values()) | set(
        (await event_ids(engine, op2.operation_id)).values()
    )
    # Complement: no table or column persists working memory.
    fields = {f.name for f in dataclasses.fields(records.WorkingBeat)}
    assert [t for t in tables if t.startswith("working_memory")] == []
    assert [
        (t, c) for t, cols in tables.items() for c in cols if c.startswith("working_memory")
    ] == []
    assert [t for t, cols in tables.items() if fields <= cols.keys()] == []


async def test_episodic_memory_is_inert_when_the_module_is_off(engine: AsyncEngine) -> None:
    """Under the Lite preset (episodic off) a turn's commit writes no memory and enqueues no
    importance job; the job, enqueued anyway, calls no model. Complement: the same turn under
    ``Structured (M4)`` writes them and enqueues the job."""
    lite = await world(engine)
    _, written = await turn(engine, lite.story_id, actor=lite.characters["Cal"])
    assert written == []
    assert await memory_rows(engine, lite.story_id) == {}
    assert await importance_jobs(engine, lite.story_id) == []

    structured = await _structured_world(engine)
    _, written = await turn(engine, structured.story_id, actor=structured.characters["Cal"])
    assert len(written) == 2
    (job,) = await importance_jobs(engine, structured.story_id)
    assert job.coalesce_key == importance.coalesce_key(structured.story_id)


async def test_the_importance_job_scores_each_memory_once(
    engine: AsyncEngine, models: ModelAccessLayer
) -> None:
    """A turn's commit enqueues one coalesced scoring job; a run calls ``importance_scoring``
    once per unscored memory with ``{text, kind}`` only, and the stub's score is derived from
    the text. An author-set importance is never overwritten, and a second run calls nothing.
    Complement: a story whose module is off gets no call even when a job is queued."""
    w = await _structured_world(engine)
    story = w.story_id
    await turn(engine, story, actor=w.characters["Cal"], say="Hi.", reply="Hello.")
    await turn(engine, story, actor=w.characters["Cal"], say="Bye.", reply=None)
    assert len(await importance_jobs(engine, story)) == 1  # coalesced
    found = await memory_rows(engine, story)
    pinned = next(iter(found))
    await execute_in_tx(
        engine,
        ("UPDATE memories SET importance = 0.99, importance_at = 1 WHERE id = :m", {"m": pinned}),
    )
    models.stub.calls.clear()
    assert await run_importance(engine, models, story) == [queue.CommitOutcome.COMMITTED]
    calls = [c for c in models.stub.calls if c.purpose == "importance_scoring"]
    assert len(calls) == len(found) - 1
    assert all(set(c.fields or ()) <= {"text", "kind"} for c in calls)
    scored = await memory_rows(engine, story)
    assert scored[pinned].importance == pytest.approx(0.99)
    for memory_id, row in scored.items():
        if memory_id == pinned:
            continue
        assert row.importance == pytest.approx(
            digest_score(f"episodic memory: {row.text}"), abs=1e-4
        )
        assert row.importance_at == row.narrative_turn
    (job,) = await importance_jobs(engine, story)
    assert job.status == "done"
    assert job.result["scored"] == len(found) - 1
    # Nothing left: a new job finds nothing and calls nothing.
    async with engine.begin() as conn:
        await importance.schedule_in(conn, story)
    models.stub.calls.clear()
    await run_importance(engine, models, story)
    assert [c for c in models.stub.calls if c.purpose == "importance_scoring"] == []

    lite = await world(engine)
    async with engine.begin() as conn:
        await importance.schedule_in(conn, lite.story_id)
    await run_importance(engine, models, lite.story_id)
    assert [c for c in models.stub.calls if c.purpose == "importance_scoring"] == []
    assert registered_tasks()["importance_scoring"].interval == importance.INTERVAL


async def test_the_e_04_fixture_ranks_pivotal_over_trivial_after_decay(
    engine: AsyncEngine, models: ModelAccessLayer
) -> None:
    """E-04's labeled fixture (``evals/sets/importance/cases.jsonl``, #157's set) scripted into
    the stub: scored by the job, every pivotal memory outranks every trivial one under
    ``decayed_importance`` at the story's committed turns, with the deployment's half-life."""
    cases = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]
    w = await _structured_world(engine)
    story = w.story_id
    for case in cases:
        await turn(engine, story, say=None, reply=case["inputs"]["text"])
        models.stub.when(case["inputs"]["text"], case["stub_answer"], purpose="importance_scoring")
    await run_importance(engine, models, story)
    found = await memory_rows(engine, story)
    async with engine.connect() as conn:
        now = await records.committed_turns(conn, story)
        half_life = await records.half_life_turns(conn)
    assert now == len(cases)
    ranked = {
        r.text: records.decayed_importance(r.importance, r.importance_at, now, half_life)
        for r in found.values()
    }
    pivotal = [ranked[c["inputs"]["text"]] for c in cases if c["expected"] == "pivotal"]
    trivial = [ranked[c["inputs"]["text"]] for c in cases if c["expected"] == "trivial"]
    assert pivotal
    assert trivial
    assert min(pivotal) > max(trivial)


async def test_the_table_is_guarded(engine: AsyncEngine) -> None:
    """Only the importance, access, tier, embedding and membership columns may change; the
    text changes only under an erasure; a DELETE and a TRUNCATE are refused outside erasure and
    story deletion."""
    w = await _structured_world(engine)
    _, (memory, *_) = await turn(engine, w.story_id, actor=w.characters["Cal"])
    assert set(MUTABLE_MEMORY_COLUMNS) >= {"importance", "embedding", "membership"}
    await execute_in_tx(
        engine,
        (
            "UPDATE memories SET importance = 0.4, last_accessed_at = now(), "
            "retrieval_tier = 'retired' WHERE id = :m",
            {"m": memory.id},
        ),
    )
    for statement in (
        "UPDATE memories SET text = 'rewritten' WHERE id = :m",
        "UPDATE memories SET provenance = 'author' WHERE id = :m",
        "DELETE FROM memories WHERE id = :m",
        "TRUNCATE memories",
    ):
        with pytest.raises(DBAPIError) as raised:
            await execute_in_tx(engine, (statement, {"m": memory.id}))
        assert sqlstate(raised.value) == "EPI01", statement
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL epistrel.erasure = 'on'"))
        await conn.execute(
            text("UPDATE memories SET text = '[erased]' WHERE id = :m"), {"m": memory.id}
        )
    assert (await memory_rows(engine, w.story_id))[memory.id].text == "[erased]"


async def test_a_touch_is_a_heap_only_update(engine: AsyncEngine) -> None:
    """#452: a retrieval's ``last_accessed_at`` touch on a memory with a full-size (toasted)
    embedding writes no index entry. A ``BEFORE`` guard returned a detoasted ``NEW``, so the
    indexed ``embedding`` looked changed and every touch inserted into every index."""
    w = await _structured_world(engine)
    _, (memory, *_) = await turn(engine, w.story_id, actor=w.characters["Cal"])
    vector = "[" + ",".join(f"{(i * 7919 % 1000) / 1000 - 0.5:.6f}" for i in range(768)) + "]"
    await execute_in_tx(
        engine,
        (
            "UPDATE memories SET embedding = CAST(:v AS vector) WHERE id = :m",
            {"m": memory.id, "v": vector},
        ),
    )
    counts = text(
        # The session's not-yet-flushed earlier transactions count here too (PG >= 15), so the
        # touch is the difference.
        "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_xact_user_tables WHERE relname = 'memories'"
    )
    async with engine.begin() as conn:
        assert (
            await conn.scalar(text("SELECT reloptions FROM pg_class WHERE relname = 'memories'"))
        ) == ["fillfactor=70"]
        before = (await conn.execute(counts)).one()
        await conn.execute(
            text("UPDATE memories SET last_accessed_at = now() WHERE id = :m"), {"m": memory.id}
        )
        after = (await conn.execute(counts)).one()
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)


async def test_the_writer_records_the_occasion_and_the_lexical_vector(engine: AsyncEngine) -> None:
    """#141's writer hook: a beat's ``event_type`` and ``participants`` are written (its actor
    first), ``beat_occasion`` derives them from a text and its resolved mentions, the generated
    ``search`` vector is the text's under ``english`` and an erasure rewriting the text
    recomputes it, and the guard still refuses other changes. Complements: a participant that is
    not one of the story's entities, or an event type over 64 characters, is refused and
    nothing is written."""
    from epistrel.core.errors import InvalidRequest
    from epistrel.core.events import Operation
    from epistrel.core.resolution.types import Mention, MentionKind
    from epistrel.core.retrieval.pipeline import beat_occasion

    w = await _structured_world(engine)
    story, cal, bobby = w.story_id, w.characters["Cal"], w.people["Bobby"]
    mentions = [Mention((0, 5), MentionKind.NAME, bobby), Mention((6, 9), MentionKind.GROUP, None)]
    event_type, participants = beat_occasion("Bobby won the game.", mentions)
    assert (event_type, participants) == ("game", (bobby,))

    async def write(beat: records.Beat) -> list[records.Memory]:
        found: dict[str, list[records.Memory]] = {}
        async with Operation(
            engine, story_id=story, kind=OperationKind.TURN, principal="p-test"
        ) as op:
            op.append("player_input", {"text": beat.text}, provenance=Provenance.PLAYER, beat=1)

            async def step(conn: Any) -> None:
                (recorded,) = await records.beats_of_operation(conn, op.operation_id)
                found["m"] = await records.write_episodic(
                    conn, op, [dataclasses.replace(beat, event_id=recorded.event_id)]
                )

            op.add_commit_step(step)
            await op.commit()
        return found["m"]

    beat = records.Beat(
        "say",
        "Bobby won the game.",
        "",
        beat=1,
        character_id=cal,
        provenance=RecordProvenance.PLAYER,
        event_type=event_type,
        participants=participants,
    )
    (memory,) = await write(beat)
    assert memory.event_type == "game"
    assert memory.participants == (cal, bobby)
    (row,) = await rows(
        engine,
        "SELECT search::text AS search, search @@ to_tsquery('english', 'games') AS hit "
        "FROM memories WHERE id = :m",
        m=memory.id,
    )
    assert row.hit
    assert "'bobbi'" in row.search
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL epistrel.erasure = 'on'"))
        await conn.execute(
            text("UPDATE memories SET text = '[erased]' WHERE id = :m"), {"m": memory.id}
        )
    (erased,) = await rows(
        engine, "SELECT search::text AS search FROM memories WHERE id = :m", m=memory.id
    )
    assert "bobbi" not in erased.search
    with pytest.raises(DBAPIError):
        await execute_in_tx(
            engine,
            ("UPDATE memories SET event_type = 'party' WHERE id = :m", {"m": memory.id}),
        )
    for bad in (
        dataclasses.replace(beat, participants=("01J9Z8Q4N5V6W7X8Y9Z0A1B2C9",)),
        dataclasses.replace(beat, event_type="x" * 65),
    ):
        with pytest.raises(InvalidRequest):
            await write(bad)
    assert len(await memory_rows(engine, story)) == 1


async def test_a_fork_copies_memories_with_their_vectors(engine: AsyncEngine) -> None:
    """A fork at turn 1 copies turn 1's memories (vectors byte-for-byte, sources remapped to
    the fork's events) and not turn 2's."""
    w = await _structured_world(engine)
    story = w.story_id
    await turn(engine, story, actor=w.characters["Cal"], say="One.", reply=None)
    await turn(engine, story, actor=w.characters["Cal"], say="Two.", reply=None)
    await embed_all(engine, story)
    forked = await fork(engine, story, at_turn=1)
    copied = await memory_rows(engine, forked.story_id)
    assert [r.text for r in copied.values()] == ["Cal says: One."]
    (row,) = copied.values()
    assert row.vector == "[0.25,0.5,0.75]"
    fork_events = {
        str(r.id)
        for r in await rows(engine, "SELECT id FROM events WHERE story_id = :s", s=forked.story_id)
    }
    assert set(row.source_event_ids) <= fork_events


@pytest.mark.part("M4", "FR-TURN-4")
async def test_u_11__latent(pg_url: str, engine: AsyncEngine) -> None:
    """A structured turn's ``latent`` beat (FR-TURN-4; #150) routes to canon with the scope it
    declares (``characters``: Ada alone), never to a memory, and no character viewpoint's
    message or history shows it, while an omniscient read returns it with its scope. A
    ``latent`` that declares nothing normalizable is kept as an ``objective`` episodic memory
    instead, reported ``latent_unnormalized`` (complement: no fact)."""
    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        wren, ada = ids["Wren"], ids["Ada"]
        await place(engine, story, "cellar")
        secret, loose = "Ada hid the ledger in the cellar.", "Something stirs below."
        epistrel.models.stub.script(
            "generation",
            [
                render(
                    beat("say", "Nothing to see.", intent="sincere"),
                    beat(
                        "latent",
                        secret,
                        visibility={"scope": "characters", "characters": [ada]},
                        propositions=[claim("Ada", "located_at", "cellar")],
                    ),
                    beat("latent", loose),
                )
            ],
        )
        response = await respond_turn(client, story, body(wren, "Ada?"))
        assert response.status_code == 200, response.text
        played = response.json()
        assert "latent_unnormalized" in played["degradation"]
        assert secret not in played["message"]["text"]
        assert all(b["channel"] != "latent" for b in played["beats"])
        facts = await rows(
            engine,
            "SELECT visibility, beat FROM facts WHERE story_id = :s AND provenance = 'render'",
            s=story,
        )
        assert [(f.visibility, f.beat) for f in facts] == [
            ({"scope": "characters", "characters": [ada]}, 3)
        ]
        memories = await rows(
            engine,
            "SELECT text, visibility, beat FROM memories WHERE story_id = :s ORDER BY beat",
            s=story,
        )
        assert [(m.beat, m.visibility) for m in memories if m.text == loose] == [
            (4, {"scope": "objective"})
        ]
        assert all(secret not in m.text for m in memories)
        for viewpoint in (f"character:{wren}", f"character:{ada}"):
            items = await history(client, story, viewpoint)
            assert all(i["channel"] != "latent" for i in items)
            assert all(secret not in i["text"] for i in items)
        omniscient = [i for i in await history(client, story, "omniscient") if i["text"] == secret]
        assert [i["visibility"] for i in omniscient] == [
            {"scope": "characters", "characters": [ada]}
        ]


# -- I-09 (M4) ---------------------------------------------------------------------------------

LINDA = "Linda May"
"""The subject: "May" is also a common word, so "May the rain stop." is a candidate."""


@pytest.mark.part("M4", "FR-ADMIN-2")
async def test_i_09__memories(pg_url: str, engine: AsyncEngine) -> None:
    """Erase Linda: no memory names her afterwards, in any case-folded form (ADR-156), and
    erasure's residue scan reports 0; the memories she voiced or holds are deleted with
    markers; a memory that names her is redacted in place, its vector nulled in the erasure's
    transaction; the candidate ("May the rain stop.") is listed by the dry run and erased only
    once confirmed. Complement: a memory that never names her survives byte-identical, its
    vector included. A later write naming her is refused."""
    settings = e.erasure_settings(pg_url)
    async with respond_client(settings, engine) as (client, _):
        story, cast = await new_rest_story(client, "Wren", LINDA, "Ty")
        await use_structured(engine, story)
        linda, ty, wren = cast[LINDA], cast["Ty"], cast["Wren"]
        _, named = await turn(
            engine, story, actor=wren, say="I saw Linda May at the well.", reply=None
        )
        _, folded = await turn(
            engine, story, actor=wren, say="LINDA MAY is late again.", reply=None
        )
        voiced_op, voiced = await turn(
            engine, story, say=None, reply="I am moving to Leeds.", voiced=[linda]
        )
        _, candidate = await turn(engine, story, say=None, reply="May the rain stop.")
        _, kept = await turn(engine, story, actor=ty, say=None, do="waters the garden", reply=None)
        held = await derived_memory(
            engine,
            story,
            "She keeps a list of debts.",
            [(await event_ids(engine, voiced_op.operation_id))["message_rendered"]],
            holder=linda,
        )
        await embed_all(engine, story)
        before = await memory_rows(engine, story)

        listed = await e.erase(client, "character", linda, story, dry_run=True)
        assert any(c["record_table"] == "memories" for c in listed["candidates"])
        assert listed["records"].get("memories", 0) >= 1
        done = await e.erase(client, "character", linda, story, confirm_all=True)
        assert done["residue"] == 0
        assert done["candidates_remaining"] == 0

    after = await memory_rows(engine, story)
    gone = {voiced[0].id, held}
    assert set(before) - set(after) == gone
    markers = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'erasure_marker' "
        "AND payload->>'record_table' = 'memories'",
        s=story,
    )
    assert {m.payload["record_id"] for m in markers} == gone
    for memory in (named[0], folded[0], candidate[0]):
        row = after[memory.id]
        assert "[erased]" in row.text
        assert row.embedding is None
        assert row.embedding_model_version is None
    assert after[named[0].id].text == "Wren says: I saw [erased] at the well."
    # Zero residue in memories, in every case-folded form; no vector of a touched memory left.
    residue = await e.residue(engine, story, ["Linda", "linda", "LINDA", "Linda May"])
    assert residue.get("memories", 0) == 0
    assert all(
        "linda" not in r.text.casefold() and "may the" not in r.text.casefold()
        for r in after.values()
    )
    # Complement: the memory that never named her is byte-identical, vector included.
    assert after[kept[0].id]._asdict() == before[kept[0].id]._asdict()
    assert after[kept[0].id].vector == "[0.25,0.5,0.75]"
    # The resurrection guard: an engine-written beat naming her is refused.
    with pytest.raises(ErasedSubjectReferenced):
        await turn(engine, story, say=None, reply="Linda May returns.")


async def test_the_trust_tier_is_derived_from_the_provenance() -> None:
    """Each provenance maps to one trust tier, mechanically."""
    assert {p.value: records.trust_tier_of(p).value for p in RecordProvenance} == {
        "author": "author",
        "seed": "author",
        "system_consolidated": "engine",
        "in_world": "in_world",
        "render": "in_world",
        "director": "in_world",
        "player": "in_world",
        "external": "external",
        "resolution": "engine",
        "backfill": "engine",
    }


async def test_a_memory_kind_is_one_of_the_tiers() -> None:
    """The tiers are FR-MEM-1..3's; only the episodic writer ships here."""
    assert [k.value for k in MemoryKind] == ["episodic", "semantic", "reflection"]
    assert frozenset({"say", "do", "narration"}) == records.OBSERVABLE


async def test_the_lite_path_is_untouched(engine: AsyncEngine) -> None:
    """A Lite story's turns through the Lite pipeline's own events leave ``memories`` empty:
    nothing in the Lite path calls the writer."""
    story = await new_story(engine)
    await play_turn(engine, story, "hello")
    assert await memory_rows(engine, story) == {}
