"""S-DELETE: deleting one story removes exactly its rows; stories are self-contained."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel import Engine, ProfileOverridesPatch, RelationRequest
from epistrel.config import Settings
from epistrel.core import clock, turn_gate
from epistrel.core.commit import lifecycle
from epistrel.core.commit.sketch import ThunkSketch
from epistrel.core.context import RequestContext
from epistrel.core.enums import OpenSlot, OperationKind, Provenance
from epistrel.core.epistemic import divergences
from epistrel.core.errors import GuardrailViolation
from epistrel.core.events import Operation
from epistrel.core.feedback import FeedbackRequest
from epistrel.core.ids import new_ulid
from epistrel.core.ingest import IngestRequest
from epistrel.core.membership import Position
from epistrel.core.propositions import registry as predicate_registry
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.stories import STORY_ID_EXCEPTIONS, STORY_TABLES
from epistrel.core.tables.ownership import EntityType
from epistrel.core.turns import beats
from epistrel.core.verify import shadow
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.orchestrator.jobs import timers
from epistrel.orchestrator.jobs.handlers.rebuild import REBUILD_KIND
from epistrel.orchestrator.policies.input_gate import EntryPoint, GateText
from epistrel.orchestrator.policies.schemas import StoryPolicyItem
from tests import (
    canon_support,
    escalation_support,
    exposure_support,
    memory_support,
    partition_support,
)
from tests import erasure_support as erasure
from tests import genesis_support as g
from tests.event_support import interleave, play_turn, rows
from tests.policy_support import unique, words
from tests.respond_support import respond_client
from tests.story_support import WRITE, create, dump, public_tables, rest, story_body

AUTHOR = RequestContext(principal="p-author")


async def _populate(engine: AsyncEngine, lib: Engine, story_id: str, character_id: str) -> None:
    """Turns, background work between them, story and character policies, a rebuild, an
    ingested narration (a ``client_events`` row, #58), a rejected input (an ``audit_log``
    row naming the story), a relation (``entity_relations``, #104), a story-scoped subject
    reference (``subject_references``: #105 creates the index, #123 writes its story rows, so
    the row is planted), a reply rated and rated again (a rating and its history, #112), an
    override of its profile's dials (``story_profile_overrides``, #113), and a finished rating
    check with a cached verdict (``rating_check_jobs``, ``guardrail_check_results``, #118,
    planted). The victim also gets a
    queued turn (``turn_queue``, #63)."""
    for n in range(1, 3):
        await play_turn(engine, story_id, f"turn {n} of {story_id}")
        await interleave(engine, story_id, after_turn=n)
    (reply, *_) = await rows(
        engine,
        "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered' ORDER BY seq",
        s=story_id,
    )
    rater = RequestContext(principal=f"p-rater-{story_id}")
    for rating in (2, 4):
        await lib.rate_message(rater, story_id, reply.id, FeedbackRequest(rating=rating))
    await lib.policies_set_story(
        AUTHOR,
        story_id,
        [
            StoryPolicyItem.model_validate(
                {"name": unique("own"), "rule": words("forbidden"), "message": "No."}
            ),
            StoryPolicyItem.model_validate(
                {
                    "name": unique("cast"),
                    "rule": {"type": "entity_list"},
                    "scope": "character",
                    "scope_id": character_id,
                    "message": "Not them.",
                }
            ),
            StoryPolicyItem.model_validate(
                {
                    "name": unique("input"),
                    "rule": words("unsayable"),
                    "applies_to": ["text_input"],
                    "message": "Not as input.",
                }
            ),
        ],
    )
    await lib.rebuild_projections(AUTHOR, story_id)
    await lib.set_profile_overrides(
        AUTHOR, story_id, ProfileOverridesPatch(settings={"pins": {"max_pinned": 9}})
    )
    reader = RelationRequest(principal=f"p-reader-{story_id}", relation="player")
    await lib.add_relation(AUTHOR, EntityType.STORY, story_id, reader)
    narration = {"client_event_id": "narration-1", "kind": "narration", "text": f"of {story_id}"}
    await lib.ingest(AUTHOR, story_id, IngestRequest.model_validate({"events": [narration]}))
    with pytest.raises(GuardrailViolation):
        await lib.gate_input(
            AUTHOR, story_id, EntryPoint.INGEST, [GateText("events[0]", "an unsayable thing")]
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO subject_references (id, subject_kind, subject_id, scope, story_id, "
                "record_table, record_id, field, ref_kind) VALUES (:id, 'character', :c, "
                "'story', :s, 'events', :r, 'payload.text', 'mentioned')"
            ),
            {"id": new_ulid(), "c": character_id, "s": story_id, "r": new_ulid()},
        )
        # A finished rating check and a cached verdict (#118), planted: a check needs a worker.
        await conn.execute(
            text(
                "INSERT INTO rating_check_jobs (id, story_id, from_rating_id, target_rating_id, "
                "target_rating_version, status, progress, violations, started_by, started_at, "
                "finished_at) SELECT :id, :s, content_rating_id, content_rating_id, "
                "(SELECT current_version_id FROM content_ratings r "
                "WHERE r.id = stories.content_rating_id), 'refused', "
                "'{\"done\": 1, \"total\": 1}', '[]', 'p-author', now(), now() "
                "FROM stories WHERE id = :s"
            ),
            {"id": new_ulid(), "s": story_id},
        )
        await conn.execute(
            text(
                "INSERT INTO guardrail_check_results (story_id, record_key, policy_id, "
                "policy_version, verdict) VALUES (:s, :r, 'p-any', 1, 'pass')"
            ),
            {"s": story_id, "r": new_ulid()},
        )
        # A genesis snapshot (#119), planted on the victim's character: the story table a
        # creation from a scenario writes.
        await conn.execute(
            text(
                "INSERT INTO character_snapshots (story_id, character_id, source_catalog_id, "
                "version, content, published_at) VALUES (:s, :c, :e, 1, "
                '\'{"public_card": {"name": "Ada"}}\', now())'
            ),
            {"s": story_id, "c": character_id, "e": new_ulid()},
        )
        # A deferred genesis item (#122), planted: the story table a Lite genesis writes.
        await conn.execute(
            text(
                "INSERT INTO genesis_deferred (id, story_id, operation_id, field_ref, "
                "field_class, location, capability_needed, payload) VALUES (:id, :s, :o, "
                "'starting_state.rules[0]', 'story_rule', '{}', 'structured', '{}')"
            ),
            {"id": new_ulid(), "s": story_id, "o": new_ulid()},
        )
        # The structured stores (#131): an entity with an alias, an auto predicate (a
        # configuration change) with a proposal, a proposition and an author's closed set.
        ann = await store.add_entity(conn, story_id, "person", "Ann", aliases=["Annie"])
        auto = await predicate_registry.auto_register(conn, story_id, "sails with")
        await predicate_registry.file_proposal(
            conn, story_id, auto.id, predicate_registry.ProposedChanges(symmetric=True)
        )
        found = await predicate_registry.load(conn, story_id)
        await store.write_proposition(
            conn,
            story_id,
            Proposition(
                subject_id=ann, predicate_id=auto.id, object_id=ann, valid_time=Interval.of(0, 9)
            ),
            found,
        )
        await store.declare_closed_set(
            conn, story_id, store.ClosedSet(auto.id, ann, (ann,)), found, asserted_by="author"
        )
    # Bitemporal canon (#133): a fact version, superseded, so both versions are the story's.
    sailing = Proposition(
        subject_id=ann, predicate_id=auto.id, object_id=ann, valid_time=Interval.of(10, 20)
    )
    first = await canon_support.commit(engine, story_id, sailing)
    await canon_support.supersede(
        engine,
        story_id,
        first.id,
        sailing.model_copy(update={"valid_time": Interval.of(5, 25)}),
        authority="author",
    )
    # The structured projections (#137): the player at a place and a friendship, so the turn
    # writing them leaves a placement, a relationship and a scene row.
    async with engine.begin() as conn:
        player = await conn.scalar(
            text("SELECT id FROM characters WHERE story_id = :s AND is_player"), {"s": story_id}
        )
        here = await store.add_entity(conn, story_id, "place", "the quay")
        me = await store.add_entity(conn, story_id, "person", "Me", character_id=player)
    await canon_support.commit(engine, story_id, canon_support.at(me, here, 0, None))
    await canon_support.commit(
        engine,
        story_id,
        Proposition(
            subject_id=me,
            predicate_id=canon_support.KNOWS,
            object_id=ann,
            valid_time=Interval.of(0, None),
        ),
    )
    # Belief partitions (#159): the other character's belief about the player, and its
    # projection row.
    held = await partition_support.believe(
        engine,
        story_id,
        character_id,
        Proposition(
            subject_id=me,
            predicate_id=canon_support.KNOWS,
            object_id=ann,
            valid_time=Interval.of(0, None),
        ),
    )
    # Exposures (#164): the other character exposed to the player's claim.
    await exposure_support.expose(
        engine,
        story_id,
        character_id,
        Proposition(
            subject_id=me,
            predicate_id=canon_support.KNOWS,
            object_id=ann,
            valid_time=Interval.of(0, None),
        ),
        source=str(player),
    )
    # Divergences (#166): a lie of the other character's against its belief.
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal=canon_support.PRINCIPAL
    ) as op:
        op.append("player_input", {"text": "lie"}, provenance=Provenance.PLAYER)
        op.append("message_rendered", {"text": "A lie."})

        async def lie(conn: AsyncConnection) -> None:
            await divergences.record_divergences(
                conn,
                op,
                [
                    divergences.Divergence(
                        utterance_id=new_ulid(),
                        speaker=character_id,
                        asserted_proposition_id=held.proposition_id,
                        position=Position(op.narrative_turn or 0, op.turn_variant or 0, 0, 2, 0),
                        believed_belief_id=held.id,
                        believed_proposition_id=held.proposition_id,
                    )
                ],
            )

        op.add_commit_step(lie)
        await op.commit()
    # Memory records (#135): a memory of the story, written by its own operation.
    await memory_support.derived_memory(engine, story_id, "Ann sails at dawn.", [])
    # The hot set (#147): an overflow episode of the story's vectors, planted (one needs a
    # story over its budget).
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO overflow_episodes (id, story_id, scope, scope_id, opened_turn, "
                "deadline_turn, excess_at_open, excess) VALUES (:id, :s, 'story', :s, 1, 51, "
                "1, 1)"
            ),
            {"id": new_ulid(), "s": story_id},
        )
    # Commitments (#146): a salient thunk (someone sails with Ann) and its ledger entry.
    sketch = ThunkSketch(
        subject_text="someone",
        relation_text=auto.name,
        object_text="Ann",
        predicate_id=auto.id,
        object_id=ann,
        open_slot=OpenSlot.SUBJECT,
    )

    async def owe(conn: AsyncConnection, op: Operation) -> None:
        await lifecycle.introduce(conn, op, sketch, valid_time=0, salient=True, on=True)

    sampled, _ = await canon_support.run(engine, story_id, owe)
    # Claim detection (#148): a shadow sample of that turn.
    async with engine.begin() as conn:
        written = await shadow.write_sample(
            conn,
            story_id,
            sampled.operation_id,
            status=shadow.SAMPLED,
            extraction_claims=1,
            missed=[shadow.Missed(beat=2, start=0, end=4, category="possession")],
        )
    assert written is not None
    # Escalation records (#155): one entailment call of that turn.
    await escalation_support.window_rows(
        engine, story_id, sampled.operation_id, "core|functional|fluent|affirmed", cases=1, agreed=1
    )
    # The structured turn (#150): an utterance of the input of a turn of its own.
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.TURN, principal="p-test"
    ) as op:
        op.append(
            "player_input",
            {"text": "Ann sails?", "say": "Ann sails?"},
            provenance=Provenance.PLAYER,
            character_id=ann,
            beat=1,
        )

        async def speak(conn: AsyncConnection) -> None:
            ids = await beats.beat_event_ids(conn, op.operation_id)
            await beats.write_utterances(
                conn,
                op,
                [
                    beats.Utterance(
                        beat=1,
                        speaker_character_id=ann,
                        proposition_ids=(),
                        objective_relation="unresolved",
                    )
                ],
                event_ids=ids,
            )

        op.add_commit_step(speak)
        await op.commit()


def _due() -> datetime:
    return clock.now()


async def _rows_of(engine: AsyncEngine, story_id: str) -> dict[str, set[str]]:
    """Every row that belongs to ``story_id``, as text, per table. The tables come from
    ``information_schema`` (every table with a ``story_id`` column), not from ``STORY_TABLES``,
    so a table the deletion forgets shows up as rows left behind; plus the story-scoped rows of
    ``config_changes`` and ``idempotency_keys`` (#61), which name the story by ``scope_id``, the
    key of the story's creation (#61), which names its operation, with those keys' subject-index
    rows (#123), and the story's relations (#104), which name it by ``entity_id``."""
    owned: dict[str, set[str]] = {}
    async with engine.connect() as conn:
        scoped = await conn.scalars(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'story_id'"
            )
        )
        for table in sorted(set(scoped) - STORY_ID_EXCEPTIONS):
            found = await conn.scalars(
                text(f'SELECT t::text FROM "{table}" t WHERE story_id = :s'),  # noqa: S608
                {"s": story_id},
            )
            owned[table] = set(found)
        owned["stories"] = set(
            await conn.scalars(text("SELECT t::text FROM stories t WHERE id = :s"), {"s": story_id})
        )
        owned["config_changes"] = set(
            await conn.scalars(
                text(
                    "SELECT t::text FROM config_changes t WHERE scope = 'story' AND scope_id = :s"
                ),
                {"s": story_id},
            )
        )
        # Its own keys, and the deployment-scoped key of its creation (bound to its operation).
        owned["idempotency_keys"] = set(
            await conn.scalars(
                text(
                    "SELECT t::text FROM idempotency_keys t WHERE (scope = 'story' AND "
                    "scope_id = :s) OR operation_id IN "
                    "(SELECT operation_id FROM operations WHERE story_id = :s)"
                ),
                {"s": story_id},
            )
        )
        # The index rows of those keys' stored results (#123): a deployment-scoped result's are
        # catalog-scoped (no story_id), and they go with the key.
        owned["subject_references"] |= set(
            await conn.scalars(
                text(
                    "SELECT t::text FROM subject_references t "
                    "WHERE t.record_table = 'idempotency_keys' AND t.record_id IN "
                    "(SELECT k.scope || ':' || k.scope_id || ':' || k.key FROM idempotency_keys k "
                    "WHERE (k.scope = 'story' AND k.scope_id = :s) OR k.operation_id IN "
                    "(SELECT operation_id FROM operations WHERE story_id = :s))"
                ),
                {"s": story_id},
            )
        )
        owned["entity_relations"] = set(
            await conn.scalars(
                text(
                    "SELECT t::text FROM entity_relations t "
                    "WHERE entity_type = 'story' AND entity_id = :s"
                ),
                {"s": story_id},
            )
        )
    return owned


@pytest.mark.part("M2", "FR-STORE-14,16")
async def test_s_delete(engine: AsyncEngine, settings: Settings) -> None:
    """Delete one story among three: exactly its rows are removed, every other row of every
    table is byte-identical, and the only new rows are the deletion's own record (its
    operation, event, audit row and key, and the ``deleted`` ownership event naming the owner
    and the relations removed, #104). Complement: the deleted story had rows in every
    story-scoped table and a relation, so the comparison bites."""
    lib = Engine(settings, db=engine)
    async with rest(settings, engine) as client:
        created = [await create(client, story_body("Ada", "Brann")) for _ in range(3)]
        for story in created:
            await _populate(engine, lib, story["story_id"], story["characters"][1]["id"])
        await lib.archive_story(AUTHOR, created[2]["story_id"])
        victim = created[1]["story_id"]
        # A turn waiting in the victim's queue (#63): the deletion takes it too.
        await turn_gate.enqueue(
            engine,
            victim,
            operation_id=new_ulid(),
            kind=turn_gate.RESPOND,
            request={"body": {}},
            principal="p-author",
            idempotency_key=None,
            viewpoint=None,
            origin="generated",
            queue_max=5,
        )
        # A background job of the victim's (#70): the deletion takes it too.
        async with engine.begin() as conn:
            await job_queue.enqueue(conn, REBUILD_KIND, {"story_id": victim}, story_id=victim)
            # A one-shot timer of the victim's (#71): the deletion takes it too.
            await timers.schedule(conn, "test_timer", victim, _due(), story_id=victim)

        owned = await _rows_of(engine, victim)
        assert all(owned.values()), {t: len(r) for t, r in owned.items() if not r}
        before = await dump(engine)
        response = await client.delete(f"/stories/{victim}", headers=WRITE)
        assert response.status_code == 204, response.text
        after = await dump(engine)

    assert set(after) == set(before)
    removed = {table: before[table] - after[table] for table in before}
    detached = removed.pop("audit_log")
    assert {t: r for t, r in removed.items() if r} == {t: r for t, r in owned.items() if r}
    added = {table: after[table] - before[table] for table in after}
    assert {t for t, r in added.items() if r} == {
        "operations",
        "events",
        "audit_log",
        "idempotency_keys",
        "ownership_events",
    }
    (dropped,) = await rows(
        engine,
        "SELECT entity_type, entity_id, kind, old, new, acting_principal FROM ownership_events "
        "WHERE kind = 'deleted' AND entity_id = :s",
        s=victim,
    )
    assert tuple(dropped) == (
        "story",
        victim,
        "deleted",
        {
            "owner_principal": "p-author",
            "relations": [{"principal": f"p-reader-{victim}", "relation": "player"}],
        },
        None,
        "p-author",
    )
    assert len(added["ownership_events"]) == 1
    # The deletion's own Idempotency-Key (#61): deployment-scoped, so it outlives the story.
    (key,) = await rows(
        engine,
        "SELECT scope, scope_id, status, result->>'status' AS answered, operation_id "
        "FROM idempotency_keys WHERE key = :k",
        k=response.headers["Idempotency-Key"],
    )
    assert tuple(key[:4]) == ("deployment", "deployment", "done", "204")
    # The audit_log exception (#56): the victim's rows stay with story_id nulled, and the
    # deletion writes one story_deleted row; no other story's audit row changed.
    audit = await rows(
        engine,
        "SELECT id, story_id, kind, principal, details->>'story_id' FROM audit_log "
        "WHERE details->>'story_id' = :s OR id = ANY(:ids) ORDER BY kind",
        s=victim,
        ids=[r.split(",")[0].lstrip("(") for r in detached],
    )
    assert len(detached) == 1
    assert [(r[1], r[2], r[3]) for r in audit] == [
        (None, "guardrail_input_rejected", "p-author"),
        (None, "story_deleted", "p-author"),
    ]
    assert audit[1][4] == victim
    assert len(added["audit_log"]) == 2
    record = await rows(
        engine,
        "SELECT o.story_id, o.kind, e.type, e.payload->>'story_id' FROM events e "
        "JOIN operations o ON o.operation_id = e.operation_id WHERE e.type = 'story_deleted' "
        "AND e.payload->>'story_id' = :s",
        s=victim,
    )
    assert [tuple(r) for r in record] == [
        (None, OperationKind.CONTROL.value, "story_deleted", victim)
    ]
    assert key.operation_id == next(iter(added["operations"])).split(",")[0].lstrip("(")
    assert (len(added["operations"]), len(added["events"])) == (1, 1)
    assert all(not r for r in (await _rows_of(engine, victim)).values())


async def test_story_isolation_schema(engine: AsyncEngine) -> None:
    """Every table with a ``story_id`` column is enumerated by ``STORY_TABLES`` (or is a
    declared deployment-level exception), so deletion cannot miss a new one. No story-scoped
    table has a foreign key or a column that could name another story, and
    ``characters.story_id`` is NOT NULL. Data check: no event belongs to an operation of
    another story."""
    async with engine.connect() as conn:
        with_story_id = set(
            await conn.scalars(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'story_id'"
                )
            )
        )
        assert with_story_id == {t.name for t in STORY_TABLES} | STORY_ID_EXCEPTIONS
        story_tables = sorted(with_story_id - STORY_ID_EXCEPTIONS)

        story_like = await conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(:t) "
                "AND column_name LIKE '%story%' AND column_name <> 'story_id'"
            ),
            {"t": story_tables},
        )
        assert story_like.all() == []

        foreign_keys = await conn.execute(
            text(
                "SELECT c.conrelid::regclass::text, a.attname, c.confrelid::regclass::text, "
                "fa.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "JOIN pg_attribute fa ON fa.attrelid = c.confrelid "
                "AND fa.attnum = ANY(c.confkey) "
                "WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(:t)"
            ),
            {"t": story_tables},
        )
        allowed = {
            ("story_id", "stories", "id"),
            ("operation_id", "operations", "operation_id"),
            # #160's mandatory provenance: a belief and the transition that produced it name each
            # other (deferred keys); the provenance trigger holds both to one story and holder.
            ("provenance", "belief_transitions", "id"),
            ("belief_id", "beliefs", "id"),
            # #166: a divergence names the contrary belief its lie deviates from (a deferred
            # key); both are the speaker's, in one story.
            ("believed_belief_id", "beliefs", "id"),
        }
        found = {
            (column, target, target_column) for _, column, target, target_column in foreign_keys
        }
        assert found <= allowed, found - allowed

        nullable = await conn.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'characters' AND column_name = 'story_id'"
            )
        )
        assert nullable == "NO"
        crossed = await conn.scalar(
            text(
                "SELECT count(*) FROM events e JOIN operations o "
                "ON o.operation_id = e.operation_id "
                "WHERE e.story_id IS DISTINCT FROM o.story_id"
            )
        )
        assert crossed == 0
    assert "stories" in await public_tables(engine)


@pytest.mark.part("M3", "FR-ADMIN-2; FR-STORE-14")
async def test_s_delete__erasure(pg_url: str, engine: AsyncEngine) -> None:
    """Right-to-be-forgotten reaches each story the subject appears in, and no other: catalog
    character Halvard (alias "Hal") is in two stories created from one scenario; a third story
    never had him. Erasing him in the first leaves the other two byte-identical (every row of
    theirs, by ``story_id``, ``scope_id`` and their operations' keys) while the first names him
    nowhere and no longer has his character (his reconstruction fails there); erasing him in
    the second reaches it too, and the third is still byte-identical."""
    settings = erasure.erasure_settings(pg_url)
    terms = ("Halvard Quist", "Hal")
    async with respond_client(settings, engine) as (client, _):
        halvard = await erasure.linda(client, "Halvard Quist", "Hal")
        scenario = g.scenario_body(
            g.role("uncle", fill="default", character={"catalog_id": halvard}),
            g.role("player", kind="player", label="The eldest"),
        )
        scenario["starting_state"]["seeded_memories"] = [
            {
                "text": "Hal buried the boat keys.",
                "holder": "world",
                "audience": ["self"],
                "disclosure": "secret",
            }
        ]
        scenario_id, _ = await g.published_scenario(client, scenario)
        first, second = [
            await g.create_from(client, g.from_scenario(scenario_id, ignore_warnings=True))
            for _ in range(2)
        ]
        third = await create(client, story_body("Ada", "Brann"))
        stories = [first["story_id"], second["story_id"], third["story_id"]]
        for story in stories[:2]:
            assert sum((await erasure.residue(engine, story, terms)).values()) > 0

        untouched = {s: await _rows_of(engine, s) for s in stories[1:]}
        await erasure.erase(client, "catalog_character", halvard, stories[0])
        assert {s: await _rows_of(engine, s) for s in stories[1:]} == untouched
        assert sum((await erasure.residue(engine, stories[0], terms)).values()) == 0
        assert sum((await erasure.residue(engine, stories[1], terms)).values()) > 0
        read = await client.get(f"/stories/{stories[0]}", headers=WRITE)
        assert read.status_code == 200, read.text
        assert [c["role_id"] for c in read.json()["characters"]] == ["player"]

        unrelated = await _rows_of(engine, stories[2])
        await erasure.erase(client, "catalog_character", halvard, stories[1])
        assert sum((await erasure.residue(engine, stories[1], terms)).values()) == 0
        assert await _rows_of(engine, stories[2]) == unrelated
