"""Helpers for the rating-change tests (#118): a scratch database with the app over it, a
rating policy linked into Teen, story helpers, and a worker that runs the rating checks.

Each test runs on its own scratch database: it links a policy into the seeded Teen rating
(the seeds list none, so every seeded rating's effective set is empty and every move between
them would relax), it counts rows, and it runs the job worker, which in the shared database
would claim other tests' jobs.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.policies.model import Policy
from epistrel.core.ratings import RatingReplacement, get_rating, update_rating
from epistrel.core.seeds.ratings import TEEN_ID
from epistrel.engine import Engine
from epistrel.orchestrator.jobs.handlers import rating_check as rating_check_job
from epistrel.orchestrator.jobs.worker import Worker, WorkerConfig
from epistrel.orchestrator.policies.schemas import PolicyFields
from tests.auth_support import auth_settings
from tests.conformance.conftest import migrated_database
from tests.model_support import config_key
from tests.story_support import READ, WRITE

OPERATOR = RequestContext(principal="p-operator")
AUTHOR = "p-author"
"""The principal of ``WRITE``: who asks for the changes."""


@dataclass
class Scratch:
    settings: Settings
    db: AsyncEngine
    client: httpx.AsyncClient
    lib: Engine


@asynccontextmanager
async def rating_db(pg_url: str) -> AsyncIterator[Scratch]:
    """A fresh migrated database (dropped afterwards), the REST app over it (no lifespan, so no
    worker runs until a test runs one), and the rating-check job bound to its models."""
    with migrated_database(pg_url, f"epistrel_rate_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub", config_key=config_key())
        db = make_engine(settings)
        app = create_app(settings)
        lib: Engine = app.state.epistrel
        lib.bind(db)
        rating_check_job.bind(lib.models)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield Scratch(settings, db, client, lib)
        finally:
            rating_check_job.bind(None)
            await lib.drainer.stop()
            await lib.models.aclose()
            await db.dispose()


async def guard_rating(
    s: Scratch,
    rating_id: str = TEEN_ID,
    rule: dict[str, Any] | None = None,
    *,
    name: str = "no-dragons",
    message: str = "No dragons here.",
    applies_to: list[str] | None = None,
) -> Policy:
    """A rating-scoped policy (by default: no dragons, on the scope's default targets, text
    output) linked into ``rating_id``'s own policies."""
    fields: dict[str, Any] = {
        "scope": "rating",
        "scope_id": rating_id,
        "name": f"{name}-{secrets.token_hex(3)}",
        "rule": rule or {"type": "wordlist", "words": ["dragon"]},
        "message": message,
    }
    if applies_to is not None:
        fields["applies_to"] = applies_to
    created = await s.lib.policies_create(OPERATOR, PolicyFields.model_validate(fields))
    rating = (await get_rating(s.db, rating_id)).rating
    await update_rating(
        s.db,
        rating_id,
        RatingReplacement(
            name=rating.name,
            description=rating.description,
            display_order=rating.display_order,
            prompt_guidance=rating.prompt_guidance,
            image_guidance=rating.image_guidance,
            policy_ids=[*rating.policy_ids, created.policy.id],
            inherits_from=list(rating.inherits_from),
        ),
        principal=OPERATOR.principal or "",
    )
    return created.policy


async def change_rating(
    client: httpx.AsyncClient, story_id: str, rating_id: str, **headers: str
) -> httpx.Response:
    return await client.put(
        f"/stories/{story_id}/content-rating",
        json={"content_rating_id": rating_id},
        headers={**WRITE, **headers},
    )


async def check(client: httpx.AsyncClient, story_id: str, job_id: str) -> dict[str, Any]:
    response = await client.get(f"/stories/{story_id}/content-rating/checks/{job_id}", headers=READ)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def story(client: httpx.AsyncClient, story_id: str) -> dict[str, Any]:
    response = await client.get(f"/stories/{story_id}", headers=READ)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def worker(db: AsyncEngine) -> Worker:
    """A worker that runs due jobs one at a time, without timers or LISTEN."""
    return Worker(
        db, WorkerConfig(poll_interval=0.2, lease_s=30.0, listen=False, concurrency=1, timers=False)
    )


async def run_jobs(db: AsyncEngine, *, runs: int = 50) -> int:
    """Run due jobs until none is due (at most ``runs``); how many ran."""
    found = worker(db)
    ran = 0
    while ran < runs and await found.run_one("rating-test:0"):
        ran += 1
    return ran


async def due_now(db: AsyncEngine) -> None:
    """Make every queued job due now (a requeue's wait, or a failed attempt's backoff)."""
    async with db.begin() as conn:
        await conn.execute(text("UPDATE jobs SET due_at = now() WHERE status = 'queued'"))


async def scalar(db: AsyncEngine, sql: str, **params: Any) -> Any:
    async with db.connect() as conn:
        return await conn.scalar(text(sql), params)


async def rows(db: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with db.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def story_counts(db: AsyncEngine, story_id: str) -> dict[str, int]:
    """The story's events and operations, and the deployment's ``config_changes``."""
    return {
        "events": int(
            await scalar(db, "SELECT count(*) FROM events WHERE story_id = :s", s=story_id)
        ),
        "operations": int(
            await scalar(db, "SELECT count(*) FROM operations WHERE story_id = :s", s=story_id)
        ),
        "config_changes": int(await scalar(db, "SELECT count(*) FROM config_changes")),
    }


__all__ = [
    "AUTHOR",
    "OPERATOR",
    "Scratch",
    "change_rating",
    "check",
    "due_now",
    "guard_rating",
    "rating_db",
    "rows",
    "run_jobs",
    "scalar",
    "story",
    "story_counts",
    "worker",
]
