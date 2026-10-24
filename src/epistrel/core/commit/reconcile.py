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

**Knowledge obligations** (FR-COMMIT-14, M5; #165). With per-character partitions on, a binding
to a modeled character C (not the player) also obliges C's knowledge: C knew the act and its
circumstances (the binding and its implied obligations) from the thunk's original time, at
``known`` (``Knowledge``). The conflict search then runs over C's **later sincere lines** too:
C's ``say`` beats with intent ``sincere`` or unlabelled, its ``do`` and its ``think`` beats,
after the thunk's introduction, on the active timeline. A line conflicts when a proposition it
declares contradicts the knowledge (the other polarity, or another value of a functional key,
over overlapping time), or when a sincere ``say`` or a ``think`` is a wh-question about the
thunk's event (#148's ``question`` detector, ``core.verify.question``: "who was at your door?"
maps to not-knowing who knocked). The conflicting lines are their events
(``Conflict.conflicting_lines``). A non-conflicting binding commits the knowledge with it
(``core.commit.lifecycle.depend``: a ``resolution`` transition per proposition, antecedent the
thunk, held from the thunk's introduction, and C's backdated memory of the act). Then
**the soft flag** (FR-COMMIT-11, ``core.commit.epistemic.could_have_known``) is asked, when the
caller names the resolving position (``now``).

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
recorded history, so it is trivially ``Ok``. Logs carry ids and counts only (invariant 3): a
line's text is read only to compare its words with the thunk's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.membership import Position, active_operation
from epistrel.core.propositions.model import Cardinality, Instant, Polarity, Proposition
from epistrel.core.propositions.registry import Registry, canonical, imply, load
from epistrel.core.readset import KeyedLookup, StructuredReadSet
from epistrel.core.tables import characters, events, operations
from epistrel.core.verify import question
from epistrel.core.verify.neighborhood import Seed, fact_neighborhood
from epistrel.core.verify.rules import VerdictKind, check_with, options_for

if TYPE_CHECKING:
    from epistrel.core.commit.epistemic import Flag
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
class Knowledge:
    """The knowledge a binding obliges its bound character to have held (FR-COMMIT-14, M5):
    the holder, the propositions (the binding and its implied obligations) and the narrative
    position and story-minute it holds from (the thunk's introduction and original time)."""

    holder: str
    propositions: tuple[Proposition, ...]
    held_from: Position
    story_time: int | None = None


@dataclass(frozen=True, slots=True)
class Ok:
    """No recorded conflict: the novel obligations to commit with the resolution, the
    uncertain ones (recorded, not committed), whether any check ran, the knowledge the bound
    character commits with it (M5) and the could-have-known soft flag, if raised."""

    binding: Proposition
    obligations: tuple[Obligation, ...] = ()
    to_commit: tuple[Proposition, ...] = ()
    uncertain: tuple[Proposition, ...] = ()
    checked: bool = True
    knowledge: Knowledge | None = None
    flag: Flag | None = None


@dataclass(frozen=True, slots=True)
class Conflict:
    """The binding contradicts recorded history: its obligations, the fact ids the conflicting
    verdicts cite, their neighborhood's fact ids, the entity the binding names and the bound
    character's later sincere lines (event ids) its knowledge contradicts."""

    binding: Proposition
    obligations: tuple[Obligation, ...]
    conflicting_records: tuple[str, ...]
    context: tuple[str, ...] = ()
    bound_entity: str | None = None
    conflicting_lines: tuple[str, ...] = ()


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


# -- knowledge obligations (M5) ------------------------------------------------------------------

SINCERE_INTENTS = frozenset({"sincere", "unspecified"})
"""The ``say`` intents a line is sincere under (an unlabelled one included)."""
LINE_TYPES = ("beat_say", "beat_do", "beat_think")
QUESTION_TYPES = frozenset({"beat_say", "beat_think"})


def introduced_at(thunk: Thunk) -> Position:
    """The thunk's introduction as a narrative position (its turn, variant and beat)."""
    return Position(thunk.narrative_turn or 0, thunk.turn_variant or 0, 0, thunk.beat or 0, 0)


async def knowledge_holder(
    conn: AsyncConnection, story_id: str, entity_id: str | None, *, settings: Any = None
) -> str | None:
    """The character a binding to ``entity_id`` obliges knowledge of: the entity when it is a
    story character other than the player and the story keeps per-character partitions."""
    from epistrel.core.profiles.capabilities import module_on
    from epistrel.core.profiles.effective import effective_configuration

    if entity_id is None:
        return None
    current = settings
    if current is None:
        current = (await effective_configuration(conn, story_id, validate=False)).settings
    if not module_on(current, "partitions"):
        return None
    row = (
        await conn.execute(
            select(characters.c.is_player).where(
                characters.c.story_id == story_id, characters.c.id == entity_id
            )
        )
    ).one_or_none()
    return None if row is None or bool(row.is_player) else entity_id


def _span(p: Proposition) -> tuple[int | None, int | None]:
    window = _window(p)
    return (None, None) if window is None else window


def _overlap(a: Proposition, b: Proposition) -> bool:
    (a0, a1), (b0, b1) = _span(a), _span(b)
    starts_before_b_ends = a0 is None or b1 is None or a0 <= b1
    b_starts_before_a_ends = b0 is None or a1 is None or b0 <= a1
    return starts_before_b_ends and b_starts_before_a_ends


def contradicts(line: Proposition, known: Proposition, registry: Registry) -> bool:
    """Whether a line's proposition contradicts a known one: the same key with the other
    polarity, or (both affirmed) another value of a functional predicate for the same subject
    and qualifiers, over overlapping time."""
    a, b = canonical(line, registry), canonical(known, registry)
    if not _overlap(a, b):
        return False
    if a.normalized_key() == b.normalized_key():
        return a.polarity is not b.polarity
    predicate = registry.get(a.predicate_id)
    return (
        predicate is not None
        and predicate.cardinality is Cardinality.FUNCTIONAL
        and a.predicate_id == b.predicate_id
        and a.subject_id == b.subject_id
        and a.qualifier_key() == b.qualifier_key()
        and a.polarity is Polarity.AFFIRMED
        and b.polarity is Polarity.AFFIRMED
        and a.object_key() != b.object_key()
    )


@dataclass(frozen=True, slots=True)
class Line:
    """One of the bound character's later lines: its event, beat kind (``beat_say`` …), its
    position, its text and the propositions it declared."""

    event_id: str
    type: str
    position: Position
    text: str
    propositions: tuple[Proposition, ...] = ()


def _line(row: Any) -> Line | None:
    payload = row.payload if isinstance(row.payload, dict) else {}
    if row.type == "beat_say" and str(payload.get("intent", "unspecified")) not in SINCERE_INTENTS:
        return None
    declared: list[Proposition] = []
    for item in payload.get("propositions") or ():
        doc = item.get("proposition") if isinstance(item, dict) else None
        if isinstance(doc, dict):
            try:
                declared.append(Proposition.model_validate(doc))
            except ValueError:
                continue
    return Line(
        str(row.id),
        str(row.type),
        Position(
            int(row.narrative_turn),
            int(row.turn_variant or 0),
            int(row.slot_k or 0),
            int(row.beat or 0),
            int(row.step or 0),
        ),
        str(payload.get("text") or ""),
        tuple(declared),
    )


async def later_lines(
    conn: AsyncConnection, story_id: str, holder: str, after: Position
) -> list[Line]:
    """``holder``'s sincere lines after ``after`` on the active timeline, in narrative order."""
    o = operations.alias("line_operation")
    rows = await conn.execute(
        select(
            events.c.id,
            events.c.type,
            events.c.payload,
            events.c.narrative_turn,
            events.c.turn_variant,
            events.c.slot_k,
            events.c.beat,
            events.c.step,
        )
        .select_from(events.join(o, o.c.operation_id == events.c.operation_id))
        .where(
            events.c.story_id == story_id,
            events.c.character_id == holder,
            events.c.type.in_(LINE_TYPES),
            events.c.narrative_turn.is_not(None),
            active_operation(o),
        )
        .order_by(events.c.seq)
    )
    found = [_line(r) for r in rows]
    return [x for x in found if x is not None and after.before(x.position)]


async def _event_text(conn: AsyncConnection, event_id: str | None) -> str:
    if event_id is None:
        return ""
    payload = await conn.scalar(select(events.c.payload).where(events.c.id == event_id))
    return str(payload.get("text") or "") if isinstance(payload, dict) else ""


async def sincere_conflicts(
    conn: AsyncConnection,
    story_id: str,
    thunk: Thunk,
    knowledge: Knowledge,
    registry: Registry,
) -> tuple[str, ...]:
    """The bound character's later sincere lines inconsistent with ``knowledge`` (see the
    module docstring): their event ids."""
    sketch = thunk.sketch
    terms = [
        t
        for t in (
            sketch.subject_text,
            sketch.relation_text.replace("_", " "),
            sketch.object_text,
            await _event_text(conn, thunk.introduced_event_id),
        )
        if t
    ]
    slot = thunk.open_slot.value
    found: list[str] = []
    for line in await later_lines(conn, story_id, knowledge.holder, knowledge.held_from):
        if any(
            contradicts(p, k, registry) for p in line.propositions for k in knowledge.propositions
        ) or (line.type in QUESTION_TYPES and question.asks_about(line.text, slot, terms)):
            found.append(line.event_id)
    return tuple(found)


async def reconcile(
    conn: AsyncConnection,
    story_id: str,
    thunk: Thunk,
    binding: Proposition,
    *,
    registry: Registry | None = None,
    read_set: StructuredReadSet | None = None,
    now: Position | None = None,
    settings: Any = None,
) -> Reconciliation:
    """Reconcile ``binding`` (the thunk's proposition with its slot filled, at the thunk's
    original time) against recorded history; see the module docstring. ``now`` is the
    resolving position: given, a binding with no conflict is also asked the could-have-known
    soft flag."""
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
    holder = await knowledge_holder(conn, story_id, bound_entity(thunk, binding), settings=settings)
    if holder is None:
        return Ok(binding, obligations, tuple(to_commit), tuple(uncertain))
    knowledge = Knowledge(holder, (binding, *implied), introduced_at(thunk), thunk.valid_time)
    lines = await sincere_conflicts(conn, story_id, thunk, knowledge, registry)
    if lines:
        log.debug("reconcile: thunk %s binding rejected, %d lines conflict", thunk.id, len(lines))
        return Conflict(
            binding, obligations, (), (), bound_entity(thunk, binding), conflicting_lines=lines
        )
    flag = None
    if now is not None:
        from epistrel.core.commit import epistemic

        flag = await epistemic.could_have_known(
            conn,
            story_id,
            thunk,
            binding,
            holder,
            now=now,
            settings=settings,
            registry=registry,
            read_set=lookup.read_set,
        )
    return Ok(
        binding,
        obligations,
        tuple(to_commit),
        tuple(uncertain),
        knowledge=knowledge,
        flag=flag,
    )


async def author_directed(*_: object, **__: object) -> Reconciliation:
    """An author-directed resolution (FR-COMMIT-12) goes through the amendment feature, which is
    M7's: not available in M4."""
    raise NotImplementedError("author-directed resolution (FR-COMMIT-12) arrives with M7")


__all__ = [
    "CONTEXT_DEPTH",
    "LINE_TYPES",
    "SINCERE_INTENTS",
    "Conflict",
    "Knowledge",
    "Line",
    "Obligation",
    "ObligationKind",
    "Ok",
    "Reconciliation",
    "author_directed",
    "bound_entity",
    "contradicts",
    "introduced_at",
    "knowledge_holder",
    "later_lines",
    "obligations_of",
    "reconcile",
    "sincere_conflicts",
]
