"""Lite history rewrites (FR-LITE-8, FR-REWRITE-1,4,6,10; #245): reach is model-searched.

A Lite story's reply says Ada was a farmer. The author corrects the past ("Ada was always a
sailor."): ``lite_rewrite_parse`` reads it as a change statement that changes the world
(#700), and the ``lite_rewrite_reach`` pass searches with it, finds the reply and fixes it. The
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
PARSED = json.dumps({"change_statement": CORRECTION, "world_change": True})
"""#700: ``lite_rewrite_parse``'s answer: the correction as its change statement, a world
change (so the amendment carries a ``lite_note``)."""
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
        stub.when("", PARSED, purpose="lite_rewrite_parse")
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
        purposes = [c.purpose for c in stub.calls]
        assert purposes.count("lite_rewrite_parse") == 1
        assert "rewrite_parse" not in purposes  # the structured parse never runs for Lite
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
        stub.when("", PARSED, purpose="lite_rewrite_parse")
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
    """#700: the correction states a world change (``lite_rewrite_parse``'s ``world_change``), so
    the applied amendment carries one ``lite_note`` part ("Author correction: Ada was always a
    sailor."), and the next Lite prompt lists it under "Author corrections". The preview's
    ``reach`` says the transcript was model-searched, over turn 1, not truncated; the re-check
    call ran (two reach calls). Undone, the note leaves the prompt."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        stub.when("", PARSED, purpose="lite_rewrite_parse")
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
    """#700: the reach searches in chunks within its call cap; with the deployment's
    ``lite_reach_max_calls`` lowered to two (one chunk and the re-check) and
    ``lite_reach_chunk_messages`` to one line, a story of two turns (four
    lines) is searched over its newest line only: ``reach.truncated`` is true and an
    ``apply_directly`` request previews first."""
    from epistrel.core.settings_schema import RewriteSettings
    from epistrel.orchestrator.rewrite import lite

    real = lite.reach
    capped = RewriteSettings(lite_reach_max_calls=2, lite_reach_chunk_messages=1)

    async def reach(*args: Any, **kwargs: Any) -> Any:
        return await real(args[0], args[1], args[2], args[3], capped, **kwargs)

    monkeypatch.setattr(lite, "reach", reach)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        played = await respond(client, story, respond_body(actor, "And then?"))
        assert played.status_code == 200, played.text
        stub.when("", PARSED, purpose="lite_rewrite_parse")
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


ASKS = json.dumps(
    {
        "change_statement": "",
        "world_change": True,
        "ambiguities": [
            {
                "about": "which trade",
                "question": "Was Ada a sailor or a fisher?",
                "options": ["sailor", "fisher"],
                "recommended": 0,
            }
        ],
    }
)


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-1,4")
async def test_s_lite_rewrite__clarification(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#700: ``lite_rewrite_parse`` reads the instruction first. An ambiguity is a
    ``clarification`` question with a recommended option, and the reach waits for the answer
    (no ``lite_rewrite_reach`` call yet, no change planned). Answered, the instruction is read
    again with the answer (a second parse call that carries it), the reach searches with the
    change statement, and the preview plans the fix."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        stub.reset()
        stub.script("lite_rewrite_parse", [ASKS, PARSED])
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": "Ada had another trade."},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert session["state"] == "awaiting_answers"
        (question,) = session["questions"]
        assert question["kind"] == "clarification"
        assert question["recommended_option_id"] in {o["id"] for o in question["options"]}
        assert [c.purpose for c in stub.calls].count("lite_rewrite_reach") == 0
        assert session["preview"] is None or session["preview"]["changes"] == []

        answered = await rs.answer_all(client, story, session)
        assert answered.status_code == 200, answered.text
        session = answered.json()
        parses = [c for c in stub.calls if c.purpose == "lite_rewrite_parse"]
        assert len(parses) == 2
        assert "Was Ada a sailor or a fisher? sailor" in "\n".join(
            m.content for m in parses[1].messages
        )
        assert [c.purpose for c in stub.calls].count("lite_rewrite_reach") == 2  # + re-check
        assert [c["new_text"] for c in session["preview"]["changes"]] == [SAILOR]
        assert session["preview"]["reach"]["method"] == "model_searched"


MODES: tuple[tuple[Any, str], ...] = tuple(
    (mode, preview)
    for mode in ("synchronous", "best_effort", {"threshold": "high"})
    for preview in ("preview_first", "apply_directly")
)


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-4")
async def test_s_lite_rewrite__six_modes(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#700 (FR-REWRITE-5): detection is identical across the six ``resolution_mode`` x
    ``preview`` combinations: one ``plan_hash`` and one preview of changes. The apply is held
    back (``_apply_directly`` answers the opened session) so every combination plans against
    the same story; each session is cancelled before the next opens."""
    from epistrel.orchestrator.rewrite import session as rewrite_session

    async def held(_db: Any, _models: Any, _story: str, written: Any, **_: Any) -> Any:
        return written

    monkeypatch.setattr(rewrite_session, "_apply_directly", held)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        stub.when("", PARSED, purpose="lite_rewrite_parse")
        stub.when("", REACHED, purpose="lite_rewrite_reach")
        hashes: set[str] = set()
        changes: list[Any] = []
        for mode, preview in MODES:
            opened = await client.post(
                f"/stories/{story}/rewrites",
                params=rs.OMNISCIENT,
                json={"instruction": CORRECTION, "resolution_mode": mode, "preview": preview},
                headers=WRITE,
            )
            assert opened.status_code == 201, (mode, preview, opened.text)
            session = opened.json()
            (found,) = await rows(
                engine, "SELECT plan_hash FROM rewrite_sessions WHERE id = :i", i=session["id"]
            )
            hashes.add(str(found.plan_hash))
            changes.append(session["preview"]["changes"])
            cancelled = await client.post(
                f"/stories/{story}/rewrites/{session['id']}/cancel",
                params=rs.OMNISCIENT,
                headers=WRITE,
            )
            assert cancelled.status_code == 200, cancelled.text
        assert len(hashes) == 1, hashes
        assert all(c == changes[0] for c in changes)
        assert await rs.count(engine, "amendments", story) == 0


@pytest.mark.part("M7", "FR-LITE-8")
async def test_s_lite_rewrite__parallel_reach(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#700: the reach searches ``lite_reach_parallelism`` chunks at once. With chunks of one
    line and a parallelism of three, a story of four lines is searched in two waves (three
    chunks, then one), every line read once, within the call cap; the stub's latency shows the
    waves overlap (the first wave's three calls are in flight together)."""
    import asyncio

    from epistrel.core.settings_schema import RewriteSettings
    from epistrel.orchestrator.rewrite import lite

    real = lite.reach
    settings = RewriteSettings(lite_reach_chunk_messages=1, lite_reach_parallelism=3)

    async def reach(*args: Any, **kwargs: Any) -> Any:
        return await real(args[0], args[1], args[2], args[3], settings, **kwargs)

    monkeypatch.setattr(lite, "reach", reach)
    in_flight = peak = 0
    real_call = lite._call

    async def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
            return await real_call(*args, **kwargs)
        finally:
            in_flight -= 1

    monkeypatch.setattr(lite, "_call", counted)
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        played = await respond(client, story, respond_body(actor, "And then?"))
        assert played.status_code == 200, played.text
        stub.reset()
        stub.when("", PARSED, purpose="lite_rewrite_parse")
        stub.when("", json.dumps({"hits": []}), purpose="lite_rewrite_reach")
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION},
            headers=WRITE,
        )
        assert opened.status_code in (201, 422), opened.text
        reads = [c for c in stub.calls if c.purpose == "lite_rewrite_reach"]
        lines = len(await _messages(engine, story))
        assert len(reads) == lines  # one call per one-line chunk, no fix to re-check
        assert peak == 3


PAPERS = "I saw your farm papers."
SAILING = "I saw your sailing papers."


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-3,4,6")
async def test_s_lite_rewrite__conflicts(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#700 (FR-REWRITE-3): two of the reach's fixes cannot apply silently, so each is an
    irreducible ``high`` conflict on its line, with options ``rewrite|drop|keep`` and
    ``rewrite`` recommended:

    - the player's own line ("I saw your farm papers."): a player-authored line is never
      rewritten silently (``player_authored``);
    - the reply's fix, which breaks a global policy: the output gate (#56) refuses it, so it is
      ``fix_refused`` (the policy id in ``refused_changes``, never the text).

    Under ``synchronous`` both are asked and nothing is planned silently. Answered with the
    recommendations, the apply rewrites each line through ``rewrite_record`` with the change
    statement; the transcript reads the rewritten lines and the policy word appears nowhere."""
    from epistrel.core.ids import new_ulid
    from tests.policy_support import unique

    operator = {**WRITE, "X-Epistrel-Principal": "p-operator"}
    word = f"zarquon{new_ulid().lower()}"
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        made = await client.post(
            "/admin/policies",
            json={
                "scope": "global",
                "name": unique("lite-fix"),
                "rule": {"type": "wordlist", "words": [word]},
                "message": "Please keep that word out of the story.",
            },
            headers=operator,
        )
        assert made.status_code == 201, made.text
        policy = made.json()["id"]
        try:
            story = str((await create(client, story_body("Wren", "Ada")))["story_id"])
            cast = await rows(
                engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story
            )
            actor = next(str(c.id) for c in cast if c.name == "Wren")
            stub.when("", FARMER, purpose="generation")
            played = await respond(client, story, respond_body(actor, PAPERS))
            assert played.status_code == 200, played.text
            stub.reset()
            stub.when("", PARSED, purpose="lite_rewrite_parse")
            hits = {
                "hits": [
                    {
                        "turn": 1,
                        "quote": "farm papers",
                        "relation": "conflicts",
                        "fix_text": SAILING,
                        "reason": "names the farm",
                    },
                    {
                        "turn": 1,
                        "quote": "I was a farmer",
                        "relation": "conflicts",
                        "fix_text": f"Ada wipes her hands. I sailed the {word}.",
                        "reason": "names her old trade",
                    },
                ]
            }
            stub.script("lite_rewrite_reach", [json.dumps(hits), json.dumps({"hits": []})])
            opened = await client.post(
                f"/stories/{story}/rewrites",
                params=rs.OMNISCIENT,
                json={"instruction": CORRECTION},
                headers=WRITE,
            )
            assert opened.status_code == 201, opened.text
            session = opened.json()
            assert word not in opened.text
            preview = session["preview"]
            assert preview["changes"] == [], (preview["changes"], preview["refused_changes"])
            assert {"reason": "guardrail", "policy_ids": [policy]}.items() <= next(
                r for r in preview["refused_changes"] if r.get("reason") == "guardrail"
            ).items()
            conflicts = preview["blast_radius"]["irreducible"]
            assert sorted(c["reason"] for c in conflicts) == ["fix_refused", "player_authored"]
            assert {c["severity"] for c in conflicts} == {"high"}
            assert all(c["options"] == ["rewrite", "drop", "keep"] for c in conflicts)
            asked = [q for q in session["questions"] if q["kind"] == "conflict"]
            assert len(asked) == 2
            assert all(q["required"] for q in asked)
            assert session["state"] == "awaiting_answers"

            stub.when("", json.dumps({"text": SAILING}), purpose="rewrite_record")
            answered = await rs.answer_all(client, story, session)
            assert answered.status_code == 200, answered.text
            session = answered.json()
            applied = await rs.apply(client, story, session["id"], session["base_revision"])
            assert applied.status_code == 200, applied.text
            records = [c for c in stub.calls if c.purpose == "rewrite_record"]
            assert len(records) == 2
            assert all(CORRECTION in "\n".join(m.content for m in c.messages) for c in records)
            lines = await _transcript(engine, story)
            assert PAPERS not in lines
            assert FARMER not in lines
            assert not any(word in line for line in lines)
        finally:
            gone = await client.delete(f"/admin/policies/{policy}", headers=operator)
            assert gone.status_code in (200, 204), gone.text


FARM_AGAIN = "Ada glances at the fields. The farm was hard work."
SAIL_AGAIN = "Ada glances at the harbour. The sea was hard work."


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-9")
async def test_s_lite_rewrite__revalidation(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#700 (#237's Lite read set): a turn played after the preview is searched at apply (and
    only it: one reach call over its lines, plus the re-check of its fix). Its new hit makes the
    plan stale: 409 ``rewrite_stale`` with ``new_reach_hits``, and the recomputed preview plans
    both fixes. Applied at the new base, both lines are fixed. Complement: a session whose story
    did not move applies with no reach call at apply."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        stub.reset()
        stub.when("", PARSED, purpose="lite_rewrite_parse")
        stub.script("lite_rewrite_reach", [REACHED, json.dumps({"hits": []})])
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        session = opened.json()
        assert [c["new_text"] for c in session["preview"]["changes"]] == [SAILOR]

        stub.reset()
        stub.when("", FARM_AGAIN, purpose="generation")
        later = await respond(client, story, respond_body(actor, "Tell me more."))
        assert later.status_code == 200, later.text
        stub.reset()
        again = {
            "hits": [
                {
                    "turn": 2,
                    "quote": "The farm was hard work",
                    "relation": "conflicts",
                    "fix_text": SAIL_AGAIN,
                    "reason": "names the farm",
                }
            ]
        }
        stub.script("lite_rewrite_reach", [json.dumps(again), json.dumps({"hits": []})])
        stale = await rs.apply(client, story, session["id"], session["base_revision"])
        assert stale.status_code == 409, stale.text
        error = stale.json()["error"]
        assert error["code"] == "rewrite_stale"
        reaches = [c for c in stub.calls if c.purpose == "lite_rewrite_reach"]
        assert len(reaches) == 2  # the new turn's lines in one chunk, then the re-check
        searched = "\n".join(m.content for m in reaches[0].messages)
        assert "Turn 2" in searched
        assert "Turn 1" not in searched
        current = (
            await client.get(
                f"/stories/{story}/rewrites/{session['id']}", params=rs.OMNISCIENT, headers=WRITE
            )
        ).json()
        assert current["last_error"]["reasons"][0]["kind"] == "new_reach_hits"
        assert sorted(c["new_text"] for c in current["preview"]["changes"]) == sorted(
            [SAILOR, SAIL_AGAIN]
        )
        if current["state"] != "awaiting_approval":
            current = (await rs.answer_all(client, story, current)).json()
        stub.reset()
        applied = await rs.apply(client, story, current["id"], current["base_revision"])
        assert applied.status_code == 200, applied.text
        assert [c for c in stub.calls if c.purpose == "lite_rewrite_reach"] == []
        lines = await _transcript(engine, story)
        assert SAILOR in lines
        assert SAIL_AGAIN in lines


@pytest.mark.part("M7", "FR-LITE-8; FR-REWRITE-10")
async def test_s_lite_rewrite__undo_reach(stub_settings: Settings, engine: AsyncEngine) -> None:
    """#700 (FR-REWRITE-10): an undo of a Lite amendment searches only the lines committed after
    it, with the template statement ("Undo: Ada was always a sailor. (the original text is
    restored)", no parse call). A later line that still says she sailed is a conflict
    (``after_undo``) offered a fix: answered with the recommendation (``rewrite``), the apply
    rewrites it through ``rewrite_record``. Complement: lines before the amendment are not
    searched (one reach call: the later turn's lines)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub
        story = await _lite_story(client, engine, stub)
        cast = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        actor = next(str(c.id) for c in cast if c.name == "Wren")
        stub.reset()
        stub.when("", PARSED, purpose="lite_rewrite_parse")
        stub.script("lite_rewrite_reach", [REACHED, json.dumps({"hits": []})])
        opened = await client.post(
            f"/stories/{story}/rewrites",
            params=rs.OMNISCIENT,
            json={"instruction": CORRECTION, "preview": "apply_directly"},
            headers=WRITE,
        )
        assert opened.status_code == 201, opened.text
        assert opened.json()["state"] == "applied", opened.text
        stub.reset()
        stub.when("", SAIL_AGAIN, purpose="generation")
        later = await respond(client, story, respond_body(actor, "Tell me more."))
        assert later.status_code == 200, later.text

        (amendment,) = await rows(
            engine, "SELECT id FROM amendments WHERE story_id = :s AND kind = 'rewrite'", s=story
        )
        stub.reset()
        after = {
            "hits": [
                {
                    "turn": 2,
                    "quote": "The sea was hard work",
                    "relation": "conflicts",
                    "fix_text": None,
                    "reason": "still says she sailed",
                }
            ]
        }
        stub.script("lite_rewrite_reach", [json.dumps(after)])
        undone = await client.post(
            f"/stories/{story}/amendments/{amendment.id}/undo",
            params=rs.OMNISCIENT,
            json={},
            headers=WRITE,
        )
        assert undone.status_code in (200, 201), undone.text
        session = undone.json()
        reaches = [c for c in stub.calls if c.purpose == "lite_rewrite_reach"]
        assert len(reaches) == 1
        searched = "\n".join(m.content for m in reaches[0].messages)
        assert "Undo: Ada was always a sailor. (the original text is restored)" in searched
        assert "Turn 2" in searched
        assert "Turn 1" not in searched
        assert [c for c in stub.calls if c.purpose == "lite_rewrite_parse"] == []
        conflicts = session["preview"]["blast_radius"]["irreducible"]
        assert [c["reason"] for c in conflicts if c["reason"] == "after_undo"] == ["after_undo"]

        stub.reset()
        stub.when("", json.dumps({"text": FARM_AGAIN}), purpose="rewrite_record")
        if session["state"] != "awaiting_approval":
            session = (await rs.answer_all(client, story, session)).json()
        applied = await rs.apply(client, story, session["id"], session["base_revision"])
        assert applied.status_code == 200, applied.text
        lines = await _transcript(engine, story)
        assert FARMER in lines  # the undo restored the original line
        assert FARM_AGAIN in lines
        assert SAIL_AGAIN not in lines
