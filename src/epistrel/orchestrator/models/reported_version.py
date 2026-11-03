"""The version each model reports (FR-PROF-16; ADR-107; #234).

A hosted model can change behind an unchanged name. The Model Access Layer records the version a
completion reports (the OpenAI-compatible ``system_fingerprint``; the stub reports none unless a
test sets one) in ``model_reported_versions``, once per change (``observe``: an in-process memo
skips the write while the value holds). A configuration's fingerprint names the latest stored
version of each model (``latest``, read with the model configuration at a turn's start, never the
value a call observes mid-turn), so a reported version change is a new fingerprint
(``uncertified``), and drift under an unchanged version is a canary's to catch (``stale``).

#710: when a completion reports no ``system_fingerprint`` (common for local OpenAI-compatible
servers), the version is the ``version`` field of the model's ``/models`` entry, when the
adapter lists one (``ModelAccessLayer._listed_version``). The listing is probed at the first
such call and then at most once an hour per purpose (``LISTING_TTL_S``, an in-process cache:
``listing``/``remember_listing``); neither reported, the version stays null.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.tables import model_reported_versions

_SEEN: dict[tuple[str, str], str] = {}
_LISTINGS: dict[str, tuple[float, dict[str, str]]] = {}
LISTING_TTL_S = 3600.0
"""#710: how long one ``/models`` listing of a purpose's endpoint is reused (hourly)."""


def listing(purpose: str) -> dict[str, str] | None:
    """The cached ``/models`` versions of ``purpose``'s endpoint, or ``None`` when absent or
    older than ``LISTING_TTL_S``."""
    found = _LISTINGS.get(purpose)
    if found is None or time.monotonic() - found[0] > LISTING_TTL_S:
        return None
    return found[1]


def remember_listing(purpose: str, versions: dict[str, str]) -> dict[str, str]:
    """Cache ``versions`` as ``purpose``'s listing (see ``listing``)."""
    _LISTINGS[purpose] = (time.monotonic(), dict(versions))
    return _LISTINGS[purpose][1]


def endpoint_hash(base_url: str | None) -> str:
    """The SHA-256 of an endpoint's base URL (never stored in clear)."""
    return hashlib.sha256((base_url or "").encode("utf-8")).hexdigest()


async def observe(
    db: AsyncEngine | None, model_id: str, reported: str | None, *, base_url: str | None = None
) -> None:
    """Record ``reported`` for ``model_id`` when it changed (see the module docstring)."""
    if db is None or not reported or not model_id:
        return
    key = (endpoint_hash(base_url), model_id)
    if _SEEN.get(key) == reported:
        return
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO model_reported_versions (base_url_hash, model_id, reported_version, "
                "first_seen_at, last_seen_at) VALUES (:h, :m, :v, now(), now()) "
                "ON CONFLICT (base_url_hash, model_id, reported_version) "
                "DO UPDATE SET last_seen_at = now()"
            ),
            {"h": key[0], "m": model_id, "v": reported},
        )
    _SEEN[key] = reported


async def latest(conn: AsyncConnection) -> dict[str, str]:
    """Each model's latest reported version (by when it was last seen)."""
    t = model_reported_versions.c
    rows: Any = await conn.execute(
        select(t.model_id, t.reported_version).order_by(t.model_id, t.last_seen_at.desc())
    )
    found: dict[str, str] = {}
    for row in rows:
        found.setdefault(str(row.model_id), str(row.reported_version))
    return found


def forget() -> None:
    """Drop the in-process memos (tests)."""
    _SEEN.clear()
    _LISTINGS.clear()


__all__ = [
    "LISTING_TTL_S",
    "endpoint_hash",
    "forget",
    "latest",
    "listing",
    "observe",
    "remember_listing",
]
