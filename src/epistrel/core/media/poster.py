"""Video posters: ffmpeg's first-frame extraction, and ffprobe, through one seam (FR-MEDIA-3;
#107).

Every video asset has a poster image (``media_assets.poster_media_id``). When an upload supplies
none, Epistrel takes one from the video's first frame with **ffmpeg**. ffmpeg is a GPL program
the image installs and Epistrel runs as a separate process; it is never linked or imported, and
it is not a Python dependency (the owner's decision; project invariant 2's exception, ADR-151).

``PosterExtractor`` is the seam: the Engine holds one (``Engine.posters``), tests replace it,
and the video probe (``ffprobe``, for width, height and duration) goes through it too.
``FfmpegTools`` is the real one:

- ``EPISTREL_FFMPEG_PATH`` names ffmpeg (default ``ffmpeg`` on ``PATH``); ffprobe is taken from
  beside it (``ffprobe``, or the same name with ``ffmpeg`` replaced).
- ``status`` runs ``<tool> -version`` once (5 s at most) and keeps the answer: ``/health``
  reports ``ffmpeg`` and ``ffprobe`` as ``ok`` or ``missing`` (never fatal), and without ffmpeg a
  video uploaded without a poster is refused (422 ``poster_required``, naming ``ffmpeg_path``).
- ``extract`` runs ``ffmpeg -y -nostdin -loglevel error -protocol_whitelist file,pipe -ss 0 -i
  <video> -frames:v 1`` with a scale to at most 1280 pixels a side (aspect kept), writing a PNG,
  through ``subprocess.run`` with a 30 s timeout. A failure, a timeout or no frame is 422
  ``poster_extraction_failed``; the log line carries the exit status and the size of ffmpeg's
  error output, never the output itself (it can quote the video's metadata).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from epistrel.core.errors import PosterExtractionFailed, PosterRequired
from epistrel.core.media import probe

logger = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
EXTRACT_TIMEOUT_S = 30.0
VERSION_TIMEOUT_S = 5.0
POSTER_MAX_PX = 1280
POSTER_MIME_TYPE = "image/png"
SETTING = "ffmpeg_path"
"""The setting a refusal names (``EPISTREL_FFMPEG_PATH``)."""
SCALE = (
    f"scale='min({POSTER_MAX_PX},iw)':'min({POSTER_MAX_PX},ih)'"
    ":force_original_aspect_ratio=decrease"
)

ToolState = Literal["ok", "missing"]


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """Whether ffmpeg and ffprobe answered ``-version``."""

    ffmpeg: ToolState
    ffprobe: ToolState


class PosterExtractor(Protocol):
    """The ffmpeg seam (see the module docstring)."""

    async def status(self) -> ToolStatus:
        """ffmpeg's and ffprobe's availability, checked once and kept."""
        ...

    async def probe_video(self, path: Path) -> probe.Dimensions:
        """A video's width, height and duration (ffprobe); nothing on any failure."""
        ...

    async def extract(self, video: Path, out: Path) -> None:
        """Write the video's first frame to ``out`` as a PNG of at most 1280 pixels a side;
        422 ``poster_required`` without ffmpeg, 422 ``poster_extraction_failed`` when it
        fails."""
        ...


def ffprobe_beside(ffmpeg_path: str) -> str:
    """The ffprobe that goes with ``ffmpeg_path``: in the same directory when it names one, on
    ``PATH`` otherwise; ``ffprobe``, or the same file name with ``ffmpeg`` replaced."""
    path = Path(ffmpeg_path)
    name = path.name.replace(FFMPEG, FFPROBE, 1) if FFMPEG in path.name else FFPROBE
    return str(path.with_name(name)) if path.parent != Path() else name


def poster_required() -> PosterRequired:
    return PosterRequired(
        "a video needs a poster: supply one (`poster` or a `poster_file` part), or install "
        "ffmpeg (EPISTREL_FFMPEG_PATH) so one is taken from its first frame",
        {"setting": SETTING},
    )


def _version(executable: str | None) -> ToolState:
    if executable is None:
        return "missing"
    try:
        done = subprocess.run(  # noqa: S603 - a fixed argv naming the configured executable
            [executable, "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=VERSION_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return "missing"
    return "ok" if done.returncode == 0 else "missing"


class FfmpegTools:
    """The real ``PosterExtractor``: ffmpeg and ffprobe as separate processes."""

    def __init__(self, ffmpeg_path: str = FFMPEG) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_beside(ffmpeg_path)
        self._status: ToolStatus | None = None
        self._lock = threading.Lock()

    def _resolved(self, name: str) -> str | None:
        return shutil.which(name)

    def _check(self) -> ToolStatus:
        with self._lock:
            if self._status is None:
                self._status = ToolStatus(
                    ffmpeg=_version(self._resolved(self.ffmpeg_path)),
                    ffprobe=_version(self._resolved(self.ffprobe_path)),
                )
            return self._status

    async def status(self) -> ToolStatus:
        if self._status is not None:
            return self._status
        return await asyncio.to_thread(self._check)

    async def probe_video(self, path: Path) -> probe.Dimensions:
        if (await self.status()).ffprobe != "ok":
            return probe.Dimensions()
        return await probe.video_dimensions(path, executable=self._resolved(self.ffprobe_path))

    def _run(self, executable: str, video: Path, out: Path) -> None:
        argv = [
            executable,
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            "0",
            "-i",
            f"file:{video}",
            "-frames:v",
            "1",
            "-vf",
            SCALE,
            "-update",
            "1",
            "-f",
            "image2",
            "-c:v",
            "png",
            f"file:{out}",
        ]
        try:
            done = subprocess.run(  # noqa: S603 - a fixed argv; the paths are our staging files
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=EXTRACT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("poster extraction timed out after %ss", EXTRACT_TIMEOUT_S)
            raise PosterExtractionFailed(
                "ffmpeg did not take a poster from the video in time", {"reason": "timeout"}
            ) from None
        except OSError as exc:
            logger.warning("poster extraction could not start: %s", type(exc).__name__)
            raise PosterExtractionFailed(
                "ffmpeg could not be started", {"reason": "not_started"}
            ) from None
        if done.returncode != 0:
            logger.warning(
                "poster extraction failed: exit %d, %d bytes of error output",
                done.returncode,
                len(done.stderr or b""),
            )
            raise PosterExtractionFailed(
                "ffmpeg could not take a poster from the video's first frame",
                {"reason": "ffmpeg_failed"},
            )
        if not out.is_file() or out.stat().st_size == 0:
            raise PosterExtractionFailed(
                "the video has no frame to take a poster from", {"reason": "no_frame"}
            )

    async def extract(self, video: Path, out: Path) -> None:
        executable = self._resolved(self.ffmpeg_path)
        if executable is None or (await self.status()).ffmpeg != "ok":
            raise poster_required()
        try:
            await asyncio.to_thread(self._run, executable, video, out)
        except BaseException:
            await asyncio.to_thread(out.unlink, missing_ok=True)
            raise


__all__ = [
    "EXTRACT_TIMEOUT_S",
    "POSTER_MAX_PX",
    "POSTER_MIME_TYPE",
    "SETTING",
    "FfmpegTools",
    "PosterExtractor",
    "ToolStatus",
    "ffprobe_beside",
    "poster_required",
]
