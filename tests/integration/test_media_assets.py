"""Media assets in detail (#105): uploads and their refusals, the moderation hook, reads,
retirement, listing, the subject-reference index, the media settings, the schema, and the media
sweep. The milestone parts are ``test_c_15__media_upload`` (C-15), ``test_i_17__media_sweep``
(I-17) and ``test_i_21__media_retire`` (I-21); the building blocks without a database are in
``tests/test_media_units.py``.

The sweep's tests each run on a scratch database with its own media root, since a sweep purges
deployment-wide. Until #115 gives catalog drafts and versions their image fields, a reference is
planted the way #115 will store it (``public_card.images``), straight into the draft or a
version row."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel import MediaRetired, MediaUpload, RatingFields, RelationRequest, RequestContext
from epistrel.config import Settings
from epistrel.core import clock
from epistrel.core.ids import new_ulid
from epistrel.core.media import assets as media
from epistrel.core.media.signing import CLOCK_SKEW_S, moderation_secret
from epistrel.core.media.sweep import sweep
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.tables.media import MediaState
from epistrel.core.tables.ownership import EntityType
from epistrel.orchestrator.jobs.handlers import media_sweep as media_sweep_job
from epistrel.orchestrator.jobs.recurring import NoPayload
from epistrel.orchestrator.jobs.types import ClaimedJob, JobContext
from tests.auth_support import MEDIA_ROOT, auth_settings
from tests.catalog_support import character as catalog_character
from tests.event_support import rows, scalar
from tests.media_support import (
    READ,
    WRITE,
    Scratch,
    blob_path,
    count,
    files,
    hook_server,
    jpeg,
    media_client,
    metadata,
    mp4,
    png,
    refused_url,
    scratch,
    set_media,
    sha256,
    staged_files,
    upload,
    uploaded,
    wait_for,
)
from tests.model_support import CONFIG_KEY, config_key

AUTHOR = RequestContext(principal="p-media")


@pytest.fixture
def keyed(pg_url: str) -> Settings:
    """Settings with ``EPISTREL_CONFIG_KEY``, so the moderation hook can be signed."""
    return auth_settings(pg_url, config_key=config_key())


@pytest.fixture
async def defaults(settings: Settings, engine: AsyncEngine) -> Any:
    """Restore the media settings a test changes (the database is shared)."""
    yield
    async with media_client(settings, engine) as client:
        await set_media(
            client,
            media_allowed_types=["image/png", "image/jpeg", "image/webp", "video/mp4"],
            media_max_bytes=50 * 1024 * 1024,
            media_moderation_webhook_url=None,
            media_moderation_timeout_ms=5000,
            public_base_url=None,
        )


def _error(response: Any) -> tuple[int, str, Any]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def _nothing_stored(engine: AsyncEngine, before: int, data: bytes) -> None:
    """No row, no blob and no staged file remains of a refused upload of ``data``."""
    assert await count(engine, "SELECT count(*) FROM media_assets") == before
    assert not blob_path(MEDIA_ROOT, sha256(data)).exists()
    assert staged_files(MEDIA_ROOT) == []


# -- uploads ---------------------------------------------------------------------------------------


async def test_upload_stores_the_asset_and_one_blob(
    settings: Settings, engine: AsyncEngine
) -> None:
    """An upload answers 201 with the asset: the sniffed MIME type, the size, Pillow's
    dimensions, the checksum, ``live``/``uploaded``, the rating's id and current version, the
    owner (the requester, or the named ``owner``) and who created it. The bytes sit read-only at
    ``<root>/<sha256[:2]>/<sha256>``; nothing stays staged; a committed ``control`` operation by
    the requester records it. Identical bytes uploaded again make a second asset over the same
    blob. The content route serves the bytes with their type, ``ETag`` and ``Cache-Control``."""
    data = png(5, 4)
    async with media_client(settings, engine) as client:
        response = await client.post(
            "/media",
            files=files(data, metadata(alt_text="A door", owner="p-owner")),
            headers={**WRITE, "Idempotency-Key": f"up-{secrets.token_hex(4)}"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert {k: body[k] for k in ("kind", "mime_type", "size", "width", "height")} == {
            "kind": "image",
            "mime_type": "image/png",
            "size": len(data),
            "width": 5,
            "height": 4,
        }
        assert (body["checksum"], body["state"], body["source"]) == (
            sha256(data),
            "live",
            "uploaded",
        )
        assert (body["owner_principal"], body["created_by"]) == ("p-owner", "p-media")
        assert body["content_rating_id"] == EVERYONE_ID
        assert (body["duration_ms"], body["alt_text"]) == (None, "A door")
        assert body["idempotency_key"] == response.headers["Idempotency-Key"]
        stored = blob_path(MEDIA_ROOT, body["checksum"])
        assert stored.read_bytes() == data
        assert not stored.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        assert staged_files(MEDIA_ROOT) == []
        (operation,) = await rows(
            engine,
            "SELECT kind, principal, status FROM operations WHERE idempotency_key = :k",
            k=response.headers["Idempotency-Key"],
        )
        assert tuple(operation) == ("control", "p-media", "committed")

        again = await uploaded(client, data)
        assert again["id"] != body["id"]
        assert again["checksum"] == body["checksum"]
        assert sorted(p.name for p in stored.parent.iterdir()).count(body["checksum"]) == 1

        content = await client.get(f"/media/{body['id']}/content", headers=READ)
        assert content.status_code == 200
        assert content.content == data
        assert content.headers["content-type"] == "image/png"
        assert content.headers["etag"] == f'"{body["checksum"]}"'
        assert "immutable" in content.headers["cache-control"]
        assert int(content.headers["content-length"]) == len(data)
        read = await client.get(f"/media/{body['id']}", headers=READ)
        assert read.json() == {
            k: v for k, v in body.items() if k not in ("idempotency_key", "config_revision")
        }


async def test_upload_is_idempotent(settings: Settings, engine: AsyncEngine) -> None:
    """A retry with the same key, metadata and bytes answers the stored asset with
    ``Idempotent-Replay: true`` and stores nothing more; the same key with other bytes is 409
    ``idempotency_conflict``."""
    data = png()
    key = f"up-{secrets.token_hex(6)}"
    headers = {**WRITE, "Idempotency-Key": key}
    async with media_client(settings, engine) as client:
        first = await client.post("/media", files=files(data, metadata()), headers=headers)
        assert first.status_code == 201, first.text
        before = await count(engine, "SELECT count(*) FROM media_assets")
        again = await client.post("/media", files=files(data, metadata()), headers=headers)
        assert (again.status_code, again.headers.get("Idempotent-Replay")) == (201, "true")
        assert again.json()["id"] == first.json()["id"]
        assert await count(engine, "SELECT count(*) FROM media_assets") == before
        other = await client.post("/media", files=files(png(), metadata()), headers=headers)
        assert _error(other)[:2] == (409, "idempotency_conflict")
        assert staged_files(MEDIA_ROOT) == []


async def test_subjects_are_required_and_deduplicated(
    settings: Settings, engine: AsyncEngine
) -> None:
    """``depicted_subjects`` omitted or ``null`` is 422 ``subjects_required`` (nothing stored);
    ``[]`` is accepted; repeated subjects are stored once, in order."""
    a, b = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C5", "01J9Z8Q4N5V6W7X8Y9Z0A1B2C7"
    async with media_client(settings, engine) as client:
        before = await count(engine, "SELECT count(*) FROM media_assets")
        for meta in (metadata(depicted_subjects=None), {**metadata(), "depicted_subjects": None}):
            data = png()
            refused = await upload(client, data, meta)
            assert _error(refused) == (
                422,
                "subjects_required",
                {"field": "body.metadata.depicted_subjects"},
            )
            await _nothing_stored(engine, before, data)
        empty = await uploaded(client, depicted_subjects=[])
        assert empty["depicted_subjects"] == []
        subjects = [
            {"kind": "catalog_character", "id": a},
            {"kind": "persona", "id": b},
            {"kind": "catalog_character", "id": a},
        ]
        twice = await uploaded(client, depicted_subjects=subjects)
        assert twice["depicted_subjects"] == subjects[:2]


async def test_upload_refusals_store_nothing(
    settings: Settings, engine: AsyncEngine, defaults: Any
) -> None:
    """Each refusal answers its code at its field and leaves no row, no blob and no staged
    file: a missing or unknown or retired rating, metadata the schema refuses, bytes of a type
    that is unrecognised, not allowed, or not what was declared, and a file larger than
    ``media_max_bytes`` (cut while streaming, or refused by the declared length first)."""
    lib_rating = await _retired_rating(settings, engine)
    cases: list[tuple[str, bytes, dict[str, Any], str | None, tuple[int, str, Any]]] = [
        (
            "missing rating",
            png(),
            {k: v for k, v in metadata().items() if k != "content_rating_id"},
            "image/png",
            (422, "validation_error", "body.metadata.content_rating_id"),
        ),
        (
            "unknown rating",
            png(),
            metadata(content_rating_id="0" * 26),
            "image/png",
            (422, "rating_retired", "unknown"),
        ),
        (
            "retired rating",
            png(),
            metadata(content_rating_id=lib_rating),
            "image/png",
            (422, "rating_retired", "retired"),
        ),
        (
            "unknown field",
            png(),
            metadata(colour="red"),
            "image/png",
            (422, "validation_error", "body.metadata.colour"),
        ),
        (
            "lax type",
            png(),
            metadata(kind=5),
            "image/png",
            (422, "validation_error", "body.metadata.kind"),
        ),
        (
            "NUL",
            png(),
            metadata(caption="a\x00b"),
            "image/png",
            (422, "validation_error", "body.metadata.caption"),
        ),
        (
            "bad subject id",
            png(),
            metadata(depicted_subjects=[{"kind": "character", "id": "nope"}]),
            "image/png",
            (422, "validation_error", "body.metadata.depicted_subjects[0].id"),
        ),
        (
            "too many subjects",
            png(),
            metadata(
                depicted_subjects=[
                    {"kind": "character", "id": f"01J9Z8Q4N5V6W7X8Y9Z0A1B{n:03d}"}
                    for n in range(51)
                ]
            ),
            "image/png",
            (422, "validation_error", "body.metadata.depicted_subjects"),
        ),
        (
            "unrecognised",
            b"plain text, not an image" * 4,
            metadata(),
            None,
            (415, "media_type_not_allowed", "unrecognized"),
        ),
        (
            "not allowed",
            b"GIF89a" + secrets.token_bytes(80),
            metadata(),
            "image/gif",
            (415, "media_type_not_allowed", "not_allowed"),
        ),
        (
            "declared otherwise",
            png(),
            metadata(),
            "image/jpeg",
            (415, "media_type_not_allowed", "declared_mismatch"),
        ),
        (
            "kind mismatch",
            png(),
            metadata(kind="video"),
            "image/png",
            (415, "media_type_not_allowed", "kind_mismatch"),
        ),
        (
            "video as image",
            mp4(),
            metadata(),
            "video/mp4",
            (415, "media_type_not_allowed", "kind_mismatch"),
        ),
    ]
    async with media_client(settings, engine) as client:
        before = await count(engine, "SELECT count(*) FROM media_assets")
        for name, data, meta, content_type, expected in cases:
            response = await upload(client, data, meta, content_type=content_type)
            status, code, details = _error(response)
            where = (
                details.get("field") if expected[1] == "validation_error" else details.get("reason")
            )
            assert (status, code, where) == expected, (name, response.text)
            await _nothing_stored(engine, before, data)

        await set_media(client, media_max_bytes=100)
        big = png(30, 30)
        assert len(big) > 100
        streamed = await upload(client, big)
        assert _error(streamed)[:2] == (413, "media_too_large")
        assert _error(streamed)[2]["limit"] == 100
        await _nothing_stored(engine, before, big)
        huge = b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(media.ENVELOPE_MAX_BYTES + 200)
        declared = await upload(client, huge)
        status, code, details = _error(declared)
        assert (status, code) == (413, "media_too_large")
        assert details["size"] > media.ENVELOPE_MAX_BYTES  # the declared length, refused unread
        await _nothing_stored(engine, before, huge)
        await set_media(
            client, media_allowed_types=["image/png", "image/gif"], media_max_bytes=10_000
        )
        gif = await upload(client, b"GIF89a" + secrets.token_bytes(80), content_type="image/gif")
        assert gif.status_code == 201, gif.text
        assert gif.json()["mime_type"] == "image/gif"
        undeclared = await upload(client, png(), content_type="application/octet-stream")
        assert undeclared.status_code == 201, undeclared.text
        alias = await upload(client, jpeg(), content_type="image/jpg")
        assert _error(alias)[2]["reason"] == "not_allowed"  # an alias of image/jpeg, not allowed


async def _retired_rating(settings: Settings, engine: AsyncEngine) -> str:
    from epistrel import Engine

    lib = Engine(settings, db=engine)
    try:
        created = await lib.create_content_rating(
            AUTHOR, RatingFields(name=f"Gone {secrets.token_hex(3)}", inherits_from=[EVERYONE_ID])
        )
        await lib.retire_content_rating(AUTHOR, created.rating.id)
        return created.rating.id
    finally:
        await lib.models.aclose()


async def test_malformed_bodies(settings: Settings, engine: AsyncEngine) -> None:
    """Not multipart, the file before the metadata, no file, a part after the file, metadata
    that is not JSON, or a body cut short: 422 at its field, nothing stored. The principal and
    the credential are checked before the body: 400 and 401."""
    data = png()
    meta = json.dumps(metadata())
    async with media_client(settings, engine) as client:
        before = await count(engine, "SELECT count(*) FROM media_assets")
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("json", {"json": metadata()}, "body"),
            (
                "file first",
                {"files": {"file": ("a.png", data, "image/png"), "metadata": (None, meta)}},
                "body.metadata",
            ),
            ("no file", {"files": {"metadata": (None, meta, "application/json")}}, "body.file"),
            (
                "extra part",
                {
                    "files": {
                        "metadata": (None, meta, "application/json"),
                        "file": ("a.png", data, "image/png"),
                        "more": (None, "x"),
                    }
                },
                "body",
            ),
            ("not json", {"files": files(data, "{not json")}, "body.metadata"),
        ]
        for name, kwargs, field in cases:
            response = await client.post("/media", headers=WRITE, **kwargs)
            assert _error(response)[:2] == (422, "validation_error"), (name, response.text)
            assert _error(response)[2]["field"] == field, name
        boundary = "x" * 16
        cut = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n{meta}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + data
        truncated = await client.post(
            "/media",
            content=cut,
            headers={**WRITE, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert _error(truncated)[:2] == (422, "validation_error"), truncated.text
        await _nothing_stored(engine, before, data)
        anonymous = await client.post("/media", files=files(data, metadata()), headers=READ)
        assert _error(anonymous)[:2] == (400, "principal_required")
        stranger = await client.post(
            "/media", files=files(data, metadata()), headers={"X-Epistrel-Principal": "p-x"}
        )
        assert _error(stranger)[:2] == (401, "unauthenticated")
        await _nothing_stored(engine, before, data)


async def test_the_library_uploads_bytes(settings: Settings, engine: AsyncEngine) -> None:
    """The library takes the metadata model and the bytes (or chunks); the same refusals."""
    from epistrel import Engine, MediaUpload, SubjectsRequired

    lib = Engine(settings, db=engine)
    try:
        data = png(7, 3)
        request = MediaUpload.model_validate(metadata(caption="Library upload"))
        written = await lib.upload_media(AUTHOR, request, data, content_type="image/png")
        assert (written.asset.width, written.asset.caption) == (7, "Library upload")

        async def chunks() -> Any:
            for start in range(0, len(data), 7):
                yield data[start : start + 7]

        again = await lib.upload_media(AUTHOR, request, chunks())
        assert again.asset.checksum == written.asset.checksum
        content = await lib.get_media_content(AUTHOR, written.asset.id)
        assert (await content.read(), content.mime_type) == (data, "image/png")
        with pytest.raises(SubjectsRequired):
            await lib.upload_media(
                AUTHOR, MediaUpload.model_validate(metadata(depicted_subjects=None)), png()
            )
    finally:
        await lib.models.aclose()


# -- the moderation hook ---------------------------------------------------------------------------


async def test_media_moderation(keyed: Settings, engine: AsyncEngine, defaults: Any) -> None:
    """A hook that accepts lets the upload through (201), signed and with the asset's metadata,
    its SHA-256 and a 5-minute content URL. One that rejects is 422 ``moderation_rejected``
    with its reason; one that times out, refuses the connection, answers a non-2xx status (a
    redirect included), a body that is not JSON, or an unknown decision is 503
    ``moderation_unavailable``. In every case but accept, no row, no blob and no staged file
    exist afterwards (asserted on the table and the filesystem)."""
    async with media_client(keyed, engine) as client:
        before = await count(engine, "SELECT count(*) FROM media_assets")
        with hook_server() as hook:
            await set_media(client, media_moderation_webhook_url=hook.url)
            data = png()
            earliest = int(time.time())  # the URL is signed during the upload
            accepted = await upload(
                client,
                data,
                metadata(caption="Gull Rock", alt_text="The light", depicted_subjects=[]),
            )
            latest = int(time.time())
            assert accepted.status_code == 201, accepted.text
            (sent,) = hook.received
            payload = sent.json
            assert set(payload) == {"asset", "sha256", "content_url"}
            assert payload["sha256"] == sha256(data)
            assert payload["asset"] == {
                "id": accepted.json()["id"],
                "kind": "image",
                "mime_type": "image/png",
                "size": len(data),
                "width": 3,
                "height": 2,
                "duration_ms": None,
                "content_rating_id": EVERYONE_ID,
                "alt_text": "The light",
                "caption": "Gull Rock",
                "owner_principal": "p-media",
                "depicted_subjects": [],
            }
            url = urlsplit(payload["content_url"])
            assert url.path == f"/media/{accepted.json()['id']}/content"
            query = parse_qs(url.query)
            assert set(query) == {"exp", "sig"}
            # Short-lived: the URL expires 5 minutes after it was signed, during the upload.
            (exp,) = query["exp"]
            assert earliest + 5 * 60 <= int(exp) <= latest + 5 * 60
            timestamp = sent.headers["X-Epistrel-Timestamp"]
            secret = bytes.fromhex(moderation_secret(CONFIG_KEY))
            expected = hmac.new(secret, f"{timestamp}.".encode() + sent.body, hashlib.sha256)
            assert sent.headers["X-Epistrel-Signature"] == f"sha256={expected.hexdigest()}"
            assert sent.headers["Content-Type"] == "application/json"
            count_after_accept = before + 1
        assert await count(engine, "SELECT count(*) FROM media_assets") == count_after_accept

        refusals: list[tuple[dict[str, Any], int, str, dict[str, Any]]] = [
            (
                {"answer": {"decision": "reject", "reason": "nudity"}},
                422,
                "moderation_rejected",
                {"reason": "nudity"},
            ),
            ({"delay_s": 1.0}, 503, "moderation_unavailable", {"reason": "timeout"}),
            ({"status": 500}, 503, "moderation_unavailable", {"reason": "status", "status": 500}),
            (
                {"status": 302, "location": "http://127.0.0.1:9/"},
                503,
                "moderation_unavailable",
                {"reason": "status", "status": 302},
            ),
            (
                {"answer": b"<html>ok</html>"},
                503,
                "moderation_unavailable",
                {"reason": "malformed"},
            ),
            (
                {"answer": {"decision": "maybe"}},
                503,
                "moderation_unavailable",
                {"reason": "malformed"},
            ),
            ({"answer": ["accept"]}, 503, "moderation_unavailable", {"reason": "malformed"}),
        ]
        for options, status, code, details in refusals:
            with hook_server(**options) as hook:
                await set_media(
                    client, media_moderation_webhook_url=hook.url, media_moderation_timeout_ms=300
                )
                data = png()
                refused = await upload(client, data)
                assert _error(refused) == (status, code, details), (options, refused.text)
                assert len(hook.received) == 1
                await _nothing_stored(engine, count_after_accept, data)
        await set_media(client, media_moderation_webhook_url=refused_url())
        data = png()
        unreachable = await upload(client, data)
        assert _error(unreachable) == (503, "moderation_unavailable", {"reason": "connection"})
        await _nothing_stored(engine, count_after_accept, data)


async def test_the_hook_reads_the_staged_bytes(
    keyed: Settings, engine: AsyncEngine, defaults: Any
) -> None:
    """While the hook decides, its ``content_url`` (with the service credential) serves the
    staged bytes, which no other request can see: without the signature, or with a tampered
    one, the asset is 404. The URL is short-lived: it expires 5 minutes after it was signed and
    serves until 30 seconds (the clock skew) past that, then 404. ``public_base_url`` prefixes
    the URL. After the hook accepts, the content route serves the committed asset."""
    data = png(9, 9)
    async with media_client(keyed, engine) as client:
        with hook_server(hold=True) as hook:
            await set_media(
                client,
                media_moderation_webhook_url=hook.url,
                public_base_url="https://media.example.test/epistrel",
            )
            pending = asyncio.create_task(upload(client, data))
            await wait_for(hook.arrived)
            content_url = hook.received[0].json["content_url"]
            assert content_url.startswith("https://media.example.test/epistrel/media/")
            path = content_url.removeprefix("https://media.example.test/epistrel")
            staged = await client.get(path, headers=READ)
            assert (staged.status_code, staged.content) == (200, data)
            assert staged.headers["content-type"] == "image/png"
            assert "etag" not in staged.headers
            bare = path.split("?", 1)[0]
            assert (await client.get(bare, headers=READ)).status_code == 404
            tampered = path[:-2] + ("AA" if not path.endswith("AA") else "BB")
            assert (await client.get(tampered, headers=READ)).status_code == 404
            exp = int(parse_qs(urlsplit(path).query)["exp"][0])
            assert 0 < exp - time.time() <= 5 * 60
            last = datetime.fromtimestamp(exp + CLOCK_SKEW_S, UTC)
            with clock.frozen_at(last):
                assert (await client.get(path, headers=READ)).status_code == 200
            with clock.frozen_at(last + timedelta(seconds=1)):
                assert (await client.get(path, headers=READ)).status_code == 404
            hook.release.set()
            done = await pending
            assert done.status_code == 201, done.text
            committed = await client.get(bare, headers=READ)
            assert (committed.status_code, committed.content) == (200, data)


async def test_a_hook_needs_the_config_key(
    settings: Settings, engine: AsyncEngine, defaults: Any
) -> None:
    """With a hook configured and ``EPISTREL_CONFIG_KEY`` unset, nothing can be signed: 503
    ``config_key_missing`` and nothing stored (the hook is never called)."""
    async with media_client(settings, engine) as client:
        before = await count(engine, "SELECT count(*) FROM media_assets")
        with hook_server() as hook:
            await set_media(client, media_moderation_webhook_url=hook.url)
            data = png()
            refused = await upload(client, data)
            assert _error(refused)[:2] == (503, "config_key_missing")
            assert hook.received == []
            await _nothing_stored(engine, before, data)


# -- reads, retirement, listing ------------------------------------------------------------------


async def test_retire_keeps_reads_and_refuses_new_references(
    settings: Settings, engine: AsyncEngine
) -> None:
    """``DELETE`` retires (204): the metadata reads ``retired``, the content is still served,
    the asset leaves the default listing (``state=retired`` lists it), and a new reference is
    422 ``media_retired``. Deleting again changes nothing and records nothing; an unknown id is
    404. A live asset can be referenced."""
    async with media_client(settings, engine) as client:
        data = png()
        asset = await uploaded(client, data)
        live = await uploaded(client)
        media_id = asset["id"]
        key = f"del-{secrets.token_hex(4)}"
        deleted = await client.delete(
            f"/media/{media_id}", headers={**WRITE, "Idempotency-Key": key}
        )
        assert deleted.status_code == 204, deleted.text
        replay = await client.delete(
            f"/media/{media_id}", headers={**WRITE, "Idempotency-Key": key}
        )
        assert (replay.status_code, replay.headers.get("Idempotent-Replay")) == (204, "true")
        operations = await count(engine, "SELECT count(*) FROM operations WHERE kind = 'control'")
        again = await client.delete(f"/media/{media_id}", headers=WRITE)
        assert again.status_code == 204
        assert (
            await count(engine, "SELECT count(*) FROM operations WHERE kind = 'control'")
            == operations
        )
        read = await client.get(f"/media/{media_id}", headers=READ)
        assert (read.status_code, read.json()["state"]) == (200, "retired")
        content = await client.get(f"/media/{media_id}/content", headers=READ)
        assert (content.status_code, content.content) == (200, data)
        listed = await client.get("/media", params={"limit": 200}, headers=READ)
        ids = {item["id"] for item in listed.json()["items"]}
        assert media_id not in ids
        assert live["id"] in ids
        retired = await client.get(
            "/media", params={"state": "retired", "limit": 200}, headers=READ
        )
        assert media_id in {item["id"] for item in retired.json()["items"]}
        for path, method in ((f"/media/{'0' * 26}", "GET"), (f"/media/{'0' * 26}", "DELETE")):
            missing = await client.request(method, path, headers=WRITE)
            assert _error(missing)[:2] == (404, "not_found")
        assert (await client.get(f"/media/{'0' * 26}/content", headers=READ)).status_code == 404
    async with engine.begin() as conn:
        await media.assert_referenceable(conn, [live["id"]])
        with pytest.raises(MediaRetired) as refused:
            await media.assert_referenceable(conn, [live["id"], media_id], field="images")
        assert refused.value.details == {
            "media_id": media_id,
            "field": "images[1]",
            "reason": "retired",
        }
        with pytest.raises(MediaRetired) as unknown:
            await media.assert_referenceable(conn, ["0" * 26])
        assert unknown.value.details["reason"] == "unknown"


async def test_list_filters_and_pages(settings: Settings, engine: AsyncEngine) -> None:
    """The listing is newest first and pages by ``cursor``; ``owner``, ``relation`` and
    ``kind`` narrow it only when given; a bad cursor and ``state=candidate`` are 422."""
    owner = f"p-lister-{secrets.token_hex(3)}"
    async with media_client(settings, engine) as client:
        made = [await uploaded(client, owner=owner) for _ in range(3)]
        video = await uploaded(client, mp4(), content_type="video/mp4", kind="video", owner=owner)
        assert (video["mime_type"], video["duration_ms"]) == ("video/mp4", None)
        related = await client.post(
            f"/media/{made[0]['id']}/relations",
            json={"principal": "p-editor-x", "relation": "editor"},
            headers=WRITE,
        )
        assert related.status_code == 201, related.text
        mine = await client.get("/media", params={"owner": owner}, headers=READ)
        assert [i["id"] for i in mine.json()["items"]] == [
            video["id"],
            *(m["id"] for m in reversed(made)),
        ]
        images = await client.get("/media", params={"owner": owner, "kind": "image"}, headers=READ)
        assert [i["kind"] for i in images.json()["items"]] == ["image"] * 3
        editor = await client.get("/media", params={"relation": "editor:p-editor-x"}, headers=READ)
        assert [i["id"] for i in editor.json()["items"]] == [made[0]["id"]]
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"owner": owner, "limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            page = (await client.get("/media", params=params, headers=READ)).json()
            seen += [i["id"] for i in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == [i["id"] for i in mine.json()["items"]]
        everyone = await client.get("/media", params={"limit": 200}, headers=READ)
        assert {m["id"] for m in made} <= {i["id"] for i in everyone.json()["items"]}
        for params, field in (
            ({"cursor": "nonsense"}, "query.cursor"),
            ({"state": "candidate"}, "query.state"),
        ):
            refused = await client.get("/media", params=params, headers=READ)
            assert _error(refused)[:2] == (422, "validation_error")
            assert _error(refused)[2]["field"] == field


# -- the subject-reference index ---------------------------------------------------------------


async def test_uploads_index_their_subjects(settings: Settings, engine: AsyncEngine) -> None:
    """Every upload writes ``subject_references`` rows (``scope = catalog``, ``record_table =
    media_assets``): one ``declared`` row per depicted subject, and a ``mentioned`` row for each
    catalog character whose name its caption, alt text, prompt or negative prompt holds as a
    whole word, ignoring case. A name inside another word, in a caption or a negative prompt,
    indexes nothing."""
    name = f"Mara{secrets.token_hex(3)}"
    async with media_client(settings, engine) as client:
        created = await client.post(
            "/catalog/characters", json=catalog_character(name), headers=WRITE
        )
        assert created.status_code == 201, created.text
        character = created.json()["id"]
        declared = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"
        asset = await uploaded(
            client,
            caption=f"{name.upper()} at the door.",
            alt_text=f"Portrait of {name}",
            generation_metadata={
                "prompt": f"{name}, oil painting",
                "negative_prompt": f"blurry {name.lower()}",
            },
            depicted_subjects=[{"kind": "persona", "id": declared}],
        )
        found = await rows(
            engine,
            "SELECT subject_kind, subject_id, scope, story_id, record_table, field, ref_kind "
            "FROM subject_references WHERE record_id = :r ORDER BY field",
            r=asset["id"],
        )
        assert [tuple(r) for r in found] == [
            (
                "catalog_character",
                character,
                "catalog",
                None,
                "media_assets",
                "alt_text",
                "mentioned",
            ),
            (
                "catalog_character",
                character,
                "catalog",
                None,
                "media_assets",
                "caption",
                "mentioned",
            ),
            ("persona", declared, "catalog", None, "media_assets", "depicted_subjects", "declared"),
            (
                "catalog_character",
                character,
                "catalog",
                None,
                "media_assets",
                "generation_metadata.negative_prompt",
                "mentioned",
            ),
            (
                "catalog_character",
                character,
                "catalog",
                None,
                "media_assets",
                "generation_metadata.prompt",
                "mentioned",
            ),
        ]
        plain = await uploaded(
            client,
            caption=f"Someone else entirely, {name}ish.",
            generation_metadata={"negative_prompt": f"x{name}y"},
        )
        assert (
            await count(
                engine,
                "SELECT count(*) FROM subject_references WHERE record_id = :r",
                r=plain["id"],
            )
            == 0
        )


# -- settings --------------------------------------------------------------------------------------


async def test_media_settings(settings: Settings, engine: AsyncEngine, defaults: Any) -> None:
    """The media settings read and write through ``/admin/settings`` (each change recorded as a
    prospective configuration change); the hook URL and base URL take ``null``; a URL that is
    not http(s), a type listed twice or one that is not ``type/subtype`` is 422.
    ``media_sweep_interval`` moves its recurring task's row."""
    async with media_client(settings, engine) as client:
        read = (await client.get("/admin/settings", headers=READ)).json()
        assert read["media_allowed_types"] == ["image/png", "image/jpeg", "image/webp", "video/mp4"]
        assert (read["media_max_bytes"], read["media_moderation_timeout_ms"]) == (52_428_800, 5000)
        assert (read["media_moderation_webhook_url"], read["public_base_url"]) == (None, None)
        assert read["media_sweep_interval"] == 3600
        written = await set_media(client, media_moderation_webhook_url="https://hook.example/m")
        assert written["media_moderation_webhook_url"] == "https://hook.example/m"
        changes = await client.get(
            "/admin/config-changes",
            params={"setting": "media_moderation_webhook_url"},
            headers=READ,
        )
        latest = changes.json()["items"][0]
        assert (latest["class"], latest["new"]) == ("prospective", "https://hook.example/m")
        cleared = await set_media(client, media_moderation_webhook_url=None)
        assert cleared["media_moderation_webhook_url"] is None
        for bad, field in (
            (
                {"media_moderation_webhook_url": "ftp://hook.example"},
                "body.media_moderation_webhook_url",
            ),
            ({"public_base_url": "/relative"}, "body.public_base_url"),
            ({"media_allowed_types": ["image/png", "image/png"]}, "body.media_allowed_types"),
            ({"media_allowed_types": ["PNG"]}, "body.media_allowed_types[0]"),
            ({"media_allowed_types": []}, "body.media_allowed_types"),
            ({"media_max_bytes": 0}, "body.media_max_bytes"),
            ({"media_sweep_interval": 5}, "body.media_sweep_interval"),
            ({"media_max_bytes": None}, "body.media_max_bytes"),
        ):
            refused = await client.put("/admin/settings", json=bad, headers=WRITE)
            assert _error(refused)[:2] == (422, "validation_error"), (bad, refused.text)
            assert _error(refused)[2]["field"] == field, bad

        from sqlalchemy import select

        from epistrel.core.tables import recurring_tasks
        from epistrel.orchestrator.jobs import timers

        await timers.reconcile(engine)
        async with engine.connect() as conn:
            interval = await conn.scalar(
                select(recurring_tasks.c.interval).where(recurring_tasks.c.name == "media_sweep")
            )
        assert interval is not None
        assert interval.total_seconds() == 3600
        try:
            await set_media(client, media_sweep_interval=600)
            async with engine.connect() as conn:
                interval = await conn.scalar(
                    select(recurring_tasks.c.interval).where(
                        recurring_tasks.c.name == "media_sweep"
                    )
                )
            assert interval is not None
            assert interval.total_seconds() == 600
            await timers.reconcile(engine)  # a worker's start-up keeps the setting's value
            async with engine.connect() as conn:
                interval = await conn.scalar(
                    select(recurring_tasks.c.interval).where(
                        recurring_tasks.c.name == "media_sweep"
                    )
                )
            assert interval is not None
            assert interval.total_seconds() == 600
        finally:
            await set_media(client, media_sweep_interval=3600)


# -- the store and the start-up check --------------------------------------------------------------


def test_an_unwritable_media_root_refuses_the_start(tmp_path: Path) -> None:
    """``epistrel serve`` exits 78 naming the media root when it cannot be written, before it
    touches the database."""
    import os
    import subprocess
    import sys

    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")}
    env |= {
        "EPISTREL_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/x",
        "EPISTREL_SERVICE_CREDENTIALS": "c" * 40,
        "EPISTREL_MEDIA_ROOT": str(blocked / "media"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "epistrel.cli", "serve"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    assert proc.returncode == 78, proc.stderr
    assert str(blocked / "media") in proc.stderr
    assert "Traceback" not in proc.stderr


# -- the schema ------------------------------------------------------------------------------------

MEDIA_COLUMNS = {
    "id": ("bpchar", "NO"),
    "owner_principal": ("text", "NO"),
    "kind": ("text", "NO"),
    "mime_type": ("text", "NO"),
    "size": ("int8", "NO"),
    "width": ("int4", "YES"),
    "height": ("int4", "YES"),
    "duration_ms": ("int8", "YES"),
    "checksum": ("bpchar", "NO"),
    "content_rating_version_id": ("bpchar", "NO"),
    "alt_text": ("text", "YES"),
    "caption": ("text", "YES"),
    "generation_metadata": ("jsonb", "YES"),
    "source": ("text", "NO"),
    "state": ("text", "NO"),
    "depicted_subjects": ("jsonb", "NO"),
    "created_at": ("timestamptz", "NO"),
    "created_by": ("text", "NO"),
}
REFERENCE_COLUMNS = {
    "id": ("bpchar", "NO"),
    "subject_kind": ("text", "NO"),
    "subject_id": ("bpchar", "NO"),
    "scope": ("text", "NO"),
    "story_id": ("bpchar", "YES"),
    "record_table": ("text", "NO"),
    "record_id": ("text", "NO"),
    "field": ("text", "YES"),
    "ref_kind": ("text", "NO"),
}


async def test_media_tables(engine: AsyncEngine) -> None:
    """Migration 0022 creates ``media_assets`` with the asset's columns (``kind`` image|video,
    ``source`` uploaded|generated, ``state`` live|retired|candidate, a lowercase SHA-256
    ``checksum``) and ``subject_references`` in #123's shape (a story row names its story, a
    catalog row none; one row per subject, record and field, a null field included). The
    checks refuse what the vocabularies leave out."""
    from sqlalchemy.exc import IntegrityError

    from epistrel.core.ids import new_ulid
    from tests.event_support import execute_in_tx

    for table, expected in (
        ("media_assets", MEDIA_COLUMNS),
        ("subject_references", REFERENCE_COLUMNS),
    ):
        found = await rows(
            engine,
            "SELECT column_name, udt_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t",
            t=table,
        )
        assert {r.column_name: (r.udt_name, r.is_nullable) for r in found} == expected, table
    insert = (
        "INSERT INTO media_assets (id, owner_principal, kind, mime_type, size, checksum, "
        "content_rating_version_id, source, state, depicted_subjects, created_at, created_by) "
        "VALUES (:id, 'p', :kind, 'image/png', 1, :sum, :v, :source, :state, '[]', now(), 'p')"
    )
    good = {"kind": "image", "sum": "a" * 64, "v": "0" * 26, "source": "uploaded", "state": "live"}
    for bad in ({"kind": "audio"}, {"sum": "A" * 64}, {"source": "found"}, {"state": "gone"}):
        with pytest.raises(IntegrityError):
            await execute_in_tx(engine, (insert, {**good, **bad, "id": new_ulid()}))
    reference = (
        "INSERT INTO subject_references (id, subject_kind, subject_id, scope, story_id, "
        "record_table, record_id, field, ref_kind) VALUES (:id, 'persona', :s, :scope, :story, "
        "'media_assets', :r, NULL, 'declared')"
    )
    subject, record = new_ulid(), new_ulid()
    await execute_in_tx(
        engine,
        (
            reference,
            {"id": new_ulid(), "s": subject, "scope": "catalog", "story": None, "r": record},
        ),
    )
    cases: list[dict[str, str | None]] = [
        {"scope": "story", "story": None},  # a story row names its story
        {"scope": "catalog", "story": new_ulid()},  # a catalog row names none
        {"scope": "catalog", "story": None},  # the same subject, record and (null) field again
    ]
    for wrong in cases:
        with pytest.raises(IntegrityError):
            await execute_in_tx(
                engine, (reference, {"id": new_ulid(), "s": subject, "r": record, **wrong})
            )
    await execute_in_tx(
        engine, ("DELETE FROM subject_references WHERE subject_id = :s", {"s": subject})
    )


# -- the media sweep -------------------------------------------------------------------------------

SWEEPER = RequestContext(principal="p-author")
OLD = 2 * 3600.0


@pytest.fixture
async def space(pg_url: str, tmp_path: Path) -> AsyncIterator[Scratch]:
    async with scratch(pg_url, tmp_path / "media") as found:
        yield found


async def put(space: Scratch, data: bytes | None = None, **fields: Any) -> str:
    """An uploaded asset's id (through the library)."""
    request = MediaUpload.model_validate(metadata(**fields))
    written = await space.lib.upload_media(SWEEPER, request, png() if data is None else data)
    return written.asset.id


async def execute(space: Scratch, sql: str, **params: Any) -> None:
    async with space.db.begin() as conn:
        await conn.execute(text(sql), params)


async def plant_in_draft(space: Scratch, media_id: str) -> str:
    """A catalog character whose draft names ``media_id`` (as #115 will)."""
    from epistrel import CharacterCreate

    created = await space.lib.create_character_draft(
        SWEEPER, CharacterCreate(**catalog_character())
    )
    await execute(
        space,
        "UPDATE catalog.character_catalog SET draft = draft || "
        "jsonb_build_object('public_card', jsonb_build_object('images', "
        "jsonb_build_object('gallery', jsonb_build_array(CAST(:m AS text))))) WHERE id = :c",
        m=media_id,
        c=created.character.id,
    )
    return created.character.id


async def plant_in_version(space: Scratch, media_id: str) -> str:
    """A catalog character with a published version that names ``media_id`` (as #115 will)."""
    from epistrel import CharacterCreate

    created = await space.lib.create_character_draft(
        SWEEPER, CharacterCreate(**catalog_character())
    )
    await execute(
        space,
        "INSERT INTO catalog.character_versions (catalog_id, version, content, published_at, "
        "published_by) VALUES (:c, 1, jsonb_build_object('name', 'Mara', 'public_card', "
        "jsonb_build_object('images', jsonb_build_object('primary', CAST(:m AS text)))), "
        "now(), 'p-author')",
        m=media_id,
        c=created.character.id,
    )
    return created.character.id


async def ids(space: Scratch) -> set[str]:
    return {str(r.id) for r in await rows(space.db, "SELECT id FROM media_assets")}


def aged(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


async def test_sweep_purges_what_nothing_references(space: Scratch) -> None:
    """One run purges the retired assets nothing references and the live ones older than a day,
    and keeps the young, the referenced (a draft, a published version) and the candidates. A
    purged asset's relations go with a ``deleted`` ownership event by ``system``, and so do its
    subject references. Bytes nothing names go: at once after a purge, after an hour for any
    other blob; so do staged uploads older than an hour. A second run changes nothing."""
    young = await put(space)
    retired = await put(space, depicted_subjects=[{"kind": "persona", "id": new_ulid()}])
    in_draft = await put(space)
    in_version = await put(space)
    candidate = await put(space)
    for media_id in (retired, in_draft):
        await space.lib.delete_media(SWEEPER, media_id)
    await space.lib.add_relation(
        SWEEPER, EntityType.MEDIA, retired, RelationRequest(principal="p-x", relation="editor")
    )
    await plant_in_draft(space, in_draft)
    await plant_in_version(space, in_version)
    await execute(space, "UPDATE media_assets SET state = 'candidate' WHERE id = :m", m=candidate)
    retired_sum = (await space.lib.get_media(SWEEPER, retired)).checksum
    assert (
        await scalar(
            space.db, "SELECT count(*) FROM subject_references WHERE record_id = :m", m=retired
        )
        == 1
    )

    orphan_old = "a" * 64
    orphan_new = "b" * 64
    for name in (orphan_old, orphan_new):
        path = blob_path(space.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"left behind")
    aged(blob_path(space.root, orphan_old), OLD)
    stale = space.root / "tmp" / new_ulid()
    fresh = space.root / "tmp" / new_ulid()
    stale.write_bytes(b"crashed upload")
    fresh.write_bytes(b"upload in progress")
    aged(stale, OLD)

    counts = await sweep(space.db, space.lib.media)
    assert counts == {"assets_purged": 1, "blobs_deleted": 2, "staged_removed": 1}
    assert await ids(space) == {young, in_draft, in_version, candidate}
    assert not blob_path(space.root, retired_sum).exists()
    assert not blob_path(space.root, orphan_old).exists()
    assert blob_path(space.root, orphan_new).exists()
    assert (not stale.exists(), fresh.exists()) == (True, True)
    assert (
        await scalar(
            space.db, "SELECT count(*) FROM entity_relations WHERE entity_id = :m", m=retired
        )
        == 0
    )
    assert (
        await scalar(
            space.db, "SELECT count(*) FROM subject_references WHERE record_id = :m", m=retired
        )
        == 0
    )
    (deleted,) = await rows(
        space.db,
        "SELECT old, acting_principal FROM ownership_events "
        "WHERE entity_id = :m AND kind = 'deleted'",
        m=retired,
    )
    assert tuple(deleted) == (
        {"owner_principal": "p-author", "relations": [{"principal": "p-x", "relation": "editor"}]},
        "system",
    )

    tomorrow = clock.now() + timedelta(hours=25)
    later = await sweep(space.db, space.lib.media, now=tomorrow)
    assert later["assets_purged"] == 1
    assert await ids(space) == {in_draft, in_version, candidate}
    again = await sweep(space.db, space.lib.media, now=tomorrow)
    assert again == {"assets_purged": 0, "blobs_deleted": 0, "staged_removed": 0}

    await execute(space, "UPDATE catalog.character_catalog SET draft = '{}'::jsonb")
    freed = await sweep(space.db, space.lib.media)
    assert freed["assets_purged"] == 1
    assert await ids(space) == {in_version, candidate}


async def test_identical_bytes_share_one_blob(space: Scratch) -> None:
    """Two assets with the same bytes share one blob: purging one keeps the bytes, purging both
    deletes them."""
    data = png()
    first, second = await put(space, data), await put(space, data)
    stored = blob_path(space.root, sha256(data))
    assert [p.name for p in stored.parent.iterdir()] == [stored.name]
    await space.lib.delete_media(SWEEPER, first)
    assert (await sweep(space.db, space.lib.media))["assets_purged"] == 1
    assert stored.exists()
    await space.lib.delete_media(SWEEPER, second)
    counts = await sweep(space.db, space.lib.media)
    assert (counts["assets_purged"], counts["blobs_deleted"]) == (1, 1)
    assert not stored.exists()


async def test_the_recurring_job(space: Scratch) -> None:
    """The ``media_sweep`` job sweeps the store its process bound (``Engine.start_workers``)
    and records its outcome on its ``recurring_tasks`` row; with no store bound it is a no-op.
    The task's interval is the ``media_sweep_interval`` setting."""
    from epistrel.orchestrator.jobs import timers

    retired = await put(space)
    await space.lib.delete_media(SWEEPER, retired)
    await timers.reconcile(space.db)
    assert (
        await scalar(
            space.db,
            "SELECT extract(epoch FROM interval) FROM recurring_tasks WHERE name = 'media_sweep'",
        )
        == 3600
    )
    job = ClaimedJob(
        id=new_ulid(),
        kind=media_sweep_job.MEDIA_SWEEP,
        story_id=None,
        payload={},
        attempt=1,
        failures=0,
        due_at=clock.now(),
        lease_until=None,
        worker_id=None,
    )
    context = JobContext(db=space.db, job=job, payload=NoPayload())
    media_sweep_job.bind(None)
    idle = await media_sweep_job.MEDIA_SWEEP_HANDLER.run(context)
    assert idle.result == {"store": False}
    assert retired in await ids(space)
    await space.lib.start_workers("api")
    try:
        assert media_sweep_job.bound() is space.lib.media
        done = await media_sweep_job.MEDIA_SWEEP_HANDLER.run(context)
    finally:
        await space.lib.stop_workers()
    assert media_sweep_job.bound() is None
    assert done.result == {"assets_purged": 1, "blobs_deleted": 1, "staged_removed": 0}
    assert retired not in await ids(space)
    assert (
        await scalar(space.db, "SELECT last_status FROM recurring_tasks WHERE name = 'media_sweep'")
        == "ok"
    )


async def test_a_reference_added_mid_sweep_keeps_the_asset(space: Scratch) -> None:
    """The sweep checks an asset's references again once it holds the asset's row: an asset
    its batch found unreferenced, but which a catalog draft starts referencing before the sweep
    reaches it, is kept. Complement: the same sweep purges it once the draft lets go of it."""
    from epistrel.testing import pauses

    asset = await put(space)
    await space.lib.delete_media(SWEEPER, asset)
    with pauses.held(pauses.MEDIA_SWEEP_BEFORE_PURGE) as hold:
        swept = asyncio.ensure_future(sweep(space.db, space.lib.media))
        await hold.arrived(1)
        entry = await plant_in_draft(space, asset)
        hold.release()
        counts = await swept
    assert counts["assets_purged"] == 0
    assert asset in await ids(space)
    await execute(
        space, "UPDATE catalog.character_catalog SET draft = '{}'::jsonb WHERE id = :c", c=entry
    )
    assert (await sweep(space.db, space.lib.media))["assets_purged"] == 1
    assert asset not in await ids(space)


async def test_a_purge_waits_for_a_new_reference(space: Scratch) -> None:
    """A write that references an asset holds it ``FOR SHARE`` (``assert_referenceable``) until
    it commits; the sweep skips a row it cannot lock and re-checks the references of the rows it
    does lock, so it never purges an asset a write is adding a reference to."""
    from epistrel.core.media.assets import assert_referenceable

    asset = await put(space)
    later = clock.now() + timedelta(hours=25)
    async with space.db.begin() as conn:
        await assert_referenceable(conn, [asset])
        counts = await sweep(space.db, space.lib.media, now=later)
        assert counts["assets_purged"] == 0  # locked by the referencing write: skipped
        # The write that took the asset's share lock now references it, and commits.
        await conn.execute(
            text(
                "INSERT INTO catalog.character_catalog (id, owner_principal, listing_status, "
                "status, draft, draft_revision, created_at, updated_at) VALUES (:c, 'p', "
                "'private', 'draft', jsonb_build_object('images', "
                "jsonb_build_array(CAST(:m AS text))), 1, now(), now())"
            ),
            {"m": asset, "c": new_ulid()},
        )
    assert (await sweep(space.db, space.lib.media, now=later))["assets_purged"] == 0
    assert (await space.lib.get_media(SWEEPER, asset)).state is MediaState.LIVE


async def test_each_run_goes_on_where_the_last_stopped(
    space: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run handles a bounded number of rows and blobs, and the next run goes on from there, so
    the sweep eventually reaches everything, however much there is. With a run cut to two rows
    and two blobs: referenced assets older than a day, more of them than a run handles, never
    take the places of a retired, unreferenced asset created after them, which the first run
    purges; and a blob nothing names that sorts after more named blobs than a run scans is
    deleted by the second run, which goes on from the cursor the first one left. Once a run
    reaches the last blob the cursor is cleared, so the next run starts again at the first.
    Regression: every run started again at the oldest row and at the first blob, so neither was
    ever reached."""
    from epistrel.core.media import sweep as sweep_module

    monkeypatch.setattr(sweep_module, "BATCH", 2)
    monkeypatch.setattr(sweep_module, "MAX_BATCHES", 1)
    in_use = [await put(space) for _ in range(3)]
    for media_id in in_use:
        await plant_in_draft(space, media_id)
    retired = await put(space)
    await space.lib.delete_media(SWEEPER, retired)
    named = sorted([(await space.lib.get_media(SWEEPER, m)).checksum for m in in_use])
    orphan = "f" * 64  # sorts after every named blob
    left = blob_path(space.root, orphan)
    left.parent.mkdir(parents=True, exist_ok=True)
    left.write_bytes(b"left behind")
    aged(left, OLD)
    store = space.lib.media
    later = clock.now() + timedelta(hours=25)

    first = await sweep(space.db, store, now=later)
    assert first == {"assets_purged": 1, "blobs_deleted": 1, "staged_removed": 0}
    assert await ids(space) == set(in_use)
    assert left.exists()
    assert store.sweep_cursor() == named[1]

    second = await sweep(space.db, store, now=later)
    assert second == {"assets_purged": 0, "blobs_deleted": 1, "staged_removed": 0}
    assert not left.exists()
    assert store.sweep_cursor() is None
    assert all(blob_path(space.root, checksum).exists() for checksum in named)


async def _waiting_for_a_checksum_lock(space: Scratch, sweeping: asyncio.Future[Any]) -> None:
    """Return once a session waits for a checksum's advisory lock, or once ``sweeping`` has
    finished without waiting."""
    deadline = time.monotonic() + 10
    while not sweeping.done():
        waiting = await scalar(
            space.db,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
            "AND classid = CAST(:c AS oid) AND objsubid = 2",
            c=media.LOCK_CLASS,
        )
        if waiting:
            return
        assert time.monotonic() < deadline, "the sweep neither waited for the lock nor finished"
        await asyncio.sleep(0.02)


async def test_the_sweep_waits_for_an_upload_of_the_same_bytes(space: Scratch) -> None:
    """An upload holds its checksum's advisory lock from its insert to its commit, and the sweep
    deletes a blob only under that lock. An asset purged while an upload of the same bytes is
    committing (that upload's ``put`` found the blob in place and kept it) leaves the bytes: the
    sweep waits for the upload's commit, then finds its row. The new asset serves the bytes.
    Regression guard: without the lock, the sweep deleted the bytes under the uncommitted row,
    whose content then answered 404 ``bytes_missing``."""
    from epistrel.testing import pauses

    data = png()
    gone = await put(space, data)
    await space.lib.delete_media(SWEEPER, gone)
    stored = blob_path(space.root, sha256(data))
    with pauses.held(pauses.MEDIA_UPLOAD_BEFORE_COMMIT) as hold:
        uploading = asyncio.ensure_future(put(space, data))
        await hold.arrived(1)
        assert staged_files(space.root) == []  # ``put`` kept the blob and dropped its copy
        sweeping = asyncio.ensure_future(sweep(space.db, space.lib.media))
        await _waiting_for_a_checksum_lock(space, sweeping)
        hold.release()
        fresh = await uploading
        counts = await sweeping
    assert counts == {"assets_purged": 1, "blobs_deleted": 0, "staged_removed": 0}
    assert await ids(space) == {fresh}
    assert stored.exists()
    async with media_client(space.settings, space.db) as client:
        served = await client.get(f"/media/{fresh}/content", headers=READ)
    assert (served.status_code, served.content) == (200, data)
