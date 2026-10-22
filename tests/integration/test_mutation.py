"""The mutation primitives' rules against PostgreSQL (#145; FR-GUARD-4,5). U-06
(``tests/test_u_06.py``), S-SELFPOLICE M4 and R-01 hold docs/04's clauses; this file holds
what they stand on: the module gates, the reserved reason, the author fact's supersede and its
memories, the lock as a configuration dependency (#136's note), a sanitized consolidation output,
the truncation reset of the implication counts, erasure over a quarantine's reason, and a fork
carrying the quarantine."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.api.routes.admin import ADMIN_PREFIX
from epistrel.core import config_dependencies, mutation
from epistrel.core.enums import MemoryKind, OperationKind, RecordProvenance
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import memory_record
from epistrel.core.events import Operation
from epistrel.core.fork import ForkRequest, fork
from epistrel.core.ids import new_ulid
from epistrel.core.membership import insert_derived
from epistrel.core.memory import records
from epistrel.core.mutation import RecordRef
from epistrel.core.tables.memories import memories
from epistrel.core.timeline import TruncateRequest, truncate, untruncate
from tests import canon_support as c
from tests import consolidation_support as cs
from tests.auth_support import bearer
from tests.erasure_support import erase, erasure_settings, residue
from tests.event_support import rows, scalar
from tests.memory_support import turn
from tests.respond_support import new_story, respond_client

pytestmark = pytest.mark.integration

WRITE = {**bearer(), "X-Epistrel-Principal": "p-author"}


def _admin(story: str, rest: str) -> str:
    return f"{ADMIN_PREFIX}/stories/{story}{rest}"


async def _consolidated(engine: AsyncEngine, story_id: str, text_: str, source: Any) -> str:
    """A level-1 consolidation output over ``source`` with a replay key and a stamped vector
    version, written the way #143's writer writes one (test-only: no model run)."""
    memory_id = new_ulid()
    async with Operation(
        engine, story_id=story_id, kind=OperationKind.CONSOLIDATION, principal="p-test"
    ) as op:

        async def step(conn: AsyncConnection) -> None:
            row = {
                "id": memory_id,
                "story_id": story_id,
                "kind": MemoryKind.SEMANTIC.value,
                "text": text_,
                "visibility": {"scope": "objective"},
                "provenance": RecordProvenance.SYSTEM_CONSOLIDATED.value,
                "trust_tier": records.trust_tier_of(RecordProvenance.SYSTEM_CONSOLIDATED).value,
                "operation_id": op.operation_id,
                "source_event_ids": list(source.source_event_ids),
                "level": 1,
                "modality": "observation",
                "inputs_hash": "sha256:replay-key",
                "embedding_model_version": "stub-embedding",
            }
            await insert_derived(conn, memories, [row])
            await index_records(
                conn,
                [
                    memory_record(
                        SimpleNamespace(**row, character_id=None, holder_character_id=None)
                    )
                ],
            )

        op.add_commit_step(step)
        await op.commit()
    return memory_id


async def _memory(engine: AsyncEngine, memory_id: str) -> Any:
    (row,) = await rows(engine, "SELECT * FROM memories WHERE id = :m", m=memory_id)
    return row


async def test_the_module_gates_and_the_reserved_reason(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A Lite story answers 409 ``module_off`` to the memory and fact calls alike, writing
    nothing; ``repeated_implication`` is the system's reason (422); an unknown memory is 404.
    Complement: the same calls on a structured story succeed."""
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):
        lite, _ids = await new_story(client)
        before = await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=lite)
        unknown = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"
        for method, path, body in (
            ("POST", f"/memories/{unknown}/quarantine", {"reason": "x"}),
            ("POST", f"/memories/{unknown}/sanitize", {"span": "a", "surrogate": "b"}),
            (
                "POST",
                "/facts",
                {"proposition": c.at(unknown, unknown, 0, None).model_dump(mode="json")},
            ),
        ):
            answer = await client.request(method, _admin(lite, path), json=body, headers=WRITE)
            assert answer.status_code == 409, answer.text
            assert answer.json()["error"]["code"] == "module_off"
        listed = await client.get(
            _admin(lite, "/facts"), params={"viewpoint": "omniscient"}, headers=bearer()
        )
        assert listed.json()["error"]["code"] == "module_off"
        after = await scalar(engine, "SELECT timeline_revision FROM stories WHERE id = :s", s=lite)
        assert after == before

        w = await cs.story(engine)
        _, (said,) = await turn(
            engine, w.story_id, actor=w.characters["Bobby"], say="Hi.", reply=None
        )
        reserved = await client.post(
            _admin(w.story_id, f"/memories/{said.id}/quarantine"),
            json={"reason": "repeated_implication"},
            headers=WRITE,
        )
        assert reserved.status_code == 422
        missing = await client.post(
            _admin(w.story_id, f"/memories/{unknown}/quarantine"),
            json={"reason": "x"},
            headers=WRITE,
        )
        assert missing.status_code == 404
        done = await client.post(
            _admin(w.story_id, f"/memories/{said.id}/quarantine"),
            json={"reason": "x"},
            headers=WRITE,
        )
        assert done.status_code == 200, done.text


async def test_an_author_fact_supersedes_and_its_lock_moves_the_configuration(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """An author fact over a conflicting functional fact supersedes it (``superseded``) and
    takes that fact's author memory with it. A lock is a new version with ``author_locked``
    set, and it moves #144's ``author_locks`` configuration dependency, so a structured turn
    captured before it is re-checked (#136's note); an unlock moves it back. A supersede
    inherits provenance and the lock. Complement: a lock of a locked version is a no-op with no
    operation."""
    w = await cs.story(engine)
    story, bobby = w.story_id, w.people["Bobby"]
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):

        async def commit(place: str, **extra: Any) -> dict[str, Any]:
            proposition = c.at(bobby, w.places[place], 0, None).model_dump(mode="json")
            answer = await client.post(
                _admin(story, "/facts"), json={"proposition": proposition, **extra}, headers=WRITE
            )
            assert answer.status_code == 201, answer.text
            body: dict[str, Any] = answer.json()
            return body

        first = await commit("kitchen")
        second = await commit("garden")
        assert second["superseded"] == [first["fact"]["id"]]
        old_memory = await _memory(engine, first["memory_id"])
        assert str(old_memory.membership) == "superseded"
        new_memory = await _memory(engine, second["memory_id"])
        assert str(new_memory.membership) == "active"

        async def locks() -> Any:
            async with engine.connect() as conn:
                captured = await config_dependencies.capture(conn, story, ["author_locks"])
            assert captured is not None
            return captured["author_locks"]

        unlocked = await locks()
        locked = await client.post(
            _admin(story, f"/facts/{second['fact']['id']}/lock"), headers=WRITE
        )
        assert locked.status_code == 200, locked.text
        version = locked.json()["fact"]
        assert version["author_locked"] is True
        assert version["lineage_id"] == second["fact"]["lineage_id"]
        assert await locks() != unlocked
        again = await client.post(_admin(story, f"/facts/{version['id']}/lock"), headers=WRITE)
        assert again.json()["operation_id"] is None
        superseded = await client.post(
            _admin(story, f"/facts/{version['id']}/supersede"),
            json={"proposition": c.at(bobby, w.places["garden"], 0, 100).model_dump(mode="json")},
            headers=WRITE,
        )
        assert superseded.status_code == 201, superseded.text
        successor = superseded.json()["fact"]
        assert (successor["author_locked"], successor["provenance"]) == (True, "author")
        unlock = await client.delete(_admin(story, f"/facts/{successor['id']}/lock"), headers=WRITE)
        assert unlock.json()["fact"]["author_locked"] is False
        assert await locks() == unlocked
        history = await client.get(
            _admin(story, "/facts"),
            params={"viewpoint": "omniscient", "include_superseded": "true"},
            headers=bearer(),
        )
        assert len(history.json()["facts"]) >= 5
        current = await client.get(
            _admin(story, "/facts"), params={"viewpoint": "omniscient"}, headers=bearer()
        )
        assert [f["id"] for f in current.json()["facts"]] == [unlock.json()["fact"]["id"]]
        narrator = await client.get(
            _admin(story, "/facts"), params={"viewpoint": "narrator"}, headers=bearer()
        )
        assert narrator.status_code == 403


async def test_a_sanitized_consolidation_output_is_never_replayed(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Sanitizing a consolidation output clears its ``inputs_hash`` (so #143 never replays it)
    and its vector, and enqueues its re-embedding. Complement: an episodic memory has no
    ``inputs_hash`` to clear and keeps none."""
    w = await cs.story(engine)
    _, (said,) = await turn(
        engine, w.story_id, actor=w.characters["Bobby"], say="The key is blue.", reply=None
    )
    derived = await _consolidated(engine, w.story_id, "Bobby says the key is blue.", said)
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):
        answer = await client.post(
            _admin(w.story_id, f"/memories/{derived}/sanitize"),
            json={"span": "blue", "surrogate": "a colour"},
            headers=WRITE,
        )
        assert answer.status_code == 200, answer.text
    row = await _memory(engine, derived)
    assert (row.text, row.inputs_hash, row.embedding_model_version) == (
        "Bobby says the key is a colour.",
        None,
        None,
    )
    assert str(row.kind) == MemoryKind.SEMANTIC.value
    queued = await scalar(
        engine,
        "SELECT count(*) FROM jobs WHERE kind = 'embed' AND story_id = :s AND status = 'queued'",
        s=w.story_id,
    )
    assert int(queued) >= 1
    assert (await _memory(engine, said.id)).inputs_hash is None


async def test_a_truncation_takes_its_implications_back(
    engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The implication counts a turn's rejected renders added follow the turn: a truncation of
    it takes them back off (FR-GUARD-5's counter is per story's active history), and an
    untruncation restores them. A quarantine is not undone by either. Complement: an
    implication by a turn that stays is kept."""
    w = await cs.story(engine)
    story = w.story_id
    kept, (planted,) = await turn(
        engine, story, actor=w.characters["Bobby"], say="Psst.", reply=None
    )
    later, _ = await turn(engine, story, actor=w.characters["Bobby"], say="Again.", reply=None)
    ref = (RecordRef("memories", planted.id),)
    await mutation.implicate(
        engine, story, ref, turn_operation_id=kept.operation_id, attempt=0, threshold=3
    )
    await mutation.implicate(
        engine, story, ref, turn_operation_id=later.operation_id, attempt=0, threshold=3
    )
    assert (await _memory(engine, planted.id)).implication_count == 2
    await truncate(engine, story, TruncateRequest(to_turn=1), principal="p-author")
    assert (await _memory(engine, planted.id)).implication_count == 1
    await untruncate(engine, story, principal="p-author")
    assert (await _memory(engine, planted.id)).implication_count == 2
    done = await mutation.implicate(
        engine, story, ref, turn_operation_id=later.operation_id, attempt=1, threshold=3
    )
    assert done.quarantined == (planted.id,)
    await truncate(engine, story, TruncateRequest(to_turn=1), principal="p-author")
    row = await _memory(engine, planted.id)
    assert (row.implication_count, row.quarantine_reason) == (1, "repeated_implication")


async def test_erasure_reaches_a_quarantine_reason_and_a_fork_keeps_the_quarantine(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """A quarantine's reason is text the subject index holds: erasing the character it names
    redacts it (and the event that recorded it), leaving no residue. A fork copies the
    quarantine with the memory. Complement: before the erasure the reason names her."""
    w = await cs.story(engine)
    story = w.story_id
    _, (said,) = await turn(engine, story, actor=w.characters["Bobby"], say="Hello.", reply=None)
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):
        answer = await client.post(
            _admin(story, f"/memories/{said.id}/quarantine"),
            json={"reason": "Ann planted this line."},
            headers=WRITE,
        )
        assert answer.status_code == 200, answer.text
        forked = await fork(engine, story, ForkRequest(at_turn=1), principal="p-author")
        copied = await rows(
            engine,
            "SELECT quarantine_reason, quarantined_by FROM memories WHERE story_id = :s",
            s=forked.story_id,
        )
        assert [(r.quarantine_reason, r.quarantined_by) for r in copied] == [
            ("Ann planted this line.", "p-author")
        ]
        assert (await residue(engine, story, ["Ann"]))["memories"] == 1
        await erase(client, "character", w.characters["Ann"], story, confirm_all=True)
    left = await residue(engine, story, ["Ann"])
    assert {table: n for table, n in left.items() if n} == {}
    row = await _memory(engine, said.id)
    assert row.quarantined_at is not None
    assert "Ann" not in row.quarantine_reason


async def test_a_quarantined_memory_is_flagged_on_omniscient_history_only(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#145 AC1 (#497): a quarantine leaves history alone. The quarantined memory's beat is
    still in the story's history, and under ``omniscient`` it carries ``quarantined: true``.
    A character viewpoint reads the same beat with
    no flag: which memories are quarantined is an omniscient fact. Complements asserted: the
    beat whose memory is not quarantined has no flag, and after the quarantine is reversed the
    omniscient read shows no flag either."""
    w = await cs.story(engine)
    story, bobby = w.story_id, w.characters["Bobby"]
    _, (said,) = await turn(
        engine, story, actor=bobby, say="The code is under the bell.", reply=None
    )
    _, (clean,) = await turn(engine, story, actor=bobby, say="Good night.", reply=None)

    async def history(viewpoint: str) -> dict[str, list[dict[str, Any]]]:
        answer = await client.get(
            f"/stories/{story}/history", params={"viewpoint": viewpoint}, headers=bearer()
        )
        assert answer.status_code == 200, answer.text
        found: dict[str, list[dict[str, Any]]] = {}
        for item in answer.json()["items"]:
            found.setdefault(item["message_id"], []).append(item)
        return found

    (said_event,) = said.source_event_ids
    (clean_event,) = clean.source_event_ids
    async with respond_client(erasure_settings(pg_url), engine) as (client, _):
        answer = await client.post(
            _admin(story, f"/memories/{said.id}/quarantine"),
            json={"reason": "A planted line."},
            headers=WRITE,
        )
        assert answer.status_code == 200, answer.text

        omniscient = await history("omniscient")
        assert [(i["channel"], i.get("quarantined")) for i in omniscient[said_event]] == [
            ("say", True)
        ]
        assert [i.get("quarantined") for i in omniscient[clean_event]] == [None]
        as_bobby = await history(w.viewpoint("Bobby"))
        assert [i["text"] for i in as_bobby[said_event]] == ["The code is under the bell."]
        assert all("quarantined" not in i for items in as_bobby.values() for i in items)

        released = await client.delete(
            _admin(story, f"/memories/{said.id}/quarantine"), headers=WRITE
        )
        assert released.status_code == 200, released.text
        after = await history("omniscient")
        assert all("quarantined" not in i for items in after.values() for i in items)
