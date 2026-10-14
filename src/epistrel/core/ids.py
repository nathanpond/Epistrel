"""Identifiers: ULIDs stored as ``char(26)`` (engine-wide decision, #44)."""

from __future__ import annotations

import secrets
import time
from typing import Annotated

from pydantic import StringConstraints

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26
ULID_PATTERN = rf"^[{CROCKFORD}]{{{ULID_LENGTH}}}$"
"""A well-formed id: 26 Crockford base32 characters, upper case, as ``new_ulid`` writes them."""

Ulid = Annotated[str, StringConstraints(pattern=ULID_PATTERN)]
"""A request field holding an Engine id; a malformed one is 422 at its ``details.field`` (#62)."""


def new_ulid() -> str:
    """A ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32, 26 chars."""
    return _encode((time.time_ns() // 1_000_000) << 80 | secrets.randbits(80))


def _encode(value: int) -> str:
    chars = []
    for _ in range(ULID_LENGTH):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid_after(previous: str) -> str:
    """A new ULID that sorts after ``previous``: ``new_ulid()``, or ``previous`` + 1 when the
    new one would not (two ids minted in the same millisecond sort at random otherwise)."""
    candidate = new_ulid()
    if candidate > previous:
        return candidate
    value = 0
    for char in previous:
        value = value * 32 + CROCKFORD.index(char)
    return _encode(value + 1)
