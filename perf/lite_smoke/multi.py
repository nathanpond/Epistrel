"""P-04's multi-character variant: seeding its stories and configuring its engine (#182).

The structured variant's ten stories (``structured.SPEC``: 10,000 memories, 20,000 events, 12
characters each, ``scale.build_scale_stories``), each then made a multi-character table:

- **the profile**: the seeded ``Structured`` profile's latest version (v2, #181: per-character
  partitions, lazy resolution, testimony ``model``, perception ``full``, per-character renders),
  ``turn_concurrency = queue`` with the Lite reference stories' queue bound, and
  ``structured_load``'s planted canon (every character owns the brass lantern);
- **knowledge** (``epistrel.testing.fixtures.knowledge``): per character 30% of its memories
  private, 200 beliefs and 50 unresolved exposures;
- **the room**: the player (the cast's first) and the two primaries (the next two) are placed
  in ``ROOM`` by author facts; every other character is placed in ``ELSEWHERE`` and given the
  fixed role ``silent``, so a turn renders exactly the two primaries (no escalation, no reactive
  narration). The primaries keep the default role: the request names them.

Each turn the player speaks and the two primaries render ``sequential``ly in request order
(``render_character_ids``), the viewpoint ``character:<player>``.

The engine runs with ``compose.multi.yml`` over the Lite stack's files: the structured stub
settings plus a third render claim (``RENDER_CLAIMS``): on the renders that declare neither the
contradicted nor the novel claim (10% each, as the structured variant), the ``say`` testifies
about the first primary ("<first> owns the brass lantern", supported by canon), so the second
render's testimony about the first is verified and adjudicated (testimony ``model``) and the
witnesses' exposures written; every stub render also declares one ``think`` beat
(``RenderedBeats.stub_example``). ``cache_key`` names the seed's ``pg_dump`` as the structured
variant's does, over this module and the knowledge builder as well.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import update

from epistrel.core.context import RequestContext
from epistrel.core.mutation import FactRequest
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.stories import ParticipationSpec
from epistrel.core.tables import stories as stories_table
from epistrel.engine import Engine
from epistrel.testing import perf, structured_load
from epistrel.testing.fixtures import knowledge, scale
from perf.lite_smoke import REPO_ROOT, STORIES, Target
from perf.lite_smoke.seed import StoryRef, seed_engine
from perf.lite_smoke.structured import SPEC, seed_settings

COMPOSE_FILE = REPO_ROOT / "perf" / "lite_smoke" / "compose.multi.yml"
ROOM = "the reading room"
ELSEWHERE = "the far yard"
LOCATED_AT = CORE_BY_NAME["located_at"].id
PRIMARIES = 2
"""The primaries each turn renders (the cast's second and third)."""
TESTIMONY_SHARE = 0.8
"""The renders (after the 10% contradicted and 10% novel) whose ``say`` testifies about the
first primary."""


def render_claims(first_primary: str) -> list[dict[str, Any]]:
    """The engine's ``EPISTREL_STUB_RENDER_CLAIMS`` (``compose.multi.yml`` holds it as JSON):
    the structured variant's mix, then testimony about ``first_primary`` (a cast name)."""
    return [
        *structured_load.render_claims(*structured_load.P04_SHARES),
        {
            "share": TESTIMONY_SHARE,
            "subject": first_primary,
            "relation": "owns",
            "object": structured_load.PLANTED_ITEM,
        },
    ]


CAST = scale.person_names(SPEC.characters)
RENDER_CLAIMS = render_claims(CAST[1])
"""Every story's cast has the same names (``scale.person_names``), so one claim mix fits all."""


def _all_time(subject: str, place: str) -> FactRequest:
    return FactRequest(
        proposition=Proposition(
            subject_id=subject,
            predicate_id=LOCATED_AT,
            object_id=place,
            valid_time=Interval.of(None, None),
        )
    )


async def prepare(
    epistrel: Engine, ctx: RequestContext, story: scale.ScaleStory, index: int
) -> StoryRef:
    """Make one built structured story a multi-character table (see the module docstring)."""
    story_id = story.story_id
    cast = [story.characters[n] for n in scale.person_names(len(story.characters))]
    player, primaries, others = cast[0], cast[1 : 1 + PRIMARIES], cast[1 + PRIMARIES :]
    async with epistrel.db.begin() as conn:
        await conn.execute(
            update(stories_table)
            .where(stories_table.c.id == story_id)
            .values(turn_concurrency="queue", turn_queue_max=perf.TURN_QUEUE_MAX)
        )
        room = await store.add_entity(conn, story_id, "place", ROOM)
        elsewhere = await store.add_entity(conn, story_id, "place", ELSEWHERE)
    await structured_load.use_structured(epistrel.db, story_id)
    await structured_load.plant(epistrel, ctx, story_id, cast)
    for who in (player, *primaries):
        await epistrel.commit_fact(ctx, story_id, _all_time(who, room))
    for who in others:
        await epistrel.commit_fact(ctx, story_id, _all_time(who, elsewhere))
        await epistrel.set_participation(
            ctx, story_id, who, ParticipationSpec(role="silent", fixed=True)
        )
    return StoryRef(story_id, player, primaries[0], story.head, index, tuple(primaries))


SMOKE_KNOWLEDGE = knowledge.KnowledgeSpec(beliefs=20, exposures=5)
"""PR CI's smoke run (``--smoke``): a tenth of the beliefs and exposures, so its seed stays
short."""


async def seed(
    target: Target,
    *,
    stories: int = STORIES,
    spec: scale.ScaleSpec = SPEC,
    log: Callable[[str], None] = print,
    known: knowledge.KnowledgeSpec = knowledge.KnowledgeSpec(),  # noqa: B008 - frozen
) -> list[StoryRef]:
    """Build ``stories`` structured stories of ``spec`` and make each a multi-character table
    with its knowledge (``known``); return them."""
    settings = seed_settings(target.dsn)
    db = seed_engine(settings, stories)
    epistrel = Engine(settings, db=db)
    ctx = RequestContext(principal=target.principal)
    try:
        built = await scale.build_scale_stories(
            db,
            spec,
            stories,
            name="P-04 multi-character story",
            principal=target.principal,
            log=log,
        )
        found: list[StoryRef] = []
        for index, fixture in enumerate(built):
            written = await knowledge.apply(db, spec, fixture.story, known)
            log(
                f"story {index + 1}: {written.beliefs} beliefs, {written.exposures} exposures, "
                f"{written.private_memories} private memories"
            )
            found.append(await prepare(epistrel, ctx, fixture.story, index))
        log(f"seeded {len(found)} multi-character stories")
        return found
    finally:
        await epistrel.models.aclose()
        await db.dispose()


def cache_key(
    stories: int,
    spec: scale.ScaleSpec = SPEC,
    known: knowledge.KnowledgeSpec = knowledge.KnowledgeSpec(),  # noqa: B008 - frozen
) -> str:
    """The cached seed's name: migration head + a hash of the builders, the spec and the
    knowledge spec."""
    digest = hashlib.sha256()
    for module in (scale, structured_load, knowledge):
        digest.update(inspect.getsource(module).encode())
    digest.update(Path(__file__).read_bytes())
    digest.update(
        json.dumps(
            {"spec": asdict(spec), "stories": stories, "knowledge": asdict(known)}, sort_keys=True
        ).encode()
    )
    return f"{scale.alembic_head()}-{digest.hexdigest()[:12]}"


__all__ = [
    "CAST",
    "COMPOSE_FILE",
    "ELSEWHERE",
    "PRIMARIES",
    "RENDER_CLAIMS",
    "ROOM",
    "SMOKE_KNOWLEDGE",
    "SPEC",
    "cache_key",
    "prepare",
    "render_claims",
    "seed",
]
