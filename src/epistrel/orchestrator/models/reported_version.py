"""The version each model reports (FR-PROF-16; ADR-107; #234).

A hosted model can change behind an unchanged name. The Model Access Layer records the version a
completion reports (the OpenAI-compatible ``system_fingerprint``; the stub reports none unless a
test sets one) in ``model_reported_versions``, once per change (``observe``: an in-process memo
skips the write while the value holds). A configuration's fingerprint names the latest stored
version of each model (``latest``, read with the model configuration at a turn's start, never the
value a call observes mid-turn), so a reported version change is a new fingerprint
(``uncertified``), and drift under an unchanged version is a canary's to catch (``stale``).
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.tables import model_reported_versions

_SEEN: dict[tuple[str, str], str] = {}


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
    """Drop the in-process memo (tests)."""
    _SEEN.clear()


__all__ = ["endpoint_hash", "forget", "latest", "observe"]
