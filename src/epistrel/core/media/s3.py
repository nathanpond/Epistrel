"""The S3-compatible blob store (FR-MEDIA-1,3; docs/02 §4.10; #107).

``S3BlobStore`` keeps each blob at the key ``<sha256[:2]>/<sha256>`` of one bucket
(``EPISTREL_MEDIA_S3_BUCKET``), on any S3-compatible endpoint (``EPISTREL_MEDIA_S3_ENDPOINT``;
unset: AWS's own), through ``boto3`` with signature v4 (region ``EPISTREL_MEDIA_S3_REGION``,
default ``us-east-1``), path-style addressing when ``EPISTREL_MEDIA_S3_PATH_STYLE`` is on (MinIO),
and the access key pair from the settings or, when unset, boto3's own credential chain. The key
layout mirrors the local store: blobs under their shard, resumable uploads under ``uploads/``,
the sweep cursor at ``.sweep-cursor`` (and ``exports/`` stays free for #126).

- **Uploads** stage on the local media root like every upload (``LocalStaging``); ``put`` sends
  the staged file with its ``Content-Type`` and ``Cache-Control: private, max-age=31536000,
  immutable`` (s3transfer, multipart above 64 MiB), never a ``Content-Disposition``.
- **Reads** are proxied: ``open_range`` asks the store for the byte range itself (a ``Range``
  request), and the content route streams the answer in 64 KiB chunks.
- **Signed URLs**: ``presign`` gives a presigned ``GET`` the store itself serves until it expires,
  named for ``EPISTREL_MEDIA_S3_PUBLIC_ENDPOINT`` when that differs from the endpoint the server
  reaches (``GET /media/{id}/url``; FR-MEDIA-3).
- **Resumable sessions** are the bucket's native multipart uploads under ``uploads/<upload id>``:
  each part goes to the store as it arrives (``UploadPart``); completion completes the multipart
  upload, streams the assembled object back to be hashed and checked, and ``put`` copies it to
  its checksum key on the store's side. A completion that fails after that (a checksum mismatch,
  a moderation refusal) re-opens the session's parts from the assembled object
  (``reopen_parts``: ``UploadPartCopy``), so the session stays open for a retry.

Every call that fails at the store is 503 ``media_store_unavailable`` (``details.operation`` and
the store's error code, or ``connection``); a missing object is ``FileNotFoundError``, as on the
local store. Logs carry the operation and the error code only.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from epistrel.core.errors import MediaStoreUnavailable
from epistrel.core.media.store import (
    CACHE_CONTROL,
    CHECKSUM_PATTERN,
    COPY_CHUNK,
    STAGED_NAME_PATTERN,
    SWEEP_CURSOR,
    UPLOADS_DIR,
    LocalStaging,
    MediaRootUnwritable,
    Sink,
    blob_key,
    check_checksum,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

logger = logging.getLogger(__name__)

MULTIPART_THRESHOLD = 64 * 1024 * 1024
"""Above this, ``put`` sends a blob (or copies an assembled upload) in parts."""
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 60
BLOB_KEY_PATTERN = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{64})$")
MISSING = frozenset({"NoSuchKey", "NotFound", "404"})
NO_UPLOAD = frozenset({"NoSuchUpload", "NotFound", "404"})


def _code(exc: ClientError) -> str:
    error: Any = exc.response.get("Error", {})
    return str(error.get("Code", "")) or "unknown"


class _Body:
    """An S3 response body as a ``ByteStream``."""

    def __init__(self, body: Any) -> None:
        self._body = body

    def read(self, size: int = -1, /) -> bytes:
        data: bytes = self._body.read(None if size is None or size < 0 else size)
        return data

    def close(self) -> None:
        self._body.close()


class S3BlobStore(LocalStaging):
    """The S3-compatible ``BlobStore`` (see the module docstring)."""

    kind = "s3"

    def __init__(
        self,
        root: Path,
        *,
        bucket: str,
        endpoint: str | None = None,
        public_endpoint: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        path_style: bool = False,
        verify_tls: bool = True,
    ) -> None:
        super().__init__(root)
        self.bucket = bucket
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "auto"},
            connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=READ_TIMEOUT_S,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        session = boto3.session.Session()

        def client(endpoint_url: str | None) -> S3Client:
            return session.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=config,
                verify=verify_tls,
            )

        self.client = client(endpoint)
        self.public = client(public_endpoint) if public_endpoint else self.client
        self.transfer = TransferConfig(
            multipart_threshold=MULTIPART_THRESHOLD, multipart_chunksize=MULTIPART_THRESHOLD
        )

    # -- error mapping -----------------------------------------------------------------------

    def _call(
        self,
        operation: str,
        run: Callable[..., Any],
        *,
        missing: frozenset[str] = MISSING,
        **arguments: Any,
    ) -> Any:
        """``run(**arguments)``; a missing object is ``FileNotFoundError``, any other failure
        503 ``media_store_unavailable``."""
        try:
            return run(**arguments)
        except ClientError as exc:
            code = _code(exc)
            if code in missing:
                raise FileNotFoundError(operation) from None
            logger.warning("media store %s failed: %s", operation, code)
            raise MediaStoreUnavailable(
                f"the media store failed to {operation.replace('_', ' ')}",
                {"operation": operation, "reason": code},
            ) from None
        except BotoCoreError as exc:
            logger.warning("media store %s failed: %s", operation, type(exc).__name__)
            raise MediaStoreUnavailable(
                f"the media store could not be reached to {operation.replace('_', ' ')}",
                {"operation": operation, "reason": "connection"},
            ) from None

    def _upload_key(self, upload_id: str) -> str:
        if not STAGED_NAME_PATTERN.fullmatch(upload_id):
            raise ValueError("an upload session is named by its id")
        return f"{UPLOADS_DIR}/{upload_id}"

    # -- start-up ------------------------------------------------------------------------------

    def prepare(self) -> None:
        """The local staging area, then the bucket: it must exist and answer
        (``MediaRootUnwritable`` otherwise; exit 78)."""
        super().prepare()
        try:
            self._call("check_bucket", self.client.head_bucket, Bucket=self.bucket)
        except (FileNotFoundError, MediaStoreUnavailable) as exc:
            reason = (
                "it does not exist"
                if isinstance(exc, FileNotFoundError)
                else f"the store answered {exc.details.get('reason')}"
            )
            raise MediaRootUnwritable(
                f"the S3 bucket {self.bucket} cannot be used ({reason}); create it, or set "
                "EPISTREL_MEDIA_S3_BUCKET and EPISTREL_MEDIA_S3_ENDPOINT to one that exists"
            ) from None

    # -- blobs ---------------------------------------------------------------------------------

    def _head(self, key: str) -> dict[str, Any] | None:
        try:
            found: dict[str, Any] = self._call(
                "head_object", self.client.head_object, Bucket=self.bucket, Key=key
            )
        except FileNotFoundError:
            return None
        return found

    def put(
        self,
        staged: Path,
        sha256: str,
        *,
        content_type: str | None = None,
        assembled: str | None = None,
    ) -> bool:
        key = blob_key(sha256)
        if self._head(key) is not None:
            staged.unlink(missing_ok=True)
            return False
        extra: dict[str, Any] = {"CacheControl": CACHE_CONTROL}
        if content_type is not None:
            extra["ContentType"] = content_type
        if assembled is not None:
            self._call(
                "copy_object",
                self.client.copy,
                CopySource={"Bucket": self.bucket, "Key": self._upload_key(assembled)},
                Bucket=self.bucket,
                Key=key,
                ExtraArgs={**extra, "MetadataDirective": "REPLACE"},
                Config=self.transfer,
            )
        else:
            self._call(
                "put_object",
                self.client.upload_file,
                Filename=str(staged),
                Bucket=self.bucket,
                Key=key,
                ExtraArgs=extra,
                Config=self.transfer,
            )
        staged.unlink(missing_ok=True)
        return True

    def open(self, sha256: str) -> _Body:
        answer = self._call(
            "get_object", self.client.get_object, Bucket=self.bucket, Key=blob_key(sha256)
        )
        return _Body(answer["Body"])

    def open_range(self, sha256: str, start: int, end: int) -> _Body:
        answer = self._call(
            "get_object",
            self.client.get_object,
            Bucket=self.bucket,
            Key=blob_key(sha256),
            Range=f"bytes={start}-{end}",
        )
        return _Body(answer["Body"])

    def exists(self, sha256: str) -> bool:
        return self._head(blob_key(sha256)) is not None

    def delete(self, sha256: str) -> bool:
        key = blob_key(sha256)
        if self._head(key) is None:
            return False
        self._call("delete_object", self.client.delete_object, Bucket=self.bucket, Key=key)
        return True

    def size(self, sha256: str) -> int:
        found = self._head(blob_key(sha256))
        if found is None:
            raise FileNotFoundError(sha256)
        return int(found["ContentLength"])

    def _list(self, **arguments: Any) -> Iterator[dict[str, Any]]:
        """Every object ``ListObjectsV2`` lists for ``arguments``, page by page."""
        token: str | None = None
        while True:
            page_arguments = dict(arguments)
            if token is not None:
                page_arguments.pop("StartAfter", None)
                page_arguments["ContinuationToken"] = token
            page = self._call(
                "list_objects", self.client.list_objects_v2, Bucket=self.bucket, **page_arguments
            )
            yield from page.get("Contents", [])
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")

    def list_checksums(self, after: str | None = None) -> Iterator[str]:
        arguments = {"StartAfter": blob_key(after)} if after is not None else {}
        for item in self._list(**arguments):
            matched = BLOB_KEY_PATTERN.fullmatch(item.get("Key", ""))
            if matched and matched.group(2)[:2] == matched.group(1):
                yield matched.group(2)

    def age(self, sha256: str) -> float | None:
        found = self._head(blob_key(sha256))
        if found is None:
            return None
        modified: datetime = found["LastModified"]
        return time.time() - modified.timestamp()

    def presign(self, sha256: str, ttl_s: int) -> str:
        url: str = self._call(
            "presign",
            self.public.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": blob_key(sha256)},
            ExpiresIn=ttl_s,
        )
        return url

    # -- the sweep cursor ----------------------------------------------------------------------

    def sweep_cursor(self) -> str | None:
        try:
            answer = self._call(
                "get_object", self.client.get_object, Bucket=self.bucket, Key=SWEEP_CURSOR
            )
        except FileNotFoundError:
            return None
        body = answer["Body"]
        try:
            value = body.read(256).decode("ascii", "replace").strip()
        finally:
            body.close()
        return value if CHECKSUM_PATTERN.fullmatch(value) else None

    def set_sweep_cursor(self, checksum: str | None) -> None:
        if checksum is None:
            self._call(
                "delete_object", self.client.delete_object, Bucket=self.bucket, Key=SWEEP_CURSOR
            )
            return
        self._call(
            "put_object",
            self.client.put_object,
            Bucket=self.bucket,
            Key=SWEEP_CURSOR,
            Body=check_checksum(checksum).encode("ascii"),
        )

    # -- resumable upload sessions ---------------------------------------------------------

    def begin_parts(self, upload_id: str, content_type: str) -> str:
        key = self._upload_key(upload_id)
        self.session_dir(upload_id).mkdir(parents=True, exist_ok=True)
        answer = self._call(
            "create_multipart_upload",
            self.client.create_multipart_upload,
            Bucket=self.bucket,
            Key=key,
            ContentType=content_type,
        )
        return str(answer["UploadId"])

    def store_part(self, upload_id: str, number: int, staged: Path, store_id: str | None) -> None:
        if store_id is None:
            raise ValueError("an S3 session has a multipart upload id")
        try:
            with staged.open("rb") as body:
                self._call(
                    "upload_part",
                    self.client.upload_part,
                    missing=frozenset(),
                    Bucket=self.bucket,
                    Key=self._upload_key(upload_id),
                    UploadId=store_id,
                    PartNumber=number,
                    Body=body,
                )
        finally:
            staged.unlink(missing_ok=True)

    def _parts(self, upload_id: str, store_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        marker = 0
        while True:
            page = self._call(
                "list_parts",
                self.client.list_parts,
                missing=NO_UPLOAD,
                Bucket=self.bucket,
                Key=self._upload_key(upload_id),
                UploadId=store_id,
                PartNumberMarker=marker,
            )
            found.extend(dict(part) for part in page.get("Parts", []))
            if not page.get("IsTruncated"):
                return found
            marker = int(page.get("NextPartNumberMarker", 0))

    def stored_parts(self, upload_id: str, store_id: str | None) -> dict[int, int] | None:
        if self._head(self._upload_key(upload_id)) is not None:
            return None  # completed by an earlier attempt: the assembled object has them all
        if store_id is None:
            return {}
        try:
            parts = self._parts(upload_id, store_id)
        except FileNotFoundError:  # aborted
            return {}
        return {int(part["PartNumber"]): int(part["Size"]) for part in parts}

    def assemble(self, upload_id: str, part_count: int, store_id: str | None, target: Sink) -> None:
        key = self._upload_key(upload_id)
        if self._head(key) is None:  # not completed by an earlier attempt
            if store_id is None:
                raise ValueError("an S3 session has a multipart upload id")
            etags = {int(p["PartNumber"]): p["ETag"] for p in self._parts(upload_id, store_id)}
            parts = [{"PartNumber": n, "ETag": etags[n]} for n in range(1, part_count + 1)]
            self._call(
                "complete_multipart_upload",
                self.client.complete_multipart_upload,
                missing=frozenset(),
                Bucket=self.bucket,
                Key=key,
                UploadId=store_id,
                MultipartUpload={"Parts": parts},
            )
        answer = self._call("get_object", self.client.get_object, Bucket=self.bucket, Key=key)
        body = answer["Body"]
        try:
            while chunk := body.read(COPY_CHUNK):
                target.write(chunk)
        finally:
            body.close()

    def reopen_parts(
        self, upload_id: str, store_id: str | None, part_size: int, size: int
    ) -> str | None:
        """After a completion that failed once the multipart upload was completed: a new
        multipart upload whose parts are copied from the assembled object, which is then
        removed; its id (``store_id`` unchanged when nothing was completed). The session's
        parts are what they were."""
        key = self._upload_key(upload_id)
        if self._head(key) is None:
            return store_id
        answer = self._call(
            "create_multipart_upload",
            self.client.create_multipart_upload,
            Bucket=self.bucket,
            Key=key,
        )
        reopened = str(answer["UploadId"])
        number, start = 1, 0
        while start < size:
            end = min(start + part_size, size) - 1
            self._call(
                "upload_part_copy",
                self.client.upload_part_copy,
                missing=frozenset(),
                Bucket=self.bucket,
                Key=key,
                UploadId=reopened,
                PartNumber=number,
                CopySource={"Bucket": self.bucket, "Key": key},
                CopySourceRange=f"bytes={start}-{end}",
            )
            number, start = number + 1, end + 1
        self._call("delete_object", self.client.delete_object, Bucket=self.bucket, Key=key)
        return reopened

    def _abort(self, upload_id: str, store_id: str | None) -> None:
        key = self._upload_key(upload_id)
        if store_id is not None:
            with contextlib.suppress(FileNotFoundError):  # completed or aborted already
                self._call(
                    "abort_multipart_upload",
                    self.client.abort_multipart_upload,
                    missing=NO_UPLOAD,
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=store_id,
                )
        self._call("delete_object", self.client.delete_object, Bucket=self.bucket, Key=key)
        self.remove_session_dir(upload_id)

    def finish_parts(self, upload_id: str, store_id: str | None) -> None:
        self._abort(upload_id, store_id)

    def abort_parts(self, upload_id: str, store_id: str | None) -> None:
        self._abort(upload_id, store_id)

    def stale_part_uploads(self, older_than_s: float) -> list[tuple[str, str | None]]:
        """The store's multipart uploads and assembled objects under ``uploads/`` older than
        ``older_than_s``, and the local session directories as old. Multipart uploads are
        listed without a prefix (MinIO lists them by exact key only) and filtered here."""
        now = time.time()
        found: dict[str, str | None] = {}
        markers: dict[str, Any] = {}
        while True:
            page = self._call(
                "list_multipart_uploads",
                self.client.list_multipart_uploads,
                Bucket=self.bucket,
                **markers,
            )
            for upload in page.get("Uploads", []):
                name = str(upload.get("Key", "")).removeprefix(f"{UPLOADS_DIR}/")
                started = upload.get("Initiated")
                if (
                    STAGED_NAME_PATTERN.fullmatch(name)
                    and started is not None
                    and now - started.timestamp() >= older_than_s
                ):
                    found[name] = str(upload["UploadId"])
            if not page.get("IsTruncated"):
                break
            markers = {
                "KeyMarker": page.get("NextKeyMarker", ""),
                "UploadIdMarker": page.get("NextUploadIdMarker", ""),
            }
        for item in self._list(Prefix=f"{UPLOADS_DIR}/"):
            name = str(item.get("Key", "")).removeprefix(f"{UPLOADS_DIR}/")
            modified = item.get("LastModified")
            if (
                STAGED_NAME_PATTERN.fullmatch(name)
                and modified is not None
                and now - modified.timestamp() >= older_than_s
            ):
                found.setdefault(name, None)
        for name in self.session_dirs(older_than_s):
            found.setdefault(name, None)
        return sorted(found.items())


__all__ = ["MULTIPART_THRESHOLD", "S3BlobStore"]
