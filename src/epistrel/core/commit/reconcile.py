"""Resolution reconciliation: no thunk resolution commits against recorded history
(FR-COMMIT-14, FR-COMMIT-11; docs/02 §4.4; #154).

``reconcile(conn, story_id, thunk, binding) -> Ok | Conflict`` is asked before every resolution
of a thunk commits (``core.commit.lifecycle.depend`` calls it; so does the structured turn's
screen before its repair ladder, ``orchestrator.commit.resolution``). ``binding`` is the thunk's
proposition with its open slot filled, at the time the resolution's fact would take: the thunk's
original ``valid_time``.

**The obligations** are the retroactive propositions the binding implies at that time, derived
from declared predicate semantics only (no world knowledge): the bounded closure of the binding
and the canon ``via`` joins from its object under the story's declared rules (#131's ``imply``;
"whoever knocked at the porch was at the porch"), each ``implied``; and, for each of them (and the
binding) on a functional predicate, the exclusivity its uniqueness key carries (``located_at``
one place at a time: at the porch at t is *not elsewhere* at t), recorded as ``exclusive``.
Knowledge obligations ("Bobby knows he knocked") arrive with M5's partitions.

**The conflict search** runs #140's rules (``core.verify.rules.check_with``, canon, omniscient)
over recorded history on the binding and on every implied obligation, at the thunk's original
time. A ``contradicted`` verdict on any of them is a ``Conflict``: the obligations, the records
it cites (``conflicting_records``: fact ids) and the dependency cluster around them
(``context``, #142's ``fact_neighborhood``), so a repair can steer the generation away from the
bound entity (``bound_entity``). Otherwise ``Ok``: an implied obligation that recorded history
already ``supported`` is skipped, an ``uncertain`` one is recorded (``uncertain``) and treated as
no conflict, and the ``novel`` ones are ``to_commit``: the caller commits them with the
resolution's fact, in one transaction (``provenance = resolution``, anchored to the thunk). With
verification off for the story, nothing is checked (``Ok`` with ``checked = False``).

``reconcile`` only reads and reports. A binding to an entity created in the same turn has no
recorded history, so it is trivially ``Ok``. Logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.propositions.model import Cardinality, Instant, Polarity, Proposition
from epistrel.core.propositions.registry import Registry, imply, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.verify.neighborhood import Seed, fact_neighborhood
from epistrel.core.verify.rules import VerdictKind, check_with, options_for

if TYPE_CHECKING:
    from epistrel.core.commit.lifecycle import Thunk

log = logging.getLogger(__name__)

ObligationKind = Literal["implied", "exclusive"]
CONTEXT_DEPTH = 1
"""How far ``Conflict.context`` reaches from each conflicting record."""


@dataclass(frozen=True, slots=True)
class Obligation:
    """One retroactive proposition a binding implies (see the module docstring): ``implied``
    (it holds at the thunk's time) or ``exclusive`` (the functional key's other values do
    not)."""

    proposition: Proposition
    kind: ObligationKind


@dataclass(frozen=True, slots=True)
class Ok:
    """No recorded conflict: the novel obligations to commit with the resolution, the
    uncertain ones (recorded, not committed), and whether any check ran."""

    binding: Proposition
    obligations: tuple[Obligation, ...] = ()
    to_commit: tuple[Proposition, ...] = ()
    uncertain: tuple[Proposition, ...] = ()
    checked: bool = True


@dataclass(frozen=True, slots=True)
class Conflict:
    """The binding contradicts recorded history: its obligations, the fact ids the conflicting
    verdicts cite, their neighborhood's fact ids and the entity the binding names."""

    binding: Proposition
    obligations: tuple[Obligation, ...]
    conflicting_records: tuple[str, ...]
    context: tuple[str, ...] = ()
    bound_entity: str | None = None


Reconciliation = Ok | Conflict


def bound_entity(thunk: Thunk, binding: Proposition) -> str | None:
    """The entity the binding puts in the thunk's open slot (``None`` for a literal)."""
    slot = thunk.open_slot.value
    if slot == "subject":
        return binding.subject_id
    if slot == "object":
        return binding.object_id
    return None


async def obligations_of(
    lookup: KeyedLookup, story_id: str, binding: Proposition, registry: Registry
) -> tuple[Obligation, ...]:
    """The binding's obligations under the story's declared rules (see the module docstring)."""
    rules = registry.rules()
    found: list[Obligation] = []
    derived: list[Proposition] = []
    if rules and binding.polarity is Polarity.AFFIRMED:
        joins: list[Proposition] = []
        if binding.object_id is not None:
            window = _window(binding)
            for via in sorted({r.via for r in rules if r.via is not None}):
                facts = await lookup.canon_at(
                    story_id, predicate=via, subjects=binding.object_id, window=window
                )
                joins += [f.proposition for f in facts]
        closure = imply([binding, *joins], rules)
        derived = [d for d in closure.derived if d.subject_id == binding.subject_id]
    found += [Obligation(d, "implied") for d in derived]
    for p in (binding, *derived):
        predicate = registry.get(p.predicate_id)
        if predicate is not None and predicate.cardinality is Cardinality.FUNCTIONAL:
            found.append(Obligation(p, "exclusive"))
    return tuple(found)


def _window(p: Proposition) -> tuple[int | None, int | None] | None:
    vt = p.valid_time
    if isinstance(vt, Instant):
        return (vt.at, vt.until)
    return (
        None if vt.start is None else vt.start.outer,
        None if vt.end is None else vt.end.outer,
    )


async def reconcile(
    conn: AsyncConnection,
    story_id: str,
    thunk: Thunk,
    binding: Proposition,
    *,
    registry: Registry | None = None,
    read_set: StructuredReadSet | None = None,
) -> Reconciliation:
    """Reconcile ``binding`` (the thunk's proposition with its slot filled, at the thunk's
    original time) against recorded history; see the module docstring."""
    registry = registry or await load(conn, story_id)
    lookup = KeyedLookup(conn, read_set if read_set is not None else StructuredReadSet())
    obligations = await obligations_of(lookup, story_id, binding, registry)
    options = await options_for(conn, story_id)
    if options is None:
        return Ok(binding, obligations, checked=False)
    contradicted = False
    conflicting: list[str] = []
    to_commit: list[Proposition] = []
    uncertain: list[Proposition] = []
    implied = [o.proposition for o in obligations if o.kind == "implied"]
    for target in (binding, *implied):
        verdict = await check_with(lookup, story_id, target, registry=registry, options=options)
        if verdict.kind is VerdictKind.CONTRADICTED:
            contradicted = True
            conflicting += [e.fact_id for e in verdict.evidence if e.fact_id is not None]
            continue
        if target is binding:
            continue
        if verdict.kind is VerdictKind.NOVEL:
            to_commit.append(target)
        elif verdict.kind is VerdictKind.UNCERTAIN:
            uncertain.append(target)
    records = tuple(dict.fromkeys(conflicting))
    if contradicted:
        context: dict[str, None] = {}
        for fact_id in records:
            found = await fact_neighborhood(
                lookup, story_id, Seed(fact_id=fact_id), depth=CONTEXT_DEPTH, registry=registry
            )
            context.update(dict.fromkeys(f.id for f in found.facts()))
        log.debug(
            "reconcile: thunk %s binding rejected, %d records conflict", thunk.id, len(records)
        )
        return Conflict(
            binding,
            obligations,
            records,
            tuple(context),
            bound_entity(thunk, binding),
        )
    return Ok(binding, obligations, tuple(to_commit), tuple(uncertain))


async def author_directed(*_: object, **__: object) -> Reconciliation:
    """An author-directed resolution (FR-COMMIT-12) goes through the amendment feature, which is
    M7's: not available in M4."""
    raise NotImplementedError("author-directed resolution (FR-COMMIT-12) arrives with M7")


__all__ = [
    "CONTEXT_DEPTH",
    "Conflict",
    "Obligation",
    "ObligationKind",
    "Ok",
    "Reconciliation",
    "author_directed",
    "bound_entity",
    "obligations_of",
    "reconcile",
]
