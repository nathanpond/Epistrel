"""Message feedback (#112) beyond the C-26 parts: the request's refusals, which messages can be
rated, the context a rating copies (and never recomputes), re-rating, retraction and history in
detail, ratings that write nothing else, the principal rule, archived stories and turns in
flight, the deployment's tag set, text cap and retention, story deletion and forks, the
subject-indexing seams, the M7 compaction hook, concurrent ratings, retractions and tag removals,
and the guard over which modules may name the feedback tables.

The parts are in ``tests/conformance/test_c_26.py``. The tests that change the deployment's
feedback settings or age ratings run on their own scratch database (``scratch_feedback``)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import Settings
from epistrel.core import clock, feedback
from epistrel.core.config_changes import lock_deployment
from epistrel.core.context import RequestContext
from epistrel.core.errors import PrincipalRequired
from epistrel.core.feedback import FeedbackRequest
from epistrel.core.ids import new_ulid
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.tables import deployment_state
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID
from epistrel.core.tables.feedback import HistoryKind, message_feedback
from epistrel.orchestrator.jobs.handlers import retention
from epistrel.testing import pauses
from tests.auth_support import auth_settings
from tests.event_support import new_story as bare_story
from tests.event_support import play_turn, rows, scalar
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
from tests.policy_support import story_item, words
from tests.respond_support import respond_client
from tests.story_support import count

SRC = Path(__file__).resolve().parents[2] / "src" / "epistrel"
AUTHOR = "p-author"
OPERATOR = "p-operator"
T0 = clock.system_clock().replace(microsecond=0) - timedelta(days=1)
"""A fixed moment for the tests that freeze the Engine's clock."""
TABLES = ("message_feedback", "feedback_history")


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _minutes(n: int) -> Any:
    return T0 + timedelta(minutes=n)


# -- the request ----------------------------------------------------------------------------------


async def test_requests_are_checked_with_nothing_written(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Each refusal is 422 at its field, with nothing written: a rating that is not an integer
    from 1 to 5 or is missing, an unknown field, more than ten tags, a blank or overlong tag, a
    NUL in the text or a tag, a text over 2000 characters once trimmed (or over 20000 as sent),
    a message id that is not a ULID (the ``turn:variant`` form included) and a malformed story
    id. Tags are trimmed and lower-cased, kept distinct and sorted; a text is trimmed, and an
    empty one is none. Complement: 2000 characters with spaces around them are accepted."""
    async with respond_client(stub_settings, engine) as (client, _):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        cases: list[tuple[Any, str]] = [
            ({"rating": True}, "body.rating"),
            ({"rating": None}, "body.rating"),
            ({}, "body.rating"),
            ({"rating": 3, "why": "x"}, "body.why"),
            ({"rating": 3, "tags": ["other"] * 11}, "body.tags"),
            ({"rating": 3, "tags": None}, "body.tags"),
            ({"rating": 3, "tags": ["   "]}, "body.tags[0]"),
            ({"rating": 3, "tags": ["x" * 65]}, "body.tags[0]"),
            ({"rating": 3, "tags": ["oth\x00er"]}, "body.tags[0]"),
            ({"rating": 3, "text": "a\x00b"}, "body.text"),
            ({"rating": 3, "text": " " + "x" * 2001 + " "}, "body.text"),
            ({"rating": 3, "text": "x" * 20_001}, "body.text"),
            ([3], "body"),
        ]
        for payload, field in cases:
            status, code, details = error_of(await rate(client, story, message, payload))
            assert (status, code, details["field"]) == (422, "validation_error", field), payload
        for bad in ("1:1", "turn-1", message.lower()):
            status, code, details = error_of(await rate(client, story, bad, {"rating": 3}))
            assert (status, details["field"]) == (422, "path.message_id"), bad
        status, _, details = error_of(await rate(client, "1:1", message, {"rating": 3}))
        assert (status, details["field"]) == (422, "path.story_id")
        assert await feedback_rows(engine, story) == []

        kept = answered(
            await rate(
                client,
                story,
                message,
                {
                    "rating": 3,
                    "tags": ["  OTHER", "repetitive", "other "],
                    "text": "  " + "y" * 2000 + "\n",
                },
            ),
            201,
        )
        assert (kept["tags"], kept["text"]) == (["other", "repetitive"], "y" * 2000)
        blank = answered(
            await rate(client, story, message, {"rating": 3, "tags": ["other"], "text": " \t "}),
            200,
        )
        assert blank["text"] is None
        (row,) = await feedback_rows(engine, story)
        assert (list(row.tags), row.text) == (["other"], None)


async def test_only_generated_messages_of_the_story_are_rated(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A reply, a safe fallback and a truncated (discarded) turn's reply can be rated: each is
    a ``message_rendered`` event the engine produced. The player's input, the consumer's
    ingested narration, another story's message and an unknown id are 404 ``not_found`` naming
    the story and the message, on every route; an unknown story is 404 naming the story.
    Nothing is written for any of them."""
    async with respond_client(stub_settings, engine) as (client, lib):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        ingested = await client.post(
            f"/stories/{story}/ingest",
            json={"events": [{"client_event_id": "n-1", "kind": "narration", "text": "Fog."}]},
            headers=as_principal(AUTHOR),
        )
        answered(ingested, 201)
        (player_input,) = await rows(
            engine, "SELECT id FROM events WHERE story_id = :s AND type = 'player_input'", s=story
        )
        (narration,) = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered' "
            "AND provenance = 'consumer'",
            s=story,
        )
        other = await played_story(client)
        for target in (player_input.id, narration.id, other.message_id, new_ulid()):
            for response in (
                await rate(client, story, target, {"rating": 3}),
                await read(client, story, target),
                await retract(client, story, target),
            ):
                assert error_of(response) == (
                    404,
                    "not_found",
                    {"story_id": story, "message_id": target},
                )
        missing = new_ulid()
        for response in (
            await rate(client, missing, message, {"rating": 3}),
            await read(client, missing, message),
            await retract(client, missing, message),
        ):
            assert error_of(response) == (404, "not_found", {"story_id": missing})
        assert await feedback_rows(engine, story) == []

        # -- a safe fallback: every draft broke the story's output policy (#68)
        await lib.policies_set_story(
            RequestContext(principal=AUTHOR), story, [story_item(words("whisky"))]
        )
        stub = lib.models.stub
        assert stub is not None
        stub.script("generation", ["Whisky for everyone."] * 3)
        fallback = await play(client, story, played.actor, "Pour me a drink.")
        assert fallback["guardrail"]["fallback_used"]
        rated = answered(
            await rate(
                client,
                story,
                fallback["message"]["message_id"],
                {"rating": 1, "tags": ["should_have_been_blocked"]},
            ),
            201,
        )
        assert (rated["turn"], rated["variant"]) == (3, 1)  # the narration was turn 2

        # -- a discarded turn: truncated away, its reply still rated
        truncated = await client.post(
            f"/stories/{story}/truncate", json={"to_turn": 0}, headers=as_principal(AUTHOR)
        )
        answered(truncated, 200)
        discarded = answered(await rate(client, story, message, {"rating": 2}), 201)
        assert (discarded["turn"], discarded["variant"], discarded["rating"]) == (1, 1, 2)


# -- the context -----------------------------------------------------------------------------------


async def test_the_context_is_copied_once_from_the_operation(pg_url: str) -> None:
    """A rating's context is its message's operation's record, copied when the rating is first
    written and never recomputed: after the generation model changes, a later turn records a
    new fingerprint and model, a rating of it copies them, and re-rating the earlier message
    keeps the earlier context. An operation that recorded no configuration stamp (#109) leaves
    its fingerprint, profile version, models and quality status null; its content-rating
    version, recorded by every operation (#64), is copied."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        story = played.story_id
        first = answered(await rate(client, story, played.message_id, {"rating": 3}), 201)
        context = first["context"]
        assert (context["profile_version"], context["quality_status"]) == (
            f"{LITE_PRESET_ID}@1",
            "uncertified",
        )
        changed = await client.put(
            "/admin/model-config/generation",
            json={"model": "ferry-model"},
            headers=as_principal(OPERATOR),
        )
        assert changed.status_code == 200, changed.text
        later = await play(client, story, played.actor, "And now?")
        assert later["fingerprint"] != played.turn["fingerprint"]
        second = answered(
            await rate(client, story, later["message"]["message_id"], {"rating": 4}), 201
        )
        assert second["context"]["config_fingerprint"] == later["fingerprint"]
        assert second["context"]["models"]["generation"]["id"] == "ferry-model"
        assert context["models"]["generation"]["id"] != "ferry-model"
        again = answered(await rate(client, story, played.message_id, {"rating": 1}), 200)
        assert again["context"] == context

        bare = await bare_story(db)
        await play_turn(db, bare, "No stamp here.")
        (message,) = await rows(
            db,
            "SELECT id, operation_id FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=bare,
        )
        recorded = await scalar(
            db,
            "SELECT config_revisions FROM operations WHERE operation_id = :o",
            o=message.operation_id,
        )
        assert "fingerprint" not in recorded
        unstamped = answered(await rate(client, bare, message.id, {"rating": 3}), 201)
        assert unstamped["context"] == {
            "operation_id": message.operation_id,
            "config_fingerprint": None,
            "profile_version": None,
            "models": None,
            "rating_version_id": recorded["rating_version"],
            "quality_status": None,
        }


def test_the_context_reads_only_what_the_operation_recorded() -> None:
    """``context_of`` copies the recorded keys and nulls a missing or malformed one: a profile
    version needs the profile id and an integer version (not a boolean), models a mapping and
    the quality status its ``status``."""
    full = {
        "fingerprint": "sha256:" + "a" * 64,
        "profile": LITE_PRESET_ID,
        "profile_version": 3,
        "models": {"generation": {"id": "m", "reported_version": None}},
        "rating_version": "R" * 26,
        "quality_status": {"status": "uncertified"},
    }
    assert feedback.context_of("O" * 26, full) == {
        "operation_id": "O" * 26,
        "config_fingerprint": "sha256:" + "a" * 64,
        "profile_version": f"{LITE_PRESET_ID}@3",
        "models": {"generation": {"id": "m", "reported_version": None}},
        "rating_version_id": "R" * 26,
        "quality_status": "uncertified",
    }
    malformed = {
        "fingerprint": "",
        "profile": LITE_PRESET_ID,
        "profile_version": True,
        "models": ["generation"],
        "rating_version": 7,
        "quality_status": "uncertified",
    }
    assert feedback.context_of("O" * 26, malformed) == {
        "operation_id": "O" * 26,
        "config_fingerprint": None,
        "profile_version": None,
        "models": None,
        "rating_version_id": None,
        "quality_status": None,
    }
    assert feedback.context_of("O" * 26, {})["profile_version"] is None


# -- re-rating, retraction and history -------------------------------------------------------------


async def test_rerating_and_retraction_in_detail(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """The same value again (tags in any order or case, the text with other spaces around it)
    writes nothing: no history entry, and ``updated_at`` kept. A changed text alone is a new
    value: the one it replaces joins the history with when it was given (``rated_at``) and
    when it was replaced. The history reads newest first. A retraction writes the final value
    as ``retracted`` and removes the rating; a second writes nothing. A rating after it is a
    new one, with a new ``created_at``. The library answers what REST does, with ``created``,
    ``changed`` and ``retracted``."""
    async with respond_client(stub_settings, engine) as (client, lib):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        me = RequestContext(principal=RATER)

        async def rate_at(minute: int, **value: Any) -> feedback.FeedbackWrite:
            with clock.frozen_at(_minutes(minute)):
                return await lib.rate_message(me, story, message, FeedbackRequest(**value))

        first = await rate_at(0, rating=3, tags=["too_long", "other"], text="Long.")
        assert (first.created, first.changed) == (True, True)
        same = await rate_at(1, rating=3, tags=["OTHER", " too_long", "other"], text="  Long. ")
        assert (same.created, same.changed) == (False, False)
        assert same.feedback == first.feedback
        assert await history_rows(engine, story) == []
        second = await rate_at(2, rating=3, tags=["other", "too_long"], text="Too long.")
        assert (second.created, second.changed) == (False, True)
        assert (second.feedback.created_at, second.feedback.updated_at) == (
            _minutes(0),
            _minutes(2),
        )
        third = await rate_at(3, rating=5)
        assert [
            (h.rating, h.tags, h.text, h.kind, h.rated_at, h.replaced_at)
            for h in third.feedback.history
        ] == [
            (3, ["other", "too_long"], "Too long.", HistoryKind.REPLACED, _minutes(2), _minutes(3)),
            (3, ["other", "too_long"], "Long.", HistoryKind.REPLACED, _minutes(0), _minutes(2)),
        ]

        with clock.frozen_at(_minutes(4)):
            gone = await lib.retract_feedback(me, story, message)
            nothing = await lib.retract_feedback(me, story, message)
        assert (gone.retracted, nothing.retracted) == (True, False)
        assert [h.kind for h in await history_rows(engine, story)] == [
            "replaced",
            "replaced",
            "retracted",
        ]
        after = await lib.get_feedback(me, story, message)
        assert (after.rating, after.tags, after.created_at, after.context) == (None, [], None, None)
        assert (after.history[0].rating, after.history[0].kind) == (5, HistoryKind.RETRACTED)
        assert (after.history[0].rated_at, after.history[0].replaced_at) == (
            _minutes(3),
            _minutes(4),
        )
        assert answered(await read(client, story, message), 200) == after.model_dump(mode="json")

        fresh = await rate_at(5, rating=1)
        assert (fresh.created, fresh.feedback.created_at, len(fresh.feedback.history)) == (
            True,
            _minutes(5),
            3,
        )


async def test_a_rating_writes_nothing_else(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A rating and its retraction are plain table writes: no operation, event, configuration
    change or idempotency key is written; the response carries no ``Idempotency-Key`` (none
    sent is echoed, none is kept) and a stale ``X-Epistrel-Expected-Revision`` is ignored. The
    same request again converges on the same rating (200)."""
    async with respond_client(stub_settings, engine) as (client, _):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        tables = ("operations", "events", "config_changes", "idempotency_keys")
        before = {t: await count(engine, f"SELECT count(*) FROM {t}") for t in tables}  # noqa: S608
        key = f"rating-{new_ulid()}"
        sent = {"Idempotency-Key": key, "X-Epistrel-Expected-Revision": "0"}
        first = await rate(client, story, message, {"rating": 2}, **sent)
        again = await rate(client, story, message, {"rating": 2}, **sent)
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json() == first.json()
        gone = await client.delete(
            f"/stories/{story}/messages/{message}/feedback", headers=as_principal(RATER) | sent
        )
        assert gone.status_code == 204
        for response in (first, again, gone):
            assert "Idempotency-Key" not in response.headers
            assert "Idempotent-Replay" not in response.headers
        after = {t: await count(engine, f"SELECT count(*) FROM {t}") for t in tables}  # noqa: S608
        assert after == before
        assert (
            await count(engine, "SELECT count(*) FROM idempotency_keys WHERE key = :k", k=key) == 0
        )


# -- the principal ---------------------------------------------------------------------------------


async def test_the_principal_is_required_and_keys_the_rating(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A rating is keyed by its principal: each principal reads its own, and one with none
    reads ``rating: null``. Every route needs the principal: 400 ``principal_required``
    without one, blank, too long or ``system`` (a read too, since it answers the requester's
    own rating); the library refuses a missing or blank one alike."""
    async with respond_client(stub_settings, engine) as (client, lib):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        answered(await rate(client, story, message, {"rating": 2}), 201)
        answered(await rate(client, story, message, {"rating": 5}, OTHER_RATER), 201)
        assert answered(await read(client, story, message), 200)["rating"] == 2
        assert answered(await read(client, story, message, OTHER_RATER), 200)["rating"] == 5
        nobody = answered(await read(client, story, message, "p-nobody"), 200)
        assert (nobody["principal"], nobody["rating"], nobody["history"]) == ("p-nobody", None, [])

        for principal in (None, "  ", "x" * 257, "system"):
            for response in (
                await rate(client, story, message, {"rating": 1}, principal),
                await read(client, story, message, principal),
                await retract(client, story, message, principal),
            ):
                assert error_of(response)[:2] == (400, "principal_required"), principal
        for ctx in (RequestContext(), RequestContext(principal=" ")):
            with pytest.raises(PrincipalRequired):
                await lib.get_feedback(ctx, story, message)
            with pytest.raises(PrincipalRequired):
                await lib.rate_message(ctx, story, message, FeedbackRequest(rating=1))
            with pytest.raises(PrincipalRequired):
                await lib.retract_feedback(ctx, story, message)
        assert [(r.principal, r.rating) for r in await feedback_rows(engine, story)] == [
            (RATER, 2),
            (OTHER_RATER, 5),
        ]


async def test_archived_stories_and_turns_in_flight(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A rating needs neither an active story nor an idle one: while a turn waits on its model
    call, an earlier reply is rated and the rating retracted; on an archived story a rating is
    written, read and retracted. Complement: the turn then finishes."""
    async with respond_client(stub_settings, engine) as (client, lib):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        stub = lib.models.stub
        assert stub is not None
        latch = stub.hold("generation")
        turn = asyncio.ensure_future(play(client, story, played.actor, "Is it late?"))
        await asyncio.wait_for(latch.entered.wait(), 10)
        assert not turn.done()
        answered(await rate(client, story, message, {"rating": 4}), 201)
        assert (await retract(client, story, message)).status_code == 204
        latch.release()
        assert (await turn)["turn"] == 2

        archived = await client.post(f"/stories/{story}/archive", headers=as_principal(AUTHOR))
        answered(archived, 200)
        answered(await rate(client, story, message, {"rating": 3}), 201)
        assert answered(await read(client, story, message), 200)["rating"] == 3
        assert (await retract(client, story, message)).status_code == 204


# -- the deployment's settings --------------------------------------------------------------------


async def _settings(client: Any, changes: dict[str, Any]) -> Any:
    return await client.put("/admin/settings", json=changes, headers=as_principal(OPERATOR))


async def _changes(db: AsyncEngine, setting: str) -> list[Any]:
    return await rows(
        db,
        "SELECT old, new, class, actor FROM config_changes WHERE scope = 'deployment' "
        "AND setting = :s ORDER BY config_revision",
        s=setting,
    )


async def test_the_deployment_tag_set(pg_url: str) -> None:
    """``feedback_tags`` starts as the nine seeded tags and is a prospective, recorded setting: a
    write replaces the list (one ``config_changes`` row with the old and new lists) and may
    empty it. Duplicates, a tag outside ``[a-z0-9_]{1,40}`` and more than 50 tags are 422.
    Removing a tag that a rating carries, or that only a history entry still carries, is 409
    ``feedback_tag_in_use`` with each tag's counts, and nothing is written; an unused tag,
    seeded or not, can be removed, and a rating naming it is then 422."""
    async with scratch_feedback(pg_url) as (client, _, db):
        shown = answered(await client.get("/admin/settings", headers=as_principal(OPERATOR)), 200)
        assert shown["feedback_tags"] == list(SEEDED_TAGS)
        for bad, field in (
            (["other", "other"], "body.feedback_tags"),
            (["Too_Long"], "body.feedback_tags[0]"),
            ([""], "body.feedback_tags[0]"),
            (["x" * 41], "body.feedback_tags[0]"),
            (["too long"], "body.feedback_tags[0]"),
            ([f"tag_{n}" for n in range(51)], "body.feedback_tags"),
            ("other", "body.feedback_tags"),
            (None, "body.feedback_tags"),
        ):
            status, code, details = error_of(await _settings(client, {"feedback_tags": bad}))
            assert (status, code, details["field"]) == (422, "validation_error", field), bad

        emptied = answered(await _settings(client, {"feedback_tags": []}), 200)
        assert emptied["feedback_tags"] == []
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        status, code, details = error_of(
            await rate(client, story, message, {"rating": 2, "tags": ["other"]})
        )
        assert (status, code, details["allowed_tags"]) == (422, "unknown_feedback_tag", [])

        tags = [*SEEDED_TAGS, "too_dark"]
        added = answered(await _settings(client, {"feedback_tags": tags}), 200)
        assert added["config_revision"] == emptied["config_revision"] + 1
        recorded = await _changes(db, "feedback_tags")
        assert [tuple(r) for r in recorded[-2:]] == [
            (list(SEEDED_TAGS), [], "prospective", OPERATOR),
            ([], tags, "prospective", OPERATOR),
        ]
        answered(
            await rate(client, story, message, {"rating": 2, "tags": ["too_dark", "other"]}), 201
        )

        refused = await _settings(client, {"feedback_tags": list(SEEDED_TAGS)})
        assert error_of(refused) == (
            409,
            "feedback_tag_in_use",
            {
                "field": "body.feedback_tags",
                "in_use": [{"tag": "too_dark", "current": 1, "history": 0}],
            },
        )
        after = answered(await client.get("/admin/settings", headers=as_principal(OPERATOR)), 200)
        assert (after["feedback_tags"], after["config_revision"]) == (
            tags,
            added["config_revision"],
        )
        assert len(await _changes(db, "feedback_tags")) == len(recorded)

        answered(await rate(client, story, message, {"rating": 2, "tags": ["other"]}), 200)
        refused = await _settings(
            client, {"feedback_tags": [t for t in tags if t not in ("other", "too_dark")]}
        )
        assert error_of(refused)[2]["in_use"] == [
            {"tag": "other", "current": 1, "history": 1},
            {"tag": "too_dark", "current": 0, "history": 1},
        ]
        trimmed = [t for t in tags if t not in ("too_short", "repetitive")]
        answered(await _settings(client, {"feedback_tags": trimmed}), 200)
        status, code, details = error_of(
            await rate(client, story, message, {"rating": 2, "tags": ["too_short"]})
        )
        assert (status, code, details["unknown_tags"]) == (
            422,
            "unknown_feedback_tag",
            ["too_short"],
        )


async def test_the_text_cap_setting(pg_url: str) -> None:
    """``feedback_text_max_chars`` (1 to 20000, default 2000) is a prospective setting that caps
    a rating's text once trimmed; it and ``feedback_retention`` survive a write of an unrelated
    setting."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        for bad in (0, 20_001, "ten", None, True):
            status, code, details = error_of(
                await _settings(client, {"feedback_text_max_chars": bad})
            )
            assert (status, code, details["field"]) == (
                422,
                "validation_error",
                "body.feedback_text_max_chars",
            ), bad
        answered(
            await _settings(client, {"feedback_text_max_chars": 10, "feedback_retention": 30}), 200
        )
        assert [tuple(r)[:3] for r in await _changes(db, "feedback_text_max_chars")] == [
            (2000, 10, "prospective")
        ]
        status, code, details = error_of(
            await rate(client, story, message, {"rating": 2, "text": "x" * 11})
        )
        assert (status, code, details["field"], details["max_chars"]) == (
            422,
            "validation_error",
            "body.text",
            10,
        )
        kept = answered(
            await rate(client, story, message, {"rating": 2, "text": "  " + "x" * 10 + "  "}), 201
        )
        assert kept["text"] == "x" * 10
        answered(await _settings(client, {"lite_summary_chunk": 12}), 200)
        shown = answered(await client.get("/admin/settings", headers=as_principal(OPERATOR)), 200)
        assert (shown["feedback_text_max_chars"], shown["feedback_retention"]) == (10, 30)


async def test_feedback_retention(pg_url: str) -> None:
    """Under the default ``story_lifetime`` the ``retention_sweep`` deletes no rating, however
    old. With ``feedback_retention`` set to 30 days it deletes a rating first given more than
    30 days ago (by ``created_at``, even one changed 10 days ago) and a history entry replaced
    more than 30 days ago, keeps the newer ones, and leaves no marker. The setting is a number
    of days from 1 to 3650 or ``story_lifetime``, and prospective."""
    async with scratch_feedback(pg_url) as (client, lib, db):
        played = await played_story(client)
        story = played.story_id
        later = await play(client, story, played.actor, "And then?")
        now = clock.now()
        old, recent = played.message_id, later["message"]["message_id"]

        async def rate_at(days: int, message: str, rating: int) -> None:
            with clock.frozen_at(now - timedelta(days=days)):
                await lib.rate_message(
                    RequestContext(principal=RATER), story, message, FeedbackRequest(rating=rating)
                )

        await rate_at(50, old, 1)  # first given 50 days ago
        await rate_at(40, old, 2)  # 1 replaced 40 days ago
        await rate_at(10, old, 3)  # 2 replaced 10 days ago: the rating changed recently
        await rate_at(20, recent, 4)  # first given 20 days ago
        await rate_at(5, recent, 5)  # 4 replaced 5 days ago

        swept = await retention.sweep_retention(db, now + timedelta(days=4000))
        assert (swept["feedback"], swept["feedback_history"]) == (0, 0)
        assert (len(await feedback_rows(db, story)), len(await history_rows(db, story))) == (2, 3)

        for bad in (0, 3651, "forever", True, 2.5, None):
            status, code, details = error_of(await _settings(client, {"feedback_retention": bad}))
            assert (status, code, details["field"]) == (
                422,
                "validation_error",
                "body.feedback_retention",
            ), bad
        answered(await _settings(client, {"feedback_retention": 30}), 200)
        assert [tuple(r)[:3] for r in await _changes(db, "feedback_retention")] == [
            ("story_lifetime", 30, "prospective")
        ]
        events = await count(db, "SELECT count(*) FROM events")
        swept = await retention.sweep_retention(db, now)
        assert (swept["feedback"], swept["feedback_history"]) == (1, 1)
        assert [r.message_id for r in await feedback_rows(db, story)] == [recent]
        assert [(r.message_id, r.rating) for r in await history_rows(db, story)] == [
            (old, 2),
            (recent, 4),
        ]
        assert await count(db, "SELECT count(*) FROM events") == events
        again = await retention.sweep_retention(db, now)
        assert (again["feedback"], again["feedback_history"]) == (0, 0)
        answered(await _settings(client, {"feedback_retention": "story_lifetime"}), 200)
        assert (await retention.sweep_retention(db, now + timedelta(days=4000)))["feedback"] == 0


# -- the story's life ------------------------------------------------------------------------------


async def test_deletion_takes_the_storys_feedback_and_forks_copy_none(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Deleting a story deletes its ratings and their history and nothing else: another story's
    stay. A fork copies no rating: the fork's copy of a rated reply is a new message, unrated,
    and the source's rating is unchanged."""
    async with respond_client(stub_settings, engine) as (client, _):
        doomed = await played_story(client)
        kept = await played_story(client)
        for played in (doomed, kept):
            answered(await rate(client, played.story_id, played.message_id, {"rating": 2}), 201)
            answered(await rate(client, played.story_id, played.message_id, {"rating": 3}), 200)
        forked = await client.post(
            f"/stories/{doomed.story_id}/fork", json={"at_turn": 1}, headers=as_principal(AUTHOR)
        )
        fork = answered(forked, 201)["story_id"]
        (copy,) = await rows(
            engine,
            "SELECT id FROM events WHERE story_id = :s AND type = 'message_rendered'",
            s=fork,
        )
        assert copy.id != doomed.message_id
        assert await feedback_rows(engine, fork) == []
        assert answered(await read(client, fork, copy.id), 200)["rating"] is None
        assert error_of(await read(client, fork, doomed.message_id))[:2] == (404, "not_found")

        deleted = await client.delete(f"/stories/{doomed.story_id}", headers=as_principal(AUTHOR))
        assert deleted.status_code == 204
        counted = "SELECT count(*) FROM {} WHERE story_id = :s"
        for table in TABLES:
            assert await count(engine, counted.format(table), s=doomed.story_id) == 0
            assert await count(engine, counted.format(table), s=kept.story_id) == 1
        assert answered(await read(client, kept.story_id, kept.message_id), 200)["rating"] == 3


# -- the seams -------------------------------------------------------------------------------------


async def test_the_subject_index_seams(
    stub_settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``index_hook`` is called in the write's transaction for every write that changes a
    rating, with the rating's record id (``<message_id>:<principal>``, the same across
    re-ratings), its story and its text (none included); ``drop_hook`` on a retraction. Neither
    runs for the same value again, a refusal, or nothing to retract. A hook that fails fails the
    write: nothing is stored."""
    seen: list[tuple[Any, ...]] = []

    async def index(conn: AsyncConnection, record_id: str, story_id: str, fields: Any) -> None:
        assert conn.in_transaction()
        seen.append(("index", record_id, story_id, dict(fields)))

    async def drop(conn: AsyncConnection, record_id: str) -> None:
        assert conn.in_transaction()
        seen.append(("drop", record_id))

    monkeypatch.setattr(feedback, "index_hook", index)
    monkeypatch.setattr(feedback, "drop_hook", drop)
    async with respond_client(stub_settings, engine) as (client, lib):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        record = feedback.record_id(message, RATER)
        assert record == f"{message}:{RATER}"
        answered(await rate(client, story, message, {"rating": 2, "text": "Wren is wrong."}), 201)
        answered(await rate(client, story, message, {"rating": 2, "text": "Wren is wrong."}), 200)
        answered(await rate(client, story, message, {"rating": 3}), 200)
        assert (
            error_of(await rate(client, story, message, {"rating": 3, "tags": ["dull"]}))[0] == 422
        )
        assert (await retract(client, story, message)).status_code == 204
        assert (await retract(client, story, message)).status_code == 204
        assert seen == [
            ("index", record, story, {"text": "Wren is wrong."}),
            ("index", record, story, {"text": None}),
            ("drop", record),
        ]

        async def failing(
            conn: AsyncConnection, record_id: str, story_id: str, fields: Any
        ) -> None:
            raise RuntimeError("the index is down")

        monkeypatch.setattr(feedback, "index_hook", failing)
        with pytest.raises(RuntimeError):
            await lib.rate_message(
                RequestContext(principal=OTHER_RATER), story, message, FeedbackRequest(rating=5)
            )
        assert [r.principal for r in await feedback_rows(engine, story)] == []


async def test_compaction_marks_ratings_and_keeps_them(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``mark_compacted`` (M7's hook) sets ``compacted_at`` once on every rating of the named
    messages of the story and keeps them: a second call marks nothing new, other messages and
    other stories are untouched, and the mark reads back."""
    async with respond_client(stub_settings, engine) as (client, _):
        played = await played_story(client)
        story, first = played.story_id, played.message_id
        second = (await play(client, story, played.actor, "And then?"))["message"]["message_id"]
        other = await played_story(client)
        for message, principal in ((first, RATER), (first, OTHER_RATER), (second, RATER)):
            answered(await rate(client, story, message, {"rating": 2}, principal), 201)
        answered(await rate(client, other.story_id, other.message_id, {"rating": 2}), 201)
        with clock.frozen_at(T0):
            async with engine.begin() as conn:
                assert await feedback.mark_compacted(conn, story, [first, other.message_id]) == 2
        async with engine.begin() as conn:
            assert await feedback.mark_compacted(conn, story, [first]) == 0
            assert await feedback.mark_compacted(conn, story, []) == 0
        marked = answered(await read(client, story, first), 200)
        assert (marked["rating"], marked["compacted_at"]) == (
            2,
            T0.isoformat().replace("+00:00", "Z"),
        )
        assert answered(await read(client, story, second), 200)["compacted_at"] is None
        assert (
            answered(await read(client, other.story_id, other.message_id), 200)["compacted_at"]
            is None
        )
        assert len(await feedback_rows(engine, story)) == 3


# -- concurrency -----------------------------------------------------------------------------------


async def _lock_waiters(db: AsyncEngine, wanted: int, within: float = 10.0) -> None:
    """Return once ``wanted`` backends of this database wait on a lock."""
    deadline = time.monotonic() + within
    waiting = (
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
    )
    while await count(db, waiting) < wanted:
        assert time.monotonic() < deadline, "no request waited on the lock in time"
        await asyncio.sleep(0.02)


async def test_concurrent_first_ratings_serialise(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Two first ratings of one message by one principal, both past their check that it has
    none, serialise on the unique key: one creates the rating (201), the other replaces it
    (200), and the history keeps the first value. One rating results, never two."""
    async with respond_client(stub_settings, engine) as (client, _):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        with pauses.held(pauses.FEEDBACK_RATE_UNRATED) as hold:
            one = asyncio.ensure_future(rate(client, story, message, {"rating": 1}))
            two = asyncio.ensure_future(rate(client, story, message, {"rating": 5}))
            await hold.arrived(2)
            hold.release()
            answers = await asyncio.gather(one, two)
        created = next(a for a in answers if a.status_code == 201).json()
        replaced = next(a for a in answers if a.status_code == 200).json()
        assert sorted(a.status_code for a in answers) == [200, 201]
        (row,) = await feedback_rows(engine, story)
        (entry,) = await history_rows(engine, story)
        assert row.rating == replaced["rating"] != created["rating"]
        assert (entry.rating, entry.kind) == (created["rating"], "replaced")


async def _commit_first_rating(
    db: AsyncEngine, story_id: str, message_id: str, principal: str, rating: int
) -> None:
    """Commit ``principal``'s first rating of the message straight to its table, as a concurrent
    request would: that request's own path stops at a held ``feedback.rate.unrated``."""
    (event,) = await rows(
        db,
        "SELECT narrative_turn, turn_variant, operation_id FROM events WHERE id = :m",
        m=message_id,
    )
    now = clock.now()
    async with db.begin() as conn:
        await conn.execute(
            insert(message_feedback).values(
                id=new_ulid(),
                story_id=story_id,
                message_id=message_id,
                turn=event.narrative_turn,
                variant=event.turn_variant,
                principal=principal,
                rating=rating,
                operation_id=event.operation_id,
                created_at=now,
                updated_at=now,
            )
        )


async def test_a_first_rating_outlasts_the_retraction_of_the_one_it_lost_to(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A first rating past its check that the principal has none loses its insert to the
    principal's concurrent first rating, which the principal's retraction holds and then
    removes (a double submit, then a retract). It waits for the retraction and then creates its
    rating (201), as if it ran after both, with the retracted value in its history: never 200
    with ``rating`` null and nothing written."""
    async with respond_client(stub_settings, engine) as (client, _):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        with (
            pauses.held(pauses.FEEDBACK_RATE_UNRATED) as rating_hold,
            pauses.held(pauses.FEEDBACK_RETRACT_LOCKED) as retraction_hold,
        ):
            late = asyncio.ensure_future(rate(client, story, message, {"rating": 5}))
            await rating_hold.arrived(1)
            await _commit_first_rating(engine, story, message, RATER, 1)
            retraction = asyncio.ensure_future(retract(client, story, message))
            await retraction_hold.arrived(1)
            rating_hold.release()
            await _lock_waiters(engine, 1)
            retraction_hold.release()
            rated, retracted = await asyncio.gather(late, retraction)
        assert retracted.status_code == 204, retracted.text
        created = answered(rated, 201)
        assert created["rating"] == 5
        assert [(h["rating"], h["kind"]) for h in created["history"]] == [(1, "retracted")]
        (row,) = await feedback_rows(engine, story)
        (entry,) = await history_rows(engine, story)
        assert (row.principal, row.rating) == (RATER, 5)
        assert (entry.principal, entry.rating, entry.kind) == (RATER, 1, "retracted")


async def test_a_story_deletion_waits_for_a_rating(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """A rating holds its story's row ``FOR KEY SHARE`` until it commits: a deletion of the
    story meanwhile waits for it, then deletes the rating with the story. No rating outlives
    its story, and the rating does not fail on the vanished story."""
    async with respond_client(stub_settings, engine) as (client, _):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        with pauses.held(pauses.FEEDBACK_RATE_UNRATED) as hold:
            rating = asyncio.ensure_future(rate(client, story, message, {"rating": 2}))
            await hold.arrived(1)
            deletion = asyncio.ensure_future(
                client.delete(f"/stories/{story}", headers=as_principal(AUTHOR))
            )
            await _lock_waiters(engine, 1)
            hold.release()
            rated, deleted = await asyncio.gather(rating, deletion)
        assert answered(rated, 201)["rating"] == 2
        assert deleted.status_code == 204, deleted.text
        for table in TABLES:
            counted = f"SELECT count(*) FROM {table} WHERE story_id = :s"  # noqa: S608
            assert await count(engine, counted, s=story) == 0


async def test_a_tag_is_never_removed_while_a_rating_carries_it(pg_url: str) -> None:
    """A rating reads the deployment's tags with its row held ``FOR SHARE`` until it commits:
    a ``feedback_tags`` write removing its tag meanwhile waits for it, then finds the tag
    carried (409 ``feedback_tag_in_use``). And a rating waiting on such a write reads the tags
    the write left: its removed tag is 422 ``unknown_feedback_tag``, with nothing written."""
    async with scratch_feedback(pg_url) as (client, _, db):
        tags = [*SEEDED_TAGS, "too_dark"]
        answered(await _settings(client, {"feedback_tags": tags}), 200)
        played = await played_story(client)
        story, message = played.story_id, played.message_id

        with pauses.held(pauses.FEEDBACK_RATE_UNRATED) as hold:
            rating = asyncio.ensure_future(
                rate(client, story, message, {"rating": 2, "tags": ["too_dark"]})
            )
            await hold.arrived(1)
            removal = asyncio.ensure_future(_settings(client, {"feedback_tags": list(SEEDED_TAGS)}))
            await _lock_waiters(db, 1)
            hold.release()
            rated, removed = await asyncio.gather(rating, removal)
        assert answered(rated, 201)["tags"] == ["too_dark"]
        assert error_of(removed)[:2] == (409, "feedback_tag_in_use")

        async with db.connect() as conn:
            async with conn.begin():
                await lock_deployment(conn)
                waiting = asyncio.ensure_future(
                    rate(client, story, message, {"rating": 4, "tags": ["too_dark"]}, OTHER_RATER)
                )
                await _lock_waiters(db, 1)
                await conn.execute(
                    update(deployment_state)
                    .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
                    .values(feedback_tags=list(SEEDED_TAGS))
                )
            refused = await waiting
        status, code, details = error_of(refused)
        assert (status, code, details["unknown_tags"]) == (
            422,
            "unknown_feedback_tag",
            ["too_dark"],
        )
        assert [r.principal for r in await feedback_rows(db, story)] == [RATER]


# -- nothing else reads feedback -------------------------------------------------------------------

ALLOWED: dict[str, str] = {
    "core/feedback.py": "the module that writes and reads ratings (#112)",
    "core/tables/feedback.py": "the tables",
    "core/stories.py": "story deletion enumerates them; forks leave them behind (#50, #67)",
    "orchestrator/jobs/handlers/retention.py": "the retention sweep expires them (#71's sweep)",
    "testing/determinism.py": "the determinism comparator orders every story table (#75)",
}
"""The modules that may name the feedback tables, and why. Later stories add theirs: #126's
reporting and export, #124's erasure, #123's subject-index registry, #128's route table."""


def naming(root: Path) -> set[str]:
    """Every module under ``root`` whose text names one of the feedback tables."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if any(table in path.read_text(encoding="utf-8") for table in TABLES)
    }


def test_only_the_listed_modules_name_the_feedback_tables(tmp_path: Path) -> None:
    """Feedback is an audit record no story view, prompt or retrieval reads: no module under
    ``src/`` names its tables but those ``ALLOWED`` lists, so a reader cannot appear
    unnoticed. Complement: the scan finds a planted reader."""
    assert naming(SRC) == set(ALLOWED)
    view = tmp_path / "core" / "views.py"
    view.parent.mkdir()
    view.write_text('QUERY = "SELECT text FROM message_feedback"\n', encoding="utf-8")
    (tmp_path / "core" / "plain.py").write_text("X = 1\n", encoding="utf-8")
    assert naming(tmp_path) == {"core/views.py"}
