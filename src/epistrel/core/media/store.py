"""The blob store: content-addressed, immutable media bytes (FR-MEDIA-1,2; docs/02 §4.10; #105).

``BlobStore`` is the port. A blob is keyed by the SHA-256 of its bytes (lowercase hex) and never
changes, so two assets with identical bytes share one blob, and a blob may be deleted only when
no asset row of any state still names its checksum (the media sweep checks that under the
checksum's advisory lock). ``LocalFilesystemStore``, the default adapter, keeps a blob at
``<root>/<sha256[:2]>/<sha256>`` (read-only), and #107 adds the S3-compatible adapter.

Uploads are **staged** first: an upload streams into ``<root>/tmp/<asset id>`` while it is
hashed and counted, and ``put`` moves the finished, fsynced file into place with one rename on
the same filesystem (when the blob already exists, the staged copy is simply dropped). Until
then the bytes are addressable by no asset. The media sweep removes staged files a crashed
upload left behind.

``list_checksums`` lists the blobs in ascending checksum order, optionally after a given one, and
the store keeps the **sweep cursor**: the checksum where the media sweep's last scan of the blobs
stopped (``<root>/.sweep-cursor``), so each run goes on where the previous one stopped instead of
starting over at the first blob.

Every method is synchronous filesystem work: async callers run them with
``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

STAGING_DIR = "tmp"
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHARD_PATTERN = re.compile(r"^[0-9a-f]{2}$")
"""A blob's directory: the first two hex digits of its checksum."""
STAGED_NAME_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
"""A staged upload is named after the asset it would become (a ULID)."""
BLOB_MODE = 0o444
PROBE_PREFIX = ".writable-"
"""The start-up check's probe files: each process writes its own (several processes, an API and
its workers, start on one media root at once)."""
SWEEP_CURSOR = ".sweep-cursor"
"""The file in the root that holds the sweep cursor (one checksum), shared by every process on
the root."""


class MediaRootUnwritable(Exception):
    """The media root (``EPISTREL_MEDIA_ROOT``) cannot be created or written: the server
    refuses to start (exit 78)."""


def check_checksum(sha256: str) -> str:
    """``sha256`` if it is a lowercase hex SHA-256 digest; ``ValueError`` otherwise (it becomes
    a path, so nothing else may reach the filesystem)."""
    if not CHECKSUM_PATTERN.fullmatch(sha256):
        raise ValueError("not a SHA-256 hex digest")
    return sha256


def _fsync_directory(path: Path) -> None:
    """Make a rename or unlink in ``path`` durable (best effort where directories cannot be
    opened)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory handles
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - filesystems that refuse a directory fsync
        pass
    finally:
        os.close(fd)


class BlobStore(Protocol):
    """Content-addressed, immutable blobs keyed by SHA-256 (see the module docstring)."""

    def put(self, staged: Path, sha256: str) -> bool:
        """Move the staged file into place as blob ``sha256``; ``False`` (the staged copy is
        dropped) when the blob already exists."""
        ...

    def open(self, sha256: str) -> BinaryIO:
        """The blob's bytes, opened for reading; ``FileNotFoundError`` when it is missing."""
        ...

    def exists(self, sha256: str) -> bool: ...

    def delete(self, sha256: str) -> bool:
        """Remove the blob; ``False`` when it was already gone."""
        ...

    def size(self, sha256: str) -> int: ...

    def list_checksums(self, after: str | None = None) -> Iterator[str]:
        """Every stored blob's checksum in ascending order: those after ``after``, or all of
        them when it is ``None``."""
        ...

    def age(self, sha256: str) -> float | None:
        """Seconds since the blob was written, or ``None`` when it is missing."""
        ...

    def sweep_cursor(self) -> str | None:
        """The checksum where the media sweep's last scan of the blobs stopped, or ``None``
        (the next scan starts at the first blob)."""
        ...

    def set_sweep_cursor(self, checksum: str | None) -> None:
        """Record where the next scan of the blobs goes on (after ``checksum``; ``None``: at the
        first blob)."""
        ...


class LocalFilesystemStore:
    """The default ``BlobStore``: a directory (a Docker volume in the image), plus the staging
    area every upload streams into."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.staging = self.root / STAGING_DIR

    # -- start-up ------------------------------------------------------------------------------

    def prepare(self) -> None:
        """Create the root and the staging area and prove both writable;
        ``MediaRootUnwritable`` (naming the path, never anything else) otherwise."""
        try:
            self.staging.mkdir(parents=True, exist_ok=True)
            probe = self.staging / f"{PROBE_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
            with probe.open("xb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
            probe.unlink()
        except OSError as exc:
            raise MediaRootUnwritable(
                f"the media root {self.root} cannot be written ({type(exc).__name__}); set "
                "EPISTREL_MEDIA_ROOT to a writable directory"
            ) from None

    # -- staging -------------------------------------------------------------------------------

    def staging_path(self, name: str) -> Path:
        """Where the upload of asset ``name`` (a ULID) is staged."""
        if not STAGED_NAME_PATTERN.fullmatch(name):
            raise ValueError("a staged upload is named by its asset id")
        return self.staging / name

    def open_staging(self, name: str) -> BinaryIO:
        """A new staging file for asset ``name``, opened for writing (it must not exist)."""
        self.staging.mkdir(parents=True, exist_ok=True)
        return self.staging_path(name).open("xb")

    def staged_files(self) -> Iterator[tuple[Path, float]]:
        """``(path, age in seconds)`` of every file in the staging area."""
        if not self.staging.is_dir():
            return
        now = time.time()
        for entry in os.scandir(self.staging):
            if entry.is_file(follow_symlinks=False) and not entry.name.startswith(PROBE_PREFIX):
                with contextlib.suppress(FileNotFoundError):
                    yield Path(entry.path), now - entry.stat(follow_symlinks=False).st_mtime

    # -- blobs ---------------------------------------------------------------------------------

    def path(self, sha256: str) -> Path:
        digest = check_checksum(sha256)
        return self.root / digest[:2] / digest

    def put(self, staged: Path, sha256: str) -> bool:
        target = self.path(sha256)
        if target.exists():
            staged.unlink(missing_ok=True)
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(staged, BLOB_MODE)
        os.replace(staged, target)
        _fsync_directory(target.parent)
        return True

    def open(self, sha256: str) -> BinaryIO:
        return self.path(sha256).open("rb")

    def exists(self, sha256: str) -> bool:
        return self.path(sha256).is_file()

    def delete(self, sha256: str) -> bool:
        target = self.path(sha256)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(target.parent)
        return True

    def size(self, sha256: str) -> int:
        return self.path(sha256).stat().st_size

    def list_checksums(self, after: str | None = None) -> Iterator[str]:
        if not self.root.is_dir():
            return
        with os.scandir(self.root) as entries:
            shards = sorted(
                entry.name
                for entry in entries
                if entry.is_dir(follow_symlinks=False) and SHARD_PATTERN.fullmatch(entry.name)
            )
        for shard in shards:
            if after is not None and shard < after[:2]:
                continue
            with os.scandir(self.root / shard) as entries:
                names = sorted(
                    entry.name
                    for entry in entries
                    if CHECKSUM_PATTERN.fullmatch(entry.name) and entry.name[:2] == shard
                )
            for name in names:
                if after is None or name > after:
                    yield name

    def age(self, sha256: str) -> float | None:
        try:
            return time.time() - self.path(sha256).stat().st_mtime
        except FileNotFoundError:
            return None

    # -- the sweep cursor ----------------------------------------------------------------------

    def sweep_cursor(self) -> str | None:
        try:
            value = (self.root / SWEEP_CURSOR).read_text(encoding="ascii").strip()
        except OSError, ValueError:  # missing or unreadable: start at the first blob
            return None
        return value if CHECKSUM_PATTERN.fullmatch(value) else None

    def set_sweep_cursor(self, checksum: str | None) -> None:
        target = self.root / SWEEP_CURSOR
        if checksum is None:
            target.unlink(missing_ok=True)
            return
        value = check_checksum(checksum)
        written = self.root / f"{SWEEP_CURSOR}.{os.getpid()}-{secrets.token_hex(4)}"
        try:
            written.write_text(value, encoding="ascii")
            os.replace(written, target)
        finally:
            written.unlink(missing_ok=True)


__all__ = [
    "CHECKSUM_PATTERN",
    "STAGING_DIR",
    "SWEEP_CURSOR",
    "BlobStore",
    "LocalFilesystemStore",
    "MediaRootUnwritable",
    "check_checksum",
]
