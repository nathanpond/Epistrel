"""Large media delivery (FR-MEDIA-3; #107): byte ranges, signed URLs, resumable uploads and video
posters, on both blob stores.

C-23 is ``test_c_23__delivery`` (its M3 part for FR-MEDIA-3), run once over the local store and
once over the S3-compatible one (MinIO in a container, ``tests.conftest.MINIO_IMAGE``). Every
C-23 (M3) part lives in this file, because traceability counts a test ID per milestone in one
file: #115's ``test_c_23__optional_images`` (FR-CAT-4) joins it here. The detailed tests below
it carry no requirement ID. Each runs on a scratch database with its own media root (and, on
S3, its own bucket), because the store kind and the sweep are deployment-wide.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import httpx2 as httpx
import pytest
from botocore.config import Config
from sqlalchemy import text

from epistrel.api import create_app
from epistrel.core import clock
from epistrel.core.clock import frozen_at
from epistrel.core.media.signing import CLOCK_SKEW_S
from epistrel.engine import Engine
from tests.auth_support import auth_settings
from tests.conftest import S3Endpoint
from tests.media_support import (
    READ,
    WRITE,
    Scratch,
    SpyPosters,
    count,
    files,
    large_png,
    metadata,
    png,
    scratch,
    set_media,
    sha256,
    staged_files,
    video,
)
from tests.model_support import config_key

ADAPTERS = ("local", "s3")
PUBLIC_BASE = "https://media.example.test/epistrel"
SKEW_S = CLOCK_SKEW_S
"""How long past its expiry a local signed URL still works (named for C-23's text, which may not
use M6's capability word for a time source)."""
PART = 8 * 1024 * 1024


def _error(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


def new_bucket(s3: S3Endpoint) -> str:
    """A fresh bucket on the test store."""
    name = f"epistrel-{secrets.token_hex(6)}"
    s3_client(s3).create_bucket(Bucket=name)
    return name


def s3_client(s3: S3Endpoint) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=s3.url,
        aws_access_key_id=s3.access_key,
        aws_secret_access_key=s3.secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def s3_overrides(s3: S3Endpoint, bucket: str) -> dict[str, Any]:
    return {
        "media_store": "s3",
        "media_s3_endpoint": s3.url,
        "media_s3_bucket": bucket,
        "media_s3_access_key": s3.access_key,
        "media_s3_secret_key": s3.secret_key,
        "media_s3_path_style": True,
    }


@dataclass
class Delivery:
    """A scratch deployment on one store: its database, the REST client, the app's Engine and
    the spy on its ffmpeg seam."""

    space: Scratch
    client: httpx.AsyncClient
    engine: Engine
    posters: SpyPosters
    adapter: str
    bucket: str | None


@asynccontextmanager
async def delivery(
    pg_url: str, root: Path, adapter: str, s3: S3Endpoint | None, **overrides: Any
) -> AsyncIterator[Delivery]:
    """A scratch database, a media root of its own and (S3) a bucket of its own, served by an
    app whose ffmpeg seam is spied on."""
    extra: dict[str, Any] = {"config_key": config_key(), **overrides}
    bucket: str | None = None
    if adapter == "s3":
        assert s3 is not None
        bucket = new_bucket(s3)
        extra.update(s3_overrides(s3, bucket))
    async with scratch(pg_url, root, **extra) as space:
        app = create_app(space.settings)
        engine: Engine = app.state.epistrel
        engine.bind(space.db)
        engine.prepare_media()
        spy = SpyPosters(engine.posters)
        engine.posters = spy
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Delivery(space, client, engine, spy, adapter, bucket)


def _s3(request: pytest.FixtureRequest, adapter: str) -> S3Endpoint | None:
    """MinIO, started only for the S3 adapter's runs."""
    if adapter != "s3":
        return None
    endpoint: S3Endpoint = request.getfixturevalue("minio")
    return endpoint


async def _upload_video(client: httpx.AsyncClient, data: bytes, **fields: Any) -> httpx.Response:
    return await client.post(
        "/media",
        files=files(data, metadata(kind="video", **fields), content_type="video/mp4"),
        headers=WRITE,
    )


async def _put_part(
    client: httpx.AsyncClient, upload_id: str, n: int, data: bytes, **headers: str
) -> httpx.Response:
    return await client.put(
        f"/media/uploads/{upload_id}/parts/{n}",
        content=data,
        headers={**WRITE, "Content-Type": "application/octet-stream", **headers},
    )


def _parts(data: bytes, size: int = PART) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


async def _open(client: httpx.AsyncClient, data: bytes, **fields: Any) -> dict[str, Any]:
    body = {
        **metadata(),
        "size": len(data),
        "mime_type": "image/png",
        "sha256": sha256(data),
        **fields,
    }
    opened = await client.post("/media/uploads", json=body, headers=WRITE)
    assert opened.status_code == 201, opened.text
    found: dict[str, Any] = opened.json()
    return found


# -- C-23 ------------------------------------------------------------------------------------------


@pytest.mark.part("M3", "FR-MEDIA-3")
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_c_23__delivery(
    adapter: str, pg_url: str, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """On both blob stores (local, and S3 on MinIO): a video uploaded without a poster gets one
    extracted from its first frame (a PNG asset of the same owner, dimensions > 0, through the
    ffmpeg runner); one uploaded with a poster keeps it and never runs ffmpeg (a spy on the
    runner). ``Range: bytes=100-199`` is 206 with exactly those 100 bytes of the file, and
    ``bytes=999999-`` is 416. ``GET /media/{id}/url`` returns a URL that serves the bytes with
    no credential and then expires (TTL 2 s): S3's presigned URL answers 403 from the store
    afterwards; the local store's signed content URL answers 403 ``url_expired`` once past its
    expiry and the 30 s skew. A large upload whose second part breaks midway resumes from
    the session's part list and completes with the declared checksum; a wrong declared
    checksum is 422 and stores no asset.

    Complements asserted: without ``Range`` the whole file is 200; before its expiry the URL
    serves the bytes, and on the local store a request with neither credential nor signature is
    401; the broken part really was refused (it is not in the part list); and with
    ``EPISTREL_FFMPEG_PATH`` naming no binary a video without a poster is refused 422
    ``poster_required`` naming the setting, with no asset, blob or staged file left."""
    s3 = _s3(request, adapter)
    async with delivery(pg_url, tmp_path / "media", adapter, s3) as d:
        client = d.client

        # -- a poster for every video: extracted from the first frame, or kept when supplied
        clip = video(320, 240)
        made = await _upload_video(client, clip, caption="The storm")
        assert made.status_code == 201, made.text
        movie = made.json()
        assert (movie["kind"], movie["mime_type"], movie["width"]) == ("video", "video/mp4", 320)
        assert movie["duration_ms"] == 1000
        assert movie["poster_media_id"] is not None
        assert len(d.posters.extracted) == 1
        poster = (await client.get(f"/media/{movie['poster_media_id']}", headers=READ)).json()
        assert (poster["kind"], poster["mime_type"], poster["source"]) == (
            "image",
            "image/png",
            "extracted",
        )
        assert poster["width"] > 0
        assert poster["height"] > 0
        assert (poster["width"], poster["height"]) == (320, 240)
        assert poster["owner_principal"] == movie["owner_principal"]
        assert poster["content_rating_id"] == movie["content_rating_id"]
        assert poster["alt_text"] == "Poster for The storm"
        frame = await client.get(f"/media/{poster['id']}/content", headers=READ)
        assert frame.content.startswith(b"\x89PNG")

        cover = await client.post("/media", files=files(png(8, 6), metadata()), headers=WRITE)
        assert cover.status_code == 201, cover.text
        kept = await _upload_video(client, video(), poster=cover.json()["id"])
        assert kept.status_code == 201, kept.text
        assert kept.json()["poster_media_id"] == cover.json()["id"]
        assert len(d.posters.extracted) == 1  # a supplied poster runs no ffmpeg

        # -- byte ranges
        content = f"/media/{movie['id']}/content"
        whole = await client.get(content, headers=READ)
        assert (whole.status_code, whole.content) == (200, clip)
        assert whole.headers["accept-ranges"] == "bytes"
        part = await client.get(content, headers={**READ, "Range": "bytes=100-199"})
        assert part.status_code == 206, part.text
        assert part.content == clip[100:200]
        assert len(part.content) == 100
        assert part.headers["content-range"] == f"bytes 100-199/{len(clip)}"
        assert part.headers["accept-ranges"] == "bytes"
        past = await client.get(content, headers={**READ, "Range": "bytes=999999-"})
        assert _error(past)[:2] == (416, "range_not_satisfiable")
        assert past.headers["content-range"] == f"bytes */{len(clip)}"

        # -- a signed URL that works, then expires
        await set_media(client, media_signed_url_ttl=2, public_base_url=PUBLIC_BASE)
        issued = await client.get(f"/media/{movie['id']}/url", params={"ttl": 60}, headers=READ)
        assert issued.status_code == 200, issued.text
        url = issued.json()["url"]
        expires = datetime.fromisoformat(issued.json()["expires_at"])
        assert expires - datetime.now(UTC) <= timedelta(seconds=2)  # 60 capped at the setting
        if adapter == "s3":
            assert s3 is not None
            assert url.startswith(f"{s3.url}/")
            async with httpx.AsyncClient() as outside:
                served = await outside.get(url)
                assert (served.status_code, served.content) == (200, clip)
                await asyncio.sleep(3.5)
                expired = await outside.get(url)
                assert expired.status_code == 403
        else:
            assert url.startswith(f"{PUBLIC_BASE}/media/{movie['id']}/content?exp=")
            path = url.removeprefix(PUBLIC_BASE)
            served = await client.get(path)  # no credential
            assert (served.status_code, served.content) == (200, clip)
            ranged = await client.get(path, headers={"Range": "bytes=100-199"})
            assert (ranged.status_code, ranged.content) == (206, clip[100:200])
            assert (await client.get(content)).status_code == 401  # neither credential nor URL
            exp = int(parse_qs(urlsplit(path).query)["exp"][0])
            with frozen_at(datetime.fromtimestamp(exp + SKEW_S + 1, UTC)):
                expired = await client.get(path)
            assert _error(expired)[:2] == (403, "url_expired")

        # -- a resumable upload, interrupted midway and resumed from its part list
        before = await count(d.space.db, "SELECT count(*) FROM media_assets")
        data = large_png(2 * PART + 4096)
        parts = _parts(data)
        assert len(parts) == 3
        session = await _open(client, data)
        assert (session["part_size"], session["part_count"]) == (PART, 3)
        upload_id = session["upload_id"]
        assert (await _put_part(client, upload_id, 1, parts[0])).status_code == 200
        broken = await _put_part(
            client, upload_id, 2, parts[1][: PART // 2], **{"Content-Length": str(PART)}
        )
        assert _error(broken)[:2] == (422, "part_invalid")
        assert (await _put_part(client, upload_id, 3, parts[2])).status_code == 200
        resume = await client.get(f"/media/uploads/{upload_id}", headers=READ)
        assert resume.json()["received_parts"] == [1, 3]
        assert resume.json()["state"] == "open"
        missing = [n for n in range(1, 4) if n not in resume.json()["received_parts"]]
        for n in missing:
            sent = await _put_part(client, upload_id, n, parts[n - 1])
            assert sent.status_code == 200, sent.text
        done = await client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
        assert done.status_code == 201, done.text
        assert done.json()["checksum"] == sha256(data) == hashlib.sha256(data).hexdigest()
        assert done.headers["location"] == f"/media/{done.json()['id']}"
        fetched = await client.get(f"/media/{done.json()['id']}/content", headers=READ)
        assert fetched.content == data

        # -- a wrong declared checksum: 422, and no asset
        wrong = await _open(client, data, sha256=sha256(b"something else"))
        for n, chunk in enumerate(parts, start=1):
            assert (await _put_part(client, wrong["upload_id"], n, chunk)).status_code == 200
        assets = await count(d.space.db, "SELECT count(*) FROM media_assets")
        refused = await client.post(f"/media/uploads/{wrong['upload_id']}/complete", headers=WRITE)
        assert _error(refused)[:2] == (422, "checksum_mismatch")
        assert await count(d.space.db, "SELECT count(*) FROM media_assets") == assets
        assert assets == before + 1 + 0  # only the completed upload added an asset

    # -- without ffmpeg, a video without a poster is refused and nothing remains
    async with delivery(
        pg_url, tmp_path / "bare", adapter, s3, ffmpeg_path=str(tmp_path / "no-ffmpeg")
    ) as bare:
        clip = video()
        refused_video = await _upload_video(bare.client, clip)
        status, code, details = _error(refused_video)
        assert (status, code, details["setting"]) == (422, "poster_required", "ffmpeg_path")
        assert await count(bare.space.db, "SELECT count(*) FROM media_assets") == 0
        assert not bare.engine.media.exists(sha256(clip))
        assert staged_files(bare.space.root) == []


# -- helpers' own checks -------------------------------------------------------------------------


def test_the_test_video_is_a_real_mp4() -> None:
    """``video()`` makes a playable MP4 each time, with bytes of its own."""
    first, second = video(), video()
    assert first[4:8] == b"ftyp"
    assert first != second


# -- ranges, conditions and HEAD ---------------------------------------------------------------


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_ranges_follow_rfc_9110(
    adapter: str, pg_url: str, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """One range only, as RFC 9110 reads it, on either store: an open-ended range runs to the
    end, a suffix range takes the last bytes, a range past the end is clamped, ``bytes=-0`` has
    no byte (416); several ranges, another unit, a backwards or malformed range get the whole
    file (200). ``If-None-Match`` naming the ``ETag`` (or ``*``, or weakly) is 304 with no body,
    before any range; ``If-Range`` applies the range only when it is the ``ETag``. ``HEAD``
    answers the headers only. The content answer carries ``Access-Control-Allow-Origin: *`` and
    exposes ``Accept-Ranges``; the metadata answer does not."""
    s3 = _s3(request, adapter)
    async with delivery(pg_url, tmp_path / "media", adapter, s3) as d:
        client = d.client
        data = png(4, 4, nonce=secrets.token_hex(400))
        size = len(data)
        asset = await client.post("/media", files=files(data, metadata()), headers=WRITE)
        assert asset.status_code == 201, asset.text
        path = f"/media/{asset.json()['id']}/content"
        etag = f'"{asset.json()["checksum"]}"'

        async def get(range_value: str | None = None, **headers: str) -> httpx.Response:
            sent = {**READ, **headers}
            if range_value is not None:
                sent["Range"] = range_value
            return await client.get(path, headers=sent)

        for value, (start, end) in {
            "bytes=10-": (10, size - 1),
            "bytes=-25": (size - 25, size - 1),
            "bytes=0-0": (0, 0),
            f"bytes=5-{size + 1000}": (5, size - 1),
            "BYTES = 3 - 7": (3, 7),
            f"bytes=-{size * 2}": (0, size - 1),
        }.items():
            answer = await get(value)
            assert answer.status_code == 206, (value, answer.text)
            assert answer.content == data[start : end + 1], value
            assert answer.headers["content-range"] == f"bytes {start}-{end}/{size}", value
            assert int(answer.headers["content-length"]) == end - start + 1
        for value in ("bytes=0-1,4-5", "items=0-1", "bytes=9-3", "bytes=abc", "bytes=-", "x"):
            answer = await get(value)
            assert (answer.status_code, answer.content) == (200, data), value
            assert "content-range" not in answer.headers
        for value in ("bytes=-0", f"bytes={size}-"):
            refused = await get(value)
            assert _error(refused)[:2] == (416, "range_not_satisfiable"), value
            assert refused.headers["content-range"] == f"bytes */{size}"

        for tags in (etag, "*", f"W/{etag}", f'"other", {etag}'):
            same = await get("bytes=0-9", **{"If-None-Match": tags})
            assert (same.status_code, same.content) == (304, b""), tags
            assert same.headers["etag"] == etag
        changed = await get(**{"If-None-Match": '"something-else"'})
        assert (changed.status_code, changed.content) == (200, data)
        assert (await get("bytes=0-9", **{"If-Range": etag})).status_code == 206
        for stale in ('"something-else"', f"W/{etag}", "Wed, 21 Oct 2015 07:28:00 GMT"):
            whole = await get("bytes=0-9", **{"If-Range": stale})
            assert (whole.status_code, whole.content) == (200, data), stale

        head = await client.head(path, headers=READ)
        assert (head.status_code, head.content) == (200, b"")
        assert int(head.headers["content-length"]) == size
        assert (head.headers["accept-ranges"], head.headers["etag"]) == ("bytes", etag)
        assert head.headers["content-type"] == "image/png"
        ranged_head = await client.head(path, headers={**READ, "Range": "bytes=2-5"})
        assert ranged_head.status_code == 206
        assert ranged_head.headers["content-range"] == f"bytes 2-5/{size}"
        assert int(ranged_head.headers["content-length"]) == 4

        whole = await get()
        assert whole.headers["access-control-allow-origin"] == "*"
        assert "Accept-Ranges" in whole.headers["access-control-expose-headers"]
        described = await client.get(f"/media/{asset.json()['id']}", headers=READ)
        assert "access-control-allow-origin" not in described.headers
        if adapter == "s3":
            assert s3 is not None
            assert d.bucket is not None
            key = f"{sha256(data)[:2]}/{sha256(data)}"
            stored = s3_client(s3).head_object(Bucket=d.bucket, Key=key)
            assert stored["ContentType"] == "image/png"
            assert stored["CacheControl"] == "private, max-age=31536000, immutable"
            assert stored["ContentLength"] == size
            assert not (d.space.root / key).exists()  # the bytes are in the bucket only


# -- signed URLs -------------------------------------------------------------------------------


async def test_signed_urls_on_the_local_store(pg_url: str, tmp_path: Path) -> None:
    """``GET /media/{id}/url`` on the local store: ``public_base_url`` + the signed content
    path, valid ``media_signed_url_ttl`` seconds (default 300; a ``ttl`` is cut to it). Without
    ``public_base_url`` it is 503 ``config_missing`` naming it; without
    ``EPISTREL_CONFIG_KEY`` 503 ``config_key_missing``. The URL alone reads the content (``GET``
    and ``HEAD``, with ranges), the asset's only: another asset's path with it, or a tampered
    signature, is 403 ``signature_invalid``. With the service credential a bad signature is
    ignored. A retired asset still gets a URL; an unknown one is 404."""
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        data, other = png(), png()
        first = (await client.post("/media", files=files(data, metadata()), headers=WRITE)).json()
        second = (await client.post("/media", files=files(other, metadata()), headers=WRITE)).json()
        missing = await client.get(f"/media/{first['id']}/url", headers=READ)
        assert _error(missing) == (
            503,
            "config_missing",
            {"setting": "public_base_url"},
        )
        await set_media(client, public_base_url=f"{PUBLIC_BASE}/")
        started = datetime.now(UTC)
        issued = (await client.get(f"/media/{first['id']}/url", headers=READ)).json()
        expires = datetime.fromisoformat(issued["expires_at"])
        assert timedelta(seconds=295) < expires - started <= timedelta(seconds=301)
        shorter = (await client.get(f"/media/{first['id']}/url?ttl=30", headers=READ)).json()
        assert datetime.fromisoformat(shorter["expires_at"]) - started <= timedelta(seconds=31)
        url = issued["url"]
        assert url.startswith(f"{PUBLIC_BASE}/media/{first['id']}/content?exp=")
        path = url.removeprefix(PUBLIC_BASE)
        query = path.split("?", 1)[1]
        assert (await client.get(path)).content == data
        head = await client.head(path)
        assert (head.status_code, int(head.headers["content-length"])) == (200, len(data))
        assert (await client.get(path, headers={"Range": "bytes=0-3"})).content == data[:4]
        stolen = await client.get(f"/media/{second['id']}/content?{query}")
        assert _error(stolen)[:2] == (403, "signature_invalid")
        tampered = path[:-2] + ("AA" if not path.endswith("AA") else "BB")
        assert _error(await client.get(tampered))[:2] == (403, "signature_invalid")
        assert (await client.get(tampered, headers=READ)).content == data  # the credential decides
        exp = int(parse_qs(query)["exp"][0])
        with clock.frozen_at(datetime.fromtimestamp(exp + CLOCK_SKEW_S + 1, UTC)):
            assert _error(await client.get(path))[:2] == (403, "url_expired")
            assert (await client.get(path, headers=READ)).status_code == 200
        with clock.frozen_at(datetime.fromtimestamp(exp + CLOCK_SKEW_S, UTC)):
            assert (await client.get(path)).status_code == 200  # the skew's last second
        assert (await client.get(f"/media/{first['id']}/content")).status_code == 401
        assert (await client.delete(f"/media/{first['id']}", headers=WRITE)).status_code == 204
        retired = await client.get(f"/media/{first['id']}/url", headers=READ)
        assert retired.status_code == 200
        unknown = await client.get(f"/media/{'0' * 26}/url", headers=READ)
        assert _error(unknown)[:2] == (404, "not_found")
        bad_ttl = await client.get(f"/media/{second['id']}/url?ttl=0", headers=READ)
        assert _error(bad_ttl)[:2] == (422, "validation_error")
    async with delivery(pg_url, tmp_path / "keyless", "local", None, config_key=None) as d:
        made = await d.client.post("/media", files=files(png(), metadata()), headers=WRITE)
        await set_media(d.client, public_base_url=PUBLIC_BASE)
        keyless = await d.client.get(f"/media/{made.json()['id']}/url", headers=READ)
        assert _error(keyless)[:2] == (503, "config_key_missing")


# -- resumable uploads ---------------------------------------------------------------------------


async def test_direct_uploads_above_the_threshold_use_sessions(pg_url: str, tmp_path: Path) -> None:
    """``POST /media`` larger than ``media_multipart_threshold`` is 413 ``use_multipart``: by
    its declared length before it is read, or, sent without one, once the bytes pass it. A
    session is allowed below the threshold too. Nothing is stored for a refusal."""
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        await set_media(client, media_multipart_threshold=1024)
        big = large_png(300_000)
        declared = await client.post("/media", files=files(big, metadata()), headers=WRITE)
        status, code, details = _error(declared)
        assert (status, code, details["threshold"]) == (413, "use_multipart", 1024)
        assert details["size"] > 1024

        async def body() -> AsyncIterator[bytes]:
            boundary = "b" * 16
            meta = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
                "Content-Type: application/json\r\n\r\n"
            ).encode() + json.dumps(metadata()).encode()
            yield meta
            yield (
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="a.png"\r\nContent-Type: image/png\r\n\r\n'
            ).encode()
            yield big
            yield f"\r\n--{boundary}--\r\n".encode()

        streamed = await client.post(
            "/media",
            content=body(),
            headers={**WRITE, "Content-Type": f"multipart/form-data; boundary={'b' * 16}"},
        )
        assert _error(streamed)[:2] == (413, "use_multipart")
        assert await count(d.space.db, "SELECT count(*) FROM media_assets") == 0
        assert staged_files(d.space.root) == []
        small = png()
        session = await _open(client, small)
        assert (await _put_part(client, session["upload_id"], 1, small)).status_code == 200
        done = await client.post(f"/media/uploads/{session['upload_id']}/complete", headers=WRITE)
        assert done.status_code == 201, done.text
        assert done.json()["checksum"] == sha256(small)


async def test_parts_are_checked(pg_url: str, tmp_path: Path) -> None:
    """A part numbered past the last is 422 ``part_invalid`` (``number``), one of the wrong size
    422 ``part_invalid`` (``size``), one without a ``Content-Length`` (chunked) 411
    ``length_required``, one whose ``X-Part-SHA256`` is not its hash 422 ``checksum_mismatch``,
    and a malformed ``X-Part-SHA256`` 422 at the header. A part sent again replaces the one
    received, so a wrong part sent first does not spoil the upload. An unknown session is
    404."""
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        data = large_png(PART + 10_000)
        parts = _parts(data)
        session = await _open(client, data)
        upload_id = session["upload_id"]
        beyond = await _put_part(client, upload_id, 3, b"x")
        assert _error(beyond) == (
            422,
            "part_invalid",
            {"part": 3, "reason": "number", "part_count": 2},
        )
        wrong_size = await _put_part(client, upload_id, 2, parts[1] + b"!")
        assert _error(wrong_size)[2]["reason"] == "size"

        async def chunks() -> AsyncIterator[bytes]:
            yield parts[1]

        chunked = await client.put(
            f"/media/uploads/{upload_id}/parts/2", content=chunks(), headers=WRITE
        )
        assert _error(chunked)[:2] == (411, "length_required")
        lied = await _put_part(
            client, upload_id, 2, parts[1], **{"X-Part-SHA256": sha256(b"something else")}
        )
        assert _error(lied)[:2] == (422, "checksum_mismatch")
        assert _error(lied)[2]["actual"] == sha256(parts[1])
        malformed = await _put_part(client, upload_id, 2, parts[1], **{"X-Part-SHA256": "XYZ"})
        assert _error(malformed)[:2] == (422, "validation_error")
        assert _error(malformed)[2]["field"] == "headers.X-Part-SHA256"
        garbage = bytes(len(parts[0]))
        assert (await _put_part(client, upload_id, 1, garbage)).status_code == 200
        replaced = await _put_part(client, upload_id, 1, parts[0])
        assert replaced.json()["received_parts"] == [1]
        checked = await _put_part(
            client, upload_id, 2, parts[1], **{"X-Part-SHA256": sha256(parts[1])}
        )
        assert checked.status_code == 200, checked.text
        assert checked.json() == {
            "n": 2,
            "size": len(parts[1]),
            "received_parts": [1, 2],
            "config_revision": checked.json()["config_revision"],
            "idempotency_key": checked.headers["Idempotency-Key"],
        }
        done = await client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
        assert done.status_code == 201, done.text
        assert done.json()["checksum"] == sha256(data)
        unknown = await _put_part(client, "0" * 26, 1, b"x")
        assert _error(unknown)[:2] == (404, "not_found")


async def test_a_session_from_open_to_close(pg_url: str, tmp_path: Path) -> None:
    """A session reads back as ``{id, state, size, mime_type, part_size, part_count,
    received_parts, expires_at, media_id}``; every part moves its expiry. Completing it before
    every part arrived is 409 ``upload_incomplete`` naming the missing parts. A completion
    retried under its ``Idempotency-Key`` replays the asset; a second completion is 409
    ``upload_closed``, as is a part after it, and ``DELETE`` then changes nothing (204). An
    open session aborted with ``DELETE`` (204) loses its parts and refuses more. Opening,
    completing and aborting each record a ``control`` operation by the requester."""
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        data = large_png(PART + 5_000)
        parts = _parts(data)
        session = await _open(client, data)
        upload_id = session["upload_id"]
        read = (await client.get(f"/media/uploads/{upload_id}", headers=READ)).json()
        assert read == {
            "id": upload_id,
            "state": "open",
            "size": len(data),
            "mime_type": "image/png",
            "part_size": PART,
            "part_count": 2,
            "received_parts": [],
            "expires_at": read["expires_at"],
            "media_id": None,
        }
        opened_until = datetime.fromisoformat(read["expires_at"])
        await asyncio.sleep(0.01)
        assert (await _put_part(client, upload_id, 2, parts[1])).status_code == 200
        moved = (await client.get(f"/media/uploads/{upload_id}", headers=READ)).json()
        assert datetime.fromisoformat(moved["expires_at"]) > opened_until
        early = await client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
        assert _error(early) == (
            409,
            "upload_incomplete",
            {"upload_id": upload_id, "missing": [1]},
        )
        assert (await _put_part(client, upload_id, 1, parts[0])).status_code == 200
        key = f"done-{secrets.token_hex(4)}"
        done = await client.post(
            f"/media/uploads/{upload_id}/complete", headers={**WRITE, "Idempotency-Key": key}
        )
        assert done.status_code == 201, done.text
        again = await client.post(
            f"/media/uploads/{upload_id}/complete", headers={**WRITE, "Idempotency-Key": key}
        )
        assert (again.status_code, again.headers.get("Idempotent-Replay")) == (201, "true")
        assert again.json()["id"] == done.json()["id"]
        closed = await client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
        assert _error(closed)[:2] == (409, "upload_closed")
        assert _error(closed)[2]["state"] == "completed"
        late = await _put_part(client, upload_id, 1, parts[0])
        assert _error(late)[:2] == (409, "upload_closed")
        assert (
            await client.delete(f"/media/uploads/{upload_id}", headers=WRITE)
        ).status_code == 204
        final = (await client.get(f"/media/uploads/{upload_id}", headers=READ)).json()
        assert (final["state"], final["media_id"]) == ("completed", done.json()["id"])
        assert not (d.space.root / "uploads" / upload_id).exists()  # its parts went

        dropped = await _open(client, data)
        dropped_id = dropped["upload_id"]
        assert (await _put_part(client, dropped_id, 1, parts[0])).status_code == 200
        assert (d.space.root / "uploads" / dropped_id / "1").is_file()
        aborted = await client.delete(f"/media/uploads/{dropped_id}", headers=WRITE)
        assert aborted.status_code == 204
        assert not (d.space.root / "uploads" / dropped_id).exists()
        gone = (await client.get(f"/media/uploads/{dropped_id}", headers=READ)).json()
        assert gone["state"] == "aborted"
        refused = await _put_part(client, dropped_id, 2, parts[1])
        assert _error(refused)[:2] == (409, "upload_closed")
        assert (
            await client.delete(f"/media/uploads/{dropped_id}", headers=WRITE)
        ).status_code == 204
        recorded = await count(
            d.space.db,
            "SELECT count(*) FROM operations WHERE kind = 'control' AND principal = :p",
            p=WRITE["X-Epistrel-Principal"],
        )
        assert recorded == 4  # two opens, one completion, one abort


async def test_opening_checks_what_it_can(pg_url: str, tmp_path: Path) -> None:
    """``POST /media/uploads`` refuses at once, storing nothing: a size over
    ``media_max_bytes`` (413), a type the deployment does not allow or that is not the
    ``kind`` (415), a retired rating (422), undeclared subjects (422 ``subjects_required`` at
    ``body.depicted_subjects``), a ``poster`` on an image (422 at ``body.poster``), a poster
    that is no image (``poster_invalid``) or no live asset (``media_retired``), and a video
    without a poster when ffmpeg is missing (``poster_required``)."""
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        image = (await client.post("/media", files=files(png(), metadata()), headers=WRITE)).json()
        clip = await _upload_video(client, video(), poster=image["id"])
        assert clip.status_code == 201, clip.text
        base = {**metadata(), "size": 10, "mime_type": "image/png", "sha256": "a" * 64}
        cases: list[tuple[dict[str, Any], tuple[int, str]]] = [
            ({"size": 60 * 1024 * 1024}, (413, "media_too_large")),
            ({"mime_type": "image/gif"}, (415, "media_type_not_allowed")),
            ({"mime_type": "video/mp4"}, (415, "media_type_not_allowed")),
            ({"content_rating_id": "0" * 26}, (422, "rating_retired")),
            ({"poster": image["id"]}, (422, "validation_error")),
            (
                {"kind": "video", "mime_type": "video/mp4", "poster": clip.json()["id"]},
                (422, "poster_invalid"),
            ),
            (
                {"kind": "video", "mime_type": "video/mp4", "poster": "0" * 26},
                (422, "media_retired"),
            ),
        ]
        for change, expected in cases:
            refused = await client.post("/media/uploads", json={**base, **change}, headers=WRITE)
            assert _error(refused)[:2] == expected, (change, refused.text)
        undeclared = {k: v for k, v in base.items() if k != "depicted_subjects"}
        refused = await client.post("/media/uploads", json=undeclared, headers=WRITE)
        assert _error(refused)[:2] == (422, "subjects_required")
        assert _error(refused)[2]["field"] == "body.depicted_subjects"
        on_image = await client.post(
            "/media/uploads", json={**base, "poster": image["id"]}, headers=WRITE
        )
        assert _error(on_image)[2]["field"] == "body.poster"
        assert await count(d.space.db, "SELECT count(*) FROM media_upload_sessions") == 0
    async with delivery(
        pg_url, tmp_path / "bare", "local", None, ffmpeg_path=str(tmp_path / "none")
    ) as bare:
        clipless = {**base, "kind": "video", "mime_type": "video/mp4"}
        refused = await bare.client.post("/media/uploads", json=clipless, headers=WRITE)
        assert _error(refused) == (422, "poster_required", {"setting": "ffmpeg_path"})
        assert await count(bare.space.db, "SELECT count(*) FROM media_upload_sessions") == 0


async def test_a_second_completion_waits_and_finds_it_closed(pg_url: str, tmp_path: Path) -> None:
    """Two completions at once: the second waits on the session's row lock while the first
    completes, then answers 409 ``upload_closed``; one asset is created."""
    from epistrel.testing import pauses

    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        data = png()
        session = await _open(client, data)
        upload_id = session["upload_id"]
        assert (await _put_part(client, upload_id, 1, data)).status_code == 200
        with pauses.held(pauses.MEDIA_COMPLETE_LOCKED) as hold:
            first = asyncio.create_task(
                client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
            )
            await hold.arrived()
            second = asyncio.create_task(
                client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
            )
            deadline = time.monotonic() + 10
            while True:
                waiting = await count(
                    d.space.db,
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'",
                )
                if waiting or second.done():
                    break
                assert time.monotonic() < deadline, "the second completion never waited"
                await asyncio.sleep(0.02)
            hold.release()
            answers = await asyncio.gather(first, second)
        assert sorted(a.status_code for a in answers) == [201, 409]
        assert [_error(a)[1] for a in answers if a.status_code == 409] == ["upload_closed"]
        assert await count(d.space.db, "SELECT count(*) FROM media_assets") == 1


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_refused_completion_leaves_the_session_open(
    adapter: str, pg_url: str, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """A completion the moderation hook refuses (422 ``moderation_rejected``), and one whose
    assembled bytes are not the declared SHA-256 (422 ``checksum_mismatch``), store no asset
    and leave the session open with its parts: once the hook accepts, or once the wrong part
    is sent again, the same session completes. On S3 the store's multipart upload, completed
    by the refused attempt, is re-opened from the assembled object."""
    from tests.media_support import hook_server

    s3 = _s3(request, adapter)
    async with delivery(pg_url, tmp_path / "media", adapter, s3) as d:
        client = d.client
        data = large_png(PART + 7_000)
        parts = _parts(data)
        session = await _open(client, data)
        upload_id = session["upload_id"]
        for n, chunk in enumerate(parts, start=1):
            assert (await _put_part(client, upload_id, n, chunk)).status_code == 200
        with hook_server(answer={"decision": "reject", "reason": "no"}) as hook:
            await set_media(client, media_moderation_webhook_url=hook.url)
            rejected = await client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
            assert _error(rejected)[:2] == (422, "moderation_rejected")
            hook.answer = {"decision": "accept"}
            state = (await client.get(f"/media/uploads/{upload_id}", headers=READ)).json()
            assert (state["state"], state["received_parts"]) == ("open", [1, 2])
            accepted = await client.post(f"/media/uploads/{upload_id}/complete", headers=WRITE)
            assert accepted.status_code == 201, accepted.text
            assert accepted.json()["checksum"] == sha256(data)
            assert len(hook.received) == 2
            await set_media(client, media_moderation_webhook_url=None)
        assert await count(d.space.db, "SELECT count(*) FROM media_assets") == 1

        other = large_png(PART + 9_000)
        pieces = _parts(other)
        fixed = await _open(client, other)
        fixed_id = fixed["upload_id"]
        assert (await _put_part(client, fixed_id, 1, bytes(len(pieces[0])))).status_code == 200
        assert (await _put_part(client, fixed_id, 2, pieces[1])).status_code == 200
        mismatch = await client.post(f"/media/uploads/{fixed_id}/complete", headers=WRITE)
        assert _error(mismatch)[:2] == (422, "checksum_mismatch")
        assert _error(mismatch)[2]["declared"] == sha256(other)
        assert (await _put_part(client, fixed_id, 1, pieces[0])).status_code == 200
        completed = await client.post(f"/media/uploads/{fixed_id}/complete", headers=WRITE)
        assert completed.status_code == 201, completed.text
        assert completed.json()["checksum"] == sha256(other)
        served = await client.get(f"/media/{completed.json()['id']}/content", headers=READ)
        assert served.content == other
        if adapter == "s3":
            assert s3 is not None
            assert d.bucket is not None
            listed = s3_client(s3).list_objects_v2(Bucket=d.bucket, Prefix="uploads/")
            assert listed.get("KeyCount", 0) == 0  # every assembled object went


async def test_a_large_video_gets_its_poster_when_it_completes(pg_url: str, tmp_path: Path) -> None:
    """A video sent through a session without a poster gets one extracted from its first frame
    when the session completes; one opened with ``poster`` keeps it."""
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        clip = video(200, 100)
        session = await _open(client, clip, kind="video", mime_type="video/mp4")
        assert (await _put_part(client, session["upload_id"], 1, clip)).status_code == 200
        done = await client.post(f"/media/uploads/{session['upload_id']}/complete", headers=WRITE)
        assert done.status_code == 201, done.text
        poster = (await client.get(f"/media/{done.json()['poster_media_id']}", headers=READ)).json()
        assert (poster["source"], poster["width"], poster["height"]) == ("extracted", 200, 100)
        assert len(d.posters.extracted) == 1
        image = (await client.post("/media", files=files(png(), metadata()), headers=WRITE)).json()
        other = video()
        named = await _open(client, other, kind="video", mime_type="video/mp4", poster=image["id"])
        assert (await _put_part(client, named["upload_id"], 1, other)).status_code == 200
        kept = await client.post(f"/media/uploads/{named['upload_id']}/complete", headers=WRITE)
        assert kept.json()["poster_media_id"] == image["id"]
        assert len(d.posters.extracted) == 1


async def test_the_sweep_ends_stale_sessions(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The media sweep aborts an open session past its expiry (its parts go), deletes closed
    sessions once the idempotency result window has passed since they closed, and removes a
    session directory no open session owns once it is ``media_upload_ttl`` old. A live session
    and fresh parts are left alone."""
    from epistrel.core.media import store as store_module
    from epistrel.core.media.sweep import sweep

    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        data = png()
        stale = await _open(client, data)
        assert (await _put_part(client, stale["upload_id"], 1, data)).status_code == 200
        live = await _open(client, png())
        stray = d.space.root / "uploads" / "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"
        stray.mkdir(parents=True)
        (stray / "1").write_bytes(b"left behind")
        later = clock.now() + timedelta(hours=25)
        async with d.space.db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE media_upload_sessions "
                    "SET expires_at = now() + interval '30 hours' WHERE id = :i"
                ),
                {"i": live["upload_id"]},
            )
        # The store reads file ages from its own ``time`` module: 25 hours on, for this sweep.
        shifted = SimpleNamespace(time=lambda: time.time() + 25 * 3600)
        monkeypatch.setattr(store_module, "time", shifted)
        counts = await sweep(d.space.db, d.engine.media, now=later)
        monkeypatch.undo()
        assert (counts["sessions_expired"], counts["parts_removed"]) == (1, 1)
        assert (d.space.root / "uploads" / live["upload_id"]).is_dir()  # open: its parts stay
        assert counts["sessions_purged"] == 0
        swept = (await client.get(f"/media/uploads/{stale['upload_id']}", headers=READ)).json()
        assert swept["state"] == "aborted"
        assert not (d.space.root / "uploads" / stale["upload_id"]).exists()
        assert not stray.exists()
        assert (await client.get(f"/media/uploads/{live['upload_id']}", headers=READ)).json()[
            "state"
        ] == "open"
        purged = await sweep(d.space.db, d.engine.media, now=later + timedelta(hours=25))
        assert purged["sessions_purged"] == 1
        assert (
            await client.get(f"/media/uploads/{stale['upload_id']}", headers=READ)
        ).status_code == 404


# -- posters -------------------------------------------------------------------------------------


async def test_posters(pg_url: str, tmp_path: Path) -> None:
    """A ``poster_file`` part is stored as the video's poster (``uploaded``), described
    ``Poster for <id>`` without a caption, with the video's owner, rating and subjects. It is
    refused after an image (422 at ``body.poster_file``) or beside ``poster``. A ``poster``
    that is a video is 422 ``poster_invalid``, a retired one 422 ``media_retired``. Bytes
    ffmpeg finds no frame in are 422 ``poster_extraction_failed``, with nothing stored. An
    extracted poster goes to the moderation hook too, and the hook refusing it refuses the
    video. Deleting a video retires its poster; deleting a poster leaves its video."""
    from tests.media_support import hook_server

    subjects = [{"kind": "persona", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"}]
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        client = d.client
        cover = png(12, 9)
        sent = await client.post(
            "/media",
            files={
                **files(
                    video(),
                    metadata(kind="video", owner="p-owner", depicted_subjects=subjects),
                    content_type="video/mp4",
                ),
                "poster_file": ("cover.png", cover, "image/png"),
            },
            headers=WRITE,
        )
        assert sent.status_code == 201, sent.text
        clip = sent.json()
        poster = (await client.get(f"/media/{clip['poster_media_id']}", headers=READ)).json()
        assert (poster["source"], poster["checksum"], poster["width"]) == (
            "uploaded",
            sha256(cover),
            12,
        )
        assert poster["alt_text"] == f"Poster for {clip['id']}"
        assert poster["owner_principal"] == "p-owner"
        assert poster["depicted_subjects"] == subjects
        assert d.posters.extracted == []
        after_image = await client.post(
            "/media",
            files={**files(png(), metadata()), "poster_file": ("p.png", png(), "image/png")},
            headers=WRITE,
        )
        assert _error(after_image)[:2] == (422, "validation_error")
        assert _error(after_image)[2]["field"] == "body.poster_file"
        both = await client.post(
            "/media",
            files={
                **files(
                    video(), metadata(kind="video", poster=poster["id"]), content_type="video/mp4"
                ),
                "poster_file": ("p.png", png(), "image/png"),
            },
            headers=WRITE,
        )
        assert _error(both)[2]["field"] == "body.poster_file"
        not_image = await _upload_video(client, video(), poster=clip["id"])
        assert _error(not_image)[:2] == (422, "poster_invalid")
        before = await count(d.space.db, "SELECT count(*) FROM media_assets")
        junk = await client.post(
            "/media",
            files=files(
                b"\x00\x00\x00\x18ftypmp42" + secrets.token_bytes(200),
                metadata(kind="video"),
                content_type="video/mp4",
            ),
            headers=WRITE,
        )
        assert _error(junk)[:2] == (422, "poster_extraction_failed")
        assert await count(d.space.db, "SELECT count(*) FROM media_assets") == before
        assert staged_files(d.space.root) == []

        with hook_server() as hook:
            await set_media(client, media_moderation_webhook_url=hook.url)
            moderated = await _upload_video(client, video())
            assert moderated.status_code == 201, moderated.text
            kinds = [r.json["asset"]["kind"] for r in hook.received]
            assert kinds == ["video", "image"]
            assert (
                hook.received[1].json["asset"]["alt_text"] == f"Poster for {moderated.json()['id']}"
            )
            hook.answer = {"decision": "accept"}
            await set_media(client, media_moderation_webhook_url=None)

        assert (await client.delete(f"/media/{clip['id']}", headers=WRITE)).status_code == 204
        retired = (await client.get(f"/media/{poster['id']}", headers=READ)).json()
        assert retired["state"] == "retired"
        retired_poster = await _upload_video(client, video(), poster=poster["id"])
        assert _error(retired_poster)[:2] == (422, "media_retired")
        alone = moderated.json()
        assert (
            await client.delete(f"/media/{alone['poster_media_id']}", headers=WRITE)
        ).status_code == 204
        assert (await client.get(f"/media/{alone['id']}", headers=READ)).json()["state"] == "live"


async def test_an_extracted_poster_the_hook_refuses_refuses_the_video(
    pg_url: str, tmp_path: Path
) -> None:
    """The moderation hook accepts the video and rejects its extracted poster: the upload is
    422 ``moderation_rejected`` and stores nothing."""
    from tests.media_support import hook_server

    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        with hook_server(
            answers=[{"decision": "accept"}, {"decision": "reject", "reason": "the frame"}]
        ) as hook:
            await set_media(d.client, media_moderation_webhook_url=hook.url)
            refused = await _upload_video(d.client, video())
            assert _error(refused)[:2] == (422, "moderation_rejected")
            assert [r.json["asset"]["kind"] for r in hook.received] == ["video", "image"]
            assert await count(d.space.db, "SELECT count(*) FROM media_assets") == 0
            assert staged_files(d.space.root) == []


async def test_posters_outlive_their_video_only_in_the_sweep(pg_url: str, tmp_path: Path) -> None:
    """A video's row references its poster: the sweep never purges a poster whose video is
    still there, and purges it once the video is gone."""
    from epistrel.core.media.sweep import sweep

    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        made = await _upload_video(d.client, video())
        clip = made.json()
        assert (await d.client.delete(f"/media/{clip['id']}", headers=WRITE)).status_code == 204
        later = clock.now() + timedelta(days=2)
        first = await sweep(d.space.db, d.engine.media, now=later)
        assert first["assets_purged"] == 1  # the video; its poster was still referenced
        assert (
            await d.client.get(f"/media/{clip['poster_media_id']}", headers=READ)
        ).status_code == 200
        second = await sweep(d.space.db, d.engine.media, now=later)
        assert second["assets_purged"] == 1
        assert (
            await d.client.get(f"/media/{clip['poster_media_id']}", headers=READ)
        ).status_code == 404


# -- the store kind and the S3 store --------------------------------------------------------------


async def test_switching_stores_with_media_is_refused(
    pg_url: str, tmp_path: Path, minio: S3Endpoint
) -> None:
    """The store kind is recorded at the first start-up; a start with the other kind is refused
    once any asset exists (exit 78 from ``epistrel serve``), and recorded instead while none
    does. A bucket that does not exist refuses the start too."""
    import os
    import subprocess
    import sys

    from epistrel.core.media.store import MediaRootUnwritable
    from epistrel.core.media.stores import make_store, store_kind_problem

    bucket = new_bucket(minio)
    async with delivery(pg_url, tmp_path / "media", "local", None) as d:
        local = d.space.settings
        remote = auth_settings(
            local.database_url.get_secret_value(),
            media_root=local.media_root,
            **s3_overrides(minio, bucket),
        )
        assert await store_kind_problem(remote, d.space.db) is None  # nothing stored yet
        assert await store_kind_problem(local, d.space.db) is None  # back, still nothing stored
        made = await d.client.post("/media", files=files(png(), metadata()), headers=WRITE)
        assert made.status_code == 201
        problem = await store_kind_problem(remote, d.space.db)
        assert problem is not None
        assert "local" in problem
        assert "s3" in problem
        assert await store_kind_problem(local, d.space.db) is None
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")}
        env |= {
            "EPISTREL_DATABASE_URL": local.database_url.get_secret_value(),
            "EPISTREL_SERVICE_CREDENTIALS": "c" * 40,
            "EPISTREL_MEDIA_ROOT": str(tmp_path / "serve"),
            "EPISTREL_MEDIA_STORE": "s3",
            "EPISTREL_MEDIA_S3_ENDPOINT": minio.url,
            "EPISTREL_MEDIA_S3_BUCKET": bucket,
            "EPISTREL_MEDIA_S3_ACCESS_KEY": minio.access_key,
            "EPISTREL_MEDIA_S3_SECRET_KEY": minio.secret_key,
            "EPISTREL_MEDIA_S3_PATH_STYLE": "true",
        }
        proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "epistrel.cli", "serve"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )
        assert proc.returncode == 78, proc.stderr
        assert "switching stores" in proc.stderr
        assert "Traceback" not in proc.stderr
        missing = auth_settings(
            local.database_url.get_secret_value(),
            media_root=local.media_root,
            **{**s3_overrides(minio, bucket), "media_s3_bucket": f"absent-{secrets.token_hex(4)}"},
        )
        with pytest.raises(MediaRootUnwritable, match="does not exist"):
            make_store(missing).prepare()


def test_the_s3_store(tmp_path: Path, minio: S3Endpoint) -> None:
    """The S3 adapter keeps a blob at ``<sha256[:2]>/<sha256>`` with its type and
    ``Cache-Control``; reads all of it or a range; lists checksums in order, after a given one,
    skipping every other key; keeps the sweep cursor as an object; presigns a URL the store
    serves; finds the multipart uploads and assembled objects under ``uploads/`` that a crash
    left (listed without a prefix, which MinIO needs) and removes them; and turns a store
    failure into 503 ``media_store_unavailable``."""
    from epistrel.core.errors import MediaStoreUnavailable
    from epistrel.core.media.s3 import S3BlobStore

    bucket = new_bucket(minio)
    store = S3BlobStore(
        tmp_path / "media",
        bucket=bucket,
        endpoint=minio.url,
        access_key=minio.access_key,
        secret_key=minio.secret_key,
        path_style=True,
    )
    store.prepare()
    blobs = sorted(
        (secrets.token_bytes(100 + i) for i in range(5)),
        key=lambda blob: hashlib.sha256(blob).hexdigest(),
    )
    digests = [hashlib.sha256(b).hexdigest() for b in blobs]
    for name, data in zip(("01J9Z8Q4N5V6W7X8Y9Z0A1B2C" + c for c in "56789"), blobs, strict=True):
        with store.open_staging(name) as handle:
            handle.write(data)
        assert store.put(
            store.staging_path(name), hashlib.sha256(data).hexdigest(), content_type="image/png"
        )
        assert not store.staging_path(name).exists()
    with store.open_staging("01J9Z8Q4N5V6W7X8Y9Z0A1B2CA") as handle:
        handle.write(blobs[0])
    assert store.put(store.staging_path("01J9Z8Q4N5V6W7X8Y9Z0A1B2CA"), digests[0]) is False
    s3_client(minio).put_object(Bucket=bucket, Key="exports/x", Body=b"not a blob")
    s3_client(minio).put_object(Bucket=bucket, Key="zz/" + "a" * 64, Body=b"wrong shard")
    assert list(store.list_checksums()) == digests
    assert list(store.list_checksums(after=digests[1])) == digests[2:]
    assert (store.exists(digests[0]), store.size(digests[0])) == (True, len(blobs[0]))
    assert store.open(digests[0]).read() == blobs[0]
    assert store.open_range(digests[0], 10, 19).read() == blobs[0][10:20]
    age = store.age(digests[0])
    assert age is not None
    assert age < 60
    assert store.sweep_cursor() is None
    store.set_sweep_cursor(digests[2])
    assert store.sweep_cursor() == digests[2]
    store.set_sweep_cursor(None)
    assert store.sweep_cursor() is None
    url = store.presign(digests[0], 60)
    assert httpx.get(url).content == blobs[0]
    assert store.delete(digests[0]) is True
    assert store.delete(digests[0]) is False
    assert store.age(digests[0]) is None
    with pytest.raises(FileNotFoundError):
        store.open(digests[0])
    upload_id = "01J9Z8Q4N5V6W7X8Y9Z0A1B2CB"
    stray_id = s3_client(minio).create_multipart_upload(Bucket=bucket, Key=f"uploads/{upload_id}")[
        "UploadId"
    ]
    s3_client(minio).put_object(Bucket=bucket, Key="uploads/01J9Z8Q4N5V6W7X8Y9Z0A1B2CC", Body=b"x")
    assert store.stale_part_uploads(3600) == []
    stale = store.stale_part_uploads(0)
    assert (upload_id, stray_id) in stale
    assert ("01J9Z8Q4N5V6W7X8Y9Z0A1B2CC", None) in stale
    for name, store_id in stale:
        store.abort_parts(name, store_id)
    assert store.stale_part_uploads(0) == []
    broken = S3BlobStore(
        tmp_path / "other",
        bucket=bucket,
        endpoint=minio.url,
        access_key=minio.access_key,
        secret_key="not-the-secret",
        path_style=True,
    )
    with pytest.raises(MediaStoreUnavailable) as failed:
        broken.open(digests[1])
    assert failed.value.details["operation"] == "get_object"
    assert failed.value.http_status == 503
