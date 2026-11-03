"""S-AMEND-ATTACH (docs/04 §7; FR-REWRITE-12, FR-STORE-29; #239): a record correction follows
its record, and a fix never jumps to another version of a line without the author.

A structured story played by direct commits (``tests.amendment_support``): Bobby at school,
turns 2-7. One amendment moves Bobby to the garden (a world-proposition correction) and fixes
the reply of turn 6 (a record part). Truncate to 4 and write 5'-7' (the same reply texts), then
swap back and forth:

- the garden correction applies in both continuations (it has no position) and is re-checked
  against each newly active chain by the support check;
- the line fix is in the corrected history only while the original turn 6 is active;
- with the primed chain active, the support check suggests porting the fix to 6' (a ``port``
  question, ``port`` recommended: 6' holds the replaced text exactly). Until the author answers,
  6' carries no record part, after every swap; approved, exactly one, written by a new
  amendment that names the part it ports.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import timeline
from epistrel.core.amendments.store import PropositionPartDraft, RecordPartDraft
from epistrel.core.canon import facts as canon
from epistrel.core.variants import ChainRef, SwapRequest
from epistrel.core.views import corrected_history
from epistrel.orchestrator.rewrite import support_check
from epistrel.orchestrator.rewrite.api import AnswerRequest
from tests import amendment_support as am
from tests import canon_support as c
from tests import projection_support
from tests.event_support import rows
from tests.memory_support import use_structured

pytestmark = pytest.mark.integration


async def _text(engine: AsyncEngine, story: str, event_id: str) -> str | None:
    found = [e for e in await corrected_history(engine, story) if e.id == event_id]
    return None if not found else str(found[0].payload.model_dump()["text"])


async def _parts_on(engine: AsyncEngine, event_id: str) -> int:
    return len(
        await rows(
            engine,
            "SELECT id FROM amendment_parts WHERE record_id = :r AND status = 'active'",
            r=event_id,
        )
    )


async def _garden(engine: AsyncEngine, story: str, w: Any) -> bool:
    async with engine.connect() as conn:
        found = await canon.canon(conn, story, subject=w.people["Bobby"], predicate=c.LOCATED_AT)
    return [f.proposition.object_id for f in found] == [w.places["garden"]]


@pytest.mark.part("M7", "FR-REWRITE-12; FR-STORE-29")
async def test_s_amend_attach(engine: AsyncEngine) -> None:
    w = await c.world(engine)
    story = w.story_id
    await use_structured(engine, story)
    bobby = w.people["Bobby"]
    await c.commit(engine, story, c.at(bobby, w.places["school"], None, None))
    replies = await am.play(engine, story, 6, start=2)
    _, made, _ = await am.amend(
        engine,
        story,
        [
            PropositionPartDraft(c.at(bobby, w.places["garden"], None, None)),
            RecordPartDraft("events", replies[6], "replace", {"text": "reply 6, fixed"}),
        ],
    )
    assert await _text(engine, story, replies[6]) == "reply 6, fixed"

    assert await projection_support.truncate(engine, story, 4) is not None
    primes = await am.play(engine, story, 3, start=5)
    assert await _garden(engine, story, w)
    assert await _parts_on(engine, primes[6]) == 0

    # Back to the original chain: the fix is there again, nothing to port.
    swapped = await projection_support.untruncate(engine, story)
    assert swapped is not None
    assert await _text(engine, story, replies[6]) == "reply 6, fixed"
    assert await _garden(engine, story, w)
    assert await _parts_on(engine, primes[6]) == 0
    async with engine.connect() as conn:
        assert await support_check.pending(conn, story) is None

    # The primed chain again: the fix goes dormant with its line; a port is suggested.
    await timeline.swap(
        engine,
        story,
        SwapRequest(to_variant_chain=ChainRef(turn=5, variant=2)),
        principal="p-author",
    )
    assert await _text(engine, story, replies[6]) is None
    assert await _text(engine, story, primes[6]) == "reply 6"
    assert await _garden(engine, story, w)
    assert await _parts_on(engine, primes[6]) == 0
    async with engine.connect() as conn:
        pending = await support_check.pending(conn, story)
    assert pending is not None
    (question,) = await rows(
        engine,
        "SELECT id, kind, options, recommended_option_id FROM rewrite_questions "
        "WHERE session_id = :i AND superseded_by IS NULL AND answered_at IS NULL",
        i=pending["session_id"],
    )
    options = {o["id"]: o["effect"] for o in question.options}
    assert question.kind == "port"
    recommended = options[question.recommended_option_id]
    assert (recommended["action"], recommended["record"]["id"]) == ("port", primes[6])
    assert {e["action"] for e in options.values()} == {"port", "skip"}

    answered = await support_check.answer(
        engine,
        None,
        story,
        pending["session_id"],
        AnswerRequest.model_validate(
            {"answers": [{"question_id": str(question.id), "accept_recommended": True}]}
        ),
        principal="p-author",
    )
    assert answered.session.state == "applied"
    assert await _parts_on(engine, primes[6]) == 1
    assert await _text(engine, story, primes[6]) == "reply 6, fixed"
    (ported,) = await rows(
        engine,
        "SELECT a.payload FROM amendments a JOIN amendment_parts p ON p.amendment_id = a.id "
        "WHERE p.record_id = :r",
        r=primes[6],
    )
    assert ported.payload["ported_from"] in {
        str(r.id)
        for r in await rows(
            engine, "SELECT id FROM amendment_parts WHERE amendment_id = :a", a=made.amendment_id
        )
    }
