"""Signed media URLs and the moderation hook's request signature (FR-MEDIA-2,3; #105).

Both are HMAC-SHA256 keyed by a **subkey** that HKDF-SHA256 derives from
``EPISTREL_CONFIG_KEY``, each with its own ``info`` string, so neither secret is the master key
(which also decrypts the stored model API keys, #51), and neither subkey reveals the other:

- ``media-url`` signs a content URL: ``sig`` = base64url (unpadded) of the HMAC of
  ``"<METHOD>|<path>|<exp>"``, where ``exp`` is the expiry in Unix seconds. A URL verifies
  until 30 seconds after ``exp`` (clock skew). The moderation hook's ``content_url`` is one,
  valid for 5 minutes; ``GET /media/{id}/url`` on the local store gives one valid for
  ``media_signed_url_ttl`` (#107), and a valid one is enough on its own to read
  ``GET /media/{id}/content`` (invariant 4's one conditional exemption). ``check`` tells an
  expired URL (``url_expired``) from a forged one (``signature_invalid``).
- ``media-moderation-hook`` signs a hook request: ``X-Epistrel-Signature: sha256=<hex>`` is the
  HMAC of ``"<X-Epistrel-Timestamp>." + body``. The consumer verifies it with the secret
  ``epistrel moderation-secret`` prints (the derived subkey in hex), never the config key.

Without ``EPISTREL_CONFIG_KEY`` nothing can be signed: ``require`` raises 503
``config_key_missing``, and nothing verifies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from epistrel.core import clock
from epistrel.core.errors import SigningKeyMissing

URL_INFO = b"media-url"
HOOK_INFO = b"media-moderation-hook"
SUBKEY_BYTES = 32
CLOCK_SKEW_S = 30
CONTENT_URL_TTL_S = 300
"""How long the moderation hook's ``content_url`` works: 5 minutes."""
SIGNATURE_PREFIX = "sha256="


def subkey(config_key: str, info: bytes) -> bytes:
    """The HKDF-SHA256 subkey of ``config_key`` (a Fernet key: URL-safe base64 of 32 bytes)
    for ``info``."""
    try:
        material = base64.urlsafe_b64decode(config_key.encode("ascii"))
    except ValueError:  # binascii.Error is one, as is a non-ASCII key
        raise ValueError("EPISTREL_CONFIG_KEY is not URL-safe base64") from None
    return HKDF(algorithm=hashes.SHA256(), length=SUBKEY_BYTES, salt=None, info=info).derive(
        material
    )


def moderation_secret(config_key: str) -> str:
    """The secret a moderation hook verifies ``X-Epistrel-Signature`` with: the hook subkey, in
    hex (``epistrel moderation-secret`` prints it)."""
    return subkey(config_key, HOOK_INFO).hex()


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


class Signer:
    """Signs and verifies with the subkeys of one config key (``None``: none configured)."""

    def __init__(self, config_key: SecretStr | None) -> None:
        self._key = config_key.get_secret_value() if config_key is not None else None

    @property
    def configured(self) -> bool:
        return self._key is not None

    def require(self, message: str | None = None) -> None:
        """503 ``config_key_missing`` unless a config key is configured."""
        if self._key is None:
            raise SigningKeyMissing(
                message
                or "EPISTREL_CONFIG_KEY is not set; the moderation hook's content URL and request "
                "signature cannot be made, so no upload can be moderated"
            )

    def _subkey(self, info: bytes) -> bytes:
        self.require()
        assert self._key is not None  # noqa: S101 - require() raised otherwise
        return subkey(self._key, info)

    # -- content URLs ------------------------------------------------------------------------

    def url_signature(self, method: str, path: str, exp: int) -> str:
        message = f"{method.upper()}|{path}|{exp}".encode()
        return _b64(hmac.new(self._subkey(URL_INFO), message, hashlib.sha256).digest())

    def sign_path(
        self, path: str, *, ttl_s: int = CONTENT_URL_TTL_S, now: datetime | None = None
    ) -> tuple[str, int]:
        """``path`` with ``?exp=&sig=`` for a ``GET`` valid ``ttl_s`` seconds, and the expiry
        (Unix seconds)."""
        exp = _epoch(now or clock.now()) + ttl_s
        return f"{path}?exp={exp}&sig={self.url_signature('GET', path, exp)}", exp

    def verify(
        self, method: str, path: str, exp: int, sig: str, *, now: datetime | None = None
    ) -> bool:
        """Whether ``sig`` signs ``method path`` until ``exp`` (plus the skew), now."""
        return self.check(method, path, exp, sig, now=now) == "valid"

    def check(
        self, method: str, path: str, exp: int, sig: str, *, now: datetime | None = None
    ) -> Literal["valid", "expired", "invalid"]:
        """``valid`` when ``sig`` signs ``method path`` and ``exp`` (plus the skew) has not
        passed; ``expired`` when it signs them but has; ``invalid`` otherwise (and always
        without a config key)."""
        if self._key is None:
            return "invalid"
        expected = self.url_signature(method, path, exp)
        if not hmac.compare_digest(expected.encode(), sig.encode()):
            return "invalid"
        if _epoch(now or clock.now()) > exp + CLOCK_SKEW_S:
            return "expired"
        return "valid"

    # -- the moderation hook ------------------------------------------------------------------

    def hook_signature(self, timestamp: int, body: bytes) -> str:
        """``X-Epistrel-Signature`` for a hook request: ``sha256=`` + the hex HMAC of
        ``"<timestamp>." + body``."""
        message = f"{timestamp}.".encode() + body
        return (
            SIGNATURE_PREFIX
            + hmac.new(self._subkey(HOOK_INFO), message, hashlib.sha256).hexdigest()
        )


__all__ = [
    "CLOCK_SKEW_S",
    "CONTENT_URL_TTL_S",
    "HOOK_INFO",
    "SIGNATURE_PREFIX",
    "URL_INFO",
    "Signer",
    "moderation_secret",
    "subkey",
]
