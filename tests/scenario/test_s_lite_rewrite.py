"""Lite history rewrites (FR-LITE-8, FR-REWRITE-1,4,6,10; #245): reach is model-searched.

A Lite story's reply says Ada was a farmer. The author corrects the past ("Ada was always a
sailor."): ``rewrite_parse`` yields no change Lite can hold (its proposition is refused,
``lite_no_canon``), and the ``lite_rewrite_reach`` pass finds the reply and fixes it. The
preview lists the one record change; applied, the transcript reads the fix, the revision moves
by one, the original stays in the event log; undone, the transcript reads the original again.
A Lite directive about the past now opens a session instead of a refusal.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import rewrite_support as rs
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import FIRST, respond, respond_client
from tests.respond_support import body as respond_body
from tests.story_support import WRITE, create, story_body

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("drained_embedding_jobs")]

FARMER = "Ada wipes her hands. I was a farmer before all this."
SAILOR = "Ada wipes her hands. I was a sailor before all this."
CORRECTION = "Ada was always a sailor."
PARSED = json.dumps(
    {
        "changes": [
            {"kind": "proposition", "subject": "Ada", "predicate": "occupation", "object": "sailor"}
        ]
    }
)
REACHED = json.dumps(
    {
        "hits": [
            {
                "turn": 1,
                "quote": "I was a farmer",
                "relation": "conflicts",
                "fix_text": SAILOR,
                "reason": "names her old trade",
            }
        ]
    }
)


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _transcript(engine: AsyncEngine, story: str) -> list[str]:
    found = await rows(
        engine,
        "SELECT text FROM transcript_messages WHERE story_id = :s AND membership = 'active' "
        "ORDER BY seq",
        s=story,
    )
    return [str(r.text) for r in found]


async def _lite_story(client: Any, engine: AsyncEngine, stub: Any) -> str:
    story = str((await create(client, story_body("Wren", "Ada")))["story_id"])
    cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
    actor = next(str(c.id) for c in cast if c.name == "Wren")
    stub.when("", FARMER, purpose="generation")
    played = await respond(client, story, respond_body(actor, FIRST))
    assert played.status_code == 200, played.text
    return story


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-1,4,6,10")
async def test_s_lite_rewrite(stub_settings: Settings, engine: AsyncEngine) -> None:
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        assert FARMER in await _transcript(engine, story)
        stub.when("", PARSED, purpose="rewrite_parse")
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        revision = await rs.revision(engine, story)
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        preview = session["preview"]
        assert [c["kind"] for c in preview["changes"]] == ["record"]
        assert preview["changes"][0]["new_text"] == SAILOR
        assert "lite_no_canon" in {r.get("reason") for r in preview["refused_changes"]}
        assert await rs.revision(engine, story) == revision  # opening writes no story truth
        assert FARMER in await _transcript(engine, story)
        if session["state"] != "awaiting_approval":
            answered = await rs.answer_all(client, story, session)
            assert answered.status_code == 200, answered.text
            session = answered.json()
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        assert await rs.revision(engine, story) == revision + 1
        lines = await _transcript(engine, story)
        assert SAILOR in lines
        assert FARMER not in lines
        originals = await rows(
            engine,
            "SELECT payload->>'text' AS body FROM events WHERE story_id = :s "
            "AND type = 'message_rendered'",
            s=story,
        )
        assert FARMER in {r.body for r in originals}  # the log keeps the original

        (amendment,) = await rows(engine, "SELECT id FROM amendments WHERE story_id = :s", s=story)
        undone = await client.post(
            f"/stories/{story}/amendments/{amendment.id}/undo",
            params=rs.OMNISCIENT,
            json={},
            headers=WRITE,
        )
        assert undone.status_code in (200, 201), undone.text
        undo_session = undone.json()
        if undo_session["state"] == "awaiting_answers":
            answered = await rs.answer_all(client, story, undo_session)
            undo_session = answered.json()
        reverted = await rs.apply(client, story, undo_session["id"], undo_session["base_revision"])
        assert reverted.status_code == 200, reverted.text
        assert FARMER in await _transcript(engine, story)


async def test_lite_past_directive_opens_a_rewrite(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """#245 retires #244's ``directive_requires_rewrite``: a Lite directive about the past is
    routed into a Lite rewrite session (202 ``routed_to_rewrite``)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        stub.when(
            "",
            json.dumps({"classification": "durable", "temporal_scope": "past", "changes": []}),
            purpose="directive_parse",
        )
        stub.when("", PARSED, purpose="rewrite_parse")
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        routed = await client.post(
            f"/stories/{story}/directives",
            params=rs.OMNISCIENT,
            json={"text": CORRECTION},
            headers=WRITE,
        )
        assert routed.status_code == 202, routed.text
        body = routed.json()
        assert body["disposition"] == "routed_to_rewrite"
        assert body["rewrite_session"]["kind"] == "directive"


async def _messages(engine: AsyncEngine, story: str) -> list[Any]:
    return await rows(
        engine,
        "SELECT message_id, text, narrative_turn, beat, role FROM transcript_messages "
        "WHERE story_id = :s AND membership = 'active' ORDER BY seq",
        s=story,
    )


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-12")
async def test_s_lite_rewrite__line_edit(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#700: a direct line edit (``line_edit {event_id, new_text}``) plans and applies with
    zero model calls; the preview shows the line before and after; the transcript reads the
    new text and the event log keeps the original; no other line changes. Complements:
    naming a turn of two messages without a beat is 422 ``beat_required`` (listing both); an
    event that is no message (the story's first non-message event) is 422
    ``line_not_editable``; the line's own text is 422 ``rewrite_noop``; an instruction and a
    line edit together are 422."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        before = await _messages(engine, story)
        reply = next(m for m in before if m.text == FARMER)
        stub.reset()

        def post(body: dict[str, Any]) -> Any:
            return client.post(
                f"/stories/{story}/rewrites", params=rs.OMNISCIENT, json=body, headers=WRITE
            )

        opened = await post({"line_edit": {"event_id": str(reply.message_id), "new_text": SAILOR}})
        assert opened.status_code == 201, opened.text
        session = opened.json()
        (change,) = session["preview"]["changes"]
        assert (change["old_text"], change["new_text"]) == (FARMER, SAILOR)
        assert session["instruction"] is None
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        assert stub.calls == []  # no parse, no reach, no gate, no rewrite_record
        after = await _messages(engine, story)
        assert [m.text for m in after] == [SAILOR if m.text == FARMER else m.text for m in before]
        logged = await rows(
            engine,
            "SELECT payload->>'text' AS body FROM events WHERE id = :e",
            e=str(reply.message_id),
        )
        assert [r.body for r in logged] == [FARMER]

        several = await post({"line_edit": {"turn": int(reply.narrative_turn), "new_text": "x"}})
        assert several.status_code == 422, several.text
        error = several.json()["error"]
        assert error["code"] == "beat_required"
        assert {b["event_id"] for b in error["details"]["beats"]} == {
            str(m.message_id) for m in after if m.narrative_turn == reply.narrative_turn
        }
        (committed,) = await rows(
            engine,
            "SELECT id, type FROM events WHERE story_id = :s AND id NOT IN "
            "(SELECT message_id FROM transcript_messages) ORDER BY seq LIMIT 1",
            s=story,
        )
        refused = await post({"line_edit": {"event_id": str(committed.id), "new_text": "x"}})
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "line_not_editable"
        assert refused.json()["error"]["details"]["event_kind"] == committed.type
        same = await post({"line_edit": {"event_id": str(reply.message_id), "new_text": SAILOR}})
        assert same.status_code == 422, same.text
        assert same.json()["error"]["code"] == "rewrite_noop"
        both = await post(
            {
                "instruction": CORRECTION,
                "line_edit": {"event_id": str(reply.message_id), "new_text": "y"},
            }
        )
        assert both.status_code == 422, both.text
        assert stub.calls == []


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-6")
async def test_s_lite_rewrite__note(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#700: the correction states a world change (its proposition has no Lite form), so the
    applied amendment carries one ``lite_note`` part ("Author correction: Ada was always a
    sailor."), and the next Lite prompt lists it under "Author corrections". The preview's
    ``reach`` says the transcript was model-searched, over turn 1, not truncated; the re-check
    call ran (two reach calls). Undone, the note leaves the prompt."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        stub.when("", PARSED, purpose="rewrite_parse")
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert session["preview"]["reach"] == {
            "method": "model_searched",
            "searched": {"from_turn": 1, "to_turn": 1},
            "truncated": False,
        }
        assert [c.purpose for c in stub.calls].count("lite_rewrite_reach") == 2
        if session["state"] != "awaiting_approval":
            session = (await rs.answer_all(client, story, session)).json()
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        notes = await rows(
            engine,
            "SELECT patch_after->>'text' AS body FROM amendment_parts "
            "WHERE story_id = :s AND kind = 'lite_note'",
            s=story,
        )
        assert [n.body for n in notes] == [f"Author correction: {CORRECTION}"]

        def prompt() -> str:
            (call,) = [c for c in stub.calls if c.purpose == "generation"][-1:]
            return "\n".join(m.content for m in call.messages)

        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        played = await respond(client, story, respond_body(actor, "And then?"))
        assert played.status_code == 200, played.text
        assert "Author corrections" in prompt()
        assert f"Author correction: {CORRECTION}" in prompt()

        (amendment,) = await rows(
            engine, "SELECT id FROM amendments WHERE story_id = :s AND kind = 'rewrite'", s=story
        )
        undone = await client.post(
            f"/stories/{story}/amendments/{amendment.id}/undo",
            params=rs.OMNISCIENT,
            json={},
            headers=WRITE,
        )
        assert undone.status_code in (200, 201), undone.text
        undo_session = undone.json()
        if undo_session["state"] == "awaiting_answers":
            undo_session = (await rs.answer_all(client, story, undo_session)).json()
        reverted = await rs.apply(client, story, undo_session["id"], undo_session["base_revision"])
        assert reverted.status_code == 200, reverted.text
        played = await respond(client, story, respond_body(actor, "Go on."))
        assert played.status_code == 200, played.text
        assert "Author corrections" not in prompt()


@pytest.mark.part("M7", "FR-LITE-8")
async def test_s_lite_rewrite__reach_truncated(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#700: the reach searches in chunks within its call cap; with the cap lowered to two
    calls (one chunk and the re-check) and a chunk of one line, a story of two turns (four
    lines) is searched over its newest line only: ``reach.truncated`` is true and an
    ``apply_directly`` request previews first."""
    from epistrel.orchestrator.rewrite import lite

    monkeypatch.setattr(lite, "MAX_CALLS", 2)
    monkeypatch.setattr(lite, "CHUNK_LINES", 1)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        played = await respond(client, story, respond_body(actor, "And then?"))
        assert played.status_code == 200, played.text
        stub.when("", PARSED, purpose="rewrite_parse")
        stub.when("", json.dumps({"hits": []}), purpose="lite_rewrite_reach")
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION, "preview": "apply_directly"},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert session["preview"]["reach"] == {
            "method": "model_searched",
            "searched": {"from_turn": 2, "to_turn": 2},
            "truncated": True,
        }
        assert session["preview_mode"] == "preview_first"
        assert session["state"] != "applied"


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-6; FR-STORE-26")
async def test_s_lite_rewrite__summary_rebuild(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """#700: a Lite story with a one-turn summary grid and a two-row window plays three turns;
    the summary job writes segments over turns 1 and 2. A line edit of turn 1's reply makes the
    segment over turn 1 stale: the apply schedules the summary job, and until it runs each
    reply reports ``degradation: ["summary_rebuilding"]`` (cause ``corrected_lines``). The run
    rebuilds that segment from the corrected line (its prompt reads the fix), naming the old
    one's operation among its sources: the old one is ``superseded`` and out of the prompt, the
    segment over turn 2 keeps its id, and the next reply reports no degradation."""
    from tests.event_support import execute_in_tx
    from tests.profile_support import WINDOW, nest, store_overrides
    from tests.summary_support import run_due_jobs, segment_rows

    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = str((await create(client, story_body("Wren", "Ada")))["story_id"])
        await execute_in_tx(
            engine,
            (
                "UPDATE stories SET settings = settings || CAST(:p AS jsonb) WHERE id = :s",
                {"s": story, "p": json.dumps({"lite_summary_chunk": 2})},
            ),
        )
        await store_overrides(engine, story, nest(WINDOW, 2))
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        stub.when("", FARMER, purpose="generation")
        for line in ("One.", "Two.", "Three."):
            played = await respond(client, story, respond_body(actor, line))
            assert played.status_code == 200, played.text
        await run_due_jobs(engine, epistrel.models, story)
        before = {(r.covers_from, r.covers_to): r for r in await segment_rows(engine, story)}
        assert {(1, 1), (2, 2)} <= set(before)

        first = next(m for m in await _messages(engine, story) if m.narrative_turn == 1)
        reply = next(
            m
            for m in await _messages(engine, story)
            if m.narrative_turn == 1 and m.message_id != first.message_id
        )
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"line_edit": {"event_id": str(reply.message_id), "new_text": SAILOR}},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        queued = await rows(
            engine,
            "SELECT id FROM jobs WHERE story_id = :s AND kind = 'lite_summary' "
            "AND status = 'queued'",
            s=story,
        )
        assert queued
        played = await respond(client, story, respond_body(actor, "Four."))
        assert played.status_code == 200, played.text
        assert "summary_rebuilding" in played.json()["degradation"]
        assert {"step": "summary_rebuilding", "cause": "corrected_lines"} in [
            {k: d[k] for k in ("step", "cause")} for d in played.json()["degradation_details"]
        ]

        stub.reset()
        await run_due_jobs(engine, epistrel.models, story)
        summaries = [c for c in stub.calls if c.purpose == "lite_summary"]
        assert any(SAILOR in m.content for c in summaries for m in c.messages)
        after = await segment_rows(engine, story)
        old = before[(1, 1)]
        (rebuilt,) = [
            r
            for r in after
            if (r.covers_from, r.covers_to) == (1, 1) and r.segment_id != old.segment_id
        ]
        assert str(old.operation_id) in {s["operation_id"] for s in rebuilt.source_versions}
        assert {r.segment_id: r.membership for r in after}[old.segment_id] == "superseded"
        assert before[(2, 2)].segment_id in {r.segment_id for r in after}
        again = await respond(client, story, respond_body(actor, "Five."))
        assert again.status_code == 200, again.text
        assert "summary_rebuilding" not in again.json().get("degradation", [])
