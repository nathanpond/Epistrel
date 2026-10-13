"""C-26: message feedback (docs/04 §5), its M3 parts.

Every C-26 (M3) part lives in this file (traceability counts a test ID per milestone in one file).

- #112's parts:
  - ``test_c_26__feedback``: rating a generated message, the principal and the context it
    records, the refusals, a deployment-added tag, re-rating with its history, a second
    principal's separate rating, retraction, and a regenerated message starting unrated
    (FR-FB-1,2, FR-AUTH-4). It runs on its own scratch database, because it adds a tag to the
    deployment's set.
  - ``test_c_26__isolation``: feedback never touches the story (FR-FB-2): a planted phrase
    reaches no prompt, event, history read, summary or story record, the rated story's prompts
    equal an unrated twin's, and a regenerated message starts unrated.

  The details are in ``tests/integration/test_feedback.py``.
- #126 adds its review slice (FR-FB-3) here.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.stories import STORY_TABLES
from epistrel.orchestrator.models.stub import StubCall
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.feedback_support import (
    OTHER_RATER,
    RATER,
    SEEDED_TAGS,
    answered,
    as_principal,
    error_of,
    feedback_rows,
    history_rows,
    play,
    played_story,
    rate,
    read,
    retract,
    scratch_feedback,
)
from tests.respond_support import new_story, respond_client
from tests.summary_support import run_due_jobs, segment_rows

AUTHOR = "p-author"
PHRASE = "Quillon Marrowgate"
"""Planted in ratings; nothing a story reads may ever hold it."""
REPLY = "The ferryman shrugs and points at the fog."
SUMMARY = "Travellers wait for a ferry in the fog."
SUMMARISED = {"lite_summary_chunk": 2}
WINDOW_OF_TWO = {"memory": {"rolling_summary": {"window_messages": 2}}}
"""Story settings and overrides (of its profile's window, #113) that make every turn after the
second summarised (#72)."""
FEEDBACK_TABLES = frozenset({"message_feedback", "feedback_history"})
ULID_TEXT = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _recorded(engine: AsyncEngine, operation_id: str) -> dict[str, Any]:
    (row,) = await rows(
        engine,
        "SELECT config_revisions FROM operations WHERE operation_id = :o",
        o=operation_id,
    )
    found: dict[str, Any] = row.config_revisions
    return found


@pytest.mark.integration
@pytest.mark.part("M3", "FR-FB-1,2; FR-AUTH-4")
async def test_c_26__feedback(pg_url: str) -> None:
    """A principal rates a generated message 1 to 5 with reason tags and free text. The rating
    records the principal, the message variant (its id, turn and variant) and the context the
    message was produced under, copied from its operation's record: the operation, the
    configuration fingerprint, the profile version, the models per purpose, the content-rating
    version and the quality status. A rating outside 1 to 5, an unknown tag and a text over the
    deployment's cap are refused with nothing written; a tag the deployment adds is accepted.
    Rating again replaces the rating, and the history keeps the earlier value; a second
    principal's rating is separate, and each principal reads its own. A retraction removes the
    rating and keeps its final value in the history. Regenerating the rated message leaves the
    earlier variant's ratings in place, and the new variant starts unrated.

    Complements asserted: tags are trimmed, lower-cased and compared as a set, so the same
    value again changes nothing (200, no history entry, ``updated_at`` kept); retracting
    nothing is still 204; a rating after a retraction is a new one (201)."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        story, message = played.story_id, played.message_id

        # -- refusals, with nothing written
        for value in (0, 6, 2.5, "3"):
            status, code, details = error_of(await rate(client, story, message, {"rating": value}))
            assert (status, code, details["field"]) == (422, "validation_error", "body.rating")
        unknown = await rate(client, story, message, {"rating": 2, "tags": ["Repetitive", "Dull"]})
        assert error_of(unknown) == (
            422,
            "unknown_feedback_tag",
            {"field": "body.tags", "unknown_tags": ["dull"], "allowed_tags": list(SEEDED_TAGS)},
        )
        status, code, details = error_of(
            await rate(client, story, message, {"rating": 2, "text": "x" * 2001})
        )
        assert (status, code, details["field"]) == (422, "validation_error", "body.text")
        assert await feedback_rows(db, story) == []

        # -- the first rating, with what it records
        first = answered(
            await rate(
                client,
                story,
                message,
                {
                    "rating": 2,
                    "tags": [" Too_Long", "repetitive"],
                    "text": "  The ferryman repeats himself.  ",
                },
            ),
            201,
        )
        operation_id = played.turn["beats"][0]["operation_id"]
        recorded = await _recorded(db, operation_id)
        assert first == {
            "story_id": story,
            "message_id": message,
            "turn": 1,
            "variant": 1,
            "principal": RATER,
            "rating": 2,
            "tags": ["repetitive", "too_long"],
            "text": "The ferryman repeats himself.",
            "compacted_at": None,
            "created_at": first["created_at"],
            "updated_at": first["created_at"],
            "context": {
                "operation_id": operation_id,
                "config_fingerprint": played.turn["fingerprint"],
                "profile_version": f"{LITE_PRESET_ID}@1",
                "models": recorded["models"],
                "rating_version_id": recorded["rating_version"],
                "quality_status": "uncertified",
            },
            "history": [],
        }
        assert recorded["fingerprint"] == played.turn["fingerprint"]
        assert set(recorded["models"]) >= {"generation"}

        # -- the same value again: nothing changes
        same = answered(
            await rate(
                client,
                story,
                message,
                {
                    "rating": 2,
                    "tags": ["too_long", "REPETITIVE", "too_long"],
                    "text": first["text"],
                },
            ),
            200,
        )
        assert same == first

        # -- rating again replaces, and the history keeps the earlier value
        second = answered(await rate(client, story, message, {"rating": 4}), 200)
        assert (second["rating"], second["tags"], second["text"]) == (4, [], None)
        assert second["created_at"] == first["created_at"]
        assert second["context"] == first["context"]
        assert [
            (h["rating"], h["tags"], h["text"], h["kind"], h["rated_at"]) for h in second["history"]
        ] == [(2, ["repetitive", "too_long"], first["text"], "replaced", first["updated_at"])]

        # -- a tag the deployment adds is accepted; a second principal's rating is separate
        added = await client.put(
            "/admin/settings",
            json={"feedback_tags": [*SEEDED_TAGS, "too_dark"]},
            headers=as_principal("p-operator"),
        )
        assert answered(added, 200)["feedback_tags"] == [*SEEDED_TAGS, "too_dark"]
        other = answered(
            await rate(client, story, message, {"rating": 5, "tags": ["too_dark"]}, OTHER_RATER),
            201,
        )
        assert (other["principal"], other["rating"], other["tags"]) == (
            OTHER_RATER,
            5,
            ["too_dark"],
        )
        mine = answered(await read(client, story, message), 200)
        assert (mine["principal"], mine["rating"], len(mine["history"])) == (RATER, 4, 1)
        theirs = answered(await read(client, story, message, OTHER_RATER), 200)
        assert (theirs["rating"], theirs["history"]) == (5, [])
        assert [(r.principal, r.rating) for r in await feedback_rows(db, story)] == [
            (RATER, 4),
            (OTHER_RATER, 5),
        ]

        # -- regenerating: the earlier variant keeps its ratings, the new one starts unrated
        regenerated = answered(
            await client.post(
                f"/stories/{story}/regenerate", json={}, headers=as_principal(AUTHOR)
            ),
            200,
        )
        newer = regenerated["message"]["message_id"]
        assert newer != message
        fresh = answered(await read(client, story, newer), 200)
        assert (fresh["turn"], fresh["variant"], fresh["rating"]) == (1, 2, None)
        assert (fresh["tags"], fresh["context"], fresh["history"]) == ([], None, [])
        assert answered(await read(client, story, message), 200)["rating"] == 4
        assert answered(await read(client, story, message, OTHER_RATER), 200)["rating"] == 5

        # -- a retraction removes the rating and keeps its final value in the history
        assert (await retract(client, story, message)).status_code == 204
        gone = answered(await read(client, story, message), 200)
        assert (gone["rating"], gone["created_at"], gone["context"]) == (None, None, None)
        assert [(h["kind"], h["rating"]) for h in gone["history"]] == [
            ("retracted", 4),
            ("replaced", 2),
        ]
        assert (await retract(client, story, message)).status_code == 204
        assert answered(await read(client, story, message, OTHER_RATER), 200)["rating"] == 5
        again = answered(await rate(client, story, message, {"rating": 3}), 201)
        assert (again["rating"], len(again["history"])) == (3, 2)
        assert [(h.principal, h.kind) for h in await history_rows(db, story)] == [
            (RATER, "replaced"),
            (RATER, "retracted"),
        ]


def _normalised(calls: list[StubCall]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Each call's purpose and messages, every id replaced by its first-seen position."""
    seen: dict[str, str] = {}

    def mark(match: re.Match[str]) -> str:
        return seen.setdefault(match.group(0), f"<id {len(seen)}>")

    return [
        (call.purpose, [(m.role, ULID_TEXT.sub(mark, m.content)) for m in call.messages])
        for call in calls
    ]


async def _story_records(engine: AsyncEngine, story_id: str) -> dict[str, list[str]]:
    """Every row of the story outside the feedback tables, and its ``stories`` row, as text."""
    found: dict[str, list[str]] = {}
    for table in STORY_TABLES:
        if table.name in FEEDBACK_TABLES:
            continue
        found[table.name] = sorted(
            r.row
            for r in await rows(
                engine,
                f'SELECT t::text AS row FROM "{table.name}" t WHERE story_id = :s',  # noqa: S608
                s=story_id,
            )
        )
    found["stories"] = [
        r.row
        for r in await rows(
            engine, "SELECT t::text AS row FROM stories t WHERE id = :s", s=story_id
        )
    ]
    return found


@pytest.mark.integration
@pytest.mark.part("M3", "FR-FB-2")
async def test_c_26__isolation(stub_settings: Settings, engine: AsyncEngine) -> None:
    """Feedback never touches the story. Two stories with the same cast and settings play the
    same six turns; in one, the first reply is rated 1 with a planted phrase and a reason tag,
    re-rated, and rated by a second principal, and the sixth reply is rated with the phrase
    before it is regenerated. Then the phrase appears in no prompt sent to a model (generation,
    guardrail or summary), in no event of the story, in no history read (a character's
    viewpoint, omniscient, and omniscient with the audit records and the summary), in no
    summary segment and in no other row of the story; the rated story's prompts equal its
    unrated twin's, ids aside; and rating, re-rating and retracting leave every story record
    as it was. The regenerated sixth reply starts unrated while the earlier variant keeps its
    rating.

    Complements asserted: the phrase is stored, in the ratings and in their history; both
    stories' summaries ran (each has segments); the reads and prompts compared are not empty."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        assert stub is not None
        stub.reset()
        stub.when("", REPLY, purpose="generation")
        stub.when("", SUMMARY, purpose="lite_summary")
        stories: dict[str, tuple[str, str]] = {}
        calls: dict[str, list[StubCall]] = {"rated": [], "twin": []}
        for name in ("rated", "twin"):
            story_id, cast = await new_story(client, settings=SUMMARISED, overrides=WINDOW_OF_TWO)
            stories[name] = (story_id, next(iter(cast.values())))

        async def turn(name: str, n: int) -> dict[str, Any]:
            story_id, actor = stories[name]
            before = len(stub.calls)
            answer = await play(client, story_id, actor, f"Line {n}: is the ferry running?")
            await run_due_jobs(engine, lib.models, story_id)
            calls[name] += stub.calls[before:]
            return answer

        rated, _ = stories["rated"]
        first = (await turn("rated", 1))["message"]["message_id"]
        await turn("twin", 1)
        untouched = await _story_records(engine, rated)
        answered(
            await rate(
                client,
                rated,
                first,
                {"rating": 1, "tags": ["factual_error"], "text": f"{PHRASE} never said that."},
            ),
            201,
        )
        answered(await rate(client, rated, first, {"rating": 2, "text": f"Still {PHRASE}."}), 200)
        answered(await rate(client, rated, first, {"rating": 1, "text": PHRASE}, OTHER_RATER), 201)
        assert (await retract(client, rated, first, OTHER_RATER)).status_code == 204
        assert await _story_records(engine, rated) == untouched, "a rating changes no story row"

        sixth = ""
        for n in range(2, 7):
            sixth = (await turn("rated", n))["message"]["message_id"]
            await turn("twin", n)
        answered(await rate(client, rated, sixth, {"rating": 1, "text": PHRASE}), 201)
        regenerated: dict[str, str] = {}
        for name in ("rated", "twin"):
            story_id, _ = stories[name]
            before = len(stub.calls)
            answer = await client.post(
                f"/stories/{story_id}/regenerate", json={}, headers=as_principal(AUTHOR)
            )
            regenerated[name] = answered(answer, 200)["message"]["message_id"]
            await run_due_jobs(engine, lib.models, story_id)
            calls[name] += stub.calls[before:]

        # -- nothing a model was sent holds the phrase, and the rated story's prompts are its
        # unrated twin's
        sent = [m.content for call in calls["rated"] for m in call.messages]
        assert sent
        assert not [text for text in sent if PHRASE in text]
        assert {call.purpose for call in calls["rated"]} >= {"generation", "lite_summary"}
        assert _normalised(calls["rated"]) == _normalised(calls["twin"])

        # -- no event, history read, summary segment or story row holds it
        events = await rows(
            engine, "SELECT category::text, payload::text FROM events WHERE story_id = :s", s=rated
        )
        assert {e.category for e in events} >= {"narrative_state", "derived", "control"}
        assert not [e for e in events if PHRASE in e.payload]
        actor = stories["rated"][1]
        reads = [
            {"viewpoint": f"character:{actor}"},
            {"viewpoint": "omniscient"},
            {"viewpoint": "omniscient", "include_audit": "1", "include_summary": "1"},
        ]
        for params in reads:
            history = await client.get(
                f"/stories/{rated}/history", params=params, headers=as_principal(AUTHOR)
            )
            text = json.dumps(answered(history, 200))
            assert REPLY in text, params
            assert PHRASE not in text, params
        segments = await segment_rows(engine, rated)
        assert segments
        assert await segment_rows(engine, stories["twin"][0])
        assert not [s for s in segments if PHRASE in s.text]
        records = await _story_records(engine, rated)
        assert not [
            (table, row) for table, found in records.items() for row in found if PHRASE in row
        ]

        # -- the phrase is where feedback is kept
        kept = await rows(
            engine,
            "SELECT text FROM message_feedback WHERE story_id = :s UNION ALL "
            "SELECT text FROM feedback_history WHERE story_id = :s",
            s=rated,
        )
        assert len([r for r in kept if r.text and PHRASE in r.text]) == 4

        # -- the regenerated reply starts unrated; the earlier variant keeps its rating
        assert answered(await read(client, rated, regenerated["rated"]), 200)["rating"] is None
        assert answered(await read(client, rated, sixth), 200)["rating"] == 1
        assert answered(await read(client, rated, first), 200)["rating"] == 2
