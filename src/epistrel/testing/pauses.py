"""Named in-process pause points: deterministic races for concurrency tests (#103).

``await pause(point)`` is a permanent call at each named point of a write path. It does nothing
unless a test holds that point (``with held(point) as hold``): then every call that reaches it
counts itself in and waits until the test calls ``hold.release()`` (or leaves the block), so two
requests can be brought to the same step before either goes on. Unlike ``crash`` (a child
process ends at a point), a pause runs in the test's own process and event loop; nothing in
``create_app`` or ``Engine`` arms one.

The points:

- ``catalog.save.before_cas``: a catalog draft save has read the entry and built the new draft;
  its compare-and-set ``UPDATE`` (on ``draft_revision``) has not run.
- ``catalog.publish.locked``: a catalog publish holds the entry's row lock and has compared the
  draft revision; the version is not written yet.
- ``ownership.add.found``: a relation add holds the entity's row lock and found the relation
  already stored (its insert did nothing); it has not read the stored row yet (#104).
- ``media.sweep.before_purge``: the media sweep found no reference to an asset in its batch;
  it has not locked the asset's row to purge it yet (#105).
- ``media.upload.before_commit``: an upload holds its checksum's advisory lock, has inserted its
  row and moved its bytes into place (or found the blob already there); its transaction has not
  committed yet (#105).
- ``media.complete.locked``: an upload session's completion holds the session's row lock and
  found it open with every part; it has not assembled the parts yet (#107).
- ``profiles.purge.before_lock``: the profile purge (``retention_sweep``) found a deleted engine
  profile that no story uses; it has not locked the profile's row to purge it yet (#106).
- ``catalog.listing_status.locked``: a listing-status change holds the deployment row ``FOR
  SHARE`` and found its value among the configured ones; it has not locked the entry yet (#110).
- ``feedback.rate.unrated``: a message rating holds the deployment row ``FOR SHARE``, has checked
  its tags and text, and found no rating of the principal's yet; it has not inserted one (#112).

This module imports nothing but the standard library, so ``epistrel.core`` can call it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

CATALOG_SAVE_BEFORE_CAS = "catalog.save.before_cas"
CATALOG_PUBLISH_LOCKED = "catalog.publish.locked"
OWNERSHIP_ADD_FOUND = "ownership.add.found"
MEDIA_SWEEP_BEFORE_PURGE = "media.sweep.before_purge"
MEDIA_UPLOAD_BEFORE_COMMIT = "media.upload.before_commit"
MEDIA_COMPLETE_LOCKED = "media.complete.locked"
PROFILE_PURGE_BEFORE_LOCK = "profiles.purge.before_lock"
CATALOG_LISTING_STATUS_LOCKED = "catalog.listing_status.locked"
FEEDBACK_RATE_UNRATED = "feedback.rate.unrated"
POINTS = (
    CATALOG_SAVE_BEFORE_CAS,
    CATALOG_PUBLISH_LOCKED,
    OWNERSHIP_ADD_FOUND,
    MEDIA_SWEEP_BEFORE_PURGE,
    MEDIA_UPLOAD_BEFORE_COMMIT,
    MEDIA_COMPLETE_LOCKED,
    PROFILE_PURGE_BEFORE_LOCK,
    CATALOG_LISTING_STATUS_LOCKED,
    FEEDBACK_RATE_UNRATED,
)

ARRIVAL_TIMEOUT_S = 10.0


class Hold:
    """A held point: the calls waiting at it, and the release."""

    def __init__(self, point: str) -> None:
        self.point = point
        self.arrivals = 0
        self._released = asyncio.Event()
        self._arrived = asyncio.Event()

    async def wait(self) -> None:
        """Count this call in and wait for the release."""
        self.arrivals += 1
        self._arrived.set()
        await self._released.wait()

    async def arrived(self, count: int = 1) -> None:
        """Return once ``count`` calls have reached the point (``TimeoutError`` after
        ``ARRIVAL_TIMEOUT_S`` seconds)."""
        async with asyncio.timeout(ARRIVAL_TIMEOUT_S):
            while self.arrivals < count:
                self._arrived.clear()
                await self._arrived.wait()

    def release(self) -> None:
        """Let every waiting call, and every later one, go on."""
        self._released.set()


_HELD: dict[str, Hold] = {}


async def pause(point: str) -> None:
    """A pause point; see the module docstring. A no-op unless a test holds ``point``."""
    hold = _HELD.get(point)
    if hold is not None:
        await hold.wait()


@contextmanager
def held(point: str) -> Iterator[Hold]:
    """Hold ``point`` for the block; leaving it releases every waiting call."""
    if point not in POINTS:
        raise ValueError(f"unknown pause point {point!r}; one of {', '.join(POINTS)}")
    if point in _HELD:
        raise RuntimeError(f"pause point {point!r} is already held")
    hold = Hold(point)
    _HELD[point] = hold
    try:
        yield hold
    finally:
        del _HELD[point]
        hold.release()


__all__ = [
    "CATALOG_PUBLISH_LOCKED",
    "CATALOG_SAVE_BEFORE_CAS",
    "FEEDBACK_RATE_UNRATED",
    "MEDIA_COMPLETE_LOCKED",
    "MEDIA_SWEEP_BEFORE_PURGE",
    "MEDIA_UPLOAD_BEFORE_COMMIT",
    "OWNERSHIP_ADD_FOUND",
    "POINTS",
    "PROFILE_PURGE_BEFORE_LOCK",
    "Hold",
    "held",
    "pause",
]
