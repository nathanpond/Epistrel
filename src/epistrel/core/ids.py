"""Identifiers: ULIDs stored as ``char(26)`` (engine-wide decision, #44)."""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Annotated

from pydantic import StringConstraints

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26
ULID_PATTERN = rf"^[{CROCKFORD}]{{{ULID_LENGTH}}}$"
"""A well-formed id: 26 Crockford base32 characters, upper case, as ``new_ulid`` writes them."""

Ulid = Annotated[str, StringConstraints(pattern=ULID_PATTERN)]
"""A request field holding an Engine id; a malformed one is 422 at its ``details.field`` (#62)."""


_MAX = (1 << 128) - 1
_lock = threading.Lock()
_last = 0
"""The value of the last id ``new_ulid`` minted in this process (0: none yet)."""


def _millis() -> int:
    """The id's timestamp: Unix time in milliseconds (a test freezes it)."""
    return time.time_ns() // 1_000_000


def _reset_after_fork() -> None:
    """A forked child starts its own sequence: it must not mint the ids its parent will."""
    global _last, _lock
    _lock = threading.Lock()
    _last = 0


os.register_at_fork(after_in_child=_reset_after_fork)


def new_ulid() -> str:
    """A ULID in the spec's monotonic mode: 48-bit millisecond timestamp + 80 random bits,
    Crockford base32, 26 chars, each sorting after the one this process minted before.

    A new millisecond draws fresh random bits; within the same millisecond (or when the clock
    steps back) the id is the previous one + 1. So id order is creation order, which a
    deterministic replay repeats: every ``ORDER BY id`` tie-break orders the same way on two
    fresh databases (NFR-DETERM-1, #477). Thread-safe. Ids stay unique, but one minted right
    after another is predictable from it: use ``random_ulid`` for an id that must not be."""
    global _last
    fresh = _millis() << 80 | secrets.randbits(80)
    with _lock:
        if fresh >> 80 <= _last >> 80:
            if _last >= _MAX:
                raise OverflowError("ULID space exhausted")
            fresh = _last + 1
        _last = fresh
    return _encode(fresh)


def random_ulid() -> str:
    """A ULID whose 80 random bits are all fresh (not monotonic): for a token another holder
    must not derive from an id it has seen (the idempotency lease's ``owner_token``)."""
    return _encode(_millis() << 80 | secrets.randbits(80))


def _encode(value: int) -> str:
    chars = []
    for _ in range(ULID_LENGTH):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid_after(previous: str) -> str:
    """A new ULID that sorts after ``previous``: ``new_ulid()``, or ``previous`` + 1 when the
    new one would not (``previous`` came from another process, or from a clock ahead of this
    one's)."""
    candidate = new_ulid()
    if candidate > previous:
        return candidate
    value = 0
    for char in previous:
        value = value * 32 + CROCKFORD.index(char)
    return _encode(value + 1)
