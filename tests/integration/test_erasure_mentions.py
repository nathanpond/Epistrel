"""Erasure redacts only mentions attributed to the subject (ADR-154; #372, #373, #374).

A name word that is also a common word ("Will", "Black") is erased only where it is the name:
capitalised mid-sentence it is certain, lowercase it is never touched, and capitalised at the
start of a sentence it is a candidate the operator confirms by id. Name words are matched as
written (#373) and hyphenated names by their halves (#374). The write-path guard follows the
same rule, so a later reply using the ordinary word is not refused.
"""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import erasure_support as e
from tests.event_support import rows, scalar
from tests.respond_support import body, new_story, played, respond, respond_client

pytestmark = pytest.mark.integration

ORDINARY = "Ty will open the black door."
QUESTION = "What will you do, Ty?"
FULL = "Ty asked Will Black."
BARE = "Ty asked Will."
OPENING = "Will you come?"
LATER = "Ty says it will rain tonight."


@pytest.fixture
def erasure_settings(pg_url: str) -> Settings:
    return e.erasure_settings(pg_url)


async def _texts(engine: AsyncEngine, story_id: str) -> list[str]:
    """Every player line and reply of the story's events, in order."""
    found = await rows(
        engine,
        "SELECT payload->>'text' AS line FROM events "
        "WHERE story_id = :s AND type IN ('player_input', 'message_rendered') "
        "ORDER BY narrative_turn, id",
        s=story_id,
    )
    return [str(r.line) for r in found if r.line is not None]


async def _will_black(client: httpx.AsyncClient, lib: Any) -> tuple[str, dict[str, str]]:
    """Wren (the player), "Will Black" and Ty, three turns played."""
    story, ids = await new_story(client, "Wren", "Will Black", "Ty")
    lib.models.stub.script("generation", [ORDINARY, BARE, "The rain keeps falling."])
    for line in (QUESTION, FULL, OPENING):
        await played(client, story, body(ids["Wren"], line))
    return story, ids


async def test_erasing_will_black_spares_the_words_will_and_black(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """#372: erase "Will Black". "Ty will open the black door." and "What will you do, Ty?" are
    unchanged (complement); "Ty asked Will Black." and "Ty asked Will." are redacted; "Will you
    come?" is a candidate, unchanged and listed by the dry run (``candidates_remaining`` counts
    it), and the residue is 0. A later model reply "Ty says it will rain tonight." answers 200
    (complement), while a reply naming "Will Black" is still refused."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        story, ids = await _will_black(client, lib)
        listed = await e.erase(client, "character", ids["Will Black"], story, dry_run=True)
        candidates = listed["candidates"]
        assert candidates, listed
        assert {c["rule"] for c in candidates} == {"candidate"}
        assert all(OPENING in c["snippet"] for c in candidates)
        # The player's line, as said and as the turn's text: one event, two fields.
        in_events = [c for c in candidates if c["record_table"] == "events"]
        assert len({c["record_id"] for c in in_events}) == 1
        assert {c["field"] for c in in_events} == {"payload.text", "payload.say"}
        assert listed["candidates_remaining"] == len(candidates)

        done = await e.erase(client, "character", ids["Will Black"], story)
        assert done["residue"] == 0
        assert done["candidates_remaining"] == len(candidates)
        assert done["candidates"] == []
        assert done["ledger_entry"]["counts"]["confirmed_candidates"] == []
        texts = await _texts(engine, story)
        assert QUESTION in texts
        assert ORDINARY in texts
        assert OPENING in texts
        assert texts.count("Ty asked [erased].") == 2
        assert not any("Will Black" in t for t in texts)

        lib.models.stub.reset()
        lib.models.stub.script("generation", [LATER])
        later = await respond(client, story, body(ids["Wren"], "And the weather?"))
        assert later.status_code == 200, later.text
        assert LATER in later.text

        budget = int(
            await scalar(
                engine,
                "SELECT coalesce((settings->>'regenerate_retry_budget')::int, 2) "
                "FROM stories WHERE id = :s",
                s=story,
            )
        )
        lib.models.stub.reset()
        lib.models.stub.script("generation", ["Will Black is at the door."] * (budget + 1))
        refused = await respond(client, story, body(ids["Wren"], "Who knocks?"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "erased_subject_referenced"
        lib.models.stub.reset()
        lib.models.stub.script("generation", ["Ty waves at Will."] * (budget + 1))
        named = await respond(client, story, body(ids["Wren"], "Who does Ty greet?"))
        assert named.status_code == 409, named.text


async def test_a_confirmed_candidate_is_erased(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """#372: the candidates the dry run listed, confirmed by id, are erased with the rest, and
    ``candidates_remaining`` is then 0; the ledger keeps the confirmed ids (never a snippet). An
    id the erasure does not meet is 422 ``unknown_erasure_candidate`` and erases nothing. No
    record the erasure writes (its operations, its key's stored result, the ledger, the audit
    log) holds a snippet."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        story, ids = await _will_black(client, lib)
        will = ids["Will Black"]
        listed = await e.erase(client, "character", will, story, dry_run=True)
        confirm = [c["candidate_id"] for c in listed["candidates"]]
        snippets = {c["snippet"] for c in listed["candidates"]}

        stale = await e.erase(client, "character", will, story, confirm=["0" * 32], status=422)
        assert stale["error"]["code"] == "unknown_erasure_candidate"
        assert stale["error"]["details"]["candidate_ids"] == ["0" * 32]
        assert await scalar(engine, "SELECT count(*) FROM characters WHERE id = :c", c=will) == 1
        assert (
            await scalar(
                engine, "SELECT count(*) FROM erasure_ledger WHERE subject_id = :c", c=will
            )
            == 0
        )

        done = await e.erase(client, "character", will, story, confirm=confirm, key="confirmed")
        assert (done["residue"], done["candidates_remaining"]) == (0, 0)
        assert done["ledger_entry"]["counts"]["confirmed_candidates"] == sorted(confirm)
        texts = await _texts(engine, story)
        assert OPENING not in texts
        assert "[erased] you come?" in texts
        assert ORDINARY in texts

        written = await rows(
            engine,
            "SELECT t::text AS r FROM erasure_ledger t WHERE subject_id = :c "
            "UNION ALL SELECT t::text FROM audit_log t WHERE t.details::text LIKE :c2 "
            "UNION ALL SELECT t::text FROM operations t WHERE operation_id IN "
            "(SELECT operation_id FROM erasure_ledger WHERE subject_id = :c) "
            "UNION ALL SELECT t::text FROM idempotency_keys t WHERE key = 'confirmed'",
            c=will,
            c2=f"%{will}%",
        )
        assert len(written) >= 4
        for record in written:
            assert not any(s in record.r for s in snippets), record.r
            assert "you come" not in record.r


async def test_name_words_match_as_written_and_by_their_halves(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """#373: a bare "Strauß" of "Jürgen Strauß" is redacted (no case-folding to "strauss"), and
    the residue is 0. #374: "Mary" and "Jane" of "Mary-Jane Watson" are redacted. #364's
    repro: a bare "Marisol" of "Marisol Quenby" still is."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        cast = ("Wren", "Jürgen Strauß", "Mary-Jane Watson", "Marisol Quenby", "Ty")
        story, ids = await new_story(client, *cast)
        lib.models.stub.script("generation", ["Ty nods."] * 3)
        lines = {
            "Jürgen Strauß": "Hi Strauß. Hi Jürgen.",
            "Mary-Jane Watson": "Hi Mary. Hi Jane.",
            "Marisol Quenby": "Hi Marisol.",
        }
        for line in lines.values():
            await played(client, story, body(ids["Wren"], line))
        for name in lines:
            done = await e.erase(client, "character", ids[name], story)
            assert done["residue"] == 0, name
        texts = await _texts(engine, story)
        assert "Hi [erased]. Hi [erased]." in texts
        assert texts.count("Hi [erased]. Hi [erased].") == 2
        assert "Hi [erased]." in texts
        for word in ("Strauß", "Jürgen", "Mary", "Jane", "Marisol"):
            assert not any(word in t for t in texts), word


async def test_the_index_records_each_mention_by_position(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """ADR-154: a prose mention's row records its field, its offsets in the field's NFC text
    and its rule, never the text; a common word gives a ``candidate`` row at the start of a
    sentence and none in lowercase. A rebuild of the story's index from its records equals
    what was indexed as it was written, before and after an erasure, and after it another
    subject's offsets still point at its name in the redacted text."""
    async with respond_client(erasure_settings, engine) as (client, lib):
        story, ids = await _will_black(client, lib)
        mentions = await rows(
            engine,
            "SELECT r.record_id, r.field, r.span_start, r.span_end, r.rule, e.payload "
            "FROM subject_references r JOIN events e ON e.id = r.record_id "
            "WHERE r.story_id = :s AND r.subject_id = :c AND r.ref_kind = 'mentioned' "
            "AND r.record_table = 'events' AND e.type IN ('player_input', 'message_rendered')",
            s=story,
            c=ids["Will Black"],
        )
        found: dict[tuple[str, str, str], str] = {}
        for m in mentions:
            text = m.payload[m.field.removeprefix("payload.")]
            found[(m.field, text[m.span_start : m.span_end], text)] = m.rule
        assert found == {
            (field, matched, line): rule
            for field in ("payload.text", "payload.say")
            for matched, line, rule in (
                ("Will Black", FULL, "name"),
                ("Will", OPENING, "candidate"),
            )
        } | {("payload.text", "Will", BARE): "part"}
        columns = await rows(
            engine,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'subject_references'",
        )
        assert {c.column_name for c in columns} >= {"span_start", "span_end", "rule"}
        assert not {"text", "term", "snippet", "matched"} & {c.column_name for c in columns}

        from epistrel.core.erasure.index import rebuild

        async with engine.connect() as conn:
            difference = await rebuild(conn, story)
        assert difference.clean, difference

        # Redacting her re-indexes each record, so another subject's offsets stay right.
        await e.erase(client, "character", ids["Will Black"], story)
        ty = await rows(
            engine,
            "SELECT r.field, r.span_start, r.span_end, e.payload "
            "FROM subject_references r JOIN events e ON e.id = r.record_id "
            "WHERE r.story_id = :s AND r.subject_id = :c AND r.ref_kind = 'mentioned' "
            "AND r.record_table = 'events' AND e.type IN ('player_input', 'message_rendered')",
            s=story,
            c=ids["Ty"],
        )
        spans = [
            (m.payload[m.field.removeprefix("payload.")], m.span_start, m.span_end) for m in ty
        ]
        assert ("Ty asked [erased].", 0, 2) in spans
        assert all(text[start:end] == "Ty" for text, start, end in spans), spans
        async with engine.connect() as conn:
            assert (await rebuild(conn, story)).clean


async def test_a_reapply_erases_the_confirmed_candidates(
    erasure_settings: Settings, engine: AsyncEngine
) -> None:
    """#125's reapply after a restore runs the erasure with the candidate ids its ledger counts
    recorded (``confirmed_candidates``): it erases them too, and skips an id the database no
    longer holds instead of refusing (the request's own 422 is for an operator's stale id)."""
    from types import SimpleNamespace

    from epistrel.core.erasure import erase as erasure
    from epistrel.core.erasure.ledger import _pending_of

    async with respond_client(erasure_settings, engine) as (client, lib):
        story, ids = await _will_black(client, lib)
        will = ids["Will Black"]
        listed = await e.erase(client, "character", will, story, dry_run=True)
        confirm = tuple(c["candidate_id"] for c in listed["candidates"])
        pending = _pending_of(
            SimpleNamespace(
                id="x",
                scope="story",
                story_ref=story,
                subject_kind="character",
                subject_id=will,
                at=None,
                counts={"confirmed_candidates": [*confirm, "f" * 32], "residue": 0},
            )
        )
        assert pending.confirmed == (*confirm, "f" * 32)
        request = erasure.ErasureRequest.model_validate(
            {"subject": {"kind": "character", "id": will}, "scope": {"story": story}}
        )
        key = erasure_settings.config_key
        assert key is not None
        done = await erasure.erase(
            engine,
            request,
            principal="system",
            config_key=key.get_secret_value(),
            compressor=lib.ports.summary_compressor,
            write_ledger=False,
            confirmed=pending.confirmed,
        )
    assert (done.residue, done.candidates_remaining) == (0, 0)
    texts = await _texts(engine, story)
    assert OPENING not in texts
    assert "[erased] you come?" in texts
