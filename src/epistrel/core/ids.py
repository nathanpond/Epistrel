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
    value = (time.time_ns() // 1_000_000) << 80 | secrets.randbits(80)
    chars = []
    for _ in range(ULID_LENGTH):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
