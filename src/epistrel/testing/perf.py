"""Seeding for the performance tests (P-03, P-04's Lite variant; NFR-PERF-3,4, #76).

The reference workload's Lite story (docs/03 NFR-PERF-4): a cast of one player and one other
character, the seeded **Teen** rating, ``turn_concurrency = queue``, 50 pinned memories of about
200 characters, and a transcript of 10,000 turns: 20,000 events, each turn a ``player_input``
(beat 1) and a ``message_rendered`` (beat 2) of about 300 characters. The story is created and
pinned through the library (``Engine``), and its turns are real ``turn`` operations committed in
bulk (``OperationBatch``, ``BATCH`` turns a transaction), so every projection is maintained as
in play. The texts are deterministic filler built from a fixed word list (``seed``-driven), so
two seeds with the same arguments write the same story.

``drain_summaries`` then brings the rolling summary up to date (#72) by running the story's
``lite_summary`` jobs in this process through the given Engine's Model Access Layer (a stub with
no latency, for seeding), so the measured turns send what a long-running story sends: the
summary and the verbatim window, not 10,000 rows.

P-03 (``tests/perf/test_p_03.py``) seeds one story with ``seed_story`` and no summaries;
``perf/lite_smoke/seed.py`` seeds the smoke's ten stories and drains their summaries.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core import clock, summary_port
from epistrel.core.context import RequestContext
from epistrel.core.enums import OperationKind
from epistrel.core.events import Operation, OperationBatch
from epistrel.core.seeds.ratings import TEEN_ID
from epistrel.core.stories import CharacterSpec, CreationSettings, StorySpec
from epistrel.core.story_texts import PinnedItemIn, PinnedMemoriesRequest
from epistrel.core.turns import stage_turn

TURNS = 10_000
"""Turns in a reference story: 20,000 events at two beats a turn (docs/03 NFR-PERF-4)."""
PINS = 50
MESSAGE_CHARS = 300
PIN_CHARS = 200
BATCH = 250
"""Turn operations committed in one ``OperationBatch`` transaction while seeding."""
TURN_QUEUE_MAX = 10
"""The reference stories' ``turn_queue_max``: the driver counts a 423 as a failure."""
PLAYER = "Wren"
OTHER = "Ada"
SEED_MODEL = "perf-seed"
SEED_VERSION = "1"
"""Bumped whenever the seeded story changes shape (the smoke's cached dump keys on it)."""

_WORDS = (  # noqa: SIM905 - a word list reads best as text
    "lantern road river bridge market tower harbor forest meadow stone ember quiet window "
    "letter coin rope wagon gate north south east west morning evening rain wind ash oak "
    "silver iron candle map ledger tavern keeper traveller guard merchant scholar ferry "
    "orchard well crossing cellar stair archive bell ribbon compass thread kettle shelf"
).split()


@dataclass(frozen=True, slots=True)
class SeededStory:
    """A seeded reference story: its id, the player's and the other character's ids."""

    story_id: str
    player_id: str
    other_id: str
    turns: int


_muted: list[Any] = []
"""The scheduler set aside while any ``summaries_muted`` block runs (and one entry per block)."""


@contextmanager
def summaries_muted() -> Iterator[None]:
    """No summary job is scheduled after each seeded turn while the block runs (the summary
    port's scheduler is unset, then restored when the last concurrent block ends)."""
    if not _muted:
        _muted.append(summary_port.scheduler())
        summary_port.set_scheduler(None)
    else:
        _muted.append(_muted[0])
    try:
        yield
    finally:
        saved = _muted.pop()
        if not _muted:
            summary_port.set_scheduler(saved)


def filler(rng: random.Random, chars: int) -> str:
    """About ``chars`` characters of plain sentences from the fixed word list."""
    words: list[str] = []
    length = 0
    while length < chars:
        word = rng.choice(_WORDS)
        words.append(word)
        length += len(word) + 1
    sentence = " ".join(words)[: chars - 1].rstrip()
    return sentence[0].upper() + sentence[1:] + "."


def spec(name: str) -> StorySpec:
    """The reference story's creation body: 1 + 1 cast, Teen, queued turns."""
    return StorySpec(
        name=name,
        characters=[
            CharacterSpec(
                name=PLAYER,
                definition="A traveller, the player's character.",
                true_age=24,
                is_player=True,
            ),
            CharacterSpec(name=OTHER, definition="An innkeeper who trusts no one.", true_age=41),
        ],
        content_rating_id=TEEN_ID,
        settings=CreationSettings(
            turn_concurrency="queue",
            turn_queue_max=TURN_QUEUE_MAX,
            default_output_viewpoint=PLAYER,
        ),
    )


def pins(seed: int) -> PinnedMemoriesRequest:
    rng = random.Random(f"pins:{seed}")  # noqa: S311 - filler text, not a secret
    return PinnedMemoriesRequest(
        items=[PinnedItemIn(text=filler(rng, PIN_CHARS)) for _ in range(PINS)]
    )


async def create_story(epistrel: Any, ctx: RequestContext, name: str, seed: int) -> SeededStory:
    """Create the reference story and its pins through the library (``Engine``)."""
    created = await epistrel.create_story(ctx, spec(name))
    ids = {c.name: c.id for c in created.characters}
    await epistrel.set_pinned_memories(ctx, created.story_id, pins(seed))
    return SeededStory(created.story_id, ids[PLAYER], ids[OTHER], 0)


async def add_turns(
    db: AsyncEngine,
    story: SeededStory,
    turns: int,
    *,
    principal: str,
    seed: int,
    batch: int = BATCH,
    progress: Callable[[int], None] | None = None,
) -> SeededStory:
    """Commit ``turns`` more turns to ``story`` as real ``turn`` operations, ``batch`` to a
    transaction. Run it under ``summaries_muted()`` (a ``drain_summaries`` follows)."""
    rng = random.Random(f"turns:{seed}:{story.turns}")  # noqa: S311 - filler text
    done = 0
    while done < turns:
        size = min(batch, turns - done)
        ops = [
            Operation(db, story_id=story.story_id, kind=OperationKind.TURN, principal=principal)
            for _ in range(size)
        ]
        group = OperationBatch(db, story.story_id, ops)
        await group.begin()
        for op in ops:
            say = filler(rng, MESSAGE_CHARS)
            stage_turn(
                op,
                actor_character_id=story.player_id,
                say=say,
                do=None,
                reply=filler(rng, MESSAGE_CHARS),
                render_character_ids=[story.other_id],
                model=SEED_MODEL,
                prompt_hash=hashlib.sha256(say.encode()).hexdigest(),
                finish_reason="stop",
            )
        await group.commit()
        done += size
        if progress is not None:
            progress(done)
    return SeededStory(story.story_id, story.player_id, story.other_id, story.turns + turns)


async def seed_story(
    epistrel: Any,
    ctx: RequestContext,
    name: str,
    *,
    turns: int = TURNS,
    seed: int = 0,
    progress: Callable[[int], None] | None = None,
) -> SeededStory:
    """A reference story with ``turns`` turns (no summary yet; no summary job is scheduled
    while it is seeded)."""
    story = await create_story(epistrel, ctx, name, seed)
    assert ctx.principal is not None  # noqa: S101 - create_story required one
    with summaries_muted():
        return await add_turns(
            epistrel.db, story, turns, principal=ctx.principal, seed=seed, progress=progress
        )


_CLAIM = text(
    "UPDATE jobs SET status = 'running', lease_until = :lease, attempt = attempt + 1, "
    "worker_id = 'w:perf-seed:0', updated_at = :now "
    "WHERE id = (SELECT id FROM jobs WHERE kind = 'lite_summary' AND story_id = :s "
    "AND status = 'queued' AND due_at <= :now ORDER BY due_at, id LIMIT 1 "
    "FOR UPDATE SKIP LOCKED) RETURNING *"
)


async def drain_summaries(epistrel: Any, story_id: str, *, max_runs: int = 10_000) -> int:
    """Run the story's ``lite_summary`` jobs in this process until it has no summary work;
    returns the runs made. The Engine's Model Access Layer answers (seed with the stub)."""
    from epistrel.orchestrator.jobs.handlers import lite_summary
    from epistrel.orchestrator.jobs.types import ClaimedJob
    from epistrel.orchestrator.jobs.worker import Worker, WorkerConfig

    db: AsyncEngine = epistrel.db
    lite_summary.bind(epistrel.models)
    worker = Worker(
        db,
        WorkerConfig(poll_interval=0.3, lease_s=300.0, listen=False, concurrency=1, timers=False),
    )
    runs = 0
    while runs < max_runs:
        await lite_summary.schedule(db, story_id)
        now = clock.now()
        async with db.begin() as conn:
            row = (
                await conn.execute(
                    _CLAIM, {"s": story_id, "now": now, "lease": now + timedelta(seconds=300)}
                )
            ).one_or_none()
        if row is None:
            return runs
        job = ClaimedJob(
            id=row.id,
            kind=row.kind,
            story_id=row.story_id,
            payload=dict(row.payload or {}),
            attempt=int(row.attempt),
            failures=int(row.failures),
            due_at=row.due_at,
            lease_until=row.lease_until,
            worker_id=row.worker_id,
            coalesce_key=row.coalesce_key,
        )
        await worker.execute(job)
        runs += 1
    raise RuntimeError(f"story {story_id} still has summary work after {max_runs} runs")


def percentile(samples: Sequence[float], q: float) -> float:
    """The nearest-rank ``q``-th percentile (``q`` in 0..100) of ``samples``."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * q // 100))
    return ordered[int(rank) - 1]


class Stopwatch:
    """Wall-clock seconds since construction (``time.perf_counter``)."""

    def __init__(self) -> None:
        self.started = time.perf_counter()

    def __call__(self) -> float:
        return time.perf_counter() - self.started


__all__ = [
    "BATCH",
    "MESSAGE_CHARS",
    "OTHER",
    "PINS",
    "PIN_CHARS",
    "PLAYER",
    "SEED_VERSION",
    "TURNS",
    "TURN_QUEUE_MAX",
    "SeededStory",
    "Stopwatch",
    "add_turns",
    "create_story",
    "drain_summaries",
    "filler",
    "percentile",
    "pins",
    "seed_story",
    "spec",
    "summaries_muted",
]
