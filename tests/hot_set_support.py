"""Helpers for the hot-set tests (#147; FR-MEM-8, NFR-SCALE-2, FR-STORE-30).

The tests run on a database of their own (``embedding_support.embedding_db``): the vector
budget needs vectors, and the first real embedding pins the deployment's dimension (a schema
change the shared session database must never see). ``story`` makes #133's world (Cal the
player, Bobby, Ann) on the ``Structured (M4)`` profile with its ``memory.hot_set.*`` dials and
consolidation set by story overrides (#113); ``cast`` makes a story with more characters.

- ``narrate`` plays narration-only turns (the stub's turns: each commit writes one shared
  episodic memory, which counts toward neither budget until it has a vector);
- ``held`` plays one turn whose commit writes memories a character holds, through
  ``core.memory.records.write_consolidated`` (the writer #143's outputs use; every write runs
  ``MEMORY_HOOKS``, so the counters and the overflow check see them as the turn commits);
- ``fake_vectors`` gives memories a vector without the embed job, then recounts (the counters
  follow only the real stamp: ``core.embeddings.stamp``);
- ``run`` runs a story's due jobs of one kind through the worker's own ``execute``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import MemoryKind
from epistrel.core.events import Operation
from epistrel.core.ids import new_ulid
from epistrel.core.memory import records, retirement
from epistrel.core.stories import CharacterSpec, StorySpec, create_story_from
from tests import canon_support as c
from tests.embedding_support import run_jobs
from tests.event_support import rows
from tests.memory_support import turn, use_structured
from tests.profile_support import store_overrides

RETIREMENT = "hot_set_retirement"
PRINCIPAL = "p-test"
NARRATION = "The rain goes on."


def overrides(*, consolidation: bool = False, **hot_set: Any) -> dict[str, Any]:
    return {"memory": {"hot_set": hot_set, "consolidation": {"enabled": consolidation}}}


async def story(db: AsyncEngine, *, consolidation: bool = False, **hot_set: Any) -> c.World:
    """#133's world on ``Structured (M4)`` with these ``memory.hot_set`` dials."""
    w = await c.world(db)
    await use_structured(db, w.story_id)
    await store_overrides(db, w.story_id, overrides(consolidation=consolidation, **hot_set))
    return w


async def cast(db: AsyncEngine, names: Sequence[str], **hot_set: Any) -> tuple[str, list[str]]:
    """A structured story whose cast is a player and ``names``: its id and their ids."""
    spec = StorySpec(
        name="hot set",
        characters=[
            CharacterSpec(name="Player", definition="The player.", true_age=30, is_player=True),
            *(CharacterSpec(name=n, definition=f"{n}.", true_age=40) for n in names),
        ],
    )
    created = await create_story_from(db, spec, principal=PRINCIPAL)
    await use_structured(db, created.story_id)
    await store_overrides(db, created.story_id, overrides(**hot_set))
    ids = {ch.name: ch.id for ch in created.characters}
    return created.story_id, [ids[n] for n in names]


async def narrate(db: AsyncEngine, story_id: str, n: int = 1) -> int:
    """``n`` narration-only turns; answers the head turn after them."""
    head = 0
    for _ in range(n):
        op, _ = await turn(db, story_id, say=None, reply=NARRATION)
        head = int(op.narrative_turn or 0)
    return head


async def narrate_to(db: AsyncEngine, story_id: str, head: int) -> int:
    """Narration-only turns until the story's head turn is ``head``."""
    async with db.connect() as conn:
        now = await retirement.head_turn(conn, story_id)
    if head > now:
        await narrate(db, story_id, head - now)
    return head


async def burst(
    db: AsyncEngine,
    story_id: str,
    holders: Mapping[str, int],
    *,
    importance: float | Sequence[float | None] | None = 0.9,
    label: str = "memory",
    covering: Sequence[str] = (),
) -> tuple[int, list[str]]:
    """One turn whose commit writes, for each holder, that many semantic memories it holds
    (``importance`` each, or one per memory in writing order), and, with ``covering``, one
    shared output that covers those memories. Answers the turn and the held memories' ids (the
    covering output last)."""
    plan = [(holder, k) for holder, n in holders.items() for k in range(n)]
    scores = list(importance) if isinstance(importance, Sequence) else [importance] * len(plan)
    ids = [new_ulid() for _ in plan]
    cover = new_ulid() if covering else None

    async def write(conn: AsyncConnection, op: Operation) -> None:
        made = [
            {
                "id": memory_id,
                "kind": MemoryKind.SEMANTIC.value,
                "text": f"{label} {k} of {holder[-6:]}",
                "holder_character_id": holder,
                "character_id": None,
                "visibility": {"scope": "characters", "characters": [holder]},
                "importance": scores[n],
            }
            for n, (memory_id, (holder, k)) in enumerate(zip(ids, plan, strict=True))
        ]
        await records.write_consolidated(conn, op, made)
        if cover is not None:
            await records.write_consolidated(
                conn,
                op,
                [
                    {
                        "id": cover,
                        "kind": MemoryKind.SEMANTIC.value,
                        "text": f"{label}: a summary",
                        "visibility": {"scope": "objective"},
                        "importance": 0.9,
                    }
                ],
                covered=covering,
            )

    op, _ = await c.run(db, story_id, write, said=f"{label} turn")
    return int(op.narrative_turn or 0), [*ids, *([cover] if cover else [])]


async def held(
    db: AsyncEngine,
    story_id: str,
    holder: str,
    n: int,
    *,
    importance: float | Sequence[float | None] | None = 0.9,
    label: str = "memory",
    covering: Sequence[str] = (),
) -> tuple[int, list[str]]:
    """``burst`` for one holder."""
    return await burst(
        db, story_id, {holder: n}, importance=importance, label=label, covering=covering
    )


async def fake_vectors(
    db: AsyncEngine, story_id: str, ids: Iterable[str] | None = None, vector: str = "[1,0,0,0]"
) -> None:
    """Give memories a vector (all the story's hot ones without one, or ``ids``) outside the
    embed job, and recount the story's counters."""
    where = "embedding IS NULL AND retrieval_tier = 'hot'" if ids is None else "id = ANY(:ids)"
    async with db.begin() as conn:
        await conn.execute(
            text(
                f"UPDATE memories SET embedding = CAST(:v AS vector), "  # noqa: S608
                f"embedding_model_version = 'stub-embed@1' WHERE story_id = :s AND {where}"
            ),
            {"s": story_id, "v": vector, "ids": list(ids or ())},
        )
        await retirement.recount(conn, story_id)


async def episodes(db: AsyncEngine, story_id: str) -> list[retirement.Episode]:
    async with db.connect() as conn:
        return await retirement.episodes(conn, story_id)


async def counts(db: AsyncEngine, story_id: str) -> retirement.HotCounts:
    """The story's counters, recounted from its rows (nothing stored)."""
    async with db.connect() as conn:
        return await retirement.exact_counts(conn, story_id)


async def stored_counts(db: AsyncEngine, story_id: str) -> dict[str, Any] | None:
    found = await rows(db, "SELECT hot_counts FROM stories WHERE id = :s", s=story_id)
    return None if found[0].hot_counts is None else dict(found[0].hot_counts)


async def memory_rows(db: AsyncEngine, story_id: str) -> dict[str, Any]:
    """The story's memories by id: tier, reason, retired turn, pin, membership, holder, text and
    whether each has a vector (``vector``: its text form)."""
    found = await rows(
        db,
        "SELECT id, text, retrieval_tier, retired_reason, retired_turn, pinned, membership, "
        "holder_character_id AS holder, embedding::text AS vector, embedding_model_version "
        "AS version, importance FROM memories WHERE story_id = :s ORDER BY created_at, id",
        s=story_id,
    )
    return {str(r.id): r for r in found}


async def run(db: AsyncEngine, kind: str, story_id: str, *, limit: int = 10) -> list[Any]:
    return await run_jobs(db, kind, story_id, limit=limit)


async def queued(db: AsyncEngine, kind: str, story_id: str) -> list[Any]:
    return await rows(
        db,
        "SELECT id, payload, coalesce_key, due_at FROM jobs WHERE kind = :k AND story_id = :s "
        "AND status = 'queued' ORDER BY created_at, id",
        k=kind,
        s=story_id,
    )


async def pin_rows(db: AsyncEngine, ids: Sequence[str], principal: str = PRINCIPAL) -> None:
    """Mark memories pinned outside the pin call (bulk test setup), and recount."""
    async with db.begin() as conn:
        story_id = await conn.scalar(
            text("SELECT story_id FROM memories WHERE id = :i"), {"i": ids[0]}
        )
        await conn.execute(
            text("UPDATE memories SET pinned = true, pinned_by = :p WHERE id = ANY(:ids)"),
            {"ids": list(ids), "p": principal},
        )
        await retirement.recount(conn, str(story_id))


__all__ = [
    "NARRATION",
    "PRINCIPAL",
    "RETIREMENT",
    "burst",
    "cast",
    "counts",
    "episodes",
    "fake_vectors",
    "held",
    "memory_rows",
    "narrate",
    "narrate_to",
    "overrides",
    "pin_rows",
    "queued",
    "run",
    "stored_counts",
    "story",
]
