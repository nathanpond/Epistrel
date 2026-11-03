"""#221's amendment parts (FR-REWRITE-12, FR-STORE-20): per-part eligibility (pure), which
tables are correctable and by what strategy, and the parts that are refused (a database test:
``record_amendment`` writes nothing when a part fails)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.amendments import store
from epistrel.core.amendments.eligibility import (
    Eligibility,
    PartTiming,
    eligible,
    partial_period,
)
from epistrel.core.amendments.store import PropositionPartDraft, RecordPartDraft
from epistrel.core.errors import AmendmentInvalidPart, ScheduleNotAmendable
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.tables import metadata
from tests import amendment_support as am
from tests import canon_support as c
from tests.event_support import rows, scalar

IN, OUT, OPEN = Eligibility.INCLUDED, Eligibility.EXCLUDED, Eligibility.UNDECIDABLE
DECADE = (date(2020, 1, 1), date(2031, 1, 1))
"""A "Saturday afternoon" anchor whose year is only known to be 2020-2030."""


def test_eligibility() -> None:
    """Offsets compare exactly under any anchor ("before the story began" is -1, "an hour
    before turn 40" its offset less 60), whatever the anchor's dates; an absolute "1990"
    against a 2020-2030 anchor is certainly before (in), "2040" certainly after (out), "2025"
    undecidable (read as in until #240); no valid start is in everywhere. Per part: one
    amendment's two parts at one checkpoint can differ."""
    turn_40 = 400
    for anchor in (None, DECADE, (date(1850, 6, 1), date(1850, 6, 2))):
        assert eligible(PartTiming(-1), story_time=turn_40, absolute=anchor) is IN
        assert eligible(PartTiming(turn_40 - 60), story_time=turn_40, absolute=anchor) is IN
        assert eligible(PartTiming(turn_40 + 1), story_time=turn_40, absolute=anchor) is OUT
    early = PartTiming(absolute=partial_period("1990"))
    late = PartTiming(absolute=partial_period("2040"))
    assert eligible(early, story_time=0, absolute=DECADE) is IN
    assert eligible(late, story_time=0, absolute=DECADE) is OUT
    assert (
        eligible(PartTiming(absolute=partial_period("2025")), story_time=0, absolute=DECADE) is OPEN
    )
    assert eligible(early, story_time=0, absolute=None) is OPEN
    assert eligible(PartTiming(), story_time=None) is IN
    assert OPEN.folds
    assert not OUT.folds
    assert partial_period("1990-05") == (date(1990, 5, 1), date(1990, 6, 1))
    assert PartTiming.of(None, {"absolute": "2040"}) == late
    with pytest.raises(ValueError, match="partial date"):
        partial_period("last spring")


def test_eligibility__bounds() -> None:
    """#240: a position bound decides by turn index, whatever the valid-time: ``story_start``
    is in everywhere; ``after T`` is out at ``T-1`` and ``T``, in at ``T⁺1`` and ``T+1``;
    ``before T`` is out at ``T-1``, in at ``T``, ``T⁺1`` and ``T+1``. Complement: a fully dated
    anchor decides "2012" exactly (never undecidable)."""
    turn = 40
    points = {"T-1": (turn - 1, 0), "T": (turn, 0), "T+slot": (turn, 1), "T+1": (turn + 1, 0)}
    start = PartTiming.of(None, {"absolute": "2012"}, {"story_start": True})
    after = PartTiming.of(None, {"absolute": "2012"}, {"relation": "after", "turn": turn})
    before = PartTiming.of(None, {"absolute": "2012"}, {"relation": "before", "turn": turn})
    found = {
        name: tuple(
            eligible(t, story_time=None, absolute=None, checkpoint=at).value
            for t in (start, after, before)
        )
        for name, at in points.items()
    }
    assert found == {
        "T-1": ("included", "excluded", "excluded"),
        "T": ("included", "excluded", "included"),
        "T+slot": ("included", "included", "included"),
        "T+1": ("included", "included", "included"),
    }
    dated = (date(2025, 6, 7), date(2025, 6, 8))
    year = PartTiming(absolute=partial_period("2012"))
    assert eligible(year, story_time=0, absolute=dated) is IN
    assert eligible(year, story_time=0, absolute=None) is OPEN


def test_correctable_tables() -> None:
    """Every table holding correctable narrative content is registered with its strategy, and
    the deliberate exclusions are named with their reason; every name is a table."""
    assert {name: spec.strategy for name, spec in store.CORRECTABLE_TABLES.items()} == {
        "events": "overlay",
        "facts": "version",
        "beliefs": "version",
        "memories": "in_place",
        "utterances": "in_place",
        "state_changes": "in_place",
    }
    assert set(store.NOT_CORRECTABLE) == {
        "external_values",
        "external_effect_outbox",
        "outbox_outcomes",
        "reconciliation_marks",
        "state_definitions",
        "scheduled_events",
    }
    assert all(store.NOT_CORRECTABLE.values())
    tables = set(metadata.tables)
    assert set(store.CORRECTABLE_TABLES) <= tables
    assert set(store.NOT_CORRECTABLE) <= tables
    assert "intent" not in store.CORRECTABLE_TABLES["utterances"].columns


async def _refused(engine: AsyncEngine, story: str, part: Any) -> Any:
    before = await scalar(engine, "SELECT count(*) FROM amendments WHERE story_id = :s", s=story)
    with pytest.raises((AmendmentInvalidPart, ScheduleNotAmendable)) as found:
        await am.amend(engine, story, [part])
    after = await scalar(engine, "SELECT count(*) FROM amendments WHERE story_id = :s", s=story)
    assert after == before
    return found.value


@pytest.mark.integration
async def test_amendment_part_refusals(engine: AsyncEngine) -> None:
    """A proposition part on ``scheduled`` is 422 ``schedule_not_amendable`` naming the
    reschedule route, nothing written (``scheduled_events`` unchanged); a relative time is
    ``unresolved_time``; a record part on a table that is not correctable, an action its table
    does not take, a column outside the allow-list, an unknown record and a belief (#236) are
    each refused with their reason, nothing written. The relaxed ``positioned`` CHECKs still
    refuse a position-less ``state_changes``/``scheduled_events`` row with no
    ``amendment_part_id``."""
    w = await c.world(engine)
    bobby = w.people["Bobby"]
    replies = await am.play(engine, w.story_id, 2)
    events_before = await scalar(
        engine, "SELECT count(*) FROM scheduled_events WHERE story_id = :s", s=w.story_id
    )
    plan = c.at(bobby, w.places["school"], None, None).model_copy(
        update={"predicate_id": SCHEDULED}
    )
    refused = await _refused(engine, w.story_id, PropositionPartDraft(plan))
    assert refused.code == "schedule_not_amendable"
    assert refused.details["route"] == store.SCHEDULE_ROUTE
    assert (
        await scalar(
            engine, "SELECT count(*) FROM scheduled_events WHERE story_id = :s", s=w.story_id
        )
        == events_before
    )
    relative = PropositionPartDraft(
        c.at(bobby, w.places["garden"], None, None), relative="last spring"
    )
    assert (await _refused(engine, w.story_id, relative)).details["reason"] == "unresolved_time"
    for part, reason in (
        (RecordPartDraft("scheduled_events", replies[1]), "table_not_correctable"),
        (RecordPartDraft("events", replies[1], "relabel", {"text": "x"}), "action_not_allowed"),
        (RecordPartDraft("utterances", replies[1], "replace", {"intent": "x"}), "record_not_found"),
        (RecordPartDraft("events", "01JZZZZZZZZZZZZZZZZZZZZZZZ", "drop"), "record_not_found"),
        (RecordPartDraft("beliefs", replies[1], "drop"), "action_not_allowed"),
    ):
        assert (await _refused(engine, w.story_id, part)).details["reason"] == reason, part
    async with engine.connect() as conn:
        for table in ("state_changes", "scheduled_events"):
            check = await conn.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                    "CAST(:t AS regclass) AND strpos(conname, 'positioned') > 0"
                ),
                {"t": table},
            )
            assert "amendment_part_id IS NOT NULL" in str(check), table


@pytest.mark.integration
async def test_amendment_in_place_state_change(pg_url: str, engine: AsyncEngine) -> None:
    """An ``in_place`` part on a committed change of a story-owned State applies: Bobby's gold
    set to 30 then spent by 20 (10) becomes a set to 25 (5 now); ``state_values`` is rebuilt and
    the later row is not rewritten. A correction that breaks the State's constraint at a later
    active value (a set to 15: -5 after the spend) is 422 ``invalid_amendment_part`` reason
    ``state_constraint``, nothing written."""
    from tests import effect_support as es
    from tests import state_support as ss
    from tests.auth_support import auth_settings
    from tests.respond_support import respond_client

    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, _):
        story, ids = await es.shop(client, engine, ss.gold(), names=("Cal", "Jenni", "Bobby"))
    bobby = ids["Bobby"]
    await ss.change(engine, story, ss.set_to("gold", 30, scope_key=bobby, reason="a purse"))
    await ss.change(engine, story, ss.delta("gold", -20, scope_key=bobby, reason="a sword"))
    assert await ss.value(engine, story, "gold", bobby) == 10
    found = [r for r in await ss.change_rows(engine, story) if r.scope_key == bobby]
    first, later = found[-2], found[-1]
    await am.amend(
        engine, story, [RecordPartDraft("state_changes", first.id, "replace", {"value": 25})]
    )
    assert await ss.value(engine, story, "gold", bobby) == 5
    assert await ss.stored_values(engine, story) == await ss.folded_values(engine, story)
    (unchanged,) = [r for r in await ss.change_rows(engine, story) if r.id == later.id]
    assert unchanged.new_value == later.new_value
    refused = await _refused(
        engine, story, RecordPartDraft("state_changes", first.id, "replace", {"value": 15})
    )
    assert refused.details["reason"] == "state_constraint"
    assert await ss.value(engine, story, "gold", bobby) == 5


@pytest.mark.integration
async def test_amendment_fork(engine: AsyncEngine) -> None:
    """A fork copies the amendments of the operations it copies, with new ids: the part names
    the fork's amendment and the fork's copy of the corrected reply; the source's rows are
    unchanged."""
    from tests import fork_support as fs

    w = await c.world(engine)
    replies = await am.play(engine, w.story_id, 2)
    await am.amend(
        engine, w.story_id, [RecordPartDraft("events", replies[1], "replace", {"text": "x"})]
    )
    source = await am.table_rows(engine, "amendment_parts", w.story_id)
    forked = await fs.fork(engine, w.story_id)
    (amendment,) = await am.table_rows(engine, "amendments", forked.story_id)
    (part,) = await am.table_rows(engine, "amendment_parts", forked.story_id)
    assert amendment["id"] != source[0]["amendment_id"]
    assert part["amendment_id"] == amendment["id"]
    copied = await scalar(
        engine,
        "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered' "
        "AND payload->>'text' = 'reply 1'",
        s=forked.story_id,
    )
    assert part["record_id"] == copied != replies[1]
    assert part["patch_after"] == source[0]["patch_after"]
    assert await am.table_rows(engine, "amendment_parts", w.story_id) == source


@pytest.mark.integration
async def test_amendment_erasure(pg_url: str, engine: AsyncEngine) -> None:
    """A story-scope erasure redacts an amendment's patch that names the subject (both tables
    leave no residue); afterwards a new amendment's parts go through the resurrection guard
    (``check_content``, a direct write path), which accepts the author typing her name in it
    (an author's text after the erasure is not refused, #127)."""
    from epistrel.core.erasure import guard
    from tests import erasure_support as e
    from tests.respond_support import respond_client

    async with respond_client(e.erasure_settings(pg_url), engine) as (client, lib):
        story = await e.played_story(client, lib, engine)
        sid = story.story_id
        replies = [
            str(r.id)
            for r in await rows(
                engine,
                "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered' "
                "ORDER BY id",
                s=sid,
            )
        ]
        named = {"text": f"{e.NAME} waved from the gate."}
        await am.amend(engine, sid, [RecordPartDraft("events", replies[0], "replace", named)])
        assert (await e.residue(engine, sid, (e.NAME,)))["amendment_parts"] == 1
        await e.erase(client, "character", story.mother, sid, confirm_all=True)
    left = await e.residue(engine, sid, (e.NAME,))
    assert (left["amendments"], left["amendment_parts"]) == (0, 0)
    with guard.recording() as calls:
        await am.amend(engine, sid, [RecordPartDraft("events", replies[-1], "replace", named)])
    assert "epistrel.core.amendments.store" in calls


@pytest.mark.integration
async def test_amendment_state_change_refusals(engine: AsyncEngine, states_delivered: None) -> None:
    """An ``in_place`` part on a ``state_changes`` row that is not a committed, unconditional
    change of a story-owned, non-derived State is refused with its reason, nothing written: a
    ``held`` reservation (``contingent_change``), a settlement row that waited on an external
    effect (``external_effect``), a rule's own row (an elapsed tick: ``derived_state``) and a
    ``world_clock`` advance (``clock_advance``)."""
    from epistrel.core.enums import OperationKind, StateChangeStatus, StateContingency
    from epistrel.core.ids import new_ulid
    from epistrel.core.membership import Position
    from epistrel.core.states import contingency as ct
    from tests import clock_support as clock
    from tests import state_support as ss
    from tests.memory_support import use_structured

    w = await c.world(engine)
    story, cal = w.story_id, w.characters["Cal"]
    await use_structured(engine, story)
    await ss.set_level(engine, story, "on")
    await clock.set_level(engine, story, "on")
    await ss.define_all(
        engine,
        story,
        ss.gold(authorization={"sources": ["any_character"]}),
        ss.gold(
            name="hunger",
            scope="story",
            constraints={},
            rules={"kind": "elapsed", "per_minutes": 10, "delta": 1},
            authorization={"sources": ["rule", "author"]},
        ),
    )
    await ss.change(engine, story, ss.set_to("gold", 100, scope_key=cal))
    payment = new_ulid()
    await ss.change(
        engine,
        story,
        ss.delta(
            "gold",
            -5,
            scope_key=cal,
            contingency=StateContingency.RESERVATION,
            status=StateChangeStatus.HELD,
            prerequisite_effect_ids=(new_ulid(),),
        ),
    )
    pending_op, _ = await ss.change(
        engine,
        story,
        ss.delta(
            "gold",
            7,
            scope_key=cal,
            contingency=StateContingency.CONDITIONAL,
            status=StateChangeStatus.PENDING,
            prerequisite_effect_ids=(payment,),
        ),
    )

    async def settle(conn: Any, op: Any) -> Any:
        return await ct.settle(
            conn, op, {payment: "confirmed"}, Position(pending_op.narrative_turn or 0, 0, 1)
        )

    await c.run(engine, story, settle, kind=OperationKind.CONTROL, turn=pending_op.narrative_turn)
    await clock.advance(engine, story, 30)
    found = await rows(
        engine,
        "SELECT c.id, c.status, c.source, d.name FROM state_changes c "
        "JOIN state_definitions d ON d.id = c.state_id WHERE c.story_id = :s ORDER BY c.id",
        s=story,
    )
    held = next(r for r in found if r.status == "held")
    settled = next(r for r in found if r.source == "settlement")
    derived = next(r for r in found if r.name == "hunger" and r.source == "rule")
    advance = next(r for r in found if r.name == "world_clock")
    for row, reason, patch in (
        (held, "contingent_change", {"delta": -4}),
        (settled, "external_effect", {"delta": 6}),
        (derived, "derived_state", {"value": 3}),
        (advance, "clock_advance", {"delta": 10}),
    ):
        part = RecordPartDraft("state_changes", str(row.id), "replace", patch)
        assert (await _refused(engine, story, part)).details["reason"] == reason, row.name
