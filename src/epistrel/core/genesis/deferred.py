"""A story's deferred genesis content, materialized when it gains the capability (FR-PROF-18,
FR-SCN-11; #231).

#122 parked what a story could not use at creation in ``genesis_deferred`` rows, one per item,
each naming the capability it waits for. When a configuration change gives the story that
capability, ``materialize_deferred`` writes them through the genesis writer #149 uses
(``core.genesis.structured.plan``/``write``, the partitions branch included), at narrative
position 0 with ``provenance = seed``, under the given operation:

- under ``upgrade_mode: backfill`` the backfill's own operation, at the start of its run: the
  rows stay dormant until the backfill completes, and seeds enter reconciliation as its earliest
  statements (a seed the re-derived history contradicts is a #230 conflict); a cancelled or
  failed backfill discards them and makes the items deferred again (``undo``);
- under ``forward_only`` (or a story with no history) the configuration change's operation,
  active at once.

Each item is materialized once (``materialized_at``, ``materialized_operation_id``). Items whose
capability is still off stay deferred. Only the ``structured``, ``episodic`` and ``partitions``
items are materialized here; the clock's and States' deferred items wait (#231's follow-up).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.genesis import epistemic, structured
from epistrel.core.genesis.materialize import PARTITIONS, STRUCTURED
from epistrel.core.profiles.capabilities import level_at_least, module_on
from epistrel.core.resolution.entities import character_spec
from epistrel.core.tables import characters, genesis_deferred, stories
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

EPISODIC = structured.EPISODIC
MATERIALIZED_HERE = (STRUCTURED, EPISODIC, PARTITIONS)
"""The deferred capabilities this module writes."""


def enabled(settings: Any) -> set[str]:
    """The deferred capabilities ``settings`` runs (of ``MATERIALIZED_HERE``)."""
    found: set[str] = set()
    if settings is None or not level_at_least(settings, "pipeline", "structured"):
        return found
    found.add(STRUCTURED)
    if module_on(settings, "episodic"):
        found.add(EPISODIC)
    if epistemic.partitions_on(settings):
        found.add(PARTITIONS)
    return found


async def pending(conn: AsyncConnection, story_id: str, settings: Any) -> dict[str, int]:
    """How many deferred items ``settings`` would materialize, by capability (an impact
    report's ``deferred_to_materialize``)."""
    wanted = enabled(settings)
    if not wanted:
        return {}
    g = genesis_deferred.c
    rows = await conn.execute(
        select(g.capability_needed, func.count())
        .where(
            g.story_id == story_id,
            g.materialized_at.is_(None),
            g.capability_needed.in_(sorted(wanted)),
        )
        .group_by(g.capability_needed)
    )
    return {str(c): int(n) for c, n in rows}


async def _cast(conn: AsyncConnection, story_id: str) -> list[structured.CastInput]:
    c = characters.c
    rows = await conn.execute(
        select(c.id, c.role_id, c.name, c.true_age, c.is_player, c.persona_snapshot, c.definition)
        .where(c.story_id == story_id)
        .order_by(c.ordinal)
    )
    return [
        structured.CastInput(
            character_id=str(r.id),
            role_id=str(r.role_id or r.id),
            name=str(r.name),
            true_age=int(r.true_age),
            spec=character_spec(
                str(r.id),
                str(r.name),
                persona=None if r.persona_snapshot is None else dict(r.persona_snapshot),
            ),
            background=structured.background_of(None, definition=r.definition),
            is_player=bool(r.is_player),
        )
        for r in rows
    ]


def _draft(snapshot: Mapping[str, Any] | None) -> ScenarioDraft | None:
    if not snapshot:
        return None
    try:
        return ScenarioDraft.model_validate(dict(snapshot))
    except ValueError:
        return None


async def materialize_deferred(
    conn: AsyncConnection,
    story_id: str,
    settings: Any,
    operation_id: str,
    *,
    principal: str,
) -> dict[str, Any]:
    """Materialize the story's deferred items ``settings`` enables (see the module docstring)
    under ``operation_id``; answers the report (``{counts, entries}``, ids and outcomes only)."""
    wanted = enabled(settings)
    if not wanted:
        return {"counts": {}, "entries": []}
    items = [
        i for i in await structured.deferred_items(conn, story_id) if i.capability_needed in wanted
    ]
    if not items:
        return {"counts": {}, "entries": []}
    g = genesis_deferred.c
    await conn.execute(  # never twice: hold the rows this materialization reads
        select(g.id).where(g.story_id == story_id, g.materialized_at.is_(None)).with_for_update()
    )
    story = (
        await conn.execute(
            select(
                stories.c.scenario_id, stories.c.scenario_version, stories.c.scenario_snapshot
            ).where(stories.c.id == story_id)
        )
    ).one()
    draft = _draft(story.scenario_snapshot)
    placements: dict[str, str] = {}
    groups: Mapping[str, Any] = {}
    opening: tuple[str | None, Any] = (None, ())
    if draft is not None:
        placements = {r.id: r.starting_location for r in draft.cast.roles if r.starting_location}
        start = draft.starting_state.opening
        if start.starting_location:
            placements.update(dict.fromkeys(start.roles_present, start.starting_location))
        groups = draft.cast.groups or {}
        opening = (start.narration, start.roles_present)
    found = structured.plan(
        str(story.scenario_id or ""),
        int(story.scenario_version or 0),
        story_id,
        items,
        await _cast(conn, story_id),
        placements=placements,
        episodic=EPISODIC in wanted,
        settings=settings,
        groups=groups,
        opening=opening,
    )
    operation = SimpleNamespace(
        operation_id=operation_id,
        story_id=story_id,
        principal=principal,
        narrative_turn=0,
        turn_variant=0,
        base_revision=None,
    )
    await structured.write(conn, story_id, operation, found)  # type: ignore[arg-type]
    refs = sorted({i.field_ref for i in items} & set(found.materialized))
    if refs:
        await conn.execute(
            update(genesis_deferred)
            .where(
                g.story_id == story_id,
                g.field_ref.in_(refs),
                g.capability_needed.in_(sorted(wanted)),
            )
            .values(
                materialized_at=func.coalesce(g.materialized_at, func.transaction_timestamp()),
                materialized_operation_id=operation_id,
            )
        )
    counts: dict[str, int] = {}
    for item in items:
        if item.field_ref in refs:
            counts[item.capability_needed] = counts.get(item.capability_needed, 0) + 1
    log.info("deferred genesis materialized", story_id=story_id, items=len(refs))
    return {
        "counts": counts,
        "entries": [
            {"field_ref": i.field_ref, "capability": i.capability_needed, "outcome": "written"}
            for i in items
            if i.field_ref in refs
        ],
        "conflicts": found.conflicts,
    }


async def on_configuration(conn: AsyncConnection, story_id: str, principal: str) -> None:
    """After a configuration change applied: materialize what the story's configuration now
    enables, under a committed control operation of the story (active at once)."""
    from epistrel.core import control
    from epistrel.core.profiles import effective

    current = await effective.effective_configuration(conn, story_id, validate=False)
    if not await pending(conn, story_id, current.settings):
        return
    operation_id = await control.record_control_operation(conn, actor=principal, story_id=story_id)
    await materialize_deferred(conn, story_id, current.settings, operation_id, principal=principal)


async def undo(conn: AsyncConnection, story_id: str, operation_id: str) -> None:
    """A cancelled or failed backfill's materialization: its items are deferred again (its
    rows are discarded with the backfill's operation)."""
    g = genesis_deferred.c
    await conn.execute(
        update(genesis_deferred)
        .where(g.story_id == story_id, g.materialized_operation_id == operation_id)
        .values(materialized_at=None, materialized_operation_id=None)
    )


__all__ = [
    "MATERIALIZED_HERE",
    "enabled",
    "materialize_deferred",
    "on_configuration",
    "pending",
    "undo",
]
