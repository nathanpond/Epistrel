"""S-AMEND-SURVIVES: amendments survive truncation (docs/04 §7; FR-STORE-21, FR-REWRITE-9;
#221).

A story played to turn 1,000; an amendment recorded after it corrects a pre-800 fact (Bobby was
at the garden, not the school, from the start) and a line of message 900 (an overlay on that
reply). Then the head is regenerated, and the story is truncated to 800: the fact correction is
still in force and the amendment's rows are untouched; the line correction is in no corrected
view, with message 900; the regeneration is in no view. Untruncating brings message 900 back
with its correction unchanged. Reverting is a separate operation: it restores the
pre-amendment values while the amendment row stays, ``reverted``."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.amendments.store import PropositionPartDraft, RecordPartDraft
from epistrel.core.canon import facts as canon
from epistrel.core.views import corrected_history
from tests import amendment_support as am
from tests import canon_support as c
from tests import projection_support as projections
from tests.concurrency_support import regenerate
from tests.event_support import rows

pytestmark = pytest.mark.integration

CORRECTED = "The ferryman nods, and the fare is paid."


async def _where(engine: AsyncEngine, story_id: str, bobby: str) -> list[str]:
    async with engine.connect() as conn:
        found = await canon.canon(conn, story_id, subject=bobby, predicate=c.LOCATED_AT)
    return [str(f.proposition.object_id) for f in found]


async def _reply(engine: AsyncEngine, story_id: str, event_id: str) -> str | None:
    found = [e for e in await corrected_history(engine, story_id) if e.id == event_id]
    return None if not found else str(getattr(found[0].payload, "text", None))


async def _transcript(engine: AsyncEngine, story_id: str, event_id: str) -> list[tuple[str, str]]:
    return [
        (r.text, r.membership)
        for r in await rows(
            engine,
            "SELECT text, membership::text AS membership FROM transcript_messages "
            "WHERE message_id = :m",
            m=event_id,
        )
    ]


@pytest.mark.part("M7", "FR-STORE-21; FR-REWRITE-9")
async def test_s_amend_survives(engine: AsyncEngine) -> None:
    """See the module docstring."""
    w = await c.world(engine)
    bobby = w.people["Bobby"]
    await c.commit(engine, w.story_id, c.at(bobby, w.places["school"], None, None))
    replies = await am.play(engine, w.story_id, 999, start=2)
    assert max(replies) == 1000
    line = replies[900]

    _, made, _ = await am.amend(
        engine,
        w.story_id,
        [
            PropositionPartDraft(c.at(bobby, w.places["garden"], None, None)),
            RecordPartDraft("events", line, "replace", {"text": CORRECTED}),
        ],
    )
    assert await _where(engine, w.story_id, bobby) == [w.places["garden"]]
    assert await _reply(engine, w.story_id, line) == CORRECTED
    assert await _transcript(engine, w.story_id, line) == [(CORRECTED, "active")]
    before = {
        t: await am.table_rows(engine, t, w.story_id) for t in ("amendments", "amendment_parts")
    }
    written = [
        r for r in await am.table_rows(engine, "facts", w.story_id) if r["amendment_part_id"]
    ]
    assert len(made.part_ids) == 2

    regenerated = await regenerate(engine, w.story_id)
    assert await projections.truncate(engine, w.story_id, 800) is not None

    assert await _where(engine, w.story_id, bobby) == [w.places["garden"]]
    after = {t: await am.table_rows(engine, t, w.story_id) for t in before}
    assert after == before
    assert [
        r for r in await am.table_rows(engine, "facts", w.story_id) if r["amendment_part_id"]
    ] == written
    assert await _reply(engine, w.story_id, line) is None
    assert await _transcript(engine, w.story_id, line) == [(CORRECTED, "discarded")]
    history = {e.operation_id for e in await corrected_history(engine, w.story_id)}
    assert regenerated.operation_id not in history

    assert await projections.untruncate(engine, w.story_id) is not None
    assert await _reply(engine, w.story_id, line) == CORRECTED
    assert await _transcript(engine, w.story_id, line) == [(CORRECTED, "active")]

    # Reverting is a separate operation.
    _, undo, inverse = await am.amend(engine, w.story_id, [], kind="undo", revert=made.amendment_id)
    assert len(inverse) == 2
    assert await _where(engine, w.story_id, bobby) == [w.places["school"]]
    assert await _reply(engine, w.story_id, line) == "reply 900"
    assert await _transcript(engine, w.story_id, line) == [("reply 900", "active")]
    (head,) = [
        r
        for r in await am.table_rows(engine, "amendments", w.story_id)
        if r["id"] == made.amendment_id
    ]
    assert (head["status"], head["reverted_by"]) == ("reverted", undo.amendment_id)


@pytest.mark.part("M7", "FR-STORE-21; FR-REWRITE-9")
async def test_s_amend_survives__directed(
    pg_url: str, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """#241: a directed regeneration is an ordinary narrative event. The S-DIRECT-GEORGE house:
    the author directs Bobby as the knocker against his recorded "I never knocked", applied
    best-effort (a ``directed_resolution`` amendment and the regenerated turn 3 commit
    together). Truncating to turn 1 discards the regenerated variant (Bobby as the knocker is
    in no view), while the ``directed_resolution`` amendment stays ``active``, its rows
    untouched."""
    from tests import rewrite_support as rs
    from tests.auth_support import auth_settings
    from tests.respond_support import body, played, respond_client
    from tests.scenario.test_s_direct_george import BOBBY, NEVER, _parsed, _regenerate
    from tests.scenario.test_s_knock import _house, _knock
    from tests.structured_turn_support import beat, claim, render

    settings = auth_settings(pg_url, model_adapter="stub")
    async with respond_client(settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story, ids = await _house(client, engine)
        await _knock(client, engine, stub, story, ids)
        stub.reset()
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        NEVER,
                        intent="sincere",
                        propositions=[claim("I", "knocked_at", "the door", negated=True)],
                    )
                )
            ],
        )
        await played(
            client,
            story,
            body(ids["Wren"], "Bobby, did you knock?", render_character_ids=[ids["Bobby"]]),
        )
        stub.reset()
        stub.script("generation", [render(beat("narration", "Nobody answers."))])
        await played(client, story, body(ids["Wren"], "Hello?"))
        stub.reset()
        stub.when("", _parsed("Bobby"), purpose="directive_parse")
        stub.when("", f'{{"text": "{rs.REWRITTEN}"}}', purpose="rewrite_record")
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "do",
                        "Jenni opens the door: Bobby stands there, knuckles still raised.",
                        propositions=[claim("Bobby", "knocked_at", "the door")],
                    )
                )
            ],
        )
        applied = await _regenerate(
            client,
            story,
            directive={"text": BOBBY},
            resolution_mode="best_effort",
            preview="apply_directly",
        )
        assert applied.status_code == 200, applied.text
        assert await _affirmed(engine, story, ids)
        before = await am.table_rows(engine, "amendments", story)
        assert [r["kind"] for r in before] == ["directed_resolution"]

    assert await projections.truncate(engine, story, 1) is not None
    affirmed = await _affirmed(engine, story, ids)
    assert affirmed == []
    assert await am.table_rows(engine, "amendments", story) == before


async def _affirmed(engine: AsyncEngine, story: str, ids: dict[str, str]) -> list[object]:
    """The active facts that Bobby knocked."""
    return list(
        await rows(
            engine,
            "SELECT f.id FROM facts f JOIN propositions p ON p.id = f.proposition_id "
            "WHERE f.story_id = :s AND f.predicate_id = :k AND p.subject_id = :b "
            "AND p.polarity = 'affirmed' AND f.membership = 'active'",
            s=story,
            k=ids["knocked_at"],
            b=ids["Bobby"],
        )
    )
