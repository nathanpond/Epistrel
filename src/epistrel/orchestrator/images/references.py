"""Reference images as a provider can fetch them (FR-IMG-1; #255, #107).

An ``ImageRequest`` names its reference images by media asset id. Before a request goes to a
hosted provider, each id becomes a URL the provider can read:

- a #107 signed URL, valid for the deployment's ``media_signed_url_ttl``: the store's own
  presigned URL (S3), or Epistrel's signed content URL under ``public_base_url``;
- a ``data:`` URL of the bytes, when no signed URL can be made (no ``public_base_url`` or no
  config key) or the URL's host is one the provider could not reach (``localhost``, a loopback,
  private, link-local or otherwise non-public address). The image is downscaled to at most
  ``DATA_URL_MAX_SIDE`` px on its longest side and ``DATA_URL_MAX_BYTES`` bytes, in its stored
  media type.

An asset that does not exist, is a candidate, or is not an image is refused with
``ImageRequestInvalid`` (``reason`` ``reference_not_found`` / ``reference_not_image``).
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.errors import ConfigMissing, MediaNotFound, SigningKeyMissing
from epistrel.core.media import assets
from epistrel.core.media.signing import Signer
from epistrel.core.media.store import BlobStore
from epistrel.core.tables.media import MediaKind
from epistrel.orchestrator.images.errors import ImageRequestInvalid

DATA_URL_MAX_SIDE = 1_024
DATA_URL_MAX_BYTES = 4 * 1024 * 1024
DATA_URL_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
"""The media types a data URL keeps; any other image is re-encoded as PNG."""
MAX_REFERENCE_PIXELS = 64_000_000


@runtime_checkable
class ReferenceResolver(Protocol):
    """Turns a reference image's media id into a URL a provider can fetch (module docs)."""

    async def resolve(self, media_id: str) -> str: ...


def provider_can_fetch(url: str) -> bool:
    """Whether a hosted provider could fetch ``url``: an ``http(s)`` URL whose host is not
    ``localhost`` and not a loopback, private, link-local, reserved or unspecified address.
    A host name is not resolved: only its spelling is judged."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def data_url(data: bytes, media_type: str) -> str:
    """``data`` as a ``data:`` URL: downscaled to ``DATA_URL_MAX_SIDE`` px on its longest side
    and ``DATA_URL_MAX_BYTES`` bytes, in ``media_type`` when it is PNG, JPEG or WebP (else
    PNG). ``ImageRequestInvalid`` (``reference_not_image``) when the bytes are no image."""
    from PIL import Image  # Pillow is loaded only when a reference is encoded

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_REFERENCE_PIXELS:
                raise ImageRequestInvalid(reason="reference_not_image")
            image.load()
            kind = DATA_URL_FORMATS.get(media_type, "PNG")
            target = media_type if media_type in DATA_URL_FORMATS else "image/png"
            if (
                max(width, height) <= DATA_URL_MAX_SIDE
                and len(data) <= DATA_URL_MAX_BYTES
                and image.format == kind
            ):
                encoded = data
            else:
                encoded = _encode(image, kind)
    except ImageRequestInvalid:
        raise
    except Exception:  # corrupt, truncated, a pixel bomb, an unknown format
        raise ImageRequestInvalid(reason="reference_not_image") from None
    return f"data:{target};base64,{base64.b64encode(encoded).decode('ascii')}"


def _encode(image: object, kind: str) -> bytes:
    from PIL import Image

    assert isinstance(image, Image.Image)  # noqa: S101 - data_url passes a PIL image
    side = DATA_URL_MAX_SIDE
    frame = image.convert("RGB") if kind == "JPEG" and image.mode not in ("RGB", "L") else image
    while True:
        copy = frame.copy()
        copy.thumbnail((side, side))
        buffer = io.BytesIO()
        copy.save(buffer, format=kind)
        encoded = buffer.getvalue()
        if len(encoded) <= DATA_URL_MAX_BYTES or side <= 64:
            return encoded
        side //= 2


@dataclass
class MediaReferences:
    """The engine's ``ReferenceResolver``: reads the asset, then a signed URL the provider can
    fetch, else a data URL of its bytes (module docs)."""

    db: Callable[[], AsyncEngine]
    """The engine's database, read on each call (an Engine may be bound after it is built)."""
    store: BlobStore
    signer: Signer

    async def resolve(self, media_id: str) -> str:
        try:
            asset = await assets.get(self.db(), media_id)
        except MediaNotFound:
            raise ImageRequestInvalid(reason="reference_not_found") from None
        if asset.kind != MediaKind.IMAGE:
            raise ImageRequestInvalid(reason="reference_not_image")
        settings = await assets.read_media_settings(self.db())
        try:
            signed = await assets.signed_url(
                self.db(), self.store, media_id, ttl_s=None, settings=settings, signer=self.signer
            )
        except ConfigMissing, SigningKeyMissing:
            signed = None
        except MediaNotFound:
            raise ImageRequestInvalid(reason="reference_not_found") from None
        if signed is not None and provider_can_fetch(signed.url):
            return signed.url
        data = await asyncio.to_thread(self._read, asset.checksum)
        return await asyncio.to_thread(data_url, data, asset.mime_type)

    def _read(self, checksum: str) -> bytes:
        try:
            handle = self.store.open(checksum)
        except FileNotFoundError:
            raise ImageRequestInvalid(reason="reference_not_found") from None
        try:
            return handle.read()
        finally:
            handle.close()


__all__ = [
    "DATA_URL_MAX_BYTES",
    "DATA_URL_MAX_SIDE",
    "MediaReferences",
    "ReferenceResolver",
    "data_url",
    "provider_can_fetch",
]
