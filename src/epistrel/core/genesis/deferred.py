"""A story's deferred genesis content, materialized when it gains the capability (FR-PROF-18,
FR-SCN-11; #231).

#122 parked what a story could not use at creation in ``genesis_deferred`` rows, one per item,
each naming the capability it waits for. When a configuration change gives the story that
capability, ``materialize_deferred`` writes them through the genesis writers #149 and #202 use
(``core.genesis.structured.plan``/``write``/``write_states``, the partitions branch included),
at narrative position 0 with ``provenance = seed``, under the given operation:

- under ``upgrade_mode: backfill`` the backfill's own operation, at the start of its run: the
  rows stay dormant until the backfill completes, and seeds enter reconciliation as its earliest
  statements (a seed the re-derived history contradicts is a #230 conflict); a cancelled or
  failed backfill discards them and makes the items deferred again (``undo``);
- under ``forward_only`` (or a story with no history) the configuration change's operation,
  active at once.

Each item is materialized once (``materialized_at``, ``materialized_operation_id``). Items whose
capability is still off stay deferred.

**The clock and States (#698).** They are configuration the story must already run (the States
writer refuses a story with States off), so they materialize once the configuration that turns
them on applies: under ``forward_only`` with the rest, under ``backfill`` when the completion
applies the held configuration (``on_configuration``, a control operation of its own; the
backfill's start materializes only ``BACKFILL_START``). The starting clock's settings are
written through the clock's definition (#193's ``PATCH /clock`` members) and its anchor as the
clock's first anchor fact at position 0 (``commit_anchor``); a setting the story has changed
from its default, an anchor the story already has, and a State of a name the story already
declares are ``kept_existing``, never overwritten. Scheduled events are canon ``scheduled``
facts with their events (#196), as at creation.

**The report** (#698, #111's entries): ``{counts, by_outcome, conflicts, entries}``, each entry
a ``MaterializedEntry`` (its outcomes, and for a merge loser the winner, the loser and the
rule). It is omniscient (seed ids, private seeds' locations); ``card`` is what a read that
names no omniscient viewpoint gets: the counts only.

A second pass (a capability turned on after the structured content was materialized) does not
re-read the cast's definition prose: those facts were written by the first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.enums import Provenance
from epistrel.core.genesis import epistemic, structured
from epistrel.core.genesis.materialize import CLOCK, PARTITIONS, STATES, STRUCTURED
from epistrel.core.genesis.models import DeferredItem, MaterializedEntry
from epistrel.core.profiles.capabilities import level_at_least, module_on
from epistrel.core.resolution.entities import character_spec
from epistrel.core.tables import characters, genesis_deferred, stories
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

EPISODIC = structured.EPISODIC
MATERIALIZED_HERE = (STRUCTURED, EPISODIC, PARTITIONS, STATES, CLOCK)
"""The deferred capabilities this module writes."""
BACKFILL_START = (STRUCTURED, EPISODIC, PARTITIONS)
"""What a backfill materializes when its run begins (the clock and States wait for its
completion to apply the configuration; see the module docstring)."""


def enabled(settings: Any, only: Iterable[str] | None = None) -> set[str]:
    """The deferred capabilities ``settings`` runs (of ``MATERIALIZED_HERE``, or of ``only``)."""
    found: set[str] = set()
    if settings is None or not level_at_least(settings, "pipeline", "structured"):
        return found
    found.add(STRUCTURED)
    if module_on(settings, "episodic"):
        found.add(EPISODIC)
    if epistemic.partitions_on(settings):
        found.add(PARTITIONS)
    if level_at_least(settings, STATES, "on"):
        found.add(STATES)
    if level_at_least(settings, CLOCK, "on"):
        found.add(CLOCK)
    return found if only is None else found & set(only)


async def pending(
    conn: AsyncConnection, story_id: str, settings: Any, only: Iterable[str] | None = None
) -> dict[str, int]:
    """How many deferred items ``settings`` would materialize, by capability (an impact
    report's ``deferred_to_materialize``)."""
    wanted = enabled(settings, only)
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
    only: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Materialize the story's deferred items ``settings`` enables (of ``only``; see the module
    docstring) under ``operation_id``; answers the report (``{counts, by_outcome, conflicts,
    entries}``)."""
    wanted = enabled(settings, only)
    if not wanted:
        return _report({}, [], 0)
    items = [
        i for i in await structured.deferred_items(conn, story_id) if i.capability_needed in wanted
    ]
    if not items:
        return _report({}, [], 0)
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
    cast = await _cast(conn, story_id)
    if await _structured_done(conn, story_id):
        # A second pass: the cast's definition prose was read by the first (and the opening's
        # exposures written with it).
        cast = [replace(member, background=()) for member in cast]
        opening = (None, ())
    found = structured.plan(
        str(story.scenario_id or ""),
        int(story.scenario_version or 0),
        story_id,
        items,
        cast,
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
        provenance=Provenance.ENGINE,
        slot=False,
        slot_k=None,
    )
    kept: dict[str, str] = {}  # field_ref -> why it was kept
    if CLOCK in wanted:
        kept.update(await _clock(conn, story_id, items, found, operation, principal))
    await structured.write(conn, story_id, operation, found)  # type: ignore[arg-type]
    if STATES in wanted and (found.states or found.initial_values):
        kept.update(await _states(conn, story_id, found, operation))
    refs = sorted({i.field_ref for i in items} & (set(found.materialized) | set(found.seeded)))
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
    entries = [_kept(e, kept) for e in found.entries]
    log.info(
        "deferred genesis materialized",
        story_id=story_id,
        items=len(refs),
        kept_existing=len(kept),
        conflicts=found.conflicts,
    )
    return _report(counts, entries, found.conflicts)


def _report(counts: dict[str, int], entries: list[Any], conflicts: int) -> dict[str, Any]:
    return {
        "counts": counts,
        "by_outcome": structured.by_outcome(entries),
        "conflicts": conflicts,
        "entries": [e.model_dump(mode="json") for e in entries],
    }


def card(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A materialization report as a read naming no omniscient viewpoint gets it: the counts by
    capability and by outcome and the conflict count, no entry (see the module docstring)."""
    if not report:
        return None
    by_outcome = report.get("by_outcome")
    if by_outcome is None:  # a report written before #698
        by_outcome = {}
        for entry in report.get("entries") or ():
            for outcome in entry.get("outcomes") or [entry.get("outcome")]:
                if outcome:
                    by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
    return {
        "counts": dict(report.get("counts") or {}),
        "by_outcome": dict(by_outcome),
        "conflicts": int(report.get("conflicts") or 0),
    }


def _kept(entry: MaterializedEntry, kept: Mapping[str, str]) -> MaterializedEntry:
    """``entry`` with ``kept_existing`` in place of its ``clock`` or ``state`` outcome when the
    story kept its own (see the module docstring)."""
    if entry.field_ref not in kept:
        return entry
    outcomes = [o for o in entry.outcomes if o not in ("clock", "state")]
    return entry.model_copy(update={"outcomes": [*outcomes, "kept_existing"]})


async def _structured_done(conn: AsyncConnection, story_id: str) -> bool:
    """Whether an earlier pass materialized the story's structured items."""
    g = genesis_deferred.c
    found = await conn.scalar(
        select(g.id)
        .where(
            g.story_id == story_id,
            g.capability_needed == STRUCTURED,
            g.materialized_at.is_not(None),
        )
        .limit(1)
    )
    return found is not None


async def _clock(
    conn: AsyncConnection,
    story_id: str,
    items: Iterable[DeferredItem],
    found: structured.Plan,
    operation: Any,
    principal: str,
) -> dict[str, str]:
    """The starting clock's settings and anchor (see the module docstring); the clock item's
    ``field_ref`` when the story kept any of its own."""
    from epistrel.core import states
    from epistrel.core.catalog.scenario_models import ScenarioClock
    from epistrel.core.catalog.starting_state import anchor_statement
    from epistrel.core.enums import RecordProvenance
    from epistrel.core.states.model import (
        CLOCK_INTERPRETIVE_MEMBERS,
        CLOCK_PROSPECTIVE_MEMBERS,
        WORLD_CLOCK,
        ClockSettings,
    )
    from epistrel.core.world_clock import HEAD, anchor_at, clockwork, commit_anchor

    item = next((i for i in items if i.field_ref == structured.CLOCK_ITEM), None)
    if item is None or found.clock is None:
        return {}
    kept = False
    current = (await states.definition(conn, story_id, WORLD_CLOCK)).clock or ClockSettings()
    have, wanted, default = (
        current.document(),
        found.clock.document(),
        ClockSettings().document(),
    )
    patch: dict[str, Any] = {}
    for member in (*CLOCK_INTERPRETIVE_MEMBERS, *CLOCK_PROSPECTIVE_MEMBERS):
        if wanted[member] == default[member] or wanted[member] == have[member]:
            continue
        if have[member] != default[member]:
            kept = True  # the story changed it: its own value stays
            continue
        patch[member] = wanted[member]
    if patch:
        await states.update_definition(conn, story_id, WORLD_CLOCK, patch, principal=principal)
    raw = item.payload.get("value")
    scenario = ScenarioClock.model_validate(raw) if isinstance(raw, Mapping) else None
    if scenario is not None and scenario.anchor is not None:
        current = (await states.definition(conn, story_id, WORLD_CLOCK)).clock or ClockSettings()
        statement = anchor_statement(scenario.anchor, clockwork(current))
        if not statement.empty:
            anchored = current.anchor or (await anchor_at(conn, story_id, HEAD)).revision
            if anchored:
                kept = True
            else:
                await commit_anchor(
                    conn,
                    story_id,
                    statement,
                    operation=operation,
                    provenance=RecordProvenance.SEED,
                )
    return {item.field_ref: "clock"} if kept else {}


async def _states(
    conn: AsyncConnection, story_id: str, found: structured.Plan, operation: Any
) -> dict[str, str]:
    """The planned States and initial values (``structured.write_states``), but for a State the
    story already declares: it and its values are ``kept_existing``."""
    from epistrel.core.membership import active_only
    from epistrel.core.tables import state_definitions

    d = state_definitions.c
    taken = {
        str(name)
        for (name,) in await conn.execute(
            select(d.name).where(d.story_id == story_id, active_only(state_definitions))
        )
    }
    kept_states = [s for s in found.states if str(s.definition.get("name")) in taken]
    if not kept_states:
        await structured.write_states(conn, story_id, operation, found)
        return {}
    names = {str(s.definition.get("name")) for s in kept_states}
    trimmed = replace(
        found,
        states=[s for s in found.states if str(s.definition.get("name")) not in names],
        initial_values=[v for v in found.initial_values if v.state not in names],
    )
    await structured.write_states(conn, story_id, operation, trimmed)
    return {s.field_ref: "state" for s in kept_states}


async def on_configuration(conn: AsyncConnection, story_id: str, principal: str) -> None:
    """After a configuration change applied: materialize what the story's configuration now
    enables, under a committed control operation of the story (active at once)."""
    from epistrel.core import control
    from epistrel.core.profiles import effective

    current = await effective.effective_configuration(conn, story_id, validate=False)
    if not await pending(conn, story_id, current.settings):
        return
    operation_id = await control.record_control_operation(conn, actor=principal, story_id=story_id)
    found = await materialize_deferred(
        conn, story_id, current.settings, operation_id, principal=principal
    )
    await record_report(conn, operation_id, found)


async def record_report(conn: AsyncConnection, operation_id: str, report: dict[str, Any]) -> None:
    """Keep a materialization's report on the operation that wrote it (``turn_report``'s
    ``materialization``; ``GET /operations/{id}`` projects it, #698)."""
    from epistrel.core.tables import operations

    o = operations.c
    current = await conn.scalar(select(o.turn_report).where(o.operation_id == operation_id))
    await conn.execute(
        update(operations)
        .where(o.operation_id == operation_id)
        .values(turn_report={**dict(current or {}), "materialization": report})
    )


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
    "BACKFILL_START",
    "MATERIALIZED_HERE",
    "card",
    "enabled",
    "materialize_deferred",
    "on_configuration",
    "pending",
    "record_report",
    "undo",
]
