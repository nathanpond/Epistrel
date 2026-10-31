"""docs/04 I-01: the event store and the atomic turn commit (the M2 part), and the staged
turn's stages (the M6 part, #212: ``test_i_01__staged``)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation
from epistrel.core.events.types import MessageRendered, PlayerInput
from epistrel.core.idempotency import IdempotentCall
from epistrel.core.states import contingency as contingency_rules
from epistrel.core.views import audit_view, corrected_history
from epistrel.orchestrator.structured import staged
from tests import effect_support as es
from tests import multi_support as ms
from tests import provider_support as ps
from tests import staged_support as sp
from tests.auth_support import auth_settings
from tests.event_support import (
    execute_in_tx,
    new_story,
    play_turn,
    record,
    rows,
    scalar,
    sqlstate,
    story_row,
)
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, structured_story


class Boom(Exception):
    """Induced failure."""


FR_STORE_2_COLUMNS = {
    "payload": ("jsonb", "jsonb", "NO"),
    "provenance": ("USER-DEFINED", "provenance", "NO"),
    "authority": ("USER-DEFINED", "authority", "NO"),
    "valid_time": ("timestamp with time zone", "timestamptz", "NO"),
    "tx_time": ("timestamp with time zone", "timestamptz", "NO"),
}

REFUSED = "EPI01"
COUNT_FOR_OPERATION = text("SELECT count(*) FROM events WHERE operation_id = :o")


async def _event_columns(engine: AsyncEngine) -> dict[str, tuple[str, str, str]]:
    found = await rows(
        engine,
        "SELECT column_name, data_type, udt_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'events'",
    )
    return {r.column_name: (r.data_type, r.udt_name, r.is_nullable) for r in found}


async def _refused(engine: AsyncEngine, sql: str, **params: str) -> str | None:
    with pytest.raises(DBAPIError) as caught:
        await execute_in_tx(engine, (sql, params))
    return sqlstate(caught.value)


@pytest.mark.part("M2", "FR-STORE-1,2,5; NFR-REL-1")
async def test_i_01(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Events are append-only with a dense per-story seq, a typed payload, provenance, authority,
    valid_time and tx_time, and the requesting principal; a turn commits all or nothing.
    Complement: the two sanctioned deletes (one story's rows; audit rows under expiry) succeed."""
    story = await new_story(engine, principal="alice")
    first = await play_turn(engine, story, "hello", principal="alice")
    second = await play_turn(engine, story, "again", principal="bob")

    # Monotonic, dense seq per story; another story counts from 1 independently.
    seqs = await rows(engine, "SELECT seq FROM events WHERE story_id = :s ORDER BY seq", s=story)
    assert [r.seq for r in seqs] == [1, 2, 3, 4, 5]
    assert first.seqs == (2, 3)
    assert second.seqs == (4, 5)
    other = await new_story(engine)
    await play_turn(engine, other, "elsewhere")
    assert await scalar(engine, "SELECT max(seq) FROM events WHERE story_id = :s", s=other) == 3

    # Typed payloads round-trip; position comes from the turn, not from seq.
    history = await corrected_history(engine, story)
    turns = [e for e in history if e.narrative_turn is not None]
    assert [(e.narrative_turn, e.type, e.payload) for e in turns] == [
        (1, "player_input", PlayerInput(text="hello")),
        (1, "message_rendered", MessageRendered(text="reply to hello")),
        (2, "player_input", PlayerInput(text="again")),
        (2, "message_rendered", MessageRendered(text="reply to again")),
    ]

    # FR-STORE-2, column by column, and on every row.
    columns = await _event_columns(engine)
    for column, expected in FR_STORE_2_COLUMNS.items():
        assert columns[column] == expected, column
    defaults = await rows(
        engine,
        "SELECT column_name, column_default FROM information_schema.columns "
        "WHERE table_name = 'events' AND column_name IN ('valid_time', 'tx_time')",
    )
    assert {r.column_name: r.column_default for r in defaults} == {
        "valid_time": "transaction_timestamp()",
        "tx_time": "transaction_timestamp()",
    }
    for event in turns:
        op = first if event.narrative_turn == 1 else second
        assert event.provenance.value == ("player" if event.type == "player_input" else "engine")
        assert event.authority.value == "authoritative"
        assert event.tx_time == op.tx_time
        assert event.valid_time == event.tx_time
    assert first.tx_time is not None
    assert second.tx_time is not None
    assert first.tx_time < second.tx_time

    # The requesting principal is stored on the operation and on every one of its events.
    recorded = await rows(
        engine,
        "SELECT o.principal AS op_principal, e.principal FROM operations o "
        "JOIN events e USING (operation_id) WHERE o.operation_id IN (:a, :b)",
        a=first.operation_id,
        b=second.operation_id,
    )
    assert sorted({(r.op_principal, r.principal) for r in recorded}) == [
        ("alice", "alice"),
        ("bob", "bob"),
    ]

    # Append-only: UPDATE, DELETE and TRUNCATE are refused by the trigger.
    assert await _refused(engine, "UPDATE events SET type = 'x' WHERE story_id = :s", s=story) == (
        REFUSED
    )
    assert await _refused(engine, "DELETE FROM events WHERE story_id = :s", s=story) == REFUSED
    assert await _refused(engine, "TRUNCATE events CASCADE") == REFUSED
    with pytest.raises(DBAPIError) as caught:  # the variable names a different story
        await execute_in_tx(
            engine,
            ("SELECT set_config('epistrel.allow_story_delete', :s, true)", {"s": other}),
            ("DELETE FROM events WHERE story_id = :s", {"s": story}),
        )
    assert sqlstate(caught.value) == REFUSED
    assert await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story) == 5

    # Complement 1: a story-scoped delete under epistrel.allow_story_delete removes that story only.
    await execute_in_tx(
        engine,
        ("SELECT set_config('epistrel.allow_story_delete', :s, true)", {"s": other}),
        ("DELETE FROM events WHERE story_id = :s", {"s": other}),
    )
    assert await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=other) == 0
    assert await scalar(engine, "SELECT count(*) FROM events WHERE story_id = :s", s=story) == 5

    # Complement 2: under epistrel.allow_audit_expiry an audit row may go, a story row may not.
    rejected = await record(
        engine,
        story,
        OperationKind.MAINTENANCE,
        "guardrail_output_rejected",
        {"text": "no", "reason": "test"},
        turn=2,
    )
    expiry: tuple[str, dict[str, Any]] = (
        "SELECT set_config('epistrel.allow_audit_expiry', 'on', true)",
        {},
    )
    with pytest.raises(DBAPIError) as caught:
        await execute_in_tx(
            engine,
            expiry,
            ("DELETE FROM events WHERE operation_id = :o", {"o": second.operation_id}),
        )
    assert sqlstate(caught.value) == REFUSED
    await execute_in_tx(
        engine,
        expiry,
        ("DELETE FROM events WHERE operation_id = :o", {"o": rejected.operation_id}),
    )
    assert [e.type for e in await audit_view(engine, story)].count("guardrail_output_rejected") == 0

    # All or nothing: an exception after three staged events leaves no event, a failed
    # operation, and the story row exactly as it was.
    before = await story_row(engine, story)
    started: list[Operation] = []
    with pytest.raises(Boom):
        await _stage_three(engine, story, started, commit=False)
    aborted = started.pop()
    await _assert_rolled_back(engine, story, aborted.operation_id, before)

    # All or nothing mid-commit: the failure comes after the event rows were inserted.
    async def explode(self: Operation, conn: AsyncConnection, *_: object) -> None:
        # The rows are visible inside the commit transaction before it fails.
        inserted = await conn.scalar(COUNT_FOR_OPERATION, {"o": self.operation_id})
        assert inserted == 3
        raise Boom

    monkeypatch.setattr(Operation, "_finalise", explode)
    with pytest.raises(Boom):
        await _stage_three(engine, story, started, commit=True)
    mid = started.pop()
    await _assert_rolled_back(engine, story, mid.operation_id, before)
    assert mid.seqs == ()
    assert mid.narrative_turn is None


async def _stage_three(
    engine: AsyncEngine, story: str, started: list[Operation], *, commit: bool
) -> None:
    """Stage three events in a turn, then fail: by raising, or inside ``commit()``."""
    async with Operation(engine, story_id=story, kind=OperationKind.TURN, principal="alice") as op:
        started.append(op)
        for said in ("one", "two", "three"):
            op.append("player_input", {"text": said})
        assert len(op.staged) == 3
        if commit:
            await op.commit()
        raise Boom


async def _assert_rolled_back(
    engine: AsyncEngine, story: str, operation_id: str, before: tuple[object, ...]
) -> None:
    assert (
        await scalar(engine, "SELECT count(*) FROM events WHERE operation_id = :o", o=operation_id)
        == 0
    )
    assert (
        await scalar(
            engine, "SELECT status FROM operations WHERE operation_id = :o", o=operation_id
        )
        == "failed"
    )
    assert await story_row(engine, story) == before


# -- M6 (#212): the staged turn ------------------------------------------------------------------


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _failing(
    client: Any,
    engine: AsyncEngine,
    epistrel: Any,
    monkeypatch: pytest.MonkeyPatch,
    target: tuple[object, str],
) -> tuple[str, int, Any]:
    """A staged purchase whose stage ``target`` (``(object, name)``) raises ``Boom``: the
    story, its revision before the turn, and the turn's operation row afterwards."""
    story, cal, _, fake = await sp.sync_story(client, engine, epistrel)
    fake.script_writes("gold", "confirmed")
    sp.script_purchase(epistrel.models.stub, cal)
    before = await sp.revision(engine, story)

    async def boom(*_: Any, **__: Any) -> Any:
        raise Boom("induced")

    with monkeypatch.context() as patched:
        patched.setattr(target[0], target[1], boom)
        try:
            answer = await sp.buy(client, story, cal)
        except Boom:
            answer = None
    assert answer is None or answer.status_code >= 500
    return story, before, await sp.staged_op(engine, story)


@pytest.mark.part("M6", "FR-EXT-19")
async def test_i_01__staged(
    stub_settings: Settings,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staged turn (#212): an induced failure inside any stage rolls back that stage only,
    leaving the turn at the previous stage boundary with the earlier stages intact, and the
    timeline revision advances exactly once at intent, once at outcome and once at completion,
    never at the claim or the stored response (read between the stages).

    - The claim's bookkeeping fails: the turn stays ``intent_committed`` (one revision), its
      attempt beats committed, no outcome.
    - The outcome transaction fails (its settlement step): ``awaiting_outcome``, still one
      revision, no ``external_outcome`` event, the intent intact.
    - The completion fails (its report step): ``outcome_recorded``, two revisions, the outcome
      event kept, no outcome-step beat and no message.
    - The response storage fails: ``completed``, three revisions, the message committed, no
      ``response`` stage logged.

    Complement: an ordinary turn still commits in one transaction, bumps the revision once and
    has ``turn_stage IS NULL``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        # The revision read between the stages of a turn that completes.
        story, cal, _, fake = await sp.sync_story(client, engine, epistrel)
        fake.script_writes("gold", "confirmed")
        sp.script_purchase(epistrel.models.stub, cal)
        seen: list[tuple[str, int]] = []
        claim_all, record_outcomes = staged.claim_all, staged.record_outcomes
        store_response = staged.store_response

        async def claiming(*args: Any, **kwargs: Any) -> Any:
            seen.append(("intent", await sp.revision(engine, story)))
            found = await claim_all(*args, **kwargs)
            seen.append(("claim", await sp.revision(engine, story)))
            return found

        async def recording(*args: Any, **kwargs: Any) -> Any:
            found = await record_outcomes(*args, **kwargs)
            seen.append(("outcome", await sp.revision(engine, story)))
            return found

        async def storing(*args: Any, **kwargs: Any) -> Any:
            seen.append(("completion", await sp.revision(engine, story)))
            await store_response(*args, **kwargs)
            seen.append(("response", await sp.revision(engine, story)))

        before = await sp.revision(engine, story)
        with monkeypatch.context() as patched:
            patched.setattr(staged, "claim_all", claiming)
            patched.setattr(staged, "record_outcomes", recording)
            patched.setattr(staged, "store_response", storing)
            answer = await sp.buy(client, story, cal)
        assert answer.status_code == 200, answer.text
        assert seen == [
            ("intent", before + 1),
            ("claim", before + 1),
            ("outcome", before + 2),
            ("completion", before + 3),
            ("response", before + 3),
        ]
        op = await sp.staged_op(engine, story)
        assert (op.status, op.turn_stage, op.timeline_revision) == (
            "committed",
            "completed",
            before + 3,
        )
        assert await sp.stage_names(engine, story) == list(staged.STAGES)
        await ps.drop_outbox_jobs(engine, story)

        # The claim's bookkeeping fails: the turn stays at its intent.
        story, before, op = await _failing(
            client, engine, epistrel, monkeypatch, (staged, "append_stage")
        )
        assert op.turn_stage == "intent_committed"
        assert await sp.revision(engine, story) == before + 1
        types = await sp.event_types(engine, op.operation_id)
        assert "beat_do" in types
        assert "external_outcome" not in types
        assert "message_rendered" not in types
        await ps.drop_outbox_jobs(engine, story)

        # The outcome transaction fails: the turn stays awaiting its outcome.
        story, before, op = await _failing(
            client, engine, epistrel, monkeypatch, (contingency_rules, "settle")
        )
        assert op.turn_stage == "awaiting_outcome"
        assert await sp.revision(engine, story) == before + 1
        types = await sp.event_types(engine, op.operation_id)
        assert "beat_do" in types
        assert "external_outcome" not in types
        await ps.drop_outbox_jobs(engine, story)

        # The completion fails: the outcome stays recorded.
        story, before, op = await _failing(
            client, engine, epistrel, monkeypatch, (staged, "write_report")
        )
        assert op.turn_stage == "outcome_recorded"
        assert await sp.revision(engine, story) == before + 2
        types = await sp.event_types(engine, op.operation_id)
        assert types.count("external_outcome") == 1
        assert "message_rendered" not in types
        assert types.count("beat_narration") == 1  # the intent's narration only
        await ps.drop_outbox_jobs(engine, story)

        # The response storage fails: the turn stays completed.
        story, before, op = await _failing(
            client, engine, epistrel, monkeypatch, (IdempotentCall, "store")
        )
        assert op.turn_stage == "completed"
        assert await sp.revision(engine, story) == before + 3
        assert "message_rendered" in await sp.event_types(engine, op.operation_id)
        assert await sp.stage_names(engine, story) == list(staged.STAGES[:-1])
        await ps.drop_outbox_jobs(engine, story)

        # Complement: an ordinary turn, one transaction, one revision, no stage.
        story, ids = await structured_story(client, "Cal", "Mira")
        stub = epistrel.models.stub
        stub.reset()
        es.narrator(stub, es.render(beat("narration", "Rain taps.")))
        ms.renders_for(stub, "Mira", es.render(beat("say", "Close the door.")))
        before = await sp.revision(engine, story)
        answer = await respond(client, story, body(ids["Cal"], None, do="waits"))
        assert answer.status_code == 200, answer.text
        assert await sp.revision(engine, story) == before + 1
        (row,) = await rows(
            engine,
            "SELECT turn_stage, stage_log FROM operations WHERE story_id = :s AND kind = 'turn'",
            s=story,
        )
        assert row.turn_stage is None
        assert not [e for e in row.stage_log or [] if e.get("stage") in staged.STAGES]
