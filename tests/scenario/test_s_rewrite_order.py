"""S-REWRITE-ORDER (docs/04 §7; FR-REWRITE-9, FR-STORE-15,20,22, FR-BITEMP-5; #239): an
amendment applies by position, not by when it was recorded.

A structured story played to turn 10 by direct commits (``tests.amendment_support``; the
docs' 1,000 turns scaled to 10, fork point 8). After turn 10 an amendment corrects Bobby's
childhood (where he lived, valid-time 0-100), introduces a childhood fact about Ann ("Bobby had a
sister" in the docs), and repairs the reply of turn 9. A fork at turn 8:

- has the corrected childhood and the introduced fact (the amendment has no position: it is
  copied, and its facts apply at their valid-time);
- has none of the amendment's repairs to records after 8 (a record part goes with its record);
- complement: the amendment was recorded after every event of turns 1-8 (its event's ``seq``
  is later), so a plain fold of the source's events by ``seq`` through turn 8 would lack it:
  position, not recording order, decides.

Variant: a second amendment whose rationale cites the reply of turn 9 is surfaced by the
fork's support check (``support``, ``keep`` recommended). A later amendment on either story
leaves the other's amendments unchanged (row diff).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.amendments.store import PropositionPartDraft, RecordPartDraft
from epistrel.core.canon import facts as canon
from epistrel.orchestrator.rewrite import support_check
from tests import amendment_support as am
from tests import canon_support as c
from tests.event_support import rows
from tests.fork_support import fork
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration

FORK_AT = 8
TABLES = ("amendments", "amendment_parts")


@pytest.mark.part("M7", "FR-REWRITE-9; FR-STORE-15,20,22; FR-BITEMP-5")
async def test_s_rewrite_order(engine: AsyncEngine) -> None:
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    bobby, ann = w.people["Bobby"], w.people["Ann"]
    await c.commit(engine, story, c.at(bobby, w.places["school"], 0, 100))
    replies = await am.play(engine, story, 9, start=2)
    _, made, _ = await am.amend(
        engine,
        story,
        [
            PropositionPartDraft(c.at(bobby, w.places["garden"], 0, 100)),
            PropositionPartDraft(c.at(ann, w.places["garden"], 0, 100)),
            RecordPartDraft("events", replies[9], "replace", {"text": "reply 9, fixed"}),
        ],
    )
    _, cited, _ = await am.amend(
        engine,
        story,
        [PropositionPartDraft(c.at(ann, w.places["school"], 200, 300))],
        rationale=[{"table": "events", "id": replies[9]}],
    )
    before = {t: await am.table_rows(engine, t, story) for t in TABLES}

    forked = await fork(engine, story, FORK_AT)
    child = forked.story_id
    # The fork's entities are its own (the copier remaps every id): read them by name.
    named = {
        r.canonical_name: str(r.id)
        for r in await rows(
            engine, "SELECT id, canonical_name FROM entities WHERE story_id = :s", s=child
        )
    }
    async with engine.connect() as conn:
        held = {
            name: [
                named_of
                for f in await canon.canon(
                    conn, child, story_time=50, subject=named[name], predicate=c.LOCATED_AT
                )
                for named_of, eid in named.items()
                if eid == f.proposition.object_id
            ]
            for name in ("Bobby", "Ann")
        }
    assert held == {"Bobby": ["garden"], "Ann": ["garden"]}
    copied = await rows(
        engine,
        "SELECT kind, record_id FROM amendment_parts WHERE story_id = :s",
        s=child,
    )
    assert {r.kind for r in copied} == {"proposition"}
    assert not [r for r in copied if r.record_id is not None]
    assert len(await rows(engine, "SELECT id FROM amendments WHERE story_id = :s", s=child)) == 2

    # Complement: recorded after every event through turn 8 of the source.
    (recorded,) = await rows(
        engine,
        "SELECT e.seq FROM events e JOIN amendments a ON a.operation_id = e.operation_id "
        "WHERE a.id = :a AND e.type = 'amendment_recorded'",
        a=made.amendment_id,
    )
    (through,) = await rows(
        engine,
        "SELECT max(seq) AS seq FROM events WHERE story_id = :s AND narrative_turn <= :n",
        s=story,
        n=FORK_AT,
    )
    assert recorded.seq > through.seq

    # Variant: the fork's support check surfaces the amendment citing turn 9's reply.
    async with engine.connect() as conn:
        pending = await support_check.pending(conn, child)
    assert pending is not None
    asked = await rows(
        engine,
        "SELECT options FROM rewrite_questions WHERE session_id = :i AND superseded_by IS NULL",
        i=pending["session_id"],
    )
    about = {o["effect"]["amendment_id"] for q in asked for o in q.options}
    copied_cited = await rows(
        engine,
        "SELECT id FROM amendments WHERE story_id = :s AND rationale_record_ids <> '[]'",
        s=child,
    )
    assert about == {str(r.id) for r in copied_cited}
    assert cited.amendment_id not in about  # the fork's copy, under its own id

    # Later amendments on either story leave the other's unchanged.
    child_before = {t: await am.table_rows(engine, t, child) for t in TABLES}
    await am.amend(
        engine, child, [PropositionPartDraft(c.at(named["Bobby"], named["school"], 0, 50))]
    )
    assert {t: await am.table_rows(engine, t, story) for t in TABLES} == before
    child_after = {t: await am.table_rows(engine, t, child) for t in TABLES}
    await am.amend(engine, story, [PropositionPartDraft(c.at(ann, w.places["school"], 0, 50))])
    assert {t: await am.table_rows(engine, t, child) for t in TABLES} == child_after
    assert child_after != child_before
