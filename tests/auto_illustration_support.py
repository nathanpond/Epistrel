"""Helpers for the automatic illustration tests (#265, FR-IMG-6): a Lite story on
``tests.story_image_generation_support``'s app (stub model adapter, stub image adapter, scripted
checks), the story's ``illustration_mode`` and cap, a Lite turn whose scripted reply may end with
an illustrate tag, a structured render marking illustration points, and the automatic jobs a
story's turns queued.

The stub ``generation`` model answers what a test scripts; a tagged reply ends with
``[[illustrate: <reason>]]``, which the pipeline strips before the reply is stored or shown."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.db import make_engine
from epistrel.core.deployment import apply_settings
from epistrel.core.idempotency import IDEMPOTENCY_HEADER
from tests import story_image_generation_support as sg
from tests import story_image_support as si
from tests.conformance.conftest import migrated_database
from tests.image_job_support import image_settings_for
from tests.respond_support import body, respond
from tests.story_support import WRITE

REPLY = "Jenni lifts the lamp: a cellar door stands open below the stair."
TAG = "[[illustrate: new_location]]"
TAGGED = f"{REPLY}\n{TAG}"


def tagged(reply: str = REPLY, reason: str = "new_location") -> str:
    return f"{reply}\n[[illustrate: {reason}]]"


async def lite_story(app: sg.GenerationApp, turns: int = 0) -> tuple[str, dict[str, str]]:
    """The Lite whisper scene (Bobby the player, Jenni, Marcus) with ``turns`` turns played and
    the image adapter and checker configured."""
    story, ids = await si.scene(app.client, turns)
    await app.configure()
    return story, ids


async def illustrate(
    client: httpx.AsyncClient, story_id: str, mode: str = "auto", cap: Any = None
) -> httpx.Response:
    """``PATCH /stories/{id}/settings`` with ``illustration_mode`` (and the cap when given)."""
    values: dict[str, Any] = {"illustration_mode": mode}
    if cap is not None:
        values["max_auto_images_per_turn"] = cap
    return await client.patch(f"/stories/{story_id}/settings", json=values, headers=WRITE)


async def set_auto(client: httpx.AsyncClient, story_id: str, cap: Any = None) -> None:
    answer = await illustrate(client, story_id, "auto", cap)
    assert answer.status_code == 200, answer.text


async def lite_turn(
    app: sg.GenerationApp,
    story_id: str,
    actor: str,
    reply: str = TAGGED,
    *,
    key: str | None = None,
    say: str = "What is down there?",
) -> httpx.Response:
    """One Lite turn whose ``generation`` answer is ``reply``."""
    app.stub.script("generation", [reply])
    headers = {IDEMPOTENCY_HEADER: key} if key is not None else None
    return await respond(app.client, story_id, body(actor, say), headers)


@asynccontextmanager
async def scratch_app(
    pg_url: str, tmp_path: Path, name: str
) -> AsyncIterator[tuple[sg.GenerationApp, AsyncEngine]]:
    """``sg.generation_app`` on a fresh migrated database of its own: for a test that changes
    the deployment's image limits (deployment-wide, so never on the shared database)."""
    with migrated_database(pg_url, f"epistrel_{name}_{secrets.token_hex(3)}") as url:
        settings = image_settings_for(url, tmp_path)
        db = make_engine(settings)
        try:
            async with sg.generation_app(settings, db) as app:
                yield app, db
        finally:
            await db.dispose()


async def deployment(db: AsyncEngine, **changes: Any) -> None:
    """Change deployment settings (the image limits) as an operator."""
    await apply_settings(db, changes, principal="op-auto")


def marked_render(beats: list[dict[str, Any]], marks: list[dict[str, Any]]) -> str:
    """A scripted structured beat render with ``illustrate`` hints."""
    return json.dumps({"beats": beats, "illustrate": marks})


async def auto_jobs(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The story's automatic image jobs, in queue order (``created_at``, then ``k``)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT * FROM image_jobs WHERE story_id = :s AND origin->>'source' = 'auto' "
                "ORDER BY created_at, (origin->'auto'->>'k')::int"
            ),
            {"s": story_id},
        )
        return [dict(r._mapping) for r in rows]


async def jobs_rows(engine: AsyncEngine, job_ids: list[str]) -> int:
    """How many ``jobs`` rows run the image jobs ``job_ids``."""
    async with engine.connect() as conn:
        found = await conn.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE kind = 'image_job' "
                "AND payload->>'image_job_id' = ANY(:ids)"
            ),
            {"ids": job_ids},
        )
    return int(found or 0)


async def reservations(engine: AsyncEngine, story_id: str) -> int:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT count(*) FROM image_reservations WHERE story_id = :s"), {"s": story_id}
        )
    return int(found or 0)


def generation_prompts(app: sg.GenerationApp) -> list[str]:
    """The text of every ``generation`` call so far."""
    return [sg.payload(c) for c in app.stub.calls if c.purpose == "generation"]


async def job_view(app: sg.GenerationApp, job_id: str) -> dict[str, Any]:
    """``GET /image-jobs/{id}`` with the service credential alone (a ``system`` job, FR-AUTH)."""
    from tests.auth_support import bearer

    answer = await app.client.get(f"/image-jobs/{job_id}", headers=bearer())
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


__all__ = [
    "REPLY",
    "TAG",
    "TAGGED",
    "auto_jobs",
    "deployment",
    "generation_prompts",
    "illustrate",
    "job_view",
    "jobs_rows",
    "lite_story",
    "lite_turn",
    "marked_render",
    "reservations",
    "scratch_app",
    "set_auto",
    "tagged",
]
