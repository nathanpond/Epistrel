"""Batch E's per-turn read guards (#621, and the others' counts as they land): a two-primary
``sequential`` turn in the key room, its reads counted by wrapping the readers' database calls.

- #621: the predicate registry is read from the database at most once per turn attempt
  (``registry.turn_cache``; ~134 a turn at P-04 scale before);
- #615: the characters' private records (their belief statements) are read at most once per
  turn attempt (``core.turn_memo``; ~35 whole-partition reads a turn at P-04 scale before);
- #619: the commit's story-constant reads (the erasure index's story subjects, the erasure
  ledger, each character's ``is_player``) once per turn, and the whole turn's statement count
  under a recorded ceiling;
- #620: a story's first multi-character turn, with P-04's per-character knowledge, commits
  under a recorded statement ceiling (the clock's basis, the transaction's timestamp, each
  holder and entity read once, not once per transition)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.erasure import guard
from epistrel.core.erasure import index as erasure_index
from epistrel.core.propositions import registry
from epistrel.core.verify import protected
from tests import multi_support as ms
from tests.auth_support import auth_settings
from tests.fixtures.key_room import KeyRoom, key_room
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@contextmanager
def counting(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> Iterator[list[Any]]:
    """Every call of ``module.name`` while inside, recorded (its first positional args)."""
    calls: list[Any] = []
    original = getattr(module, name)

    async def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[1:2])
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, name, counted)
        yield calls


async def _two_primary_turn(client: Any, stub: Any, room: KeyRoom) -> None:
    stub.reset()
    ms.renders_for(stub, "Bobby", ms.render(beat("say", "Is the lamp on?", intent="sincere")))
    ms.renders_for(stub, "Jenni", ms.render(beat("say", "It is.", intent="sincere")))
    answer = await respond(
        client,
        room.story_id,
        body(room.cal, "What now?", render_character_ids=[room.bobby, room.jenni]),
    )
    assert answer.status_code == 200, answer.text


async def test_a_turn_reads_the_registry_once(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        with counting(monkeypatch, registry, "_read") as reads:
            await _two_primary_turn(client, epistrel.models.stub, room)
        mine = [r for r in reads if r == (room.story_id,)]
        assert len(mine) <= 1, len(mine)


async def test_a_turn_reads_the_private_records_once(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        with counting(monkeypatch, protected, "_private_records") as reads:
            await _two_primary_turn(client, epistrel.models.stub, room)
        assert 1 <= len(reads) <= 1, len(reads)


TURN_STATEMENTS_MAX = 800
"""#619: the ceiling on a two-primary key-room turn's statements: 755 measured after batch E's
reads moved to the per-turn memo (764 before #619, with #615 and #621 in), with room for
noise; a regression that re-reads per record or per transition crosses it."""


async def test_a_turn_reads_story_constants_once(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event

    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        statements: list[str] = []

        def count(*args: Any) -> None:
            statements.append(str(args[2]))

        sync = engine.sync_engine
        with (
            counting(monkeypatch, erasure_index, "story_subjects") as subjects,
            counting(monkeypatch, guard, "_story_erasures") as ledger,
        ):
            event.listen(sync, "before_cursor_execute", count)
            try:
                await _two_primary_turn(client, epistrel.models.stub, room)
            finally:
                event.remove(sync, "before_cursor_execute", count)
        assert len(subjects) <= 1, len(subjects)
        assert len(ledger) <= 1, len(ledger)
        players = [s for s in statements if s.startswith("SELECT characters.is_player")]
        assert len(players) <= 3, len(players)  # Cal, Bobby, Jenni: once each
        assert len(statements) <= TURN_STATEMENTS_MAX, len(statements)


async def test_retrieved_memories_are_touched_after_the_turn(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#618: a structured turn's retrievals touch their memories' ``last_accessed_at`` in one
    statement after the turn has committed, not inside the retrieve stage."""
    from sqlalchemy import event

    async with respond_client(stub_settings, engine) as (client, epistrel):
        room = await key_room(client, engine)
        await _two_primary_turn(client, epistrel.models.stub, room)
        statements: list[str] = []

        def record(*args: Any) -> None:
            statements.append(str(args[2]))

        sync = engine.sync_engine
        event.listen(sync, "before_cursor_execute", record)
        try:
            await _two_primary_turn(client, epistrel.models.stub, room)
        finally:
            event.remove(sync, "before_cursor_execute", record)
    touches = [i for i, s in enumerate(statements) if "SET last_accessed_at" in s]
    assert len(touches) <= 1, len(touches)
    if touches:
        committed = max(i for i, s in enumerate(statements) if s.startswith("INSERT INTO events"))
        assert touches[0] > committed


async def test_new_epistemic_records_delete_no_references(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#620: the testimony turn's new beliefs, transitions and exposures index their subject
    references without first deleting any (``index_records(fresh=True)``: a row just inserted
    has none), so a turn that writes many of them saves a statement each."""
    from tests.scenario import test_s_testimony as st

    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids, _things = await st._setup(client, engine)
        with counting(monkeypatch, erasure_index, "_delete_records") as deletes:
            await st._play(client, epistrel, story, ids)
    tables = {table for (keys,) in deletes for table, _ in keys}
    assert not tables & {"beliefs", "belief_transitions", "exposures"}, tables


FIRST_TURN_COMMIT_MAX = 2_250
"""#620: the ceiling on the commit statements of a story's first two-primary turn over P-04's
per-character knowledge (200 beliefs and 50 unresolved exposures a character, 12 characters):
measured after #620's per-transaction reads, 2,193 measured, with room for noise; 4,017 before."""
SECOND_TURN_COMMIT_MAX = 360
"""#620: the same story's second turn (340 measured; 569 before)."""


def _in_commit() -> bool:
    """Whether the statement being executed is the structured commit's (``write_turn_body`` is
    on the awaiting stack: SQLAlchemy runs it in a greenlet whose parent holds the coroutine
    frames)."""
    import greenlet  # type: ignore[import-untyped]

    parent = greenlet.getcurrent().parent
    frame = parent.gr_frame if parent is not None else None
    while frame is not None:
        if frame.f_code.co_name == "write_turn_body":
            return True
        frame = frame.f_back
    return False


async def test_a_first_multi_character_turn_commits_under_its_ceiling(pg_url: str) -> None:
    """#620: the first two-primary turn of a P-04 multi-character story (its knowledge layer,
    its room, its stub render claims) commits under ``FIRST_TURN_COMMIT_MAX`` statements, and the
    second under ``SECOND_TURN_COMMIT_MAX``.

    Complement asserted: the first turn is the one with the lazy resolutions (it reports
    ``lazy_resolution_capped``), so the ceiling measures the work the issue names."""
    from perf.lite_smoke import multi
    from sqlalchemy import event

    from epistrel.core.context import RequestContext
    from epistrel.testing.fixtures import knowledge, scale
    from tests.perf.scale_support import scale_database

    spec = scale.ScaleSpec.scaled(1_000)  # P-04's scale; a smaller story has too few records
    async with scale_database(pg_url, spec) as run:
        await knowledge.apply(run.db, spec, run.fixture.story, knowledge.KnowledgeSpec())
        ref = await multi.prepare(run.lib, RequestContext(principal="p"), run.fixture.story, 0)
        settings = auth_settings(
            run.url,
            model_adapter="stub",
            stub_embedding_dimension=spec.dimension,
            stub_render_claims=multi.render_claims(scale.person_names(spec.characters)[1]),
        )
        counts: list[int] = []
        degradations: list[list[str]] = []
        async with respond_client(settings, run.db) as (client, _):
            found = [0]

            def count(*_: Any) -> None:
                if _in_commit():
                    found[0] += 1

            for _turn in range(2):
                found[0] = 0
                event.listen(run.db.sync_engine, "before_cursor_execute", count)
                try:
                    answer = await respond(
                        client,
                        ref.story_id,
                        body(
                            ref.player_id,
                            "What now?",
                            render_character_ids=list(ref.primary_ids),
                        ),
                    )
                finally:
                    event.remove(run.db.sync_engine, "before_cursor_execute", count)
                assert answer.status_code == 200, answer.text
                counts.append(found[0])
                degradations.append(answer.json().get("degradation") or [])
    assert "lazy_resolution_capped" in degradations[0], degradations
    assert counts[0] <= FIRST_TURN_COMMIT_MAX, counts
    assert counts[1] <= SECOND_TURN_COMMIT_MAX, counts
