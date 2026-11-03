"""Which turns of a story have each data-producing capability's data (FR-PROF-9; #228).

``capability_coverage`` holds, per story and capability (#106's module names, the ones
``schema.DATA_PRODUCING_MODULES`` lists), the turn intervals that have its data: ``live`` from
the turn the capability was on at creation or since (open-ended, ``to_turn`` null), and
``backfill`` over the ranges a backfill re-derived. The clock and States also record their
level (``prompt_only`` or ``on``).

``gaps`` is the single source of what a backfill must cover: the story's active turns (genesis,
turn 0, excluded) outside the capability's intervals, as maximal ``(from, to)`` runs. Adjacent
intervals are coalesced on write (``add``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.ids import new_ulid
from epistrel.core.profiles import schema
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.tables import capability_coverage, operations

LIVE = "live"
BACKFILL = "backfill"
LEVELLED = frozenset({"clock", "states", "pipeline"})
"""Capabilities whose coverage records their level (``prompt_only``/``on``; the pipeline's
``lite``/``structured``)."""

Span = tuple[int, int | None]


def capabilities_of(settings: ProfileSettings) -> dict[str, str | None]:
    """The data-producing capabilities ``settings`` has on, each with its level when it is one
    the coverage records (the clock, States), else ``None``."""
    return {
        module: (level if module in LEVELLED else None)
        for module, level in schema.enabled_modules(settings).items()
        if module in schema.DATA_PRODUCING_MODULES
    }


async def add(
    conn: AsyncConnection,
    story_id: str,
    capability: str,
    from_turn: int,
    to_turn: int | None,
    *,
    source: str,
    level: str | None = None,
    operation_id: str | None = None,
) -> None:
    """Record ``[from_turn, to_turn]`` (``None``: open-ended) as covered, coalescing the
    capability's intervals of the same source and level it touches or overlaps."""
    t = capability_coverage
    rows = list(
        await conn.execute(
            select(t.c.id, t.c.from_turn, t.c.to_turn).where(
                t.c.story_id == story_id,
                t.c.capability == capability,
                t.c.source == source,
                t.c.level.is_(None) if level is None else t.c.level == level,
            )
        )
    )
    low, high = from_turn, to_turn
    merged: list[str] = []
    for row in rows:
        r_low, r_high = int(row.from_turn), row.to_turn
        touches = (high is None or r_low <= high + 1) and (r_high is None or low <= r_high + 1)
        if touches:
            low = min(low, r_low)
            high = None if high is None or r_high is None else max(high, int(r_high))
            merged.append(str(row.id))
    if merged:
        await conn.execute(delete(t).where(t.c.id.in_(merged)))
    await conn.execute(
        t.insert().values(
            id=new_ulid(),
            story_id=story_id,
            capability=capability,
            from_turn=low,
            to_turn=high,
            level=level,
            source=source,
            operation_id=operation_id,
        )
    )


async def seed_live(
    conn: AsyncConnection, story_id: str, settings: ProfileSettings, *, from_turn: int = 1
) -> None:
    """A new story's (or a newly applied configuration's) ``live`` intervals: each capability
    ``settings`` has on, from ``from_turn``, open-ended."""
    for capability, level in capabilities_of(settings).items():
        await add(conn, story_id, capability, from_turn, None, source=LIVE, level=level)


async def seed_story(conn: AsyncConnection, story_id: str) -> None:
    """A new story's ``live`` intervals from turn 1: what its configuration has on now
    (story creation and genesis call it in their transaction)."""
    from epistrel.core.profiles import effective  # here: profiles imports this package

    current = await effective.effective_configuration(conn, story_id, validate=False)
    await seed_live(conn, story_id, current.settings)


async def extend_live(conn: AsyncConnection, story_id: str) -> None:
    """After a configuration change applies: each capability now on is ``live`` from the
    story's next turn (its earlier turns are a backfill's to cover, if any)."""
    from epistrel.core.profiles import effective

    current = await effective.effective_configuration(conn, story_id, validate=False)
    turns = await active_turns(conn, story_id)
    await close_lost(conn, story_id, current.settings, last_turn=turns[-1] if turns else 0)
    await seed_live(conn, story_id, current.settings, from_turn=(turns[-1] + 1) if turns else 1)


async def close_lost(
    conn: AsyncConnection, story_id: str, settings: ProfileSettings, *, last_turn: int
) -> None:
    """#231 (a data-producing decrease): each open ``live`` interval of a capability (or level)
    ``settings`` no longer has on ends at ``last_turn``, the last turn before the change; one
    that had not begun is removed. The data it covers is kept; a later re-enable's gap is then
    exactly the turns in between."""
    t = capability_coverage
    levels = capabilities_of(settings)
    rows = await conn.execute(
        select(t.c.id, t.c.capability, t.c.level, t.c.from_turn).where(
            t.c.story_id == story_id, t.c.source == LIVE, t.c.to_turn.is_(None)
        )
    )
    for row in list(rows):
        capability = str(row.capability)
        still = capability in levels and (
            capability not in LEVELLED or row.level is None or levels[capability] == row.level
        )
        if still:
            continue
        if int(row.from_turn) > last_turn:
            await conn.execute(delete(t).where(t.c.id == row.id))
        else:
            await conn.execute(update(t).where(t.c.id == row.id).values(to_turn=last_turn))


async def intervals(
    conn: AsyncConnection, story_id: str, capability: str, *, levels: Iterable[str] = ()
) -> list[Span]:
    """The capability's covered intervals (only those at one of ``levels`` when given)."""
    t = capability_coverage
    query = select(t.c.from_turn, t.c.to_turn, t.c.level).where(
        t.c.story_id == story_id, t.c.capability == capability
    )
    wanted = set(levels)
    return [
        (int(r.from_turn), None if r.to_turn is None else int(r.to_turn))
        for r in await conn.execute(query.order_by(t.c.from_turn))
        if not wanted or r.level in wanted
    ]


async def active_turns(conn: AsyncConnection, story_id: str) -> list[int]:
    """The story's turns on its active timeline (genesis excluded), in order."""
    o = operations.c
    found = await conn.scalars(
        select(o.narrative_turn)
        .where(
            o.story_id == story_id,
            o.kind == OperationKind.TURN.value,
            o.membership == Membership.ACTIVE.value,
            o.status == OperationStatus.COMMITTED.value,
            o.narrative_turn >= 1,
        )
        .distinct()
        .order_by(o.narrative_turn)
    )
    return [int(t) for t in found]


def runs(turns: Sequence[int]) -> list[tuple[int, int]]:
    """``turns`` (sorted) as maximal runs of consecutive turn numbers."""
    found: list[tuple[int, int]] = []
    for turn in turns:
        if found and turn == found[-1][1] + 1:
            found[-1] = (found[-1][0], turn)
        else:
            found.append((turn, turn))
    return found


def _covered(turn: int, spans: Sequence[Span]) -> bool:
    return any(low <= turn and (high is None or turn <= high) for low, high in spans)


async def gaps(
    conn: AsyncConnection, story_id: str, capability: str, *, level: str | None = None
) -> list[tuple[int, int]]:
    """The backfill range of ``capability`` (see the module docstring); for a levelled
    capability raised to ``on`` (``level``), turns covered only at ``prompt_only`` are kept as
    they are (the owner's decision 2 on #228: their recorded data stays, nothing is derived on
    top), so they are not gaps either."""
    if capability == "pipeline" and level is not None:
        spans = await intervals(conn, story_id, capability, levels=(level,))
    else:
        spans = await intervals(conn, story_id, capability)
    turns = await active_turns(conn, story_id)
    return runs([t for t in turns if not _covered(t, spans)])


async def gaps_of(
    conn: AsyncConnection, story_id: str, capabilities: Mapping[str, str | None]
) -> dict[str, list[tuple[int, int]]]:
    """``gaps`` of each of ``capabilities``, those with none left out."""
    found: dict[str, list[tuple[int, int]]] = {}
    for capability, level in capabilities.items():
        missing = await gaps(conn, story_id, capability, level=level)
        if missing:
            found[capability] = missing
    return found


__all__ = [
    "BACKFILL",
    "LEVELLED",
    "LIVE",
    "active_turns",
    "add",
    "capabilities_of",
    "close_lost",
    "extend_live",
    "gaps",
    "gaps_of",
    "intervals",
    "runs",
    "seed_live",
    "seed_story",
]
