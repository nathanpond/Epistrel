"""P-04's structured variant: seeding its stories and configuring its engine (#153).

Ten structured stories of 10,000 memories and 20,000 events each (``SPEC``: the scale fixture's
proportions at 10,000 memories, so 5,000 turns and 10,000 canon facts, 12 characters), built in
the perf database by ``scale.build_scale_stories`` with the stub's 768-dimension vectors. Each
story then gets ``turn_concurrency = queue`` (with the Lite reference stories' queue bound) and
``structured_load``'s planted canon, so the stub's claims have something to contradict. The
driver plays single-character turns: the player acts and the story's second character renders
(``StoryRef.other_id``).

The engine runs with ``compose.structured.yml`` over the Lite stack's files: the stub
embedding at the fixture's dimension and the stub's render claims (``RENDER_CLAIMS``: 10%
contradicted, 10% novel). ``cache_key`` names the seed's ``pg_dump``: the migration head and a
hash of the builder, this module and the spec (not the engine's SHA: the seed is data, and the
weekly run caches it between commits).
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import update

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.tables import stories as stories_table
from epistrel.engine import Engine
from epistrel.testing import perf, structured_load
from epistrel.testing.fixtures import scale
from perf.lite_smoke import (
    REPO_ROOT,
    STORIES,
    STRUCTURED_EVENTS,
    STRUCTURED_MEMORIES,
    Target,
)
from perf.lite_smoke.seed import StoryRef, seed_engine

SPEC = scale.ScaleSpec.scaled(STRUCTURED_MEMORIES, events=STRUCTURED_EVENTS)
"""One structured story: 10,000 memories, 20,000 events (5,000 turns, 10,000 facts)."""
COMPOSE_FILE = REPO_ROOT / "perf" / "lite_smoke" / "compose.structured.yml"
RENDER_CLAIMS = structured_load.render_claims(*structured_load.P04_SHARES)
"""The engine's ``EPISTREL_STUB_RENDER_CLAIMS`` (``compose.structured.yml`` holds it as JSON)."""


def seed_settings(dsn: str) -> Settings:
    """Library settings for seeding: the stub model and embedding (at the fixture's
    dimension), no latency."""
    return Settings.model_validate(
        {
            "database_url": dsn,
            "service_credentials": ["perf-seed-credential-000000000000000000"],
            "model_adapter": "stub",
            "stub_latency_s": 0.0,
            "stub_embedding_dimension": SPEC.dimension,
            "mode": "api",
        }
    )


async def seed(
    target: Target,
    *,
    stories: int = STORIES,
    spec: scale.ScaleSpec = SPEC,
    log: Callable[[str], None] = print,
) -> list[StoryRef]:
    """Build ``stories`` structured stories of ``spec``, queue their turns and plant their
    canon; return them."""
    settings = seed_settings(target.dsn)
    db = seed_engine(settings, stories)
    epistrel = Engine(settings, db=db)
    ctx = RequestContext(principal=target.principal)
    try:
        built = await scale.build_scale_stories(
            db, spec, stories, name="P-04 structured story", principal=target.principal, log=log
        )
        found: list[StoryRef] = []
        for index, fixture in enumerate(built):
            story = fixture.story
            async with db.begin() as conn:
                await conn.execute(
                    update(stories_table)
                    .where(stories_table.c.id == story.story_id)
                    .values(turn_concurrency="queue", turn_queue_max=perf.TURN_QUEUE_MAX)
                )
            await structured_load.plant(epistrel, ctx, story.story_id, story.characters.values())
            cast = list(story.characters.values())
            found.append(StoryRef(story.story_id, cast[0], cast[1], story.head, index))
        log(f"seeded {len(found)} structured stories")
        return found
    finally:
        await epistrel.models.aclose()
        await db.dispose()


def cache_key(stories: int, spec: scale.ScaleSpec = SPEC) -> str:
    """The cached seed's name: migration head + a hash of the builders and the spec."""
    digest = hashlib.sha256()
    for module in (scale, structured_load):
        digest.update(inspect.getsource(module).encode())
    digest.update(Path(__file__).read_bytes())
    digest.update(json.dumps({"spec": asdict(spec), "stories": stories}, sort_keys=True).encode())
    return f"{scale.alembic_head()}-{digest.hexdigest()[:12]}"


__all__ = ["COMPOSE_FILE", "RENDER_CLAIMS", "SPEC", "cache_key", "seed", "seed_settings"]
