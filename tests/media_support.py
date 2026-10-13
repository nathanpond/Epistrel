"""Helpers for the media tests (#105, #107): unique image and video bytes (real MP4s made with
ffmpeg at test time), upload bodies and calls, a local moderation hook, a scratch database with
its own media root, blob-store checks, and a spy on the ffmpeg seam."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx2 as httpx
from PIL import Image, PngImagePlugin
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.core.media import probe
from epistrel.core.media.poster import PosterExtractor, ToolStatus
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.engine import Engine
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database

PRINCIPAL = "p-media"
WRITE = {**bearer(), "X-Epistrel-Principal": PRINCIPAL}
READ = bearer()
OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}


def png(width: int = 3, height: int = 2, *, nonce: str | None = None) -> bytes:
    """A real PNG of ``width`` by ``height``; a fresh ``nonce`` text chunk makes its bytes (and
    its checksum) unique unless one is given."""
    info = PngImagePlugin.PngInfo()
    info.add_text("nonce", nonce if nonce is not None else secrets.token_hex(8))
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 120, 40)).save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def jpeg(width: int = 4, height: int = 4) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(out, format="JPEG")
    return out.getvalue() + secrets.token_bytes(8)


def mp4() -> bytes:
    """Bytes an ISO base media sniff reads as ``video/mp4`` (no probe can read a duration, and
    ffmpeg finds no frame in them)."""
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + secrets.token_bytes(64)


def video(width: int = 160, height: int = 120, *, seconds: float = 1.0) -> bytes:
    """A real MP4 (MPEG-4 Part 2, ffmpeg's test pattern) of ``width`` by ``height``, unique by a
    random comment. ffmpeg must be installed: the poster tests fail, never skip, without it
    (#107; CI installs it on the runner)."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "ffmpeg is not installed: the video tests need it (#107)"
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "video.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration={seconds}:size={width}x{height}:rate=10",
                "-c:v",
                "mpeg4",
                "-pix_fmt",
                "yuv420p",
                "-metadata",
                f"comment={secrets.token_hex(8)}",
                "-f",
                "mp4",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return out.read_bytes()


def large_png(at_least: int) -> bytes:
    """A real 2x2 PNG of at least ``at_least`` bytes: its nonce text chunk is the padding."""
    return png(2, 2, nonce=secrets.token_hex(max(1, at_least // 2)))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def metadata(**fields: Any) -> dict[str, Any]:
    """A ``metadata`` part: an image rated Everyone, depicting nobody, unless ``fields`` say
    otherwise (``depicted_subjects=None`` leaves the list out)."""
    found: dict[str, Any] = {
        "kind": "image",
        "content_rating_id": EVERYONE_ID,
        "depicted_subjects": [],
        **fields,
    }
    if found.get("depicted_subjects", ...) is None:
        del found["depicted_subjects"]
    return found


def files(
    data: bytes,
    meta: dict[str, Any] | str | bytes,
    *,
    content_type: str | None = "image/png",
    filename: str = "upload.png",
) -> dict[str, Any]:
    """``files=`` for an upload: the ``metadata`` part first, then the ``file`` part."""
    raw = meta if isinstance(meta, str | bytes) else json.dumps(meta)
    part: tuple[Any, ...] = (
        (filename, data) if content_type is None else (filename, data, content_type)
    )
    return {"metadata": (None, raw, "application/json"), "file": part}


async def upload(
    client: httpx.AsyncClient,
    data: bytes,
    meta: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    content_type: str | None = "image/png",
) -> httpx.Response:
    return await client.post(
        "/media",
        files=files(data, meta if meta is not None else metadata(), content_type=content_type),
        headers=headers or WRITE,
    )


async def uploaded(
    client: httpx.AsyncClient,
    data: bytes | None = None,
    *,
    content_type: str | None = "image/png",
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """An upload that must succeed (201); its body."""
    response = await upload(
        client,
        data if data is not None else png(),
        metadata(**fields),
        content_type=content_type,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def blob_path(root: Path, checksum: str) -> Path:
    return root / checksum[:2] / checksum


def staged_files(root: Path) -> list[Path]:
    staging = root / "tmp"
    if not staging.is_dir():
        return []
    return sorted(p for p in staging.iterdir() if not p.name.startswith(".writable-"))


async def asset_row(engine: AsyncEngine, media_id: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT * FROM media_assets WHERE id = :m"), {"m": media_id})
        ).one_or_none()


async def count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql), params) or 0)


async def set_media(client: httpx.AsyncClient, **settings: Any) -> dict[str, Any]:
    """``PUT /admin/settings`` with media settings; the answer."""
    response = await client.put("/admin/settings", json=settings, headers=OPERATOR)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@asynccontextmanager
async def media_client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The REST app over ``engine`` (no lifespan: the Engine is bound directly)."""
    app = create_app(settings)
    app.state.epistrel.bind(engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@dataclass
class Scratch:
    """A migrated scratch database with its own media root: the app's settings, an engine
    and an Engine bound to it."""

    settings: Settings
    db: AsyncEngine
    lib: Engine

    @property
    def root(self) -> Path:
        return Path(self.settings.media_root)


@asynccontextmanager
async def scratch(pg_url: str, root: Path, **overrides: Any) -> AsyncIterator[Scratch]:
    """A fresh migrated database (dropped afterwards) and a media root of its own, so a sweep
    sees only what the test put there."""
    name = f"epistrel_media_{secrets.token_hex(4)}"
    with migrated_database(pg_url, name) as url:
        settings = auth_settings(url, media_root=root, **overrides)
        db = make_engine(settings)
        lib = Engine(settings, db=db)
        try:
            yield Scratch(settings, db, lib)
        finally:
            await lib.models.aclose()
            await db.dispose()


# -- the ffmpeg seam ------------------------------------------------------------------------------


@dataclass
class SpyPosters:
    """A ``PosterExtractor`` that counts what it is asked, around the real one (#107)."""

    real: PosterExtractor
    extracted: list[Path] = field(default_factory=list)
    probed: list[Path] = field(default_factory=list)

    async def status(self) -> ToolStatus:
        return await self.real.status()

    async def probe_video(self, path: Path) -> probe.Dimensions:
        self.probed.append(path)
        return await self.real.probe_video(path)

    async def extract(self, video: Path, out: Path) -> None:
        self.extracted.append(video)
        await self.real.extract(video, out)


# -- a moderation hook -----------------------------------------------------------------------------


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class Hook:
    """A moderation hook on 127.0.0.1: answers ``status`` with ``answer`` (JSON, or raw bytes),
    after ``delay_s``; with ``hold`` set it waits until ``release`` is set (and signals
    ``arrived`` first). ``location`` makes it a redirect."""

    port: int = 0
    status: int = 200
    answer: Any = field(default_factory=lambda: {"decision": "accept"})
    delay_s: float = 0.0
    hold: bool = False
    location: str | None = None
    answers: list[Any] = field(default_factory=list)
    """Answers given in order before ``answer`` (one per request)."""
    received: list[Received] = field(default_factory=list)
    arrived: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/moderate"


@contextmanager
def hook_server(**options: Any) -> Iterator[Hook]:
    hook = Hook(**options)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            hook.received.append(Received(self.path, dict(self.headers.items()), body))
            hook.arrived.set()
            if hook.hold:
                hook.release.wait(30)
            if hook.delay_s:
                time.sleep(hook.delay_s)
            given = hook.answers.pop(0) if hook.answers else hook.answer
            raw = given if isinstance(given, bytes) else json.dumps(given).encode()
            try:
                self.send_response(hook.status)
                if hook.location is not None:
                    self.send_header("Location", hook.location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                return  # the client gave up (a timeout)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    hook.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield hook
    finally:
        hook.release.set()
        server.shutdown()
        server.server_close()


def refused_url() -> str:
    """A URL nothing listens on (the port was free a moment ago): connecting is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/moderate"


async def wait_for(event: threading.Event, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not event.is_set():
        assert time.monotonic() < deadline, "the hook was never called"
        await asyncio.sleep(0.02)


__all__ = [
    "OPERATOR",
    "PRINCIPAL",
    "READ",
    "WRITE",
    "Hook",
    "Scratch",
    "SpyPosters",
    "asset_row",
    "blob_path",
    "count",
    "files",
    "hook_server",
    "jpeg",
    "large_png",
    "media_client",
    "metadata",
    "mp4",
    "png",
    "refused_url",
    "scratch",
    "set_media",
    "sha256",
    "staged_files",
    "upload",
    "uploaded",
    "video",
    "wait_for",
]
