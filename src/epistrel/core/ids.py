"""Identifiers: ULIDs stored as ``char(26)`` (engine-wide decision, #44)."""

from __future__ import annotations

import secrets
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26


def new_ulid() -> str:
    """A ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32, 26 chars."""
    value = (time.time_ns() // 1_000_000) << 80 | secrets.randbits(80)
    chars = []
    for _ in range(ULID_LENGTH):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
