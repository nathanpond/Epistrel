"""Typed dependency edges: why a record depends on a fact, and who depends on what changed
(FR-DIRECT-7, FR-REWRITE-11; #220).

``core.tables.edges`` describes the table. This module is its only writer
(``check-dependency-edges``) and its one search:

- **Writing.** ``record_edges(conn, op, edges)`` writes ``EdgeSpec``s in the writer's
  transaction as derived records of ``op`` (``ON CONFLICT DO NOTHING``: one edge per type,
  dependent and target, so a retry is idempotent). The structured turn commit derives its
  edges deterministically (``orchestrator.structured.pipeline``); ``transition_edge`` writes a
  belief transition's ``derives`` edge (#160: a ``record`` antecedent in ``facts`` targets that
  fact, any other the belief's own proposition; ``forgetting`` writes none);
  ``amendment_edges`` writes an amendment part's edges to the facts it relies on, and a second
  edge to the amendment part that wrote such a fact (FR-REWRITE-11).
- **Searching.** ``dependents(conn, story_id, targets)`` answers the active edges whose target
  is one of ``targets``: fact ids and every version of their lineages (``facts.lineage_id``,
  both ways), proposition ids (the given ones and those versions'), proposition keys
  ``(subject, predicate)``, amendment parts, States and (for ``mentions``) entities. ``depth``
  above 1 follows ``derives`` from the beliefs found to beliefs drawn from them (a belief whose
  ``derives`` edge targets the proposition a found belief holds). Results are ordered by
  position (position-less edges after, by creation), capped at
  ``dependency_search_max_results`` with ``truncated`` set rather than silently dropped;
  ``depth`` above ``dependency_search_max_depth`` is clamped (``depth_clamped``). Read-only,
  omniscient only (another viewpoint is a ``ValueError`` in M7), never another story's edge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import and_, false, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import (
    AntecedentKind,
    BeliefTransitionKind,
    DependencyEdgeType,
    DependencyTarget,
    Membership,
)
from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position, active_only
from epistrel.core.tables.edges import DEPENDENT_TABLES, dependency_edges
from epistrel.core.tables.facts import facts
from epistrel.core.viewpoint import OMNISCIENT

DEFAULT_MAX_RESULTS = 500
DEFAULT_MAX_DEPTH = 3

ACQUISITION_KINDS = frozenset(k.value for k in BeliefTransitionKind)
"""A ``derives`` edge's ``acquisition_kind``: one of #160's transition kinds."""

UNIQUE_EDGE = ("edge_type", "dependent_table", "dependent_id", "target_kind", "target_id")


class EdgeOperation(Protocol):
    """What an edge needs of the operation writing it: its id, story and narrative position."""

    operation_id: str
    story_id: str | None
    narrative_turn: int | None
    turn_variant: int | None


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """One edge to write: the dependent record, the target, the target's proposition key (a
    ``state`` target's scope key in ``target_subject_id``), a ``derives`` edge's acquisition
    kind, the dependent's position (``None``: position-less) and its source events."""

    edge_type: DependencyEdgeType
    dependent_table: str
    dependent_id: str
    target_kind: DependencyTarget
    target_id: str
    target_subject_id: str | None = None
    target_predicate_id: str | None = None
    acquisition_kind: str | None = None
    position: Position | None = None
    source_event_ids: tuple[str, ...] = ()

    def check(self) -> None:
        if self.dependent_table not in DEPENDENT_TABLES:
            raise ValueError(f"an edge's dependent cannot be a {self.dependent_table!r} row")
        derives = self.edge_type is DependencyEdgeType.DERIVES
        if derives != (self.acquisition_kind is not None):
            raise ValueError("an acquisition kind is set exactly on a derives edge")
        if self.acquisition_kind is not None and self.acquisition_kind not in ACQUISITION_KINDS:
            raise ValueError(f"unknown acquisition kind {self.acquisition_kind!r}")
        if self.edge_type is DependencyEdgeType.MENTIONS and (
            self.target_kind is not DependencyTarget.ENTITY
        ):
            raise ValueError("a mentions edge targets an entity")
        if self.target_kind is DependencyTarget.ENTITY and (
            self.edge_type is not DependencyEdgeType.MENTIONS
        ):
            raise ValueError("only a mentions edge targets an entity")


async def record_edges(conn: AsyncConnection, op: EdgeOperation, edges: Iterable[EdgeSpec]) -> int:
    """Write ``edges`` as derived records of ``op`` in ``conn``'s transaction (one statement);
    an edge already written (same type, dependent and target) is kept as it is. Answers how many
    edges were offered."""
    story_id = op.story_id
    if story_id is None:
        raise ValueError("an edge belongs to a story")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for edge in edges:
        edge.check()
        key = (
            edge.edge_type.value,
            edge.dependent_table,
            edge.dependent_id,
            edge.target_kind.value,
            edge.target_id,
        )
        if key in seen:
            continue
        seen.add(key)
        position = (
            {f"position_{n}": None for n in ("turn", "variant", "slot_k", "beat", "step")}
            if edge.position is None
            else edge.position.columns("position")
        )
        rows.append(
            {
                "id": new_ulid(),
                "story_id": story_id,
                "edge_type": edge.edge_type.value,
                "dependent_table": edge.dependent_table,
                "dependent_id": edge.dependent_id,
                "target_kind": edge.target_kind.value,
                "target_id": edge.target_id,
                "target_subject_id": edge.target_subject_id,
                "target_predicate_id": edge.target_predicate_id,
                "acquisition_kind": edge.acquisition_kind,
                **position,
                "operation_id": op.operation_id,
                "source_event_ids": list(edge.source_event_ids),
                "narrative_turn": op.narrative_turn,
                "turn_variant": op.turn_variant,
            }
        )
    if not rows:
        return 0
    statement = (
        insert(dependency_edges)
        .values(membership=Membership.ACTIVE.value, membership_tx=func.transaction_timestamp())
        .on_conflict_do_nothing(index_elements=list(UNIQUE_EDGE))
    )
    await conn.execute(statement, rows)
    return len(rows)


# -- writers' helpers --------------------------------------------------------------------------


async def fact_keys(
    conn: AsyncConnection, story_id: str, fact_ids: Iterable[str]
) -> dict[str, tuple[str, str]]:
    """Each of ``fact_ids``' proposition key ``(subject, predicate)``."""
    wanted = list(dict.fromkeys(fact_ids))
    if not wanted:
        return {}
    f = facts.c
    found = await conn.execute(
        select(f.id, f.subject_id, f.predicate_id).where(f.story_id == story_id, f.id.in_(wanted))
    )
    return {str(r.id): (str(r.subject_id), str(r.predicate_id)) for r in found}


async def fact_edges(
    conn: AsyncConnection,
    story_id: str,
    edge_type: DependencyEdgeType,
    dependent_table: str,
    dependent_id: str,
    fact_ids: Iterable[str],
    *,
    position: Position | None = None,
    source_event_ids: tuple[str, ...] = (),
) -> list[EdgeSpec]:
    """``edge_type`` edges from one dependent to each of ``fact_ids`` (their keys filled)."""
    keys = await fact_keys(conn, story_id, fact_ids)
    return [
        EdgeSpec(
            edge_type,
            dependent_table,
            dependent_id,
            DependencyTarget.FACT,
            fact_id,
            target_subject_id=subject,
            target_predicate_id=predicate,
            position=position,
            source_event_ids=source_event_ids,
        )
        for fact_id, (subject, predicate) in keys.items()
    ]


async def transition_edge(
    conn: AsyncConnection,
    op: EdgeOperation,
    *,
    belief_id: str,
    proposition_id: str,
    subject_id: str,
    predicate_id: str,
    kind: BeliefTransitionKind,
    antecedent_kind: AntecedentKind,
    antecedent_ref: Mapping[str, Any],
    position: Position | None,
    event_id: str | None = None,
) -> int:
    """A belief transition's ``derives`` edge (#160): from the belief to the fact its ``record``
    antecedent names in ``facts``, else to the belief's own proposition; the transition's kind
    as ``acquisition_kind``. A ``forgetting`` transition writes none."""
    if kind is BeliefTransitionKind.FORGETTING or op.story_id is None:
        return 0
    sources = () if event_id is None else (event_id,)
    if antecedent_kind is AntecedentKind.RECORD and antecedent_ref.get("table") == "facts":
        found = await fact_edges(
            conn,
            op.story_id,
            DependencyEdgeType.DERIVES,
            "beliefs",
            belief_id,
            [str(antecedent_ref.get("id"))],
            position=position,
            source_event_ids=sources,
        )
        if found:
            edge = found[0]
            return await record_edges(
                conn,
                op,
                [
                    EdgeSpec(
                        edge.edge_type,
                        edge.dependent_table,
                        edge.dependent_id,
                        edge.target_kind,
                        edge.target_id,
                        target_subject_id=edge.target_subject_id,
                        target_predicate_id=edge.target_predicate_id,
                        acquisition_kind=kind.value,
                        position=position,
                        source_event_ids=sources,
                    )
                ],
            )
    return await record_edges(
        conn,
        op,
        [
            EdgeSpec(
                DependencyEdgeType.DERIVES,
                "beliefs",
                belief_id,
                DependencyTarget.PROPOSITION,
                proposition_id,
                target_subject_id=subject_id,
                target_predicate_id=predicate_id,
                acquisition_kind=kind.value,
                position=position,
                source_event_ids=sources,
            )
        ],
    )


async def amendment_edges(
    conn: AsyncConnection,
    op: EdgeOperation,
    part_id: str,
    relies_on: Sequence[str],
    *,
    written_by: Mapping[str, str] | None = None,
    edge_type: DependencyEdgeType = DependencyEdgeType.CAUSES,
) -> int:
    """An amendment part's edges (FR-REWRITE-11): one to every fact in ``relies_on``, and, for
    a fact an amendment part wrote (``written_by``: fact id → part id; #221 reads it from
    ``facts.amendment_part_id``), a second edge whose target is that part. Position-less: an
    amendment's edges belong to its rewrite session, which no truncation removes."""
    if op.story_id is None:
        raise ValueError("an edge belongs to a story")
    found = await fact_edges(
        conn, op.story_id, edge_type, "amendment_parts", part_id, relies_on, position=None
    )
    origins = dict(written_by or {})
    found += [
        EdgeSpec(
            edge_type,
            "amendment_parts",
            part_id,
            DependencyTarget.AMENDMENT_PART,
            origins[fact_id],
        )
        for fact_id in relies_on
        if fact_id in origins and origins[fact_id] != part_id
    ]
    return await record_edges(conn, op, found)


# -- searching ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Targets:
    """What changed (``dependents``' input): fact ids (their lineages are searched), proposition
    ids, proposition keys ``(subject, predicate)``, entity ids (for ``mentions``), amendment
    part ids and State ids."""

    fact_ids: tuple[str, ...] = ()
    proposition_ids: tuple[str, ...] = ()
    keys: tuple[tuple[str, str], ...] = ()
    entity_ids: tuple[str, ...] = ()
    amendment_part_ids: tuple[str, ...] = ()
    state_ids: tuple[str, ...] = ()

    def empty(self) -> bool:
        return not (
            self.fact_ids
            or self.proposition_ids
            or self.keys
            or self.entity_ids
            or self.amendment_part_ids
            or self.state_ids
        )


@dataclass(frozen=True, slots=True)
class Dependent:
    """One dependent found: the edge's type, the dependent record ``(table, id)``, its position
    (``None``: position-less), a ``derives`` edge's acquisition kind, the target it was found
    by ``(kind, id)`` and the search level it was found at (1: a direct dependent)."""

    edge_type: DependencyEdgeType
    dependent: tuple[str, str]
    position: Position | None
    acquisition_kind: str | None
    target: tuple[str, str]
    depth: int = 1
    edge_id: str = ""


@dataclass(frozen=True, slots=True)
class DependentResult:
    """A search's answer: the dependents by level then position, whether the cap cut it
    (``truncated``) and whether the depth asked was clamped (``depth_clamped``)."""

    items: list[Dependent] = field(default_factory=list)
    truncated: bool = False
    depth_clamped: bool = False


async def _lineages(
    conn: AsyncConnection, story_id: str, fact_ids: Sequence[str]
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Every version of ``fact_ids``' lineages, their proposition ids and keys."""
    if not fact_ids:
        return [], [], []
    f = facts.c
    lineage = select(f.lineage_id).where(f.story_id == story_id, f.id.in_(list(fact_ids)))
    rows = list(
        await conn.execute(
            select(f.id, f.proposition_id, f.subject_id, f.predicate_id).where(
                f.story_id == story_id, f.lineage_id.in_(lineage)
            )
        )
    )
    ids = [str(r.id) for r in rows]
    props = [str(r.proposition_id) for r in rows if r.proposition_id is not None]
    keys = list(dict.fromkeys((str(r.subject_id), str(r.predicate_id)) for r in rows))
    return ids, props, keys


def _position_of(row: Any) -> Position | None:
    return Position.of_columns(row._mapping, "position")


async def dependents(
    conn: AsyncConnection,
    story_id: str,
    targets: Targets,
    *,
    edge_types: Iterable[DependencyEdgeType] | None = None,
    depth: int = 1,
    through: Position | None = None,
    viewpoint: str = OMNISCIENT,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> DependentResult:
    """The typed dependents of ``targets`` (see the module docstring)."""
    if viewpoint != OMNISCIENT:
        raise ValueError("the dependency search is an omniscient read")
    clamped = depth > max_depth
    depth = max(1, min(depth, max_depth))
    fact_ids, lineage_props, lineage_keys = await _lineages(conn, story_id, targets.fact_ids)
    wanted_types = None if edge_types is None else [t.value for t in edge_types]
    d = dependency_edges.c
    found: list[Dependent] = []
    visited: set[tuple[str, str]] = set()
    level_targets = targets
    level_facts, level_props = fact_ids, [*lineage_props, *targets.proposition_ids]
    level_keys = [*lineage_keys, *targets.keys]
    level_beliefs: list[str] = []
    truncated = False
    for level in range(1, depth + 1):
        clauses: list[Any] = []
        if level_facts:
            clauses.append(
                and_(d.target_kind == DependencyTarget.FACT.value, d.target_id.in_(level_facts))
            )
        if level_props:
            clauses.append(
                and_(
                    d.target_kind == DependencyTarget.PROPOSITION.value,
                    d.target_id.in_(list(dict.fromkeys(level_props))),
                )
            )
        if level_keys:
            clauses.append(
                and_(
                    d.target_kind.in_(
                        [DependencyTarget.FACT.value, DependencyTarget.PROPOSITION.value]
                    ),
                    tuple_(d.target_subject_id, d.target_predicate_id).in_(
                        list(dict.fromkeys(level_keys))
                    ),
                )
            )
        if level_beliefs:
            clauses.append(and_(d.dependent_table == "beliefs", d.dependent_id.in_(level_beliefs)))
        if level == 1:
            if level_targets.amendment_part_ids:
                clauses.append(
                    and_(
                        d.target_kind == DependencyTarget.AMENDMENT_PART.value,
                        d.target_id.in_(list(level_targets.amendment_part_ids)),
                    )
                )
            if level_targets.state_ids:
                clauses.append(
                    and_(
                        d.target_kind == DependencyTarget.STATE.value,
                        d.target_id.in_(list(level_targets.state_ids)),
                    )
                )
            if level_targets.entity_ids:
                clauses.append(
                    and_(
                        d.target_kind == DependencyTarget.ENTITY.value,
                        d.edge_type == DependencyEdgeType.MENTIONS.value,
                        d.target_id.in_(list(level_targets.entity_ids)),
                    )
                )
        if not clauses:
            break
        where: list[Any] = [d.story_id == story_id, active_only(dependency_edges), or_(*clauses)]
        if level > 1:
            where.append(d.edge_type == DependencyEdgeType.DERIVES.value)
        elif wanted_types is not None:
            where.append(d.edge_type.in_(wanted_types) if wanted_types else false())
        if through is not None:
            key = tuple_(d.position_turn, d.position_slot_k, d.position_beat, d.position_step)
            where.append(or_(d.position_turn.is_(None), key <= tuple_(*through.key())))
        room = max_results - len(found)
        rows = list(
            await conn.execute(
                select(dependency_edges)
                .where(*where)
                .order_by(
                    d.position_turn.nulls_last(),
                    d.position_slot_k,
                    d.position_beat,
                    d.position_step,
                    d.created_at,
                    d.id,
                )
                .limit(room + 1)
            )
        )
        next_props: list[str] = []
        for row in rows:
            dependent = (str(row.dependent_table), str(row.dependent_id))
            if dependent in visited and level > 1:
                continue
            if len(found) >= max_results:
                truncated = True
                break
            visited.add(dependent)
            found.append(
                Dependent(
                    edge_type=DependencyEdgeType(row.edge_type),
                    dependent=dependent,
                    position=_position_of(row),
                    acquisition_kind=row.acquisition_kind,
                    target=(str(row.target_kind), str(row.target_id)),
                    depth=level,
                    edge_id=str(row.id),
                )
            )
            if row.dependent_table == "beliefs":
                next_props.append(str(row.dependent_id))
        if truncated or level == depth or not next_props:
            break
        # The next level: beliefs drawn from the beliefs just found: a transition citing one
        # (a ``record`` antecedent in ``beliefs``), or one holding its proposition.
        from epistrel.core.tables.beliefs import belief_transitions, beliefs

        b, t = beliefs.c, belief_transitions.c
        held = list(
            await conn.execute(
                select(b.proposition_id).where(b.story_id == story_id, b.id.in_(next_props))
            )
        )
        drawn = list(
            await conn.scalars(
                select(t.belief_id).where(
                    t.story_id == story_id,
                    t.antecedent_kind == AntecedentKind.RECORD.value,
                    t.antecedent_ref["table"].astext == "beliefs",
                    t.antecedent_ref["id"].astext.in_(next_props),
                )
            )
        )
        level_facts = []
        level_props = [str(r.proposition_id) for r in held]
        level_keys = []
        level_beliefs = [str(x) for x in drawn]
        level_targets = Targets()
    return DependentResult(found, truncated=truncated, depth_clamped=clamped)


async def search_bounds(conn: AsyncConnection) -> tuple[int, int]:
    """The deployment's ``dependency_search_max_results`` and ``_max_depth`` (#220)."""
    from epistrel.core.config_changes import read_section
    from epistrel.core.settings_schema import DependencySettings

    found = await read_section(conn, DependencySettings)
    return found.dependency_search_max_results, found.dependency_search_max_depth


def proposition_key(proposition: Any) -> tuple[str, str]:
    return (str(proposition.subject_id), str(proposition.predicate_id))


__all__ = [
    "ACQUISITION_KINDS",
    "Dependent",
    "DependentResult",
    "EdgeOperation",
    "EdgeSpec",
    "Targets",
    "amendment_edges",
    "dependents",
    "fact_edges",
    "fact_keys",
    "proposition_key",
    "record_edges",
    "search_bounds",
    "transition_edge",
]
