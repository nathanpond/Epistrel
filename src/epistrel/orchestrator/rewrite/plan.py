"""Planning a rewrite: the blast radius and the preview (FR-REWRITE-2,4,5, FR-DIRECT-3; #223).

``PLANNERS`` maps a story's pipeline to its ``Planner``: ``structured`` (#223) and, since
#245, ``lite``, the same planner over a Lite story's record changes, whose reach a model pass
found first (``orchestrator.rewrite.lite``; ``session`` dispatches).

**The blast radius** of each normalized change:

- a **proposition** change: #220's ``dependents()`` (depth 3) of the key's current fact
  versions, the proposition key ``(subject, predicate)`` and the object it supersedes (its
  ``mentions``);
- a **record** change: the rows derived from the corrected event (its memories, utterances and
  exposures), each a ``fix``;
- a **State** change: ``dependents(state_ids=…)`` and the key's later ``state_changes``
  (``cascaded``, ``re_evaluate``).

Each dependent is classified by ``cascade.CASCADE_RULES``; a cascaded text record (an event or
a memory a ``presupposes`` or ``derives`` edge ties to the change) **escalates** to an
irreducible conflict when the engine cannot repair it by substituting the new value for the
old surface form: it was authored by the player or a consumer (the engine never rewrites
player text silently), or its text does not name the old value at all. A dependent amendment
part tied that way is a conflict too (options add ``cascade_undo``). Each conflict carries
``severity``: ``high`` when resolving it would change a delivered message, a memory a player
character holds or a State the player sees; ``low`` otherwise. One entry per record: the
strongest classification wins (conflict, then cascaded, then incidental).

**Determinism** (FR-REWRITE-5's detection half): ``plan_hash`` is the sha256 of the canonical
JSON of the normalized changes and the buckets' record references and actions, with no ids of
the session, no times and no modes; the same instruction under any ``resolution_mode`` and
``preview`` yields the same hash. ``read_set`` is what #237 rechecks: the change keys, the
dependents and the registry revision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import any_, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import edges
from epistrel.core.enums import DependencyEdgeType
from epistrel.core.membership import active_only
from epistrel.core.tables import (
    amendment_parts,
    characters,
    entities,
    entity_aliases,
    events,
    exposures,
    memories,
    state_changes,
    state_definitions,
    utterances,
)
from epistrel.orchestrator.rewrite import hypothetical, revalidate
from epistrel.orchestrator.rewrite.cascade import RE_RUN, Classification, PlanContext, classify
from epistrel.orchestrator.rewrite.parse import Normalized, Parsed, Story

EXCERPT_MAX = 160
STRENGTH = {"incidental": 0, "relabel": 0, "cascaded": 1, "irreducible": 2}
CONFLICT_OPTIONS = ("rewrite", "drop", "keep")
PLAYER_PROVENANCES = frozenset({"player", "consumer"})
TEXT_EDGES = frozenset({DependencyEdgeType.PRESUPPOSES})
"""A cascaded text record these edges tie to the change is repaired by substitution, or is a
conflict (a ``derives`` memory is regenerated instead, #225)."""


@dataclass
class Plan:
    """A computed plan: what the preview shows and what is stored on the session."""

    base_revision: int
    changes: list[dict[str, Any]]
    incidental: list[dict[str, Any]] = field(default_factory=list)
    cascaded: list[dict[str, Any]] = field(default_factory=list)
    irreducible: list[dict[str, Any]] = field(default_factory=list)
    relabels: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    new_entities: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    read_set: dict[str, Any] = field(default_factory=dict)
    timing_uncertain: list[dict[str, Any]] = field(default_factory=list)
    """#240: changes placed by an absolute time the clock could not decide (included,
    flagged)."""

    @property
    def plan_hash(self) -> str:
        return plan_hash(self.changes, self.buckets())

    def buckets(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "incidental": self.incidental,
            "cascaded": self.cascaded,
            "irreducible": self.irreducible,
            "relabels": self.relabels,
        }

    def preview(self) -> dict[str, Any]:
        return {
            "base_revision": self.base_revision,
            "plan_hash": self.plan_hash,
            "changes": self.changes,
            "blast_radius": {
                "incidental": self.incidental,
                "cascaded": self.cascaded,
                "irreducible": self.irreducible,
            },
            "blast_radius_truncated": self.truncated,
            "new_entities": self.new_entities,
            "relabels": self.relabels,
            "refused_changes": self.refused,
            "notifications": [],
            "timing_uncertain": self.timing_uncertain,
            "suggested_ports": [],
        }


def plan_hash(changes: list[dict[str, Any]], buckets: Mapping[str, list[dict[str, Any]]]) -> str:
    """The sha256 of the canonical JSON of the changes and the buckets' record references and
    actions (no ids of the session, no times, no modes)."""
    material = {
        "changes": changes,
        "buckets": {
            name: sorted(
                (
                    entry["record"]["table"],
                    entry["record"]["id"],
                    entry.get("action"),
                    entry.get("change_key"),
                )
                for entry in entries
            )
            for name, entries in sorted(buckets.items())
        },
    }
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class Planner(Protocol):
    async def plan(
        self, conn: AsyncConnection, story: Story, parsed: Parsed, *, base_revision: int
    ) -> Plan: ...


PLANNERS: dict[str, Planner] = {}
"""The planner of each pipeline (``structured`` here; #245 registers ``lite``)."""


def register_planner(pipeline: str, planner: Planner) -> None:
    PLANNERS[pipeline] = planner


# -- the structured planner ---------------------------------------------------------------------


@dataclass
class _Record:
    table: str
    id: str
    text: str | None = None
    provenance: str | None = None
    narrated_by: str | None = None
    holder: str | None = None
    kind: str | None = None


async def _changed_facts(conn: AsyncConnection, story_id: str, change: Normalized) -> set[str]:
    """The current facts on a proposition change's key (what the change rewrites)."""
    from epistrel.core.canon import facts as canon

    s = change.summary
    if change.kind != "proposition" or str(s.get("subject_id", "")).startswith("new:"):
        return set()
    if str(s.get("predicate_id", "")).startswith("register:"):
        return set()
    current = await canon.canon(
        conn, story_id, subject=s["subject_id"], predicate=s["predicate_id"]
    )
    return {f.id for f in current}


async def _settle(
    conn: AsyncConnection,
    story_id: str,
    change: Normalized,
    classified: Classification,
    table: str,
    record_id: str,
    changed: set[str],
) -> Classification:
    """#707: #140's check under the change's hypothetical canon settles a ``causes``
    decision (``keep`` when another premise still supports it) and an inferred belief
    (``cascade`` when its premises support the new value, else ``keep``)."""
    if classified.action == RE_RUN:
        if change.kind == "proposition" and await hypothetical.inference_follows(
            conn, story_id, record_id, change.summary, changed
        ):
            return Classification(classified.bucket, "cascade")
        return Classification(classified.bucket, "keep")
    if (
        classified.action == "re_evaluate"
        and table == "events"
        and change.kind == "proposition"
        and await hypothetical.decision_supported(
            conn, story_id, record_id, change.summary, changed
        )
    ):
        return Classification(classified.bucket, "keep")
    return classified


async def _records(
    conn: AsyncConnection, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], _Record]:
    """The text and authorship of the dependents that need them (events, memories, parts)."""
    found: dict[tuple[str, str], _Record] = {}
    ids = {table: sorted(i for t, i in keys if t == table) for table, _ in keys}
    if ids.get("events"):
        e = events.c
        for row in await conn.execute(
            select(e.id, e.type, e.payload, e.provenance).where(e.id.in_(ids["events"]))
        ):
            payload = dict(row.payload or {})
            text = payload.get("text")
            found[("events", str(row.id))] = _Record(
                "events",
                str(row.id),
                text if isinstance(text, str) else None,
                str(getattr(row.provenance, "value", row.provenance)),
                payload.get("narrated_by"),
                kind=str(row.type),
            )
    if ids.get("memories"):
        m = memories.c
        for row in await conn.execute(
            select(m.id, m.text, m.provenance, m.holder_character_id).where(
                m.id.in_(ids["memories"])
            )
        ):
            found[("memories", str(row.id))] = _Record(
                "memories",
                str(row.id),
                row.text,
                str(getattr(row.provenance, "value", row.provenance)),
                holder=None if row.holder_character_id is None else str(row.holder_character_id),
            )
    if ids.get("amendment_parts"):
        p = amendment_parts.c
        for part in await conn.execute(
            select(p.id, p.kind).where(p.id.in_(ids["amendment_parts"]), p.status == "active")
        ):
            found[("amendment_parts", str(part.id))] = _Record(
                "amendment_parts", str(part.id), kind=str(part.kind)
            )
    return found


async def _surface_forms(conn: AsyncConnection, entity_id: str | None) -> tuple[str, ...]:
    """The names an entity is written with (its canonical name and aliases)."""
    if entity_id is None or entity_id.startswith("new:"):
        return ()
    name = await conn.scalar(select(entities.c.canonical_name).where(entities.c.id == entity_id))
    aliases = list(
        await conn.scalars(
            select(entity_aliases.c.alias).where(entity_aliases.c.entity_id == entity_id)
        )
    )
    return tuple(str(n) for n in [name, *aliases] if n)


async def _players(conn: AsyncConnection, story_id: str) -> set[str]:
    c = characters.c
    return {
        str(x)
        for x in await conn.scalars(
            select(c.id).where(c.story_id == story_id, c.is_player.is_(True))
        )
    }


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 1] + "…"


class _Builder:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

    def add(self, bucket: str, entry: dict[str, Any]) -> None:
        record = (entry["record"]["table"], entry["record"]["id"])
        current = self.entries.get(record)
        if current is not None:
            if STRENGTH[bucket] < STRENGTH[current[0]]:
                current[1].setdefault("edges", []).extend(entry.get("edges", []))
                return
            if STRENGTH[bucket] == STRENGTH[current[0]]:
                current[1].setdefault("edges", []).extend(entry.get("edges", []))
                return
            entry.setdefault("edges", []).extend(current[1].get("edges", []))
        self.entries[record] = (bucket, entry)

    def into(self, plan: Plan) -> None:
        for bucket, entry in sorted(
            self.entries.values(),
            key=lambda item: (item[1]["record"]["table"], item[1]["record"]["id"]),
        ):
            entry["edges"] = sorted(set(entry.get("edges", [])))
            if bucket == "relabel":
                plan.relabels.append(entry)
            else:
                getattr(plan, bucket).append(entry)


class StructuredPlanner:
    """The structured pipeline's planner (see the module docstring)."""

    async def plan(
        self, conn: AsyncConnection, story: Story, parsed: Parsed, *, base_revision: int
    ) -> Plan:
        plan = Plan(
            base_revision=base_revision,
            changes=[c.summary for c in parsed.changes],
            refused=list(parsed.refused),
            new_entities=list(parsed.new_entities),
            timing_uncertain=list(parsed.timing_uncertain),
        )
        max_results, max_depth = await edges.search_bounds(conn)
        players = await _players(conn, story.story_id)
        built = _Builder()
        read_dependents: list[dict[str, Any]] = []
        for change in parsed.changes:
            if change.kind == "proposition":
                found = await self._proposition(conn, story, change, max_results, max_depth)
            elif change.kind == "state":
                found = await self._state(conn, story, change, max_results, max_depth)
            else:
                found = None
            if found is not None:
                plan.truncated = plan.truncated or found.truncated
                await self._classify(conn, story, change, found.items, players, built)
                read_dependents.extend(
                    {
                        "table": d.dependent[0],
                        "id": d.dependent[1],
                        "edge_type": d.edge_type.value,
                    }
                    for d in found.items
                )
            if change.kind == "record":
                await self._derived_rows(conn, change, built)
            if change.kind == "state":
                await self._later_changes(conn, story, change, players, built)
        built.into(plan)
        plan.read_set = {
            "keys": sorted(c.key for c in parsed.changes),
            "dependents": sorted(
                read_dependents, key=lambda d: (d["table"], d["id"], d["edge_type"])
            ),
            "records": [
                {"table": c.summary["record"]["table"], "id": c.summary["record"]["id"]}
                for c in parsed.changes
                if c.kind == "record"
            ],
            "entity_ids": sorted(
                {
                    str(v)
                    for c in parsed.changes
                    for k, v in c.summary.items()
                    if k in ("subject_id", "object_id", "superseded_object_id") and v
                }
            ),
            "config_revisions": {"registry": story.registry.config_revision},
            "anchor_revision": None,
        }
        # #237: what the apply's revalidation compares against, taken with the plan.
        plan.read_set.update(
            await revalidate.snapshot(conn, story.story_id, plan.read_set, plan.changes)
        )
        return plan

    async def _proposition(
        self,
        conn: AsyncConnection,
        story: Story,
        change: Normalized,
        max_results: int,
        max_depth: int,
    ) -> edges.DependentResult | None:
        from epistrel.core.canon import facts as canon

        s = change.summary
        if str(s["subject_id"]).startswith("new:") or str(s["predicate_id"]).startswith(
            "register:"
        ):
            return None
        current = await canon.canon(
            conn, story.story_id, subject=s["subject_id"], predicate=s["predicate_id"]
        )
        targets = edges.Targets(
            fact_ids=tuple(f.id for f in current),
            keys=((s["subject_id"], s["predicate_id"]),),
            entity_ids=(change.superseded_object,) if change.superseded_object else (),
        )
        return await edges.dependents(
            conn,
            story.story_id,
            targets,
            depth=min(3, max_depth),
            max_results=max_results,
            max_depth=max_depth,
        )

    async def _state(
        self,
        conn: AsyncConnection,
        story: Story,
        change: Normalized,
        max_results: int,
        max_depth: int,
    ) -> edges.DependentResult:
        return await edges.dependents(
            conn,
            story.story_id,
            edges.Targets(state_ids=(str(change.summary["state_id"]),)),
            depth=1,
            max_results=max_results,
            max_depth=max_depth,
        )

    async def _classify(
        self,
        conn: AsyncConnection,
        story: Story,
        change: Normalized,
        dependents: list[Any],
        players: set[str],
        built: _Builder,
    ) -> None:
        ctx = PlanContext(change.summary)
        records = await _records(conn, {d.dependent for d in dependents})
        old_forms = await _surface_forms(conn, change.superseded_object)
        changed = await _changed_facts(conn, story.story_id, change)
        for dependent in dependents:
            table, record_id = dependent.dependent
            classified = classify(ctx, dependent)
            classified = await _settle(
                conn, story.story_id, change, classified, table, record_id, changed
            )
            entry: dict[str, Any] = {
                "record": {"table": table, "id": record_id},
                "edge_type": dependent.edge_type.value,
                "edges": [dependent.edge_id],
                "change_key": change.key,
                "target": {"kind": dependent.target[0], "id": dependent.target[1]},
                "action": classified.action,
                "position": None if dependent.position is None else list(dependent.position.key()),
            }
            found = records.get((table, record_id))
            if found is not None and found.text is not None:
                entry["excerpt"] = _excerpt(found.text)
            reason = None
            if table == "amendment_parts":
                if found is None:
                    continue  # a reverted part depends on nothing any more
                # FR-REWRITE-11 (#238): a later amendment relying on what changes is asked
                # about whatever its edge type (``cascade_undo`` recommended).
                reason = "dependent_amendment"
            elif (
                change.kind == "state"
                and dependent.edge_type in TEXT_EDGES
                and table == "events"
                and found is not None
            ):
                # #697 (FR-DIRECT-3): a line that presupposed the State's value (a #191 owned
                # reading) cannot be fixed by substituting a name: it is a conflict.
                reason = self._irreducible(found, old_forms) or "state_presupposed"
            elif dependent.edge_type in TEXT_EDGES and found is not None:
                reason = self._irreducible(found, old_forms)
            elif dependent.edge_type is DependencyEdgeType.CAUSES and table == "events":
                # #225: a decision whose beat committed effects is never regenerated; a player's
                # own decision is never rewritten silently.
                if await _decision_effects(conn, record_id):
                    reason = "decision_effects"
                elif found is not None and (
                    found.provenance in PLAYER_PROVENANCES or found.narrated_by == "player"
                ):
                    reason = "player_authored"
            if reason is not None:
                severity = "high" if found is None else self._severity(found, players)
                options = list(CONFLICT_OPTIONS)
                if table == "amendment_parts":
                    options.append("cascade_undo")
                entry.update(
                    {
                        "conflict_id": _conflict_id(change.key, table, record_id),
                        "reason": reason,
                        "severity": severity,
                        "options": options,
                        "before_excerpt": entry.get("excerpt"),
                    }
                )
                built.add("irreducible", entry)
                continue
            built.add(classified.bucket, entry)

    @staticmethod
    def _irreducible(record: _Record, old_forms: tuple[str, ...]) -> str | None:
        if record.table == "amendment_parts":
            return "dependent_amendment"
        if record.text is None:
            return None
        if record.provenance in PLAYER_PROVENANCES or record.narrated_by == "player":
            return "player_authored"
        text = record.text.casefold()
        if old_forms and not any(form.casefold() in text for form in old_forms):
            return "not_substitutable"
        return None

    @staticmethod
    def _severity(record: _Record, players: set[str]) -> str:
        if record.table == "events":
            return "high"  # a delivered message's text
        if record.table == "memories" and record.holder in players:
            return "high"
        return "low"

    async def _derived_rows(
        self, conn: AsyncConnection, change: Normalized, built: _Builder
    ) -> None:
        """The rows derived from a corrected event (per-perceiver memories, utterances,
        exposures), each a ``fix``."""
        event_id = str(change.summary["record"]["id"])
        found: list[tuple[str, str]] = []
        m = memories.c
        found += [
            ("memories", str(x))
            for x in await conn.scalars(
                select(m.id).where(
                    literal(event_id) == any_(m.source_event_ids), active_only(memories)
                )
            )
        ]
        u = utterances.c
        found += [
            ("utterances", str(x))
            for x in await conn.scalars(select(u.id).where(u.event_id == event_id))
        ]
        x = exposures.c
        found += [
            ("exposures", str(i))
            for i in await conn.scalars(select(x.id).where(x.source_event_id == event_id))
        ]
        for table, record_id in found:
            built.add(
                "cascaded",
                {
                    "record": {"table": table, "id": record_id},
                    "edge_type": None,
                    "edges": [],
                    "change_key": change.key,
                    "target": {"kind": "record", "id": event_id},
                    "action": "fix",
                    "position": None,
                },
            )

    async def _later_changes(
        self,
        conn: AsyncConnection,
        story: Story,
        change: Normalized,
        players: set[str],
        built: _Builder,
    ) -> None:
        """A State key's changes after the part's valid start: cascaded, ``re_evaluate``."""
        s = change.summary
        c = state_changes.c
        where = [
            c.story_id == story.story_id,
            c.state_id == s["state_id"],
            active_only(state_changes),
            c.amendment_part_id.is_(None),
        ]
        where.append(
            c.scope_key.is_(None) if s["scope_key"] is None else c.scope_key == s["scope_key"]
        )
        if s["valid_from_minutes"] is not None:
            where.append(c.valid_time_minutes >= s["valid_from_minutes"])
        visible = await conn.scalar(
            select(state_definitions.c.visibility).where(state_definitions.c.id == s["state_id"])
        )
        for row in await conn.execute(select(c.id, c.narrative_turn).where(*where).order_by(c.id)):
            built.add(
                "cascaded",
                {
                    "record": {"table": "state_changes", "id": str(row.id)},
                    "edge_type": None,
                    "edges": [],
                    "change_key": change.key,
                    "target": {"kind": "state", "id": s["state_id"]},
                    "action": "re_evaluate",
                    "position": None,
                    "severity": "high" if str(visible) == "player_visible" else "low",
                },
            )


async def _decision_effects(conn: AsyncConnection, event_id: str) -> bool:
    """Whether the beat of event ``event_id`` committed State changes or an outbox row (#187,
    #210): such a decision is kept, never regenerated (#225's ``decision_effects_kept``)."""
    from epistrel.core.tables import external_effect_outbox

    row = (
        await conn.execute(
            select(events.c.operation_id, events.c.beat).where(events.c.id == event_id)
        )
    ).one_or_none()
    if row is None:
        return False
    c, o = state_changes.c, external_effect_outbox.c
    changed = await conn.scalar(
        select(c.id)
        .where(
            c.operation_id == row.operation_id,
            c.beat == (row.beat or 0),
            c.amendment_part_id.is_(None),
        )
        .limit(1)
    )
    sent = await conn.scalar(
        select(o.id).where(o.operation_id == row.operation_id, o.beat == (row.beat or 0)).limit(1)
    )
    return changed is not None or sent is not None


def _conflict_id(change_key: str, table: str, record_id: str) -> str:
    raw = f"{change_key}|{table}|{record_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:26]


register_planner("structured", StructuredPlanner())
# #245 (FR-LITE-8): a Lite story plans with the same planner over record changes only (its
# reach is model-searched before the plan, ``orchestrator.rewrite.lite``).
register_planner("lite", StructuredPlanner())


__all__ = [
    "CONFLICT_OPTIONS",
    "PLANNERS",
    "Plan",
    "Planner",
    "StructuredPlanner",
    "plan_hash",
    "register_planner",
]
