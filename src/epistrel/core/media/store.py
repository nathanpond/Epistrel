"""The blob store: content-addressed, immutable media bytes (FR-MEDIA-1,2,3; docs/02 §4.10;
#105, #107).

``BlobStore`` is the port. A blob is keyed by the SHA-256 of its bytes (lowercase hex) and never
changes, so two assets with identical bytes share one blob, and a blob may be deleted only when
no asset row of any state still names its checksum (the media sweep checks that under the
checksum's advisory lock). Two adapters implement it: ``LocalFilesystemStore``, the default,
keeps a blob at ``<root>/<sha256[:2]>/<sha256>`` (read-only), and ``core.media.s3.S3BlobStore``
keeps it at the key ``<sha256[:2]>/<sha256>`` of an S3-compatible bucket (#107).

Both adapters work from the **media root** on the local filesystem (``LocalStaging``):

- Uploads are **staged** first: an upload streams into ``<root>/tmp/<asset id>`` while it is
  hashed and counted, and ``put`` moves the finished, fsynced file into place (one rename on the
  same filesystem, or one upload to the bucket; when the blob already exists, the staged copy is
  simply dropped). Until then the bytes are addressable by no asset. The media sweep removes
  staged files a crashed upload left behind.
- A resumable upload session (#107) receives each part into ``<root>/uploads/<upload id>/``:
  the local adapter keeps part ``n`` there as ``<n>``, the S3 adapter sends it to the bucket's
  native multipart upload under ``uploads/<upload id>`` and keeps nothing. ``assemble`` writes
  the parts, in order, into the staging file of the asset the session creates, so a completion
  goes through the same checks as a direct upload; on S3 the assembled object is then copied to
  its checksum key by the store itself (``put(..., assembled=upload_id)``).

Reads: ``open`` gives the whole blob and ``open_range`` the bytes ``start..end`` (inclusive): a
file seek locally, a ``Range`` request on S3, so a video player can seek (FR-MEDIA-3).
``presign`` gives a URL the store itself serves until it expires (S3), or ``None`` when the
store serves nothing itself (local: Epistrel signs its own content URL instead).

``list_checksums`` lists the blobs in ascending checksum order, optionally after a given one, and
the store keeps the **sweep cursor**: the checksum where the media sweep's last scan of the blobs
stopped, so each run goes on where the previous one stopped instead of starting over at the first
blob.

Every method is synchronous I/O: async callers run them with ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

STAGING_DIR = "tmp"
UPLOADS_DIR = "uploads"
"""The local directory of the resumable upload sessions' parts (and the S3 key prefix of their
multipart uploads)."""
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHARD_PATTERN = re.compile(r"^[0-9a-f]{2}$")
"""A blob's directory: the first two hex digits of its checksum."""
STAGED_NAME_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
"""A staged upload is named after the asset it would become (a ULID); an upload session's
directory after the session (a ULID too)."""
PART_NAME_PATTERN = re.compile(r"^[1-9][0-9]{0,5}$")
"""A stored part: its number, 1 to 999999."""
BLOB_MODE = 0o444
PROBE_PREFIX = ".writable-"
"""The start-up check's probe files: each process writes its own (several processes, an API and
its workers, start on one media root at once)."""
SWEEP_CURSOR = ".sweep-cursor"
"""The file (local) or object (S3) that holds the sweep cursor (one checksum), shared by every
process on the store."""
CACHE_CONTROL = "private, max-age=31536000, immutable"
"""A blob never changes: what the content route and the S3 objects answer."""
COPY_CHUNK = 1024 * 1024


class MediaRootUnwritable(Exception):
    """The media root (``EPISTREL_MEDIA_ROOT``) cannot be created or written, or the S3 bucket
    cannot be reached: the server refuses to start (exit 78)."""


def check_checksum(sha256: str) -> str:
    """``sha256`` if it is a lowercase hex SHA-256 digest; ``ValueError`` otherwise (it becomes
    a path or a key, so nothing else may reach the store)."""
    if not CHECKSUM_PATTERN.fullmatch(sha256):
        raise ValueError("not a SHA-256 hex digest")
    return sha256


def blob_key(sha256: str) -> str:
    """A blob's place relative to the store's root: ``<sha256[:2]>/<sha256>``."""
    digest = check_checksum(sha256)
    return f"{digest[:2]}/{digest}"


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


class ByteStream(Protocol):
    """Opened bytes: a file, a window of one, or an S3 response body."""

    def read(self, size: int = -1, /) -> bytes: ...

    def close(self) -> None: ...


class Sink(Protocol):
    """Where ``assemble`` writes a session's parts."""

    def write(self, data: bytes, /) -> int: ...


class Window:
    """The bytes ``start..start+length-1`` of an opened file (``open_range`` on the local
    store): a seek, then reads that stop at the window's end."""

    def __init__(self, handle: BinaryIO, start: int, length: int) -> None:
        handle.seek(start)
        self._handle = handle
        self._left = length

    def read(self, size: int = -1, /) -> bytes:
        if self._left <= 0:
            return b""
        wanted = self._left if size is None or size < 0 else min(size, self._left)
        chunk = self._handle.read(wanted)
        self._left -= len(chunk)
        return chunk

    def close(self) -> None:
        self._handle.close()


class BlobStore(Protocol):
    """Content-addressed, immutable blobs keyed by SHA-256, plus the local staging area every
    upload streams into (see the module docstring)."""

    kind: str
    """``local`` or ``s3`` (``EPISTREL_MEDIA_STORE``)."""
    root: Path
    """The local media root: the staging area and the upload sessions' parts."""

    def prepare(self) -> None:
        """Start-up: the staging area exists and is writable (and the bucket exists, on S3);
        ``MediaRootUnwritable`` otherwise."""
        ...

    # -- staging ------------------------------------------------------------------------------

    def staging_path(self, name: str) -> Path: ...

    def open_staging(self, name: str) -> BinaryIO: ...

    def staged_files(self) -> Iterator[tuple[Path, float]]: ...

    # -- blobs --------------------------------------------------------------------------------

    def put(
        self,
        staged: Path,
        sha256: str,
        *,
        content_type: str | None = None,
        assembled: str | None = None,
    ) -> bool:
        """Move the staged file into place as blob ``sha256`` (with ``content_type``, where the
        store keeps one); ``False`` (the staged copy is dropped) when the blob already exists.
        ``assembled`` names the upload session whose assembled object holds the same bytes in
        the store already (S3 copies it instead of uploading the staged file)."""
        ...

    def open(self, sha256: str) -> ByteStream:
        """The blob's bytes, opened for reading; ``FileNotFoundError`` when it is missing."""
        ...

    def open_range(self, sha256: str, start: int, end: int) -> ByteStream:
        """The blob's bytes ``start..end`` (inclusive, within the blob), opened for reading;
        ``FileNotFoundError`` when it is missing."""
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

    def presign(self, sha256: str, ttl_s: int) -> str | None:
        """A URL the store itself serves the blob at for ``ttl_s`` seconds, or ``None`` when the
        store serves nothing itself (local)."""
        ...

    # -- resumable upload sessions ---------------------------------------------------------

    def begin_parts(self, upload_id: str, content_type: str) -> str | None:
        """Prepare to receive a session's parts; the store's own multipart id (S3), or
        ``None``."""
        ...

    def part_staging(self, upload_id: str, number: int) -> Path:
        """A fresh local file to stream part ``number`` into before ``store_part``."""
        ...

    def store_part(self, upload_id: str, number: int, staged: Path, store_id: str | None) -> None:
        """Keep the staged part as part ``number`` of the session, replacing an earlier one
        (the staged file is consumed)."""
        ...

    def stored_parts(self, upload_id: str, store_id: str | None) -> dict[int, int] | None:
        """``{part number: size}`` of the parts the store holds for the session; ``None`` when
        it holds them assembled already (S3, after a completion attempt that went no further)."""
        ...

    def assemble(self, upload_id: str, part_count: int, store_id: str | None, target: Sink) -> None:
        """Write parts ``1..part_count``, in order, into ``target``. On S3 this completes the
        multipart upload first, so the assembled object exists in the bucket (``put``'s
        ``assembled``)."""
        ...

    def reopen_parts(
        self, upload_id: str, store_id: str | None, part_size: int, size: int
    ) -> str | None:
        """After a completion that failed (the session stays open): make the session's parts
        receivable and assemblable again; the store's multipart id from now on (unchanged
        locally, where the parts never moved)."""
        ...

    def finish_parts(self, upload_id: str, store_id: str | None) -> None:
        """Remove what the session left in the store once its asset exists (its parts locally;
        the assembled object on S3)."""
        ...

    def abort_parts(self, upload_id: str, store_id: str | None) -> None:
        """Remove every part of the session (an abort or an expiry); the store's multipart
        upload is aborted."""
        ...

    def stale_part_uploads(self, older_than_s: float) -> list[tuple[str, str | None]]:
        """``(upload id, store id)`` of every session's parts in the store older than
        ``older_than_s`` seconds (the sweep removes those of sessions that are not open)."""
        ...


class LocalStaging:
    """The media root on the local filesystem: the staging area and the upload sessions'
    parts, which every adapter uses (see the module docstring)."""

    kind = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.staging = self.root / STAGING_DIR
        self.uploads = self.root / UPLOADS_DIR

    # -- start-up ------------------------------------------------------------------------------

    def prepare(self) -> None:
        """Create the root, the staging area and the sessions' directory and prove them
        writable; ``MediaRootUnwritable`` (naming the path, never anything else) otherwise."""
        try:
            self.staging.mkdir(parents=True, exist_ok=True)
            self.uploads.mkdir(parents=True, exist_ok=True)
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

    # -- the upload sessions' local directories ---------------------------------------------

    def session_dir(self, upload_id: str) -> Path:
        """The local directory of session ``upload_id``'s parts."""
        if not STAGED_NAME_PATTERN.fullmatch(upload_id):
            raise ValueError("an upload session is named by its id")
        return self.uploads / upload_id

    def part_staging(self, upload_id: str, number: int) -> Path:
        directory = self.session_dir(upload_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f".{number}.{os.getpid()}-{secrets.token_hex(4)}"

    def remove_session_dir(self, upload_id: str) -> None:
        shutil.rmtree(self.session_dir(upload_id), ignore_errors=True)

    def session_dirs(self, older_than_s: float) -> list[str]:
        """The sessions whose local directory was last changed more than ``older_than_s``
        seconds ago."""
        if not self.uploads.is_dir():
            return []
        now = time.time()
        found: list[str] = []
        for entry in os.scandir(self.uploads):
            named = STAGED_NAME_PATTERN.fullmatch(entry.name)
            if not (named and entry.is_dir(follow_symlinks=False)):
                continue
            with contextlib.suppress(FileNotFoundError):
                if now - entry.stat(follow_symlinks=False).st_mtime >= older_than_s:
                    found.append(entry.name)
        return sorted(found)


class LocalFilesystemStore(LocalStaging):
    """The default ``BlobStore``: a directory (a Docker volume in the image), plus the staging
    area every upload streams into."""

    kind = "local"

    # -- blobs ---------------------------------------------------------------------------------

    def path(self, sha256: str) -> Path:
        return self.root / blob_key(sha256)

    def put(
        self,
        staged: Path,
        sha256: str,
        *,
        content_type: str | None = None,
        assembled: str | None = None,
    ) -> bool:
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

    def open_range(self, sha256: str, start: int, end: int) -> ByteStream:
        handle = self.path(sha256).open("rb")
        try:
            return Window(handle, start, end - start + 1)
        except BaseException:
            handle.close()
            raise

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

    def presign(self, sha256: str, ttl_s: int) -> str | None:
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

    # -- resumable upload sessions ---------------------------------------------------------

    def begin_parts(self, upload_id: str, content_type: str) -> str | None:
        self.session_dir(upload_id).mkdir(parents=True, exist_ok=True)
        return None

    def part_path(self, upload_id: str, number: int) -> Path:
        return self.session_dir(upload_id) / str(number)

    def store_part(self, upload_id: str, number: int, staged: Path, store_id: str | None) -> None:
        target = self.part_path(upload_id, number)
        os.replace(staged, target)
        _fsync_directory(target.parent)

    def stored_parts(self, upload_id: str, store_id: str | None) -> dict[int, int] | None:
        directory = self.session_dir(upload_id)
        if not directory.is_dir():
            return {}
        found: dict[int, int] = {}
        for entry in os.scandir(directory):
            if entry.is_file(follow_symlinks=False) and PART_NAME_PATTERN.fullmatch(entry.name):
                found[int(entry.name)] = entry.stat(follow_symlinks=False).st_size
        return found

    def assemble(self, upload_id: str, part_count: int, store_id: str | None, target: Sink) -> None:
        for number in range(1, part_count + 1):
            with self.part_path(upload_id, number).open("rb") as part:
                shutil.copyfileobj(part, target, COPY_CHUNK)

    def reopen_parts(
        self, upload_id: str, store_id: str | None, part_size: int, size: int
    ) -> str | None:
        return store_id

    def finish_parts(self, upload_id: str, store_id: str | None) -> None:
        self.remove_session_dir(upload_id)

    def abort_parts(self, upload_id: str, store_id: str | None) -> None:
        self.remove_session_dir(upload_id)

    def stale_part_uploads(self, older_than_s: float) -> list[tuple[str, str | None]]:
        return [(upload_id, None) for upload_id in self.session_dirs(older_than_s)]


__all__ = [
    "CACHE_CONTROL",
    "CHECKSUM_PATTERN",
    "STAGING_DIR",
    "SWEEP_CURSOR",
    "UPLOADS_DIR",
    "BlobStore",
    "ByteStream",
    "LocalFilesystemStore",
    "LocalStaging",
    "MediaRootUnwritable",
    "Sink",
    "Window",
    "blob_key",
    "check_checksum",
]
