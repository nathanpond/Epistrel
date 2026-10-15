"""Helpers for the offboarding tests (#117): a scratch database with the app over it, the world
an offboarded principal leaves behind, and a worker that runs the offboarding job.

Each test runs on its own scratch database (``offboarding_db``), because it counts rows and
runs the job worker: in the session database, the worker would also claim other tests' jobs.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.config import Settings
from epistrel.core.db import make_engine
from epistrel.engine import Engine
from epistrel.orchestrator.jobs.worker import Worker, WorkerConfig
from tests.auth_support import auth_settings, bearer
from tests.catalog_support import character as catalog_character
from tests.conformance.conftest import migrated_database
from tests.integration.ownership_fixtures import headers, ok, persona_body
from tests.media_support import files, metadata, png
from tests.model_support import config_key
from tests.profile_support import PROFILES, profile_body
from tests.scenario_support import scenario as catalog_scenario
from tests.story_support import story_body

OFFBOARDING = "/admin/offboarding"
READ = bearer()
COUNTED = (
    "stories",
    "events",
    "operations",
    "ownership_events",
    "entity_relations",
    "offboarding_jobs",
    "jobs",
    "personas",
    "catalog.character_catalog",
    "catalog.scenario_catalog",
    "media_assets",
    "engine_profiles",
)
"""The tables a request that changes nothing must leave as they were."""
EVERYTHING = {
    "stories": "delete",
    "characters": "transfer",
    "scenarios": "transfer",
    "personas": "delete",
    "media": "delete",
    "engine_profiles": "delete",
    "relations": "remove",
}
"""The detailed tests' actions: the test plan's (profiles deleted)."""


def unique(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


@dataclass
class Scratch:
    settings: Settings
    db: AsyncEngine
    client: httpx.AsyncClient
    lib: Engine


@asynccontextmanager
async def offboarding_db(pg_url: str) -> AsyncIterator[Scratch]:
    """A fresh migrated database (dropped afterwards), the REST app over it (no lifespan, so no
    worker runs until a test runs one) and the app's Engine."""
    with migrated_database(pg_url, f"epistrel_offb_{secrets.token_hex(4)}") as url:
        settings = auth_settings(url, model_adapter="stub", config_key=config_key())
        db = make_engine(settings)
        app = create_app(settings)
        lib: Engine = app.state.epistrel
        lib.bind(db)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield Scratch(settings, db, client, lib)
        finally:
            await lib.drainer.stop()
            await lib.models.aclose()
            await db.dispose()


def worker(db: AsyncEngine) -> Worker:
    """A worker that runs due jobs one at a time, without timers or LISTEN."""
    return Worker(
        db, WorkerConfig(poll_interval=0.2, lease_s=30.0, listen=False, concurrency=1, timers=False)
    )


async def run_jobs(db: AsyncEngine, *, runs: int = 50, fresh: Worker | None = None) -> int:
    """Run due jobs until none is due (at most ``runs``); how many ran."""
    found = fresh or worker(db)
    ran = 0
    while ran < runs and await found.run_one("offboarding-test:0"):
        ran += 1
    return ran


async def retry_now(db: AsyncEngine) -> None:
    """Make a job whose attempt failed due now, instead of after its backoff."""
    async with db.begin() as conn:
        await conn.execute(text("UPDATE jobs SET due_at = now() WHERE status = 'queued'"))


async def ownership_changes(db: AsyncEngine, job_id: str) -> dict[tuple[str, str], int]:
    """How many ``ownership_events`` the job wrote, per ``(entity id, kind)``."""
    found = await rows(
        db,
        "SELECT entity_id, kind, count(*) AS n FROM ownership_events WHERE job_id = :j "
        "GROUP BY entity_id, kind",
        j=job_id,
    )
    return {(str(row.entity_id), str(row.kind)): int(row.n) for row in found}


async def counts(db: AsyncEngine) -> dict[str, int]:
    async with db.connect() as conn:
        return {
            table: int(await conn.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608
            for table in COUNTED
        }


async def scalar(db: AsyncEngine, sql: str, **params: Any) -> Any:
    async with db.connect() as conn:
        return await conn.scalar(text(sql), params)


async def contents(db: AsyncEngine) -> dict[str, str | None]:
    """An md5 of every row of each ``COUNTED`` table (#338): a request that changes nothing
    leaves them equal, so a dry run that moved an owner, retired an image or soft-deleted a
    profile is caught even though no count moves."""
    async with db.connect() as conn:
        return {
            table: await conn.scalar(
                text(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {table} t")  # noqa: S608
            )
            for table in COUNTED
        }


async def rows(db: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with db.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def offboard(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    *,
    status: int,
    key: str | None = None,
    actor: str = "p-operator",
) -> tuple[dict[str, Any], httpx.Response]:
    response = await client.post(OFFBOARDING, json=body, headers=headers(actor, key))
    return await ok(response, status), response


async def report(client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    return await ok(await client.get(f"{OFFBOARDING}/{job_id}", headers=READ))


async def lookup(client: httpx.AsyncClient, principal: str, **params: Any) -> dict[str, Any]:
    return await ok(await client.get(f"/principals/{principal}", params=params, headers=READ))


@dataclass
class World:
    """What the offboarded principal ``a`` leaves behind, and who else is involved."""

    a: str
    curator: str
    player: str
    other: str
    shared_story: str = ""
    own_story: str = ""
    others_story: str = ""
    character: str = ""
    companion: str = ""
    scenario: str = ""
    persona: str = ""
    kept_image: str = ""
    """``a``'s image the character shows: it moves with the character."""
    shared_image: str = ""
    """``a``'s image another owner's character shows: retired, not purged."""
    loose_image: str = ""
    """``a``'s image nothing shows: retired."""
    profile: str = ""
    others_character: str = ""
    ids: dict[str, str] = field(default_factory=dict)


async def _image(client: httpx.AsyncClient, principal: str, nonce: str) -> str:
    body = metadata(caption=f"image {nonce}")
    response = await client.post(
        "/media", files=files(png(nonce=nonce), body), headers=headers(principal)
    )
    return str((await ok(response, 201))["id"])


async def world(client: httpx.AsyncClient, db: AsyncEngine) -> World:
    """``a`` owns two stories (one shared with ``player``), a character with its companion and
    its image, a scenario, a persona, three images (one the character shows, one another
    owner's character shows, one nothing shows) and a profile another owner's story uses.
    ``a`` is an ``editor`` of the other owner's character, and created a story the other owner
    owns (so its events name ``a``)."""
    found = World(unique("p-a"), unique("p-curator"), unique("p-player"), unique("p-other"))
    a, other = found.a, found.other
    shared = await ok(
        await client.post("/stories", json=story_body("Wren", "Ada"), headers=headers(a)), 201
    )
    found.shared_story = shared["story_id"]
    await ok(
        await client.post(
            f"/stories/{found.shared_story}/relations",
            json={"principal": found.player, "relation": "player"},
            headers=headers(a),
        ),
        201,
    )
    own = await ok(
        await client.post("/stories", json=story_body("Lark", "Moss"), headers=headers(a)), 201
    )
    found.own_story = own["story_id"]
    theirs = await ok(
        await client.post(
            "/stories", json=story_body("Pell", "Rook", owner=other), headers=headers(a)
        ),
        201,
    )
    found.others_story = theirs["story_id"]
    found.kept_image = await _image(client, a, f"kept {a}")
    found.shared_image = await _image(client, a, f"shared {a}")
    found.loose_image = await _image(client, a, f"loose {a}")
    created = await ok(
        await client.post(
            "/catalog/characters",
            json=catalog_character("Mara Venn", images={"primary": found.kept_image}),
            headers=headers(a),
        ),
        201,
    )
    found.character = created["id"]
    found.companion = created["companion_id"]
    scenario = await ok(
        await client.post(
            "/catalog/scenarios", json=catalog_scenario("Sunday Dinner"), headers=headers(a)
        ),
        201,
    )
    found.scenario = scenario["id"]
    persona = await ok(await client.post("/personas", json=persona_body(), headers=headers(a)), 201)
    found.persona = persona["id"]
    profile = await ok(await client.post(PROFILES, json=profile_body(), headers=headers(a)), 201)
    found.profile = profile["id"]
    others = await ok(
        await client.post(
            "/catalog/characters",
            json=catalog_character("Ivo Strand", images={"primary": found.shared_image}),
            headers=headers(other),
        ),
        201,
    )
    found.others_character = others["id"]
    await ok(
        await client.post(
            f"/catalog/characters/{found.others_character}/relations",
            json={"principal": a, "relation": "editor"},
            headers=headers(other),
        ),
        201,
    )
    # The other owner's story runs on a's profile (an assignment is #113's two-stage write;
    # the column is what the warning and the delete read).
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE stories SET engine_profile_id = :p WHERE id = :s"),
            {"p": found.profile, "s": found.others_story},
        )
    return found


def actions(**changes: str) -> dict[str, str]:
    return {**EVERYTHING, **changes}


__all__ = [
    "COUNTED",
    "EVERYTHING",
    "OFFBOARDING",
    "READ",
    "Scratch",
    "World",
    "actions",
    "contents",
    "counts",
    "lookup",
    "offboard",
    "offboarding_db",
    "ownership_changes",
    "report",
    "retry_now",
    "rows",
    "run_jobs",
    "scalar",
    "unique",
    "worker",
    "world",
]
