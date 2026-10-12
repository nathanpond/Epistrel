"""S-DELETE: deleting one story removes exactly its rows; stories are self-contained."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import Engine, RelationRequest
from epistrel.config import Settings
from epistrel.core import clock, turn_gate
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind
from epistrel.core.errors import GuardrailViolation
from epistrel.core.ids import new_ulid
from epistrel.core.ingest import IngestRequest
from epistrel.core.stories import STORY_ID_EXCEPTIONS, STORY_TABLES
from epistrel.core.tables.ownership import EntityType
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.orchestrator.jobs import timers
from epistrel.orchestrator.jobs.handlers.rebuild import REBUILD_KIND
from epistrel.orchestrator.policies.input_gate import EntryPoint, GateText
from epistrel.orchestrator.policies.schemas import StoryPolicyItem
from tests.event_support import interleave, play_turn, rows
from tests.policy_support import unique, words
from tests.story_support import WRITE, create, dump, public_tables, rest, story_body

AUTHOR = RequestContext(principal="p-author")


async def _populate(engine: AsyncEngine, lib: Engine, story_id: str, character_id: str) -> None:
    """Turns, background work between them, story and character policies, a rebuild, an
    ingested narration (a ``client_events`` row, #58), a rejected input (an ``audit_log``
    row naming the story) and a relation (``entity_relations``, #104). The victim also gets a
    queued turn (``turn_queue``, #63)."""
    for n in range(1, 3):
        await play_turn(engine, story_id, f"turn {n} of {story_id}")
        await interleave(engine, story_id, after_turn=n)
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
    reader = RelationRequest(principal=f"p-reader-{story_id}", relation="player")
    await lib.add_relation(AUTHOR, EntityType.STORY, story_id, reader)
    narration = {"client_event_id": "narration-1", "kind": "narration", "text": f"of {story_id}"}
    await lib.ingest(AUTHOR, story_id, IngestRequest.model_validate({"events": [narration]}))
    with pytest.raises(GuardrailViolation):
        await lib.gate_input(
            AUTHOR, story_id, EntryPoint.INGEST, [GateText("events[0]", "an unsayable thing")]
        )


def _due() -> datetime:
    return clock.now()


async def _rows_of(engine: AsyncEngine, story_id: str) -> dict[str, set[str]]:
    """Every row that belongs to ``story_id``, as text, per table. The tables come from
    ``information_schema`` (every table with a ``story_id`` column), not from ``STORY_TABLES``,
    so a table the deletion forgets shows up as rows left behind; plus the story-scoped rows of
    ``config_changes`` and ``idempotency_keys`` (#61), which name the story by ``scope_id``, the
    key of the story's creation (#61), which names its operation, and the story's relations
    (#104), which name it by ``entity_id``."""
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
