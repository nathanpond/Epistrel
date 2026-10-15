"""Feedback reporting and export (#126) beyond the C-26 and C-27 parts.

- The signals against the timeline: a regeneration and the variant it replaced, a swap back, a
  rewind, a turn written over a rewound tail (no regeneration) and an untruncate that swaps
  back.
- The context dimensions (fingerprint, content rating, scenario), the group cap, and the
  requests' refusals.
- The review lists' pages, cursors, filters and refusals, and a blocked story left out.
- The export's filters, window and refusals, its replay under an ``Idempotency-Key``, the log
  (record, list, pages), deletion, the seven-day expiry, an erasure (which rewrites only an
  export not yet downloaded), a story's deletion (the record stays), the counter, and the blob
  store's object API on the local store and on S3.

The parts are in ``tests/conformance/test_c_26.py``, ``test_c_27.py`` and ``test_c_09.py``.
Every test runs on its own scratch database (``scratch_feedback``): they change the deployment's
feedback settings or age rows.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from epistrel.core import clock, feedback_export, feedback_reporting
from epistrel.core.media.s3 import S3BlobStore
from epistrel.core.media.store import LocalFilesystemStore, check_object_key
from epistrel.core.media.sweep import EXPORT_TTL, sweep
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.tables.operations import REGENERATED_VARIANT
from tests.conftest import S3Endpoint
from tests.event_support import rows, scalar
from tests.feedback_support import (
    OPERATOR,
    OTHER_RATER,
    aggregates,
    answered,
    as_principal,
    cases_of,
    error_of,
    export,
    export_content,
    export_rows,
    feedback_rows,
    groups_of,
    play,
    played_story,
    rate,
    regenerate,
    review,
    scratch_feedback,
    set_deployment,
)
from tests.integration.test_media_delivery import new_bucket
from tests.model_support import config_key

AUTHOR = "p-author"


async def _post(client: Any, path: str, payload: Any) -> dict[str, Any]:
    response = await client.post(path, json=payload, headers=as_principal(AUTHOR))
    return answered(response, 200)


# -- the signals --------------------------------------------------------------------------------


@pytest.mark.integration
async def test_signals_follow_the_timeline(pg_url: str) -> None:
    """Each rated message (one reason tag each, so ``group_by=tag`` reads per message):

    - turn 1's first variant, rated 1 and regenerated at once: one regeneration, one
      ``regen_after_low_rating``;
    - turn 1's regenerated variant, then swapped away for the first one: one swap back;
    - turn 2's first reply, rated 2 and rewound (truncated to turn 1): one rewind, and no
      regeneration when turn 2 is played again over the rewound tail (its variant is 2);
    - that second turn 2, swapped away by the untruncate that reactivates the first: a swap
      back.

    Complement asserted: the regenerate's operation names the variant it replaced, and the turn
    played over the rewound tail names none."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        story, actor = played.story_id, played.actor
        first = played.message_id
        answered(await rate(client, story, first, {"rating": 1, "tags": ["too_long"]}), 201)
        regenerated = (await regenerate(client, story))["message"]["message_id"]
        answered(await rate(client, story, regenerated, {"rating": 4, "tags": ["too_short"]}), 201)
        await _post(
            client, f"/stories/{story}/swap", {"to_variant_chain": {"turn": 1, "variant": 1}}
        )

        rewound = (await play(client, story, actor, "And then?"))["message"]["message_id"]
        answered(await rate(client, story, rewound, {"rating": 2, "tags": ["repetitive"]}), 201)
        await _post(client, f"/stories/{story}/truncate", {"to_turn": 1})
        over = await play(client, story, actor, "Something else, then.")
        assert (over["turn"], over["variant"]) == (2, 2)
        answered(
            await rate(
                client, story, over["message"]["message_id"], {"rating": 3, "tags": ["other"]}
            ),
            201,
        )
        await _post(client, f"/stories/{story}/untruncate", {})

        by_tag = groups_of(await aggregates(client, "tag", story_id=story))
        signals = {tag: group["signals"] for tag, group in by_tag.items()}
        assert signals == {
            "too_long": {
                "regenerations": 1,
                "regen_after_low_rating": 1,
                "swaps_back": 0,
                "rewinds": 0,
            },
            "too_short": {
                "regenerations": 0,
                "regen_after_low_rating": 0,
                "swaps_back": 1,
                "rewinds": 0,
            },
            "repetitive": {
                "regenerations": 0,
                "regen_after_low_rating": 0,
                "swaps_back": 0,
                "rewinds": 1,
            },
            "other": {
                "regenerations": 0,
                "regen_after_low_rating": 0,
                "swaps_back": 1,
                "rewinds": 0,
            },
        }
        reports = await rows(
            db,
            "SELECT narrative_turn, turn_variant, turn_report FROM operations "
            "WHERE story_id = :s AND kind = 'turn' AND status = 'committed' "
            "ORDER BY narrative_turn, turn_variant",
            s=story,
        )
        marked = {
            (r.narrative_turn, r.turn_variant): (r.turn_report or {}).get(REGENERATED_VARIANT)
            for r in reports
        }
        assert marked == {(1, 1): None, (1, 2): 1, (2, 1): None, (2, 2): None}


# -- the dimensions and the requests --------------------------------------------------------------


@pytest.mark.integration
async def test_context_dimensions_and_the_cap(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fingerprint`` groups by the rating's recorded fingerprint, ``content_rating`` by the
    content rating its recorded version belongs to, ``scenario`` by the story's scenario (null
    for a blank story); a rating whose message recorded no stamp falls under ``null``. At most
    ``MAX_GROUPS`` groups are answered, the largest first, with ``truncated``.

    Complement asserted: under the cap, ``truncated`` is false."""
    async with scratch_feedback(pg_url) as (client, _, db):
        one = await played_story(client)
        two = await played_story(client)
        answered(await rate(client, one.story_id, one.message_id, {"rating": 2}), 201)
        answered(await rate(client, one.story_id, one.message_id, {"rating": 4}, OTHER_RATER), 201)
        answered(await rate(client, two.story_id, two.message_id, {"rating": 5}), 201)
        stored = await feedback_rows(db, one.story_id) + await feedback_rows(db, two.story_id)

        fingerprints = groups_of(await aggregates(client, "fingerprint"))
        assert {key: group["count"] for key, group in fingerprints.items()} == dict(
            Counter(r.config_fingerprint for r in stored)
        )
        assert None not in fingerprints
        rating_id = await scalar(
            db,
            "SELECT rating_id FROM content_rating_versions WHERE version_id = :v",
            v=stored[0].rating_version_id,
        )
        ratings = await aggregates(client, "content_rating")
        assert [(g["key"], g["count"]) for g in ratings["groups"]] == [(rating_id, 3)]
        scenarios = await aggregates(client, "scenario")
        assert [(g["key"], g["count"], g["messages"]) for g in scenarios["groups"]] == [
            (None, 3, 2)
        ]
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE message_feedback SET profile_version = NULL WHERE story_id = :s"),
                {"s": two.story_id},
            )
        profiles = await aggregates(client, "profile_version")
        assert [(g["key"], g["count"]) for g in profiles["groups"]] == [
            (f"{LITE_PRESET_ID}@1", 2),
            (None, 1),
        ]
        assert profiles["truncated"] is False

        monkeypatch.setattr(feedback_reporting, "MAX_GROUPS", 1)
        capped = await aggregates(client, "profile_version")
        assert [(g["key"], g["count"]) for g in capped["groups"]] == [(f"{LITE_PRESET_ID}@1", 2)]
        assert capped["truncated"] is True
        assert capped["totals"]["count"] == 3


@pytest.mark.integration
async def test_report_requests_are_checked(pg_url: str) -> None:
    """``group_by`` is required and one of the seven dimensions, ``from`` must not be after
    ``to``, an unknown story is 404, and the window bounds when a rating was first given:
    ``from`` inclusive, ``to`` exclusive. A review list's ``tag`` must be one of its tags.

    Complement asserted: a window starting at the rating's time includes it; one ending
    there leaves it out."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        answered(await rate(client, played.story_id, played.message_id, {"rating": 3}), 201)
        (row,) = await feedback_rows(db, played.story_id)
        at = row.created_at.isoformat()

        missing = await client.get("/feedback/aggregates", headers=as_principal(None))
        assert error_of(missing)[:2] == (422, "validation_error")
        unknown = await client.get(
            "/feedback/aggregates", params={"group_by": "colour"}, headers=as_principal(None)
        )
        assert error_of(unknown)[:2] == (422, "validation_error")
        backwards = await client.get(
            "/feedback/aggregates",
            params={
                "group_by": "day",
                "from": at,
                "to": row.created_at.replace(year=2000).isoformat(),
            },
            headers=as_principal(None),
        )
        status, code, details = error_of(backwards)
        assert (status, code, details["field"]) == (422, "validation_error", "query.from")
        absent = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
        for response in (
            await client.get(
                "/feedback/aggregates",
                params={"group_by": "day", "story_id": absent},
                headers=as_principal(None),
            ),
            await review(client, "memory", story_id=absent),
        ):
            assert error_of(response)[:2] == (404, "not_found")

        included = await aggregates(client, "day", from_=at)
        excluded = await aggregates(client, "day", to=at)
        assert (included["totals"]["count"], excluded["totals"]["count"]) == (1, 0)
        assert included["window"]["from"] is not None

        # Only the RFC 3339 form: a number of seconds is refused (C-01 found it accepted).
        numeric = await review(client, "guardrail", to="0.5")
        status, code, details = error_of(numeric)
        assert (status, code, details["field"]) == (422, "validation_error", "query.to")
        assert error_of(await export(client, {"to": 0.5}))[:2] == (422, "validation_error")

        off_list = await review(client, "guardrail", tag="factual_error")
        status, code, details = error_of(off_list)
        assert (status, code, details["field"]) == (422, "validation_error", "query.tag")


@pytest.mark.integration
async def test_review_pages_and_filters(pg_url: str) -> None:
    """A review list pages newest rating first, ``limit`` at a time, and its ``next_cursor``
    continues it; a cursor given for other filters (or the other list) is 422
    ``cursor_invalid``; ``tag`` narrows the memory list to one of its tags, and ``story_id`` to
    one story. A story blocked by a pending erasure is left out of both lists and of exports,
    and its aggregate with a story filter is 503 ``erasure_pending``.

    Complement asserted: the pages together hold every item once; once the story is no longer
    blocked its ratings are listed again."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        story = played.story_id
        messages = [played.message_id]
        for line in ("Where now?", "Who goes there?"):
            messages.append(
                (await play(client, story, played.actor, line))["message"]["message_id"]
            )
        tags = ["factual_error", "forgot_something", "factual_error"]
        for message_id, tag in zip(messages, tags, strict=True):
            answered(await rate(client, story, message_id, {"rating": 2, "tags": [tag]}), 201)
        other = await played_story(client)
        answered(
            await rate(
                client, other.story_id, other.message_id, {"rating": 1, "tags": ["factual_error"]}
            ),
            201,
        )

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 1, "story_id": story}
            if cursor is not None:
                params["cursor"] = cursor
            page = answered(await review(client, "memory", **params), 200)
            seen += [item["message_id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == list(reversed(messages))
        first = answered(await review(client, "memory", limit=1, story_id=story), 200)
        stale = await review(client, "memory", limit=1, cursor=first["next_cursor"])
        assert error_of(stale)[:2] == (422, "cursor_invalid")
        crossed = await review(client, "guardrail", cursor=first["next_cursor"], story_id=story)
        assert error_of(crossed)[:2] == (422, "cursor_invalid")
        narrowed = answered(await review(client, "memory", tag="forgot_something"), 200)
        assert [item["message_id"] for item in narrowed["items"]] == [messages[1]]

        await set_deployment(client, feedback_export=True)
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE stories SET status = 'blocked_by_erasure', "
                    "status_before_block = 'active' WHERE id = :s"
                ),
                {"s": story},
            )
        listed = answered(await review(client, "memory"), 200)
        assert [item["story_id"] for item in listed["items"]] == [other.story_id]
        written = answered(await export(client), 201)
        assert written["case_count"] == 1
        assert [
            c["position"]["story_id"]
            for c in cases_of(await export_content(client, written["export_id"]))
        ] == [other.story_id]
        held = await client.get(
            "/feedback/aggregates",
            params={"group_by": "day", "story_id": story},
            headers=as_principal(None),
        )
        assert error_of(held)[:2] == (503, "erasure_pending")
        assert (await aggregates(client, "day"))["totals"]["count"] == 4

        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE stories SET status = 'active', status_before_block = NULL WHERE id = :s"
                ),
                {"s": story},
            )
        again = answered(await review(client, "memory"), 200)
        assert len(again["items"]) == 4


# -- the export -----------------------------------------------------------------------------------


@pytest.mark.integration
async def test_export_filters_and_refusals(pg_url: str) -> None:
    """An export's window and filters (story, tags, rating range, fingerprint, profile version)
    select the current ratings it takes, and its record keeps them. A dormant variant's rating
    and an archived story's are exported. The request is checked before the switch: a body with
    an unknown field, ``min_rating`` above ``max_rating`` or a window ending before it starts
    is 422 whether exports are on or off.

    Complement asserted: each filter alone leaves out what it should and keeps the rest."""
    async with scratch_feedback(pg_url) as (client, _, db):
        played = await played_story(client)
        story = played.story_id
        answered(
            await rate(client, story, played.message_id, {"rating": 1, "tags": ["too_long"]}), 201
        )
        newer = (await regenerate(client, story))["message"]["message_id"]
        answered(await rate(client, story, newer, {"rating": 4, "tags": ["repetitive"]}), 201)
        other = await played_story(client)
        answered(await rate(client, other.story_id, other.message_id, {"rating": 5}), 201)
        archived = await client.post(
            f"/stories/{other.story_id}/archive", headers=as_principal(AUTHOR)
        )
        assert archived.status_code == 200, archived.text

        for body in (
            {"filters": {"min_rating": 4, "max_rating": 2}},
            {"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
            {"filters": {"colour": "red"}},
            {"extra": True},
        ):
            assert error_of(await export(client, body))[:2] == (422, "validation_error"), body
        assert error_of(await export(client))[:2] == (403, "feedback_export_disabled")
        await set_deployment(client, feedback_export=True)
        for body in ({"filters": {"min_rating": 4, "max_rating": 2}},):
            status, code, details = error_of(await export(client, body))
            assert (status, code, details["field"]) == (
                422,
                "validation_error",
                "body.filters.min_rating",
            )

        (row,) = [r for r in await feedback_rows(db, story) if r.message_id == newer]

        async def exported(body: dict[str, Any]) -> list[str]:
            written = answered(await export(client, body), 201)
            return sorted(
                c["message_id"]
                for c in cases_of(await export_content(client, written["export_id"]))
            )

        everything = sorted([played.message_id, newer, other.message_id])
        assert await exported({}) == everything
        assert await exported({"filters": {"story_id": story}}) == sorted(
            [played.message_id, newer]
        )
        assert await exported({"filters": {"tags": ["repetitive", "too_long"]}}) == sorted(
            [played.message_id, newer]
        )
        assert await exported({"filters": {"min_rating": 4}}) == sorted([newer, other.message_id])
        assert await exported({"filters": {"max_rating": 1}}) == [played.message_id]
        assert await exported(
            {"filters": {"config_fingerprint": row.config_fingerprint}}
        ) == sorted(
            r.message_id
            for r in await feedback_rows(db, story) + await feedback_rows(db, other.story_id)
            if r.config_fingerprint == row.config_fingerprint
        )
        assert await exported({"filters": {"profile_version": "nobody@1"}}) == []
        future = (clock.now() + timedelta(days=1)).isoformat()
        assert await exported({"from": future}) == []
        assert await exported({"to": future}) == everything

        logged = await export_rows(db)
        assert [r.filters for r in logged[:3]] == [
            {},
            {"story_id": story},
            {"tags": ["repetitive", "too_long"]},
        ]
        assert logged[-1].window_to is not None


@pytest.mark.integration
async def test_export_replay_log_and_deletion(pg_url: str) -> None:
    """A retry with the same ``Idempotency-Key`` answers the stored export and writes no second
    file or record. The log lists exports newest first in pages. Deleting an export removes its
    file and keeps its record, marked with who deleted it and when; its content is then 404
    (``reason: deleted``), and deleting it again is 204. An unknown export is 404 everywhere.
    The ``epistrel_feedback_exports_total`` counter counts each export written.

    Complement asserted: the file exists before the deletion and not after; a new key writes
    a new export."""
    async with scratch_feedback(pg_url) as (client, lib, db):
        played = await played_story(client)
        answered(await rate(client, played.story_id, played.message_id, {"rating": 3}), 201)
        await set_deployment(client, feedback_export=True)
        before = REGISTRY.get_sample_value("epistrel_feedback_exports_total") or 0.0
        first = await export(client, {}, **{"Idempotency-Key": "export-once"})
        again = await export(client, {}, **{"Idempotency-Key": "export-once"})
        assert answered(first, 201)["export_id"] == answered(again, 201)["export_id"]
        assert again.headers.get("Idempotent-Replay") == "true"
        assert len(await export_rows(db)) == 1
        conflict = await export(
            client, {"filters": {"min_rating": 2}}, **{"Idempotency-Key": "export-once"}
        )
        assert error_of(conflict)[:2] == (409, "idempotency_conflict")
        second = answered(await export(client), 201)
        assert (REGISTRY.get_sample_value("epistrel_feedback_exports_total") or 0.0) - before == 2

        page = answered(
            await client.get(
                "/admin/feedback/exports", params={"limit": 1}, headers=as_principal(None)
            ),
            200,
        )
        assert [i["export_id"] for i in page["items"]] == [second["export_id"]]
        rest = answered(
            await client.get(
                "/admin/feedback/exports",
                params={"limit": 1, "cursor": page["next_cursor"]},
                headers=as_principal(None),
            ),
            200,
        )
        assert [i["export_id"] for i in rest["items"]] == [answered(first, 201)["export_id"]]
        assert rest["next_cursor"] is None

        export_id = second["export_id"]
        path = lib.media.root / "exports" / f"{export_id}.jsonl"
        assert path.is_file()
        deleted = await client.delete(
            f"/admin/feedback/exports/{export_id}", headers=as_principal(OPERATOR)
        )
        assert deleted.status_code == 204
        assert not path.exists()
        record = answered(
            await client.get(f"/admin/feedback/exports/{export_id}", headers=as_principal(None)),
            200,
        )
        assert (record["deleted_by"], record["deleted_at"] is not None) == (OPERATOR, True)
        status, code, details = error_of(await export_content(client, export_id))
        assert (status, code, details) == (
            404,
            "not_found",
            {"export_id": export_id, "reason": "deleted"},
        )
        again_deleted = await client.delete(
            f"/admin/feedback/exports/{export_id}", headers=as_principal(OPERATOR)
        )
        assert again_deleted.status_code == 204

        absent = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C3"
        for response in (
            await client.get(f"/admin/feedback/exports/{absent}", headers=as_principal(None)),
            await export_content(client, absent),
            await client.delete(
                f"/admin/feedback/exports/{absent}", headers=as_principal(OPERATOR)
            ),
        ):
            assert error_of(response) == (404, "not_found", {"export_id": absent})


@pytest.mark.integration
async def test_exports_expire_after_seven_days(pg_url: str) -> None:
    """The media sweep deletes an export's file seven days after it was written and marks its
    record (``deleted_by: system``); a younger export is kept. Deleting the story an export
    covered keeps the record.

    Complement asserted: a second sweep expires nothing more."""
    async with scratch_feedback(pg_url) as (client, lib, db):
        played = await played_story(client)
        answered(await rate(client, played.story_id, played.message_id, {"rating": 3}), 201)
        await set_deployment(client, feedback_export=True)
        old = answered(await export(client), 201)["export_id"]
        young = answered(await export(client), 201)["export_id"]
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE feedback_export_log SET at = at - :age WHERE id = :e"),
                {"age": EXPORT_TTL + timedelta(minutes=1), "e": old},
            )
        counts = await sweep(db, lib.media)
        assert counts["exports_expired"] == 1
        assert (await sweep(db, lib.media))["exports_expired"] == 0
        assert not (lib.media.root / "exports" / f"{old}.jsonl").exists()
        assert (lib.media.root / "exports" / f"{young}.jsonl").is_file()
        records = {r.id: r for r in await export_rows(db)}
        assert (records[old].deleted_by, records[young].deleted_at) == ("system", None)

        deleted = await client.delete(f"/stories/{played.story_id}", headers=as_principal(AUTHOR))
        assert deleted.status_code == 204, deleted.text
        assert {r.id for r in await export_rows(db)} == {old, young}
        assert await feedback_rows(db, played.story_id) == []


@pytest.mark.integration
async def test_erasure_rewrites_only_exports_not_yet_downloaded(
    pg_url: str, tmp_path: Path
) -> None:
    """An erasure of a character redacts the character's name in the cases of an export nobody
    has downloaded yet (the file is rewritten, and its record's size, checksum and
    ``redacted_at`` follow), and counts them; an export already downloaded is delivered, outside
    the erasure boundary, and stays as it was. A dry run counts without writing.

    Complement asserted: a case of another story keeps the name in the same file."""
    async with scratch_feedback(pg_url, config_key=config_key(), erasure_ledger_dir=tmp_path) as (
        client,
        lib,
        db,
    ):
        played = await played_story(client)
        story = played.story_id
        named = next(name for name in played.cast if played.cast[name] != played.actor)
        text_of = f"{named} would never say that."
        answered(await rate(client, story, played.message_id, {"rating": 1, "text": text_of}), 201)
        bystander = await played_story(client)
        answered(
            await rate(
                client, bystander.story_id, bystander.message_id, {"rating": 2, "text": text_of}
            ),
            201,
        )
        await set_deployment(client, feedback_export=True)
        delivered = answered(await export(client), 201)["export_id"]
        kept = (await export_content(client, delivered)).content
        pending = answered(await export(client), 201)["export_id"]

        request = {
            "subject": {"kind": "character", "id": played.cast[named]},
            "scope": {"story": story},
        }
        dry = answered(
            await client.post(
                "/admin/erasure", json={**request, "dry_run": True}, headers=as_principal(OPERATOR)
            ),
            200,
        )
        assert dry["records"].get(feedback_export.ERASURE_TARGET) == 1
        erased = answered(
            await client.post("/admin/erasure", json=request, headers=as_principal(OPERATOR)), 200
        )
        assert erased["records"].get(feedback_export.ERASURE_TARGET) == 1

        record = {r.id: r for r in await export_rows(db)}[pending]
        rewritten = await export_content(client, pending)
        cases = {c["position"]["story_id"]: c for c in cases_of(rewritten)}
        assert cases[story]["text"] == "[erased] would never say that."
        assert cases[bystander.story_id]["text"] == text_of
        assert (record.bytes, record.sha256) == (
            len(rewritten.content),
            hashlib.sha256(rewritten.content).hexdigest(),
        )
        assert record.redacted_at is not None
        assert (await export_content(client, delivered)).content == kept
        assert {r.id: r for r in await export_rows(db)}[delivered].redacted_at is None
        assert lib.media is feedback_export.ERASURE.store


# -- the blob store's objects ---------------------------------------------------------------------


def _objects(store: Any) -> None:
    key = "exports/01J9Z8Q4N5V6W7X8Y9Z0A1B2C3.jsonl"
    store.put_object(key, b'{"a":1}\n', content_type=feedback_export.MEDIA_TYPE)
    assert store.open_object(key).read() == b'{"a":1}\n'
    store.put_object(key, b"", content_type=feedback_export.MEDIA_TYPE)
    assert store.open_object(key).read() == b""
    assert store.delete_object(key) is True
    assert store.delete_object(key) is False
    with pytest.raises(FileNotFoundError):
        store.open_object(key)
    for bad in ("exports/../x.jsonl", "uploads/01J9Z8Q4N5V6W7X8Y9Z0A1B2C3.jsonl", "exports/x"):
        with pytest.raises(ValueError, match="object key"):
            store.put_object(bad, b"", content_type="text/plain")
    assert list(store.list_checksums()) == []


def test_the_local_store_keeps_objects(tmp_path: Path) -> None:
    """The local store writes an object whole (replacing it), reads and deletes it, refuses any
    key but an export's, and never lists it as a blob."""
    store = LocalFilesystemStore(tmp_path / "media")
    store.prepare()
    _objects(store)
    assert check_object_key("exports/01J9Z8Q4N5V6W7X8Y9Z0A1B2C3.jsonl")


@pytest.mark.integration
def test_the_s3_store_keeps_objects(tmp_path: Path, minio: S3Endpoint) -> None:
    """The S3 store does the same in its bucket."""
    store = S3BlobStore(
        tmp_path / "media",
        bucket=new_bucket(minio),
        endpoint=minio.url,
        access_key=minio.access_key,
        secret_key=minio.secret_key,
        path_style=True,
    )
    store.prepare()
    _objects(store)


async def test_a_message_counts_once_however_many_low_ratings(pg_url: str) -> None:
    """``regen_after_low_rating`` counts rated messages, like every other signal (#345): two
    principals rate one message 1 and 2 and it is regenerated once, so the group has one
    regeneration and one regeneration after a low rating, from two ratings of one message.

    Complement asserted: the signal never exceeds ``regenerations`` (the counts are 1 and 1,
    not 1 and 2)."""
    async with scratch_feedback(pg_url) as (client, _, _db):
        played = await played_story(client)
        story, message = played.story_id, played.message_id
        answered(await rate(client, story, message, {"rating": 1}), 201)
        answered(await rate(client, story, message, {"rating": 2}, principal="p-second"), 201)
        await regenerate(client, story)

        totals = (await aggregates(client, "profile_version", story_id=story))["totals"]
        assert (totals["count"], totals["messages"]) == (2, 1)
        assert totals["signals"]["regenerations"] == 1
        assert totals["signals"]["regen_after_low_rating"] == 1
