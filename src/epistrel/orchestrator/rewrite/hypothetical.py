"""#140's check under the hypothetical canon of a planned change (FR-DIRECT-3,8, FR-REWRITE-10;
#707, #711).

A rewrite plans against the canon as it **would be** after its change: the current facts on the
change's key that hold the value it supersedes are gone, and the change's proposition holds in
their place. ``hypothetical_records`` builds that record set for a claim (``check_with``'s
keyed fetch, then the substitution), and ``verdict_under`` runs #140's pure ``check`` over it.
Read only; no model call.

Three questions are asked through it:

- ``decision_supported`` (#225's ``causes`` → ``re_evaluate``, #707): a decision the changed
  fact was a reason for is **kept** when one of its other premises (another fact its ``causes``
  edges name, not on the change's key) is still ``supported`` under the hypothetical canon;
  otherwise it is regenerated as before.
- ``inference_follows`` (#225's ``derives`` of kind ``inference``, #707): an inferred belief
  follows the change (``cascade``) only when its premises, the changed ones replaced by the
  change's proposition, support the new value: none of the others contradicts it
  (``premises_support``, pure); otherwise it is kept.
- ``stays_consistent`` (#238's ``keep`` offer, #711): a proposition-bearing record (a fact or a
  belief) of an undo's blast radius is offered ``keep`` only when it is not ``contradicted``
  under the restored canon.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.enums import DependencyEdgeType, DependencyTarget
from epistrel.core.membership import active_only
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.propositions.registry import Registry, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.tables.edges import dependency_edges
from epistrel.core.verify.rules import Record, Verdict, VerdictKind, check, check_with

HYPOTHETICAL = "hypothetical"
"""The proposition id of the change's own record (it is not stored)."""


def proposition_of(change: Mapping[str, Any]) -> Proposition | None:
    """The proposition a planned proposition change (its preview summary) makes true, or
    ``None`` when it names an entity not created yet or carries no object."""
    subject, predicate = change.get("subject_id"), change.get("predicate_id")
    obj = change.get("object_id")
    if not isinstance(subject, str) or not isinstance(predicate, str):
        return None
    if subject.startswith("new:") or predicate.startswith("register:"):
        return None
    if obj is None or (isinstance(obj, str) and obj.startswith("new:")):
        return None
    try:
        return Proposition(
            subject_id=subject,
            predicate_id=predicate,
            object_id=str(obj),
            polarity=Polarity(str(change.get("polarity") or "affirmed")),
            valid_time=Interval.of(
                change.get("valid_from_minutes"), change.get("valid_to_minutes")
            ),
        )
    except ValueError:
        return None


def _on_key(record: Record, change: Mapping[str, Any]) -> bool:
    p = record.proposition
    return p.subject_id == change.get("subject_id") and p.predicate_id == change.get("predicate_id")


def _superseded(record: Record, change: Mapping[str, Any], gone: set[str]) -> bool:
    """Whether the change takes ``record`` out of canon: a fact it names as gone, or a fact on
    its key holding the value it supersedes."""
    if record.fact_id is not None and record.fact_id in gone:
        return True
    if not _on_key(record, change):
        return False
    superseded = change.get("superseded_object_id")
    return superseded is not None and record.proposition.object_id == superseded


def substituted(
    records: Sequence[Record],
    change: Mapping[str, Any],
    gone: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[Record]:
    """``records`` as the hypothetical canon has them (see the module docstring)."""
    kept = [r for r in records if not _superseded(r, change, set(gone))]
    made = proposition_of(change)
    if made is not None:
        kept.append(Record(made, HYPOTHETICAL))
    return kept


async def hypothetical_records(
    conn: AsyncConnection,
    story_id: str,
    claim: Proposition,
    change: Mapping[str, Any],
    *,
    registry: Registry | None = None,
    gone: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[Record]:
    """What ``claim`` is checked over under the change's hypothetical canon."""
    fetched: list[Record] = []
    await check_with(
        KeyedLookup(conn, StructuredReadSet()),
        story_id,
        claim,
        registry=registry or await load(conn, story_id),
        fetched=fetched,
    )
    records = substituted(fetched, change, gone)
    if proposition_of(change) is not None and claim.predicate_id != change.get("predicate_id"):
        # The change's proposition bears on a claim of another predicate only through rules the
        # keyed fetch already followed; it is kept out of a foreign key's record set.
        records = [r for r in records if r.proposition_id != HYPOTHETICAL]
    return records


async def verdict_under(
    conn: AsyncConnection,
    story_id: str,
    claim: Proposition,
    change: Mapping[str, Any],
    *,
    registry: Registry | None = None,
    gone: set[str] = frozenset(),  # type: ignore[assignment]
) -> Verdict:
    """#140's verdict on ``claim`` under the change's hypothetical canon."""
    registry = registry or await load(conn, story_id)
    records = await hypothetical_records(
        conn, story_id, claim, change, registry=registry, gone=gone
    )
    return check(claim, records, registry=registry)


async def _premise_facts(
    conn: AsyncConnection, story_id: str, table: str, record_id: str, edge: DependencyEdgeType
) -> list[str]:
    d = dependency_edges.c
    found = await conn.execute(
        select(d.target_id).where(
            d.story_id == story_id,
            active_only(dependency_edges),
            d.dependent_table == table,
            d.dependent_id == record_id,
            d.edge_type == edge,
            d.target_kind == DependencyTarget.FACT,
        )
    )
    return [str(t) for (t,) in found]


async def _current(conn: AsyncConnection, story_id: str, fact_id: str) -> Any:
    """The fact's current version, or ``None`` when its lineage is closed or gone."""
    from epistrel.core.canon import facts as canon
    from epistrel.core.canon.errors import FactNotFound

    try:
        fact, current = await canon._load(conn, story_id, fact_id)
    except FactNotFound:
        return None
    return fact if current else None


async def decision_supported(
    conn: AsyncConnection,
    story_id: str,
    event_id: str,
    change: Mapping[str, Any],
    changed_fact_ids: set[str],
) -> bool:
    """Whether a decision (an event with ``causes`` edges) is still supported, under the
    change's hypothetical canon, by a premise other than the changed facts (#707)."""
    premises = await _premise_facts(conn, story_id, "events", event_id, DependencyEdgeType.CAUSES)
    registry = await load(conn, story_id)
    for fact_id in premises:
        if fact_id in changed_fact_ids:
            continue
        fact = await _current(conn, story_id, fact_id)
        if fact is None or _on_key(Record.of_fact(fact), change):
            continue
        verdict = await verdict_under(
            conn, story_id, fact.proposition, change, registry=registry, gone=changed_fact_ids
        )
        if verdict.kind is VerdictKind.SUPPORTED:
            return True
    return False


def premises_support(
    new_value: Proposition,
    premises: Sequence[Record],
    change: Mapping[str, Any],
    registry: Registry,
) -> bool:
    """Whether the inference's premises, re-run with the change in place, support
    ``new_value``: the changed premise now states it (``substituted``), so they support it
    unless one of the other premises contradicts it (#140's verdict over those). Pure."""
    others = [r for r in substituted(premises, change) if r.proposition_id != HYPOTHETICAL]
    return check(new_value, others, registry=registry).kind is not VerdictKind.CONTRADICTED


async def inference_follows(
    conn: AsyncConnection,
    story_id: str,
    belief_id: str,
    change: Mapping[str, Any],
    changed_fact_ids: set[str],
) -> bool:
    """Whether an inferred belief follows the change: its premises, re-run under the change's
    hypothetical canon, support the new value (#707)."""
    new_value = proposition_of(change)
    if new_value is None:
        return False
    premises: list[Record] = []
    for fact_id in await _premise_facts(
        conn, story_id, "beliefs", belief_id, DependencyEdgeType.DERIVES
    ):
        if fact_id in changed_fact_ids:
            continue  # replaced by the change's own proposition (``substituted``)
        fact = await _current(conn, story_id, fact_id)
        if fact is not None:
            premises.append(Record.of_fact(fact))
    return premises_support(new_value, premises, change, await load(conn, story_id))


async def stays_consistent(
    conn: AsyncConnection,
    story_id: str,
    table: str,
    record_id: str,
    changes: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether a fact or belief of an undo's blast radius stays consistent under the restored
    canon (#711): #140's verdict on its proposition is not ``contradicted`` under each
    proposition change. A record of another table is never shown consistent here."""
    from epistrel.core.tables.beliefs import beliefs
    from epistrel.core.tables.propositions import propositions

    proposition: Proposition | None = None
    if table == "facts":
        fact = await _current(conn, story_id, record_id)
        proposition = None if fact is None else fact.proposition
    elif table == "beliefs":
        from epistrel.core.propositions.codec import proposition_from_row

        row = (
            await conn.execute(
                select(propositions)
                .join(beliefs, beliefs.c.proposition_id == propositions.c.id)
                .where(beliefs.c.id == record_id)
            )
        ).one_or_none()
        proposition = None if row is None else proposition_from_row(row)
    if proposition is None:
        return False
    registry = await load(conn, story_id)
    for change in changes:
        if change.get("kind") != "proposition":
            continue
        verdict = await verdict_under(conn, story_id, proposition, change, registry=registry)
        if verdict.kind is VerdictKind.CONTRADICTED:
            return False
    return True


MERGE_CONTRADICTION = "merge_contradiction"
"""#713: the conflict reason of a merged entity's fact the survivor's canon contradicts."""


async def merge_contradictions(
    conn: AsyncConnection, story_id: str, entity_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """#713 (FR-PROP-7): the merged entities' current facts that contradict the survivor's
    canon once they speak of one entity: each merged entity's fact (as its subject), restated
    of the survivor, through #140's check; a ``contradicted`` one is an irreducible conflict
    (``rewrite``: the fact is versioned to the value the survivor's cited fact holds; or
    ``keep``)."""
    from epistrel.core.canon import facts as canon
    from epistrel.orchestrator.rewrite.plan import _conflict_id

    survivor, *others = entity_ids
    registry = await load(conn, story_id)
    lookup = KeyedLookup(conn, StructuredReadSet())
    found: list[dict[str, Any]] = []
    for other in others:
        for fact in await canon.canon(conn, story_id, subject=other):
            claim = fact.proposition.model_copy(update={"subject_id": survivor})
            if claim.object_id == other:
                continue
            verdict = await check_with(lookup, story_id, claim, registry=registry)
            if verdict.kind is not VerdictKind.CONTRADICTED:
                continue
            cited = next((e.fact_id for e in verdict.evidence if e.fact_id is not None), None)
            agree = None if cited is None else await _current(conn, story_id, cited)
            key = f"merge:{survivor}:{other}"
            found.append(
                {
                    "record": {"table": "facts", "id": fact.id},
                    "edge_type": "presupposes",
                    "edges": [],
                    "change_key": key,
                    "target": {"kind": "fact", "id": cited or fact.id},
                    "action": "fix",
                    "conflict_id": _conflict_id(key, "facts", fact.id),
                    "reason": MERGE_CONTRADICTION,
                    "severity": "high",
                    "options": ["rewrite", "keep"] if agree is not None else ["keep"],
                    "agree_with": None if agree is None else agree.id,
                    "excerpt": None,
                    "before_excerpt": None,
                }
            )
    return found


__all__ = [
    "HYPOTHETICAL",
    "MERGE_CONTRADICTION",
    "decision_supported",
    "hypothetical_records",
    "inference_follows",
    "merge_contradictions",
    "premises_support",
    "proposition_of",
    "stays_consistent",
    "substituted",
    "verdict_under",
]
