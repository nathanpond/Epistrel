"""What an upload's bytes are: the sniffed MIME type and the dimensions or duration (#105).

``sniff`` reads the type from the first bytes (magic numbers), never from what the client
declared: PNG, JPEG, WebP, GIF, the ISO base media family (MP4, QuickTime, 3GP, AVIF, HEIF) and
Matroska/WebM. A type it does not recognise is ``None``, and such an upload is refused (415).

``dimensions`` reads an image's width and height with Pillow (headers only: nothing is
decoded), and a video's width, height and duration with ``ffprobe``: the one beside
``EPISTREL_FFMPEG_PATH``, through the poster seam (``core.media.poster``, #107), or the one on
``PATH`` when no executable is given. ffmpeg and ffprobe are installed in the image and run as
separate processes, never linked (invariant 2's exception). A probe that is missing, cannot
start, fails, times out or reports a value no column can hold leaves the values ``None``: it
never fails an upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from epistrel.core.tables.media import MediaKind

logger = logging.getLogger(__name__)

SNIFF_BYTES = 64
"""How many leading bytes ``sniff`` needs (fewer when the file is shorter)."""
FFPROBE = "ffprobe"
FFPROBE_TIMEOUT_S = 10.0
INT32_MAX = 2**31 - 1

_ISO_IMAGE_BRANDS = {
    b"avif": "image/avif",
    b"avis": "image/avif",
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"mif1": "image/heif",
    b"msf1": "image/heif",
}
_ISO_OTHER_BRANDS = {
    b"qt  ": "video/quicktime",
    b"3gp4": "video/3gpp",
    b"3gp5": "video/3gpp",
    b"3gp6": "video/3gpp",
    b"3g2a": "video/3gpp2",
    b"M4A ": "audio/mp4",
}


def sniff(head: bytes) -> str | None:
    """The MIME type ``head`` (a file's first ``SNIFF_BYTES`` bytes) starts, or ``None``."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[4:8] == b"ftyp" and len(head) >= 12:
        brand = head[8:12]
        return _ISO_IMAGE_BRANDS.get(brand) or _ISO_OTHER_BRANDS.get(brand) or "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    return None


def kind_of(mime_type: str) -> MediaKind | None:
    """``image`` for ``image/*``, ``video`` for ``video/*``, else ``None``."""
    major = mime_type.split("/", 1)[0]
    if major == "image":
        return MediaKind.IMAGE
    if major == "video":
        return MediaKind.VIDEO
    return None


@dataclass(frozen=True, slots=True)
class Dimensions:
    """What a probe read (``None`` where it read nothing)."""

    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


def _bounded(value: object) -> int | None:
    """A positive int32, or ``None``: a probe's value never breaks an ``INTEGER`` column (nor
    the probe: Infinity and NaN, which JSON parsing yields for ``1e400`` and ``NaN``, have no
    ``int``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    number = int(value)
    return number if 0 < number <= INT32_MAX else None


def image_dimensions(path: Path) -> Dimensions:
    """Width and height from the image's header (Pillow); nothing on any failure."""
    from PIL import Image  # Pillow is loaded only when an image is probed

    try:
        with Image.open(path) as image:
            width, height = image.size
    except Exception:  # a probe never fails an upload (corrupt, truncated, a pixel bomb, …)
        logger.info("image probe read nothing")
        return Dimensions()
    return Dimensions(width=_bounded(width), height=_bounded(height))


def _duration_ms(value: object) -> int | None:
    try:
        seconds = float(str(value))
    except ValueError:
        return None
    if not 0 < seconds < INT32_MAX / 1000:
        return None
    return round(seconds * 1000)


def _ffprobe_dimensions(report: object) -> Dimensions:
    if not isinstance(report, dict):
        return Dimensions()
    streams = report.get("streams")
    video = next(
        (
            s
            for s in (streams if isinstance(streams, list) else [])
            if isinstance(s, dict) and s.get("codec_type") == "video"
        ),
        {},
    )
    found = report.get("format")
    duration = _duration_ms(found.get("duration")) if isinstance(found, dict) else None
    return Dimensions(
        width=_bounded(video.get("width")),
        height=_bounded(video.get("height")),
        duration_ms=duration,
    )


async def video_dimensions(path: Path, *, executable: str | None = None) -> Dimensions:
    """Width, height and duration from ``ffprobe`` (``executable``, else the one on ``PATH``;
    local files only, 10 s at most); nothing when it is not installed, cannot start or
    fails."""
    executable = executable or shutil.which(FFPROBE)
    if executable is None:
        return Dimensions()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-v",
            "error",
            # No ``-nostdin``: ffprobe has no such option (it refuses to run with it, #107), and
            # its stdin is closed below anyway.
            "-protocol_whitelist",
            "file",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_type,width,height:format=duration",
            f"file:{path}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:  # gone since it was found, not executable, no executable format, …
        logger.warning("video probe could not start: %s", type(exc).__name__)
        return Dimensions()
    try:
        async with asyncio.timeout(FFPROBE_TIMEOUT_S):
            output, _ = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        logger.info("video probe timed out")
        return Dimensions()
    if process.returncode != 0:
        logger.info("video probe read nothing")
        return Dimensions()
    try:
        return _ffprobe_dimensions(json.loads(output))
    except ValueError:
        return Dimensions()


async def dimensions(path: Path, kind: MediaKind, *, ffprobe: str | None = None) -> Dimensions:
    """The probe for ``kind`` (see the module docstring)."""
    if kind is MediaKind.IMAGE:
        return await asyncio.to_thread(image_dimensions, path)
    return await video_dimensions(path, executable=ffprobe)


__all__ = [
    "SNIFF_BYTES",
    "Dimensions",
    "dimensions",
    "image_dimensions",
    "kind_of",
    "sniff",
    "video_dimensions",
]
