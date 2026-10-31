"""The predicate registry: three layers, auto-registration, proposals and implication closure
(FR-PROP-3,4,5,8,11; docs/02 §4.9; #131).

**Layers.** A story's registry (``load``) is the core vocabulary (``vocabulary``, ``story_id``
null) plus the story's own predicates:

- **core**: shipped with documented semantics; ``sync_core`` re-syncs the rows from
  ``vocabulary`` at every start (a new predicate or a changed doc string reaches an existing
  deployment; nothing else is touched).
- **author**: declared per scenario in ``starting_state.predicates``, ``closed_sets`` and
  ``rules`` (#108's loose lists, ``FieldClass.story_rule``). ``ScenarioRules`` reads them, from the
  story's ``scenario_snapshot`` (``rules_from_snapshot``) or, for a story created before it had
  the structured pipeline, from its ``genesis_deferred`` rows with ``capability_needed =
  structured`` (``rules_from_deferred``); structured genesis (#149) registers them with full
  semantics (``register_author``). A name a core predicate has is refused, and so is a rule
  cycle.
- **auto**: a relation a render uses that nobody declared is registered on first use
  (``auto_register``) with the **weakest semantics** — ``multi``, no maximum, no exclusions,
  implications or containment, ``fluent``, argument types from the resolved entities' kinds or
  ``any``, not symmetric, owner ``story``. Its name is the relation text casefolded, with runs of
  anything but letters and digits as ``_`` (#140's alias table maps "was apprenticed to" and
  "apprenticed with" to one relation first). Registration is a story configuration change: the
  story's ``config_revision`` moves by one and a ``config_changes`` row (setting
  ``predicate:<id>``, class interpretive) is written, in the caller's transaction (the turn's);
  two turns racing to register one name meet at the unique index and both get the one row.

**Weak semantics forbid inferred exclusivity** (``Predicate.weak``): #140 never derives
``contradicted`` for a weak predicate from cardinality, exclusion, implication or containment;
only from a direct polarity conflict (``direct_polarity_conflict``: the same normalized
proposition affirmed and negated over overlapping time).

**Proposals.** The engine may propose strengthening an auto predicate (``file_proposal``;
``consider_proposal`` files ``functional`` once ``PROPOSAL_OBSERVATIONS`` affirmed propositions
of it are single-valued per subject; a structured turn's commit runs it through
``consider_proposals`` for the auto predicates its propositions use, and a proposal the author
rejected is not filed again). A proposal is inert: nothing reads it but the listing,
until ``POST /stories/{id}/predicates/{predicate_id}/approve`` applies it (all of it, or a
subset of its fields) as an interpretive configuration change (revision bump, one
``config_changes`` row). Existing propositions that the new semantics would flag are reported in
the approval's answer, never changed. ``reject`` closes it and changes nothing else.

**Direction** (``canonical``): a symmetric predicate's proposition is stored with the lower
entity id first; an inverse predicate's (``held_by``) as its non-inverse twin (``holds``) with
subject and object swapped.

**Closure** (``imply``): pure, declared rules only (the story's ``predicate_rules`` and each
predicate's own ``implications``), applied breadth-first up to ``max_depth`` (default
``IMPLICATION_MAX_DEPTH``, 3); a derived proposition's valid-time is the intersection of its
antecedents'; when the depth stops a derivation that could continue, the closure says so
(``Closure.truncated``), and #140 flags it in the verdict's evidence.

**Closed sets** are stored only when an author (``register_author``) or canon (#145's facts
route, through ``store.declare_closed_set``) asserts them; no code path writes one from
observation.

Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core import own_predicates
from epistrel.core.config_changes import ConfigScope, record_config_change
from epistrel.core.enums import StoryStatus
from epistrel.core.erasure import guard
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import predicate_record, proposal_record
from epistrel.core.errors import InvalidRequest, StoryArchived, StoryNotFound
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.codec import (
    SEMANTIC_COLUMNS,
    predicate_from_row,
    predicate_row,
    proposition_from_row,
)
from epistrel.core.propositions.model import (
    ANY,
    Bound,
    Cardinality,
    Implication,
    Instant,
    Interval,
    Layer,
    Polarity,
    Predicate,
    PredicateOwner,
    Proposition,
    QualifierKind,
    TemporalKind,
    overlaps,
)
from epistrel.core.propositions.vocabulary import CORE_BY_ID, CORE_BY_NAME, CORE_PREDICATES, SAME_AS
from epistrel.core.settings_schema import ConfigClass
from epistrel.core.tables import (
    genesis_deferred,
    predicate_proposals,
    predicates,
    propositions,
    stories,
)
from epistrel.core.tables.propositions import CORE_SCOPE

if TYPE_CHECKING:
    from epistrel.core.membership import Position
    from epistrel.core.verify.rules import OwnedValue, Verdict

log = logging.getLogger(__name__)

# ON CONFLICT arbiters spelled with literals, exactly as their indexes are (#466). A bound value
# (``coalesce(story_id, $1)``) is folded to a constant only on a custom plan: once psycopg
# prepares the statement and Postgres picks the generic plan, the parameter no longer matches
# ``uq_predicates_scope_name`` (or ``uq_predicate_proposals_pending``'s predicate) and the
# insert raises ``InvalidColumnReference``.
_PREDICATE_SCOPE_NAME = (
    func.coalesce(predicates.c.story_id, literal_column(f"'{CORE_SCOPE}'")),
    predicates.c.name,
)
_PROPOSAL_PENDING = and_(
    predicate_proposals.c.status == literal_column("'pending'"),
    predicate_proposals.c.kind == literal_column("'predicate'"),
)
_IDENTITY_PENDING = and_(
    predicate_proposals.c.status == literal_column("'pending'"),
    predicate_proposals.c.kind == literal_column("'identity'"),
)

IMPLICATION_MAX_DEPTH = 3
"""The default depth of the implication closure (FR-PROP-11)."""
PROPOSAL_OBSERVATIONS = 5
"""Affirmed, single-valued propositions of an auto predicate before the engine proposes making
it functional."""
ENGINE_PRINCIPAL = "engine"
"""Who files the engine's proposals and its auto-registrations' configuration changes."""
STRUCTURED = "structured"
RULE_FIELDS = ("predicates", "closed_sets", "rules")
NAME_MAX = 120


class PredicateNotFound(StoryNotFound):
    """The predicate a call names is not one of the story's (404 ``not_found``)."""


class ProposalNotFound(StoryNotFound):
    """The predicate has no pending proposal to approve or reject (404 ``not_found``,
    ``details.reason = no_pending_proposal``)."""


def setting_of(predicate_id: str) -> str:
    """The ``config_changes.setting`` of a predicate's registration or approval."""
    return f"predicate:{predicate_id}"


# -- the snapshot -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registry:
    """A story's registry as one read saw it: the core and story predicates, the story's own
    implication rules, and the ``config_revision`` the read captured (an operation pins the
    snapshot at its revision; a change after it moves the revision, which the operation's
    commit checks)."""

    story_id: str | None
    config_revision: int
    predicates: Mapping[str, Predicate]
    story_rules: tuple[Implication, ...] = ()

    def get(self, predicate_id: str) -> Predicate | None:
        return self.predicates.get(predicate_id)

    def named(self, name: str) -> Predicate | None:
        story = [p for p in self.predicates.values() if p.name == name and p.story_id is not None]
        return story[0] if story else CORE_BY_NAME.get(name)

    def rules(self) -> tuple[Implication, ...]:
        """Every declared implication: each predicate's own and the story's rules."""
        own = tuple(rule for p in self.predicates.values() for rule in p.implications)
        return own + self.story_rules


def core_registry() -> Registry:
    """The core vocabulary alone (no story)."""
    return Registry(None, 0, dict(CORE_BY_ID))


async def load(conn: AsyncConnection | AsyncEngine, story_id: str) -> Registry:
    """The story's registry (core + story predicates, its rules) in one read."""
    if isinstance(conn, AsyncEngine):
        async with conn.connect() as c:
            return await load(c, story_id)
    story = (
        await conn.execute(
            select(stories.c.config_revision, stories.c.predicate_rules).where(
                stories.c.id == story_id
            )
        )
    ).one_or_none()
    if story is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    rows = await conn.execute(
        select(predicates).where(
            (predicates.c.story_id.is_(None)) | (predicates.c.story_id == story_id)
        )
    )
    found = {p.id: p for p in (predicate_from_row(r) for r in rows)}
    rules = tuple(Implication.model_validate(r) for r in story.predicate_rules or ())
    return Registry(story_id, int(story.config_revision), found, rules)


# -- semantics helpers --------------------------------------------------------------------------


def canonical(proposition: Proposition, registry: Registry) -> Proposition:
    """``proposition`` in its predicate's canonical direction (see the module docstring)."""
    predicate = registry.get(proposition.predicate_id)
    if predicate is None or proposition.object_id is None:
        return proposition
    if predicate.inverse_of is not None:
        return proposition.model_copy(
            update={
                "predicate_id": predicate.inverse_of,
                "subject_id": proposition.object_id,
                "object_id": proposition.subject_id,
            }
        )
    if predicate.symmetric and proposition.object_id < proposition.subject_id:
        return proposition.model_copy(
            update={"subject_id": proposition.object_id, "object_id": proposition.subject_id}
        )
    return proposition


def direct_polarity_conflict(a: Proposition, b: Proposition) -> bool | None:
    """Whether ``a`` and ``b`` are the same normalized proposition with opposite polarity over
    overlapping time (FR-PROP-4): a logical conflict on any predicate, weak ones included.
    ``None`` when approximate bounds cannot decide the overlap."""
    if a.polarity is b.polarity or a.normalized_key() != b.normalized_key():
        return False
    return overlaps(a.valid_time, b.valid_time)


def cardinality_may_contradict(predicate: Predicate) -> bool:
    """Whether a second object for one key can ever be a conflict under ``predicate``: only for
    a functional predicate or one with a declared maximum; never for a weak one."""
    return not predicate.weak and predicate.max_cardinality is not None


def same_event(a: Proposition, b: Proposition, predicate: Predicate | None) -> bool:
    """Event identity (FR-PROP-1): a shared ``event_id``, or one functional event predicate's
    uniqueness key (a person's birth). Never time alone: two knocks at different hours of one
    day are two events, and a day-level claim of a knock identifies neither."""
    if a.event_id is not None and b.event_id is not None:
        return a.event_id == b.event_id
    if predicate is None or a.predicate_id != predicate.id or b.predicate_id != predicate.id:
        return False
    return (
        predicate.temporal_kind is TemporalKind.EVENT
        and predicate.cardinality is Cardinality.FUNCTIONAL
        and predicate.uniqueness_key(a) == predicate.uniqueness_key(b)
    )


# -- implication closure ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Closure:
    """``imply``'s answer: the propositions derived (not the inputs), the depth reached, and
    whether the depth cap stopped a derivation that could have continued."""

    derived: tuple[Proposition, ...]
    depth: int
    truncated: bool


def _lower(a: Bound | None, b: Bound | None) -> Bound | None:
    if a is None or b is None:
        return a or b
    return Bound(inner=max(a.inner, b.inner), outer=max(a.outer, b.outer))


def _upper(a: Bound | None, b: Bound | None) -> Bound | None:
    if a is None or b is None:
        return a or b
    return Bound(inner=min(a.inner, b.inner), outer=min(a.outer, b.outer))


def intersection(a: Interval | Instant, b: Interval | Instant) -> Interval | Instant | None:
    """The valid-time both ``a`` and ``b`` hold over (``None`` when it may be empty)."""
    if isinstance(a, Instant) or isinstance(b, Instant):
        instant, other = (a, b) if isinstance(a, Instant) else (b, a)
        assert isinstance(instant, Instant)  # noqa: S101 - one of the two is
        if isinstance(other, Instant):
            return instant if other == instant else None
        lo, hi = other.definite()
        inside = (lo is None or lo <= instant.at) and (hi is None or instant.until <= hi)
        return instant if inside else None
    start, end = _lower(a.start, b.start), _upper(a.end, b.end)
    if start is not None and end is not None and start.inner >= end.inner:
        return None
    try:
        return Interval(start=start, end=end)
    except InvalidRequest:
        return None


def check_rules(rules: Iterable[Implication]) -> None:
    """422 when the rules hold a cycle (``a ⇒ b ⇒ a``): a closure must terminate by design,
    not only by its depth cap."""
    graph: dict[str, set[str]] = {}
    for rule in rules:
        graph.setdefault(rule.when, set()).add(rule.then)
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        for nxt in graph.get(node, ()):
            if state.get(nxt) == 1:
                raise InvalidRequest(
                    "implication rules must not form a cycle",
                    {"field": "starting_state.rules", "reason": "rule_cycle", "predicate": nxt},
                )
            if nxt not in state:
                visit(nxt)
        state[node] = 2

    for node in list(graph):
        if node not in state:
            visit(node)


def _apply(rule: Implication, known: Sequence[Proposition]) -> list[Proposition]:
    out: list[Proposition] = []
    affirmed = [p for p in known if p.polarity is Polarity.AFFIRMED]
    for p in affirmed:
        if p.predicate_id != rule.when:
            continue
        targets: list[tuple[str | None, Interval | Instant | None]] = []
        if rule.via is None:
            targets.append((p.object_id, p.valid_time))
        elif p.object_id is not None:
            for join in affirmed:
                if join.predicate_id == rule.via and join.subject_id == p.object_id:
                    targets.append((join.object_id, intersection(p.valid_time, join.valid_time)))
        for object_id, valid_time in targets:
            if object_id is None or valid_time is None:
                continue
            out.append(
                Proposition(
                    subject_id=p.subject_id,
                    predicate_id=rule.then,
                    object_id=object_id,
                    valid_time=valid_time,
                )
            )
    return out


def imply(
    propositions: Sequence[Proposition],
    rules: Sequence[Implication],
    max_depth: int = IMPLICATION_MAX_DEPTH,
) -> Closure:
    """The bounded closure of ``propositions`` under the declared ``rules`` (see the module
    docstring). Pure: no registry read, no inference beyond the rules."""
    known: list[Proposition] = list(propositions)
    seen = {(p.normalized_key(), p.valid_time) for p in known}
    derived: list[Proposition] = []
    depth = 0
    while True:
        fresh: list[Proposition] = []
        for rule in rules:
            for candidate in _apply(rule, known):
                key = (candidate.normalized_key(), candidate.valid_time)
                if key not in seen:
                    seen.add(key)
                    fresh.append(candidate)
        if not fresh:
            return Closure(tuple(derived), depth, truncated=False)
        if depth >= max_depth:
            return Closure(tuple(derived), depth, truncated=True)
        depth += 1
        derived.extend(fresh)
        known.extend(fresh)


# -- owner lookups (#191) ----------------------------------------------------------------------


class OwnerLookup(Protocol):
    """The canon of an **owner-lookup** predicate (#191, ADR-160's owner paths): a value its owner
    holds (a story State's fold, ``core.states.verify``; #195's clock and #207's external values
    next), never a ``facts`` row. #140's ``check_with`` reads it for a canon holder only, through
    ``core.readset.KeyedLookup.owned`` (keyed, re-checked at the rebase), as the holder's
    viewpoint may read it (a value hidden from it is not read), and ``check`` hands the claim to
    ``verdict``; a ``belief(A)`` holder never consults it (ADR-167). ``implied`` turns a
    declared rule's consequence on the predicate (``holds ⇒ state_value``) into the value its
    cause states, or ``None`` when it states none."""

    async def read(
        self,
        conn: AsyncConnection,
        story_id: str,
        subjects: Sequence[str] | None,
        through: Position | None,
        viewpoint: str,
    ) -> list[OwnedValue]: ...

    def verdict(self, claim: Proposition, owned: Sequence[OwnedValue]) -> Verdict: ...

    def implied(
        self, consequence: Proposition, cause: Proposition, owned: Sequence[OwnedValue]
    ) -> Proposition | None: ...


OWNER_LOOKUPS: dict[str, OwnerLookup] = {}
"""The owner lookups by predicate id (``register_owner_lookup``)."""

OwnerActive = Callable[[AsyncConnection, str], Awaitable[bool]]
"""Whether a conditional owner lookup answers for a story (#195: the clock, while it runs)."""
OWNER_ACTIVE: dict[str, OwnerActive] = {}
"""The conditional owner lookups' tests, by predicate id (``register_owner_lookup(active=)``)."""


def register_owner_lookup(
    predicate_id: str, lookup: OwnerLookup, *, active: OwnerActive | None = None
) -> OwnerLookup:
    """Make ``predicate_id`` an owner-lookup predicate answered by ``lookup``. With ``active``
    the lookup is **conditional** (#195: ``time_is`` is the clock's only while the story runs
    the clock, and an ordinary fact-backed predicate otherwise, as in M4): ``check_with`` reads
    the owner only for a story ``active`` admits, and a pure ``check`` hands the claim to the
    lookup only when it is given the owner's values."""
    OWNER_LOOKUPS[predicate_id] = lookup
    if active is not None:
        OWNER_ACTIVE[predicate_id] = active
    else:
        OWNER_ACTIVE.pop(predicate_id, None)
    return lookup


def owner_lookup(predicate_id: str) -> OwnerLookup | None:
    """The owner lookup of ``predicate_id``, or ``None`` for a predicate whose canon is facts."""
    return OWNER_LOOKUPS.get(predicate_id)


def conditional_owner(predicate_id: str) -> bool:
    """``predicate_id``'s owner lookup answers only for some stories (``register_owner_lookup``'s
    ``active``)."""
    return predicate_id in OWNER_ACTIVE


async def owner_active(predicate_id: str, conn: AsyncConnection, story_id: str) -> bool:
    """``predicate_id`` is an owner-lookup predicate for ``story_id``: registered, and
    unconditional or admitted by its ``active`` test."""
    if predicate_id not in OWNER_LOOKUPS:
        return False
    test = OWNER_ACTIVE.get(predicate_id)
    return True if test is None else await test(conn, story_id)


# -- author declarations (#108's loose lists, typed) --------------------------------------------


class _Decl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


PredicateName = Field(min_length=1, max_length=NAME_MAX, pattern=r"^[^\W\d][\w]*$")


class PredicateDecl(_Decl):
    """An author-declared predicate: its name (letters, digits and ``_``, not a core name) and
    full semantics; ``exclusions``, ``containment_of`` and ``inverse_of`` name predicates (the
    story's own or core ones)."""

    name: str = PredicateName
    subject_type: str = ANY
    object_type: str = ANY
    cardinality: Cardinality = Cardinality.MULTI
    max_cardinality: int | None = Field(default=None, ge=1)
    key_qualifiers: tuple[str, ...] = ()
    qualifiers: dict[str, QualifierKind] = Field(default_factory=dict)
    set_object: bool = False
    temporal_kind: TemporalKind = TemporalKind.FLUENT
    exclusions: tuple[str, ...] = ()
    containment_of: str | None = None
    symmetric: bool = False
    inverse_of: str | None = None
    owner: PredicateOwner = PredicateOwner.STORY
    projection: str | None = None
    docs: str = Field(default="", max_length=2_000)


class ClosedSetDecl(_Decl):
    """An author's closed set (FR-PROP-8): the listed ``members`` are all the objects of
    ``predicate`` for the key — the ``subject`` and the ``key`` qualifiers the predicate names —
    over ``[valid_from, valid_to)`` (story-minutes; open when omitted). Subjects and members are
    scenario references (a role or entity id) structured genesis resolves to entity ids.

    With ``key_set`` (one of the predicate's key qualifiers, #441) the set closes that
    qualifier's values for the subject instead, and ``members`` are those values, never
    resolved: "Bobby's only undergraduate degree is ``bobby_ba``" is ``{predicate:
    degree_awarded, subject: bobby, key_set: degree_id, key: {level: bachelor}, members:
    [bobby_ba]}``; ``key`` then names any of the predicate's other qualifiers."""

    predicate: str = Field(min_length=1, max_length=NAME_MAX)
    subject: str = Field(min_length=1)
    key: dict[str, str | int | bool] = Field(default_factory=dict)
    members: tuple[str | int, ...] = Field(min_length=1)
    valid_from: int | None = None
    valid_to: int | None = None
    key_set: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)

    def entity_refs(self) -> tuple[str, ...]:
        """The references genesis resolves: the subject, and an object set's members."""
        if self.key_set is not None:
            return (self.subject,)
        return (self.subject, *(str(m) for m in self.members))

    def resolved_members(self, resolve: Callable[[str], str]) -> tuple[Any, ...]:
        """The members as stored: entity ids for an object set, the values for a key set."""
        if self.key_set is not None:
            return self.members
        return tuple(resolve(str(m)) for m in self.members)


class ImplicationRule(_Decl):
    """An author's rule ``when ⇒ then`` (same subject; the object carries over, or maps through
    one ``via`` join): "varsity membership implies enrollment at that institution" is
    ``{when: member_of, then: enrolled_at, via: part_of}``."""

    when: str = Field(min_length=1, max_length=NAME_MAX)
    then: str = Field(min_length=1, max_length=NAME_MAX)
    via: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)


class ScenarioRules(_Decl):
    """A scenario's story rules, typed: what ``starting_state.predicates``, ``closed_sets`` and
    ``rules`` hold."""

    predicates: tuple[PredicateDecl, ...] = ()
    closed_sets: tuple[ClosedSetDecl, ...] = ()
    rules: tuple[ImplicationRule, ...] = ()

    @classmethod
    def parse(
        cls, document: Mapping[str, Any], *, prefix: tuple[str | int, ...] = ("starting_state",)
    ) -> ScenarioRules:
        """The rules of a ``starting_state`` document (its three lists; the rest is ignored);
        422 ``validation_error`` at the failing field, and for a name a core predicate has or
        a predicate declared twice."""
        wanted = {name: list(document.get(name) or ()) for name in RULE_FIELDS}
        try:
            found = cls.model_validate(wanted)
        except ValidationError as exc:
            raise InvalidRequest.from_errors(exc.errors(), prefix=prefix) from None
        names: set[str] = set()
        for index, decl in enumerate(found.predicates):
            where = ".".join(str(p) for p in prefix)
            field_ref = f"{where}.predicates[{index}].name"
            if decl.name in CORE_BY_NAME:
                raise InvalidRequest(
                    f"{decl.name!r} is a core predicate; an author predicate needs its own name",
                    {"field": field_ref, "reason": "core_predicate_name"},
                )
            if decl.name in names:
                raise InvalidRequest(
                    f"{decl.name!r} is declared twice",
                    {"field": field_ref, "reason": "predicate_declared_twice"},
                )
            names.add(decl.name)
        return found

    @property
    def empty(self) -> bool:
        return not (self.predicates or self.closed_sets or self.rules)


def rules_from_snapshot(snapshot: Mapping[str, Any] | None) -> ScenarioRules:
    """The story rules of a ``stories.scenario_snapshot`` (none without a snapshot)."""
    state = (snapshot or {}).get("starting_state") or {}
    return ScenarioRules.parse(state if isinstance(state, Mapping) else {})


_DEFERRED_REF = re.compile(r"^starting_state\.(predicates|closed_sets|rules)\[(\d+)\]$")


async def rules_from_deferred(conn: AsyncConnection, story_id: str) -> ScenarioRules:
    """The story rules a story created without the structured pipeline kept as
    ``genesis_deferred`` rows (``capability_needed = structured``), in their scenario order."""
    rows = await conn.execute(
        select(genesis_deferred.c.field_ref, genesis_deferred.c.payload).where(
            genesis_deferred.c.story_id == story_id,
            genesis_deferred.c.capability_needed == STRUCTURED,
            genesis_deferred.c.field_ref.like("starting_state.%"),
        )
    )
    lists: dict[str, list[tuple[int, Any]]] = {name: [] for name in RULE_FIELDS}
    for row in rows:
        match = _DEFERRED_REF.match(str(row.field_ref))
        if match is None:
            continue
        payload = row.payload if isinstance(row.payload, Mapping) else {}
        lists[match.group(1)].append((int(match.group(2)), payload.get("value")))
    return ScenarioRules.parse(
        {name: [value for _, value in sorted(items)] for name, items in lists.items()}
    )


# -- writes -------------------------------------------------------------------------------------


async def sync_core(db: AsyncEngine | AsyncConnection) -> int:
    """Insert or update every core predicate from ``vocabulary`` (idempotent); answers how many
    rows changed. Run at every start."""
    if isinstance(db, AsyncEngine):
        async with db.begin() as conn:
            return await sync_core(conn)
    changed = 0
    for predicate in CORE_PREDICATES:
        row = predicate_row(predicate)
        statement = pg_insert(predicates).values(**row)
        excluded = statement.excluded
        semantic = {name: excluded[name] for name in SEMANTIC_COLUMNS}
        differs = func.row(*(predicates.c[n] for n in SEMANTIC_COLUMNS)).is_distinct_from(
            func.row(*(excluded[n] for n in SEMANTIC_COLUMNS))
        )
        result = await db.execute(
            statement.on_conflict_do_update(
                index_elements=[predicates.c.id], set_=semantic, where=differs
            ).returning(predicates.c.id)
        )
        changed += len(result.all())
    log.debug("core vocabulary re-synced: %d rows changed", changed)
    return changed


async def _index_predicates(conn: AsyncConnection, rows: Sequence[Mapping[str, Any]]) -> None:
    records = [predicate_record(SimpleNamespace(**row)) for row in rows]
    await guard.check_content(
        conn,
        str(rows[0]["story_id"]),
        [guard.Content(tuple(v for v in r.fields.values() if v)) for r in records],
        base_revision=None,
        path=__name__,
    )
    await index_records(conn, records)


async def _insert_predicates(conn: AsyncConnection, chosen: Sequence[Predicate]) -> None:
    rows = [predicate_row(p) for p in chosen]
    if not rows:
        return
    await _index_predicates(conn, rows)
    await conn.execute(insert(predicates), rows)


def _ref(names: Mapping[str, str], name: str, field_ref: str) -> str:
    found = names.get(name) or (CORE_BY_NAME[name].id if name in CORE_BY_NAME else None)
    if found is None:
        raise InvalidRequest(
            f"{name!r} is not a declared or core predicate",
            {"field": field_ref, "reason": "unknown_predicate"},
        )
    return found


Resolver = Callable[[str], str]
"""Maps a scenario reference (a role or entity id) to the story's entity id."""


@dataclass(frozen=True, slots=True)
class AuthorLayer:
    """A scenario's story rules as the registry would hold them, before anything is written:
    the author predicates (with their ids), the implication rules over predicate ids, and
    every declared or known name's id."""

    predicates: tuple[Predicate, ...]
    implications: tuple[Implication, ...]
    names: Mapping[str, str]

    def registry(self, existing: Registry) -> Registry:
        """``existing`` with this layer added (in memory: #149's genesis plan and publish
        check normalize against it)."""
        return Registry(
            existing.story_id,
            existing.config_revision,
            {**existing.predicates, **{p.id: p for p in self.predicates}},
            (*existing.story_rules, *self.implications),
        )


def author_layer(
    story_id: str | None,
    rules: ScenarioRules,
    existing: Registry,
    *,
    ids: Mapping[str, str] | None = None,
) -> AuthorLayer:
    """The author layer ``rules`` declare over ``existing`` (pure; see ``register_author``):
    422 ``validation_error`` for a name the story already has, a reference to a predicate that
    is neither declared nor core (in a predicate, a rule or a closed set) and a rule cycle.
    ``ids`` fixes the new predicates' ids by name (default: new ids)."""
    known = {p.name: p.id for p in existing.predicates.values() if p.story_id is not None}
    for index, decl in enumerate(rules.predicates):
        if decl.name in known:
            raise InvalidRequest(
                f"the story already has a predicate {decl.name!r}",
                {
                    "field": f"starting_state.predicates[{index}].name",
                    "reason": "predicate_declared_twice",
                },
            )
    fixed = dict(ids or {})
    names = {
        **known,
        **{decl.name: fixed.get(decl.name) or new_ulid() for decl in rules.predicates},
    }
    chosen: list[Predicate] = []
    for index, decl in enumerate(rules.predicates):
        where = f"starting_state.predicates[{index}]"
        try:
            chosen.append(
                Predicate(
                    id=names[decl.name],
                    story_id=story_id,
                    name=decl.name,
                    layer=Layer.AUTHOR,
                    subject_type=decl.subject_type,
                    object_type=decl.object_type,
                    cardinality=decl.cardinality,
                    max_cardinality=decl.max_cardinality,
                    key_qualifiers=decl.key_qualifiers,
                    qualifiers=decl.qualifiers,
                    set_object=decl.set_object,
                    temporal_kind=decl.temporal_kind,
                    exclusions=tuple(
                        _ref(names, n, f"{where}.exclusions") for n in decl.exclusions
                    ),
                    containment_of=None
                    if decl.containment_of is None
                    else _ref(names, decl.containment_of, f"{where}.containment_of"),
                    symmetric=decl.symmetric,
                    inverse_of=None
                    if decl.inverse_of is None
                    else _ref(names, decl.inverse_of, f"{where}.inverse_of"),
                    owner=decl.owner,
                    projection=decl.projection,
                    docs=decl.docs,
                )
            )
        except ValidationError as exc:
            raise InvalidRequest.from_errors(
                exc.errors(), prefix=("starting_state", "predicates", index)
            ) from None
    implications = [
        Implication(
            when=_ref(names, rule.when, f"starting_state.rules[{i}].when"),
            then=_ref(names, rule.then, f"starting_state.rules[{i}].then"),
            via=None
            if rule.via is None
            else _ref(names, rule.via, f"starting_state.rules[{i}].via"),
        )
        for i, rule in enumerate(rules.rules)
    ]
    for index, closed in enumerate(rules.closed_sets):
        _ref(names, closed.predicate, f"starting_state.closed_sets[{index}].predicate")
    check_rules([*existing.rules(), *implications])
    return AuthorLayer(tuple(chosen), tuple(implications), names)


async def register_author(
    conn: AsyncConnection,
    story_id: str,
    rules: ScenarioRules,
    *,
    resolve: Resolver | None = None,
    ids: Mapping[str, str] | None = None,
) -> Registry:
    """Register a scenario's story rules in the story's registry with full semantics (#149
    calls this at structured genesis, in its transaction): the predicates (layer ``author``),
    the implication rules (``stories.predicate_rules``) and the closed sets
    (``asserted_by = author``; ``resolve`` maps their references to entity ids). ``ids`` fixes
    the new predicates' ids by name (#149's genesis plan normalized its seeds against them).
    Part of the story's creation, so no configuration change is recorded."""
    from epistrel.core.propositions import store  # the store imports this module

    existing = await load(conn, story_id)
    layer = author_layer(story_id, rules, existing, ids=ids)
    chosen = list(layer.predicates)
    implications = list(layer.implications)
    names = dict(layer.names)
    await _insert_predicates(conn, chosen)
    if implications:
        await conn.execute(
            update(stories)
            .where(stories.c.id == story_id)
            .values(
                predicate_rules=[
                    *(r.model_dump(mode="json", exclude_none=True) for r in existing.story_rules),
                    *(r.model_dump(mode="json", exclude_none=True) for r in implications),
                ]
            )
        )
    registry = await load(conn, story_id)
    for index, closed in enumerate(rules.closed_sets):
        predicate = registry.get(
            _ref(names, closed.predicate, f"starting_state.closed_sets[{index}].predicate")
        )
        assert predicate is not None  # noqa: S101 - _ref found it
        resolved = resolve or (lambda ref: ref)
        await store.declare_closed_set(
            conn,
            story_id,
            store.ClosedSet(
                predicate_id=predicate.id,
                subject_id=resolved(closed.subject),
                key=closed.key,
                members=closed.resolved_members(resolved),
                valid_from=closed.valid_from,
                valid_to=closed.valid_to,
                key_set=closed.key_set,
            ),
            registry,
            asserted_by="author",
            field_ref=f"starting_state.closed_sets[{index}]",
        )
    log.debug(
        "author layer registered for story %s: %d predicates, %d rules, %d closed sets",
        story_id,
        len(chosen),
        len(implications),
        len(rules.closed_sets),
    )
    return registry


_NAME_SEPARATORS = re.compile(r"[\W_]+")


def auto_name(relation: str) -> str:
    """The auto predicate name of a relation's text: casefolded, runs of anything but letters
    and digits as ``_``, trimmed. 422 when nothing is left."""
    name = _NAME_SEPARATORS.sub("_", relation.casefold()).strip("_")[:NAME_MAX]
    if not name or name[0].isdigit():
        raise InvalidRequest(
            "a relation needs a name that starts with a letter",
            {"field": "relation", "reason": "unnamed_relation"},
        )
    return name


async def _story_revisions(
    conn: AsyncConnection, story_id: str, *, writable: bool = False
) -> tuple[int, int]:
    """The story's configuration and timeline revisions, under its row lock; ``writable``: 409
    ``story_archived`` for an archived story (its configuration is read-only)."""
    row = (
        await conn.execute(
            select(stories.c.config_revision, stories.c.timeline_revision, stories.c.status)
            .where(stories.c.id == story_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
    if writable and row.status == StoryStatus.ARCHIVED.value:
        raise StoryArchived(
            f"story {story_id} is archived; unarchive it to change it", {"story_id": story_id}
        )
    return int(row.config_revision), int(row.timeline_revision)


async def _named(conn: AsyncConnection, story_id: str, name: str) -> Predicate | None:
    found = (
        await conn.execute(
            select(predicates).where(predicates.c.story_id == story_id, predicates.c.name == name)
        )
    ).one_or_none()
    return None if found is None else predicate_from_row(found)


async def auto_register(
    conn: AsyncConnection,
    story_id: str,
    relation: str,
    *,
    subject_type: str = ANY,
    object_type: str = ANY,
    actor: str = ENGINE_PRINCIPAL,
) -> Predicate:
    """The predicate a render's ``relation`` names: an existing one (a core or story predicate
    of that name), or a new auto predicate with the weakest semantics, registered as a story
    configuration change in the caller's transaction (see the module docstring)."""
    name = auto_name(relation)
    core = CORE_BY_NAME.get(name)
    if core is not None:
        return core
    found = await _named(conn, story_id, name)
    if found is not None:
        return found
    # Under the story's lock, a turn that registered it meanwhile has committed: read again.
    revision = (await _story_revisions(conn, story_id))[0] + 1
    found = await _named(conn, story_id, name)
    if found is not None:
        return found
    predicate = Predicate(
        id=new_ulid(),
        story_id=story_id,
        name=name,
        layer=Layer.AUTO,
        subject_type=subject_type,
        object_type=object_type,
        qualifiers=None,
        docs="",
    )
    row = predicate_row(predicate)
    inserted = await conn.execute(
        pg_insert(predicates)
        .values(**row)
        .on_conflict_do_nothing(index_elements=list(_PREDICATE_SCOPE_NAME))
        .returning(predicates.c.id)
    )
    if inserted.scalar_one_or_none() is None:  # pragma: no cover - the lock serialises racers
        raced = await _named(conn, story_id, name)
        assert raced is not None  # noqa: S101 - the conflicting row exists
        return raced
    await conn.execute(
        update(stories).where(stories.c.id == story_id).values(config_revision=revision)
    )
    await _index_predicates(conn, [row])
    own_predicates.note(predicate.id)
    await record_config_change(
        conn,
        config_revision=revision,
        setting=setting_of(predicate.id),
        old=None,
        new={"name": name, "layer": Layer.AUTO.value},
        actor=actor,
        change_class=ConfigClass.interpretive,
        scope=ConfigScope.STORY,
        scope_id=story_id,
    )
    log.debug("auto predicate %s registered in story %s", predicate.id, story_id)
    return predicate


# -- proposals ----------------------------------------------------------------------------------


class ProposedChanges(BaseModel):
    """The semantics a proposal would give an auto predicate; each field left out is
    unchanged."""

    model_config = ConfigDict(extra="forbid")

    cardinality: Cardinality | None = None
    max_cardinality: int | None = Field(default=None, ge=1)
    key_qualifiers: tuple[str, ...] | None = None
    qualifiers: dict[str, QualifierKind] | None = None
    temporal_kind: TemporalKind | None = None
    exclusions: tuple[str, ...] | None = None
    containment_of: str | None = None
    symmetric: bool | None = None

    def fields(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_unset=True, exclude_none=True)


PROPOSABLE = tuple(ProposedChanges.model_fields)


def strengthened(predicate: Predicate, changes: Mapping[str, Any]) -> Predicate:
    """``predicate`` with ``changes`` applied (pure); 422 when they do not make a valid
    predicate."""
    data = predicate.model_dump()
    data.update(changes)
    if changes.get("cardinality") == Cardinality.FUNCTIONAL.value:
        data["max_cardinality"] = 1
    if "qualifiers" in changes and predicate.qualifiers is None:
        data["qualifiers"] = changes["qualifiers"]
    try:
        return Predicate.model_validate(data)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body", "changes")) from None


async def _story_predicate(conn: AsyncConnection, story_id: str, predicate_id: str) -> Predicate:
    row = (
        await conn.execute(
            select(predicates).where(
                predicates.c.id == predicate_id, predicates.c.story_id == story_id
            )
        )
    ).one_or_none()
    if row is None:
        raise PredicateNotFound(
            f"predicate {predicate_id} is not one of story {story_id}'s",
            {"story_id": story_id, "predicate_id": predicate_id},
        )
    return predicate_from_row(row)


async def file_proposal(
    conn: AsyncConnection,
    story_id: str,
    predicate_id: str,
    changes: ProposedChanges,
    *,
    filed_by: str = ENGINE_PRINCIPAL,
    evidence: Mapping[str, Any] | None = None,
) -> str:
    """File a proposal to strengthen the story's auto predicate (or answer the pending one's
    id). It changes nothing until approved. 422 for a predicate that is not auto, or changes
    that are empty or invalid."""
    predicate = await _story_predicate(conn, story_id, predicate_id)
    if predicate.layer is not Layer.AUTO:
        raise InvalidRequest(
            "only an auto-registered predicate takes proposals",
            {"field": "predicate_id", "reason": "not_auto"},
        )
    wanted = changes.fields()
    if not wanted:
        raise InvalidRequest("a proposal changes something", {"field": "changes"})
    strengthened(predicate, wanted)  # refuses a proposal that would not be a valid predicate
    proposal_id = new_ulid()
    proposed = {"changes": wanted, "evidence": dict(evidence or {})}
    inserted = await conn.execute(
        pg_insert(predicate_proposals)
        .values(
            id=proposal_id,
            story_id=story_id,
            predicate_id=predicate_id,
            proposed=proposed,
            filed_by=filed_by,
        )
        .on_conflict_do_nothing(
            index_elements=[predicate_proposals.c.predicate_id],
            index_where=_PROPOSAL_PENDING,
        )
        .returning(predicate_proposals.c.id)
    )
    if inserted.scalar_one_or_none() is None:
        pending = await conn.scalar(
            select(predicate_proposals.c.id).where(
                predicate_proposals.c.predicate_id == predicate_id,
                predicate_proposals.c.status == "pending",
            )
        )
        return str(pending)
    await index_records(
        conn,
        [
            proposal_record(
                SimpleNamespace(id=proposal_id, story_id=story_id, proposed=proposed, decision=None)
            )
        ],
    )
    log.debug("proposal %s filed for predicate %s", proposal_id, predicate_id)
    return proposal_id


IDENTITY = "identity"
"""``predicate_proposals.kind`` of an identity proposal (#171)."""
IdentityReason = Literal["canon_same_as", "uncertain_match", "characters"]
"""Why an identity proposal was filed: a canon ``same_as`` made two entities one; #163's
modeling minted a character for a description that uncertainly matched an existing one; or a
canon ``same_as`` between two modeled characters was refused (merging them is M7's)."""


async def file_identity_proposal(
    conn: AsyncConnection,
    story_id: str,
    pair: tuple[str, str],
    *,
    reason: IdentityReason,
    fact_ids: Sequence[str] = (),
    evidence: Mapping[str, Any] | None = None,
    filed_by: str = ENGINE_PRINCIPAL,
) -> str:
    """File an identity proposal (FR-PROP-7, #171) for ``pair`` in the caller's transaction, or
    answer the pending one's id (one per pair): a record that two entities may be one referent,
    never a merge (M7's amendments merge and split). ``fact_ids`` are the canon ``same_as``
    facts it rests on."""
    a, b = sorted(pair)
    proposal_id = new_ulid()
    proposed = {
        "entities": [a, b],
        "reason": reason,
        "evidence": {"fact_ids": list(fact_ids), **dict(evidence or {})},
    }
    inserted = await conn.execute(
        pg_insert(predicate_proposals)
        .values(
            id=proposal_id,
            story_id=story_id,
            predicate_id=SAME_AS,
            proposed=proposed,
            filed_by=filed_by,
            kind=IDENTITY,
            subject_entity_id=a,
            object_entity_id=b,
        )
        .on_conflict_do_nothing(
            index_elements=[
                predicate_proposals.c.story_id,
                predicate_proposals.c.subject_entity_id,
                predicate_proposals.c.object_entity_id,
            ],
            index_where=_IDENTITY_PENDING,
        )
        .returning(predicate_proposals.c.id)
    )
    if inserted.scalar_one_or_none() is None:
        pending = await conn.scalar(
            select(predicate_proposals.c.id).where(
                predicate_proposals.c.story_id == story_id,
                predicate_proposals.c.kind == IDENTITY,
                predicate_proposals.c.subject_entity_id == a,
                predicate_proposals.c.object_entity_id == b,
                predicate_proposals.c.status == "pending",
            )
        )
        return str(pending)
    await index_records(
        conn,
        [
            proposal_record(
                SimpleNamespace(id=proposal_id, story_id=story_id, proposed=proposed, decision=None)
            )
        ],
    )
    log.debug("identity proposal %s filed (%s)", proposal_id, reason)
    return proposal_id


async def identity_proposals(conn: AsyncConnection, story_id: str) -> list[dict[str, Any]]:
    """The story's identity proposals (#171), oldest first: id, pair, status, reason,
    evidence and filer."""
    rows = await conn.execute(
        select(predicate_proposals)
        .where(
            predicate_proposals.c.story_id == story_id,
            predicate_proposals.c.kind == IDENTITY,
        )
        .order_by(predicate_proposals.c.id)
    )
    return [
        {
            "id": str(r.id),
            "entities": [str(r.subject_entity_id), str(r.object_entity_id)],
            "status": r.status,
            "reason": dict(r.proposed or {}).get("reason"),
            "evidence": dict(r.proposed or {}).get("evidence") or {},
            "filed_by": r.filed_by,
        }
        for r in rows
    ]


async def single_valued_observations(
    conn: AsyncConnection, story_id: str, predicate_id: str
) -> int | None:
    """How many affirmed propositions of the predicate the story holds when each subject has
    one object across them all (``None``: some subject has two)."""
    rows = await conn.execute(
        select(propositions).where(
            propositions.c.story_id == story_id,
            propositions.c.predicate_id == predicate_id,
            propositions.c.polarity == Polarity.AFFIRMED.value,
        )
    )
    objects: dict[str, set[Any]] = {}
    count = 0
    for row in rows:
        found = proposition_from_row(row)
        objects.setdefault(found.subject_id, set()).add(found.object_key())
        count += 1
    if any(len(values) > 1 for values in objects.values()):
        return None
    return count


async def consider_proposal(conn: AsyncConnection, story_id: str, predicate_id: str) -> str | None:
    """File a ``functional`` proposal for the story's auto predicate once its observations are
    ``PROPOSAL_OBSERVATIONS`` consistent single-valued ones; answers the proposal id (``None``:
    not yet, not a weak auto predicate, or the author already rejected this proposal). A pending
    proposal's id is answered as it is."""
    predicate = await _story_predicate(conn, story_id, predicate_id)
    if predicate.layer is not Layer.AUTO or not predicate.weak:
        return None
    changes = ProposedChanges(cardinality=Cardinality.FUNCTIONAL)
    earlier = await conn.execute(
        select(
            predicate_proposals.c.id, predicate_proposals.c.status, predicate_proposals.c.proposed
        ).where(
            predicate_proposals.c.story_id == story_id,
            predicate_proposals.c.predicate_id == predicate_id,
            predicate_proposals.c.status.in_(("pending", "rejected")),
        )
    )
    found = earlier.all()
    for row in found:
        if row.status == "pending":
            return str(row.id)
    if any(dict(row.proposed or {}).get("changes") == changes.fields() for row in found):
        return None  # the author said no: a later observation does not re-open it
    observed = await single_valued_observations(conn, story_id, predicate_id)
    if observed is None or observed < PROPOSAL_OBSERVATIONS:
        return None
    return await file_proposal(
        conn,
        story_id,
        predicate_id,
        changes,
        evidence={"single_valued_observations": observed},
    )


async def consider_proposals(
    conn: AsyncConnection, story_id: str, predicate_ids: Iterable[str], registry: Registry
) -> tuple[str, ...]:
    """``consider_proposal`` for each of the story's weak auto predicates among
    ``predicate_ids`` (the predicates of the propositions a commit wrote; core and author
    predicates take no proposals and are skipped), in the caller's transaction; answers the
    pending proposals' ids. A structured turn's commit runs it (#491)."""
    filed: list[str] = []
    for predicate_id in dict.fromkeys(predicate_ids):
        predicate = registry.get(predicate_id)
        if predicate is None or predicate.layer is not Layer.AUTO or not predicate.weak:
            continue
        found = await consider_proposal(conn, story_id, predicate_id)
        if found is not None:
            filed.append(found)
    return tuple(filed)


class ApproveRequest(BaseModel):
    """``POST /stories/{id}/predicates/{predicate_id}/approve``: approve the whole pending
    proposal (``changes`` omitted) or the fields it names."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{}, {"changes": ["cardinality"]}]},
    )

    changes: list[str] | None = Field(default=None, min_length=1)


class Violation(BaseModel):
    """Existing propositions the approved semantics would flag (reported, never changed): a key
    with two objects over overlapping time."""

    subject_id: str
    proposition_ids: list[str]
    rule: Literal["cardinality"] = "cardinality"


@dataclass(frozen=True, slots=True)
class Decision:
    """An approval's or rejection's answer: the predicate as it now is, the proposal, its new
    status, the story's configuration revision, and (approval) the violations found."""

    predicate: Predicate
    proposal_id: str
    status: str
    config_revision: int
    timeline_revision: int
    applied: dict[str, Any] = field(default_factory=dict)
    violations: tuple[Violation, ...] = ()
    replayed: bool = False


async def _pending(conn: AsyncConnection, story_id: str, predicate_id: str) -> Any:
    row = (
        await conn.execute(
            select(predicate_proposals)
            .where(
                predicate_proposals.c.story_id == story_id,
                predicate_proposals.c.predicate_id == predicate_id,
                predicate_proposals.c.status == "pending",
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise ProposalNotFound(
            f"predicate {predicate_id} has no pending proposal",
            {"story_id": story_id, "predicate_id": predicate_id, "reason": "no_pending_proposal"},
        )
    return row


async def violations(
    conn: AsyncConnection, story_id: str, predicate: Predicate
) -> tuple[Violation, ...]:
    """The story's affirmed propositions of ``predicate`` that break its cardinality: per
    uniqueness key, more objects than its maximum over overlapping time."""
    if predicate.max_cardinality is None:
        return ()
    rows = await conn.execute(
        select(propositions)
        .where(
            propositions.c.story_id == story_id,
            propositions.c.predicate_id == predicate.id,
            propositions.c.polarity == Polarity.AFFIRMED.value,
        )
        .order_by(propositions.c.id)
    )
    by_key: dict[Any, list[tuple[str, Proposition]]] = {}
    for row in rows:
        found = proposition_from_row(row)
        by_key.setdefault(predicate.uniqueness_key(found), []).append((str(row.id), found))
    out: list[Violation] = []
    for (subject_id, _), items in by_key.items():
        clashing: set[str] = set()
        for i, (id_a, a) in enumerate(items):
            for id_b, b in items[i + 1 :]:
                if a.object_key() != b.object_key() and overlaps(a.valid_time, b.valid_time):
                    clashing.update((id_a, id_b))
        if len(clashing) > predicate.max_cardinality:
            out.append(Violation(subject_id=subject_id, proposition_ids=sorted(clashing)))
    return tuple(out)


async def approve(
    db: AsyncEngine,
    story_id: str,
    predicate_id: str,
    changes: Sequence[str] | None,
    *,
    principal: str,
) -> Decision:
    """Apply the predicate's pending proposal (all of it, or the fields ``changes`` names) as an
    interpretive story configuration change; report the propositions it would flag. 404 when
    the predicate is not the story's or has no pending proposal; 422 for a field the proposal
    does not hold."""
    async with db.begin() as conn:
        revision, timeline_revision = await _story_revisions(conn, story_id, writable=True)
        predicate = await _story_predicate(conn, story_id, predicate_id)
        row = await _pending(conn, story_id, predicate_id)
        proposed = dict(row.proposed.get("changes") or {})
        names = list(proposed) if changes is None else list(dict.fromkeys(changes))
        unknown = [name for name in names if name not in proposed]
        if unknown or not names:
            raise InvalidRequest(
                "approve a subset of the proposal's fields",
                {"field": "body.changes", "reason": "not_proposed", "fields": unknown},
            )
        applied = {name: proposed[name] for name in names}
        after = strengthened(predicate, applied)
        new_row = predicate_row(after)
        revision += 1
        await conn.execute(
            update(stories).where(stories.c.id == story_id).values(config_revision=revision)
        )
        await conn.execute(
            update(predicates)
            .where(predicates.c.id == predicate_id)
            .values(**{name: new_row[name] for name in SEMANTIC_COLUMNS})
        )
        before_row = predicate_row(predicate)
        moved = [name for name in SEMANTIC_COLUMNS if before_row[name] != new_row[name]]
        await record_config_change(
            conn,
            config_revision=revision,
            setting=setting_of(predicate_id),
            old={name: before_row[name] for name in moved},
            new={name: new_row[name] for name in moved},
            actor=principal,
            change_class=ConfigClass.interpretive,
            scope=ConfigScope.STORY,
            scope_id=story_id,
        )
        found = await violations(conn, story_id, after)
        decision = {
            "applied": applied,
            "violations": [v.model_dump(mode="json") for v in found],
        }
        await _decide(conn, row.id, story_id, row.proposed, "approved", principal, decision)
    log.debug("proposal %s approved: %d violations reported", row.id, len(found))
    return Decision(after, str(row.id), "approved", revision, timeline_revision, applied, found)


async def reject(db: AsyncEngine, story_id: str, predicate_id: str, *, principal: str) -> Decision:
    """Close the predicate's pending proposal unapplied (no configuration change)."""
    async with db.begin() as conn:
        revision, timeline_revision = await _story_revisions(conn, story_id, writable=True)
        predicate = await _story_predicate(conn, story_id, predicate_id)
        row = await _pending(conn, story_id, predicate_id)
        await _decide(conn, row.id, story_id, row.proposed, "rejected", principal, None)
    return Decision(predicate, str(row.id), "rejected", revision, timeline_revision)


async def _decide(
    conn: AsyncConnection,
    proposal_id: str,
    story_id: str,
    proposed: Any,
    status: str,
    principal: str,
    decision: Mapping[str, Any] | None,
) -> None:
    await conn.execute(
        update(predicate_proposals)
        .where(predicate_proposals.c.id == proposal_id)
        .values(
            status=status,
            decided_at=func.transaction_timestamp(),
            decided_by=principal,
            decision=None if decision is None else dict(decision),
        )
    )
    await index_records(
        conn,
        [
            proposal_record(
                SimpleNamespace(
                    id=proposal_id, story_id=story_id, proposed=proposed, decision=decision
                )
            )
        ],
    )


# -- the listing --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalView:
    id: str
    status: str
    proposed: dict[str, Any]
    filed_by: str


@dataclass(frozen=True, slots=True)
class PredicateList:
    """``GET /stories/{id}/predicates``: every predicate of the story's registry (core first,
    then the story's by name), each story predicate's pending proposal, and the story's rules
    and configuration revision."""

    predicates: tuple[Predicate, ...]
    pending: Mapping[str, ProposalView]
    rules: tuple[Implication, ...]
    config_revision: int


async def list_predicates(db: AsyncEngine, story_id: str) -> PredicateList:
    async with db.connect() as conn:
        registry = await load(conn, story_id)
        rows = await conn.execute(
            select(predicate_proposals).where(
                predicate_proposals.c.story_id == story_id,
                predicate_proposals.c.status == "pending",
            )
        )
        pending = {
            str(r.predicate_id): ProposalView(
                str(r.id), str(r.status), dict(r.proposed.get("changes") or {}), str(r.filed_by)
            )
            for r in rows
        }
    core_order = {p.id: i for i, p in enumerate(CORE_PREDICATES)}
    ordered = sorted(
        registry.predicates.values(),
        key=lambda p: (p.story_id is not None, core_order.get(p.id, 0), p.name),
    )
    return PredicateList(tuple(ordered), pending, registry.story_rules, registry.config_revision)


__all__ = [
    "ENGINE_PRINCIPAL",
    "IDENTITY",
    "IMPLICATION_MAX_DEPTH",
    "OWNER_ACTIVE",
    "OWNER_LOOKUPS",
    "PROPOSABLE",
    "PROPOSAL_OBSERVATIONS",
    "ApproveRequest",
    "AuthorLayer",
    "ClosedSetDecl",
    "Closure",
    "Decision",
    "IdentityReason",
    "ImplicationRule",
    "OwnerActive",
    "OwnerLookup",
    "PredicateDecl",
    "PredicateList",
    "PredicateNotFound",
    "ProposalNotFound",
    "ProposedChanges",
    "Registry",
    "ScenarioRules",
    "Violation",
    "approve",
    "author_layer",
    "auto_name",
    "auto_register",
    "canonical",
    "cardinality_may_contradict",
    "check_rules",
    "conditional_owner",
    "consider_proposal",
    "consider_proposals",
    "core_registry",
    "direct_polarity_conflict",
    "file_identity_proposal",
    "file_proposal",
    "identity_proposals",
    "imply",
    "intersection",
    "list_predicates",
    "load",
    "owner_active",
    "owner_lookup",
    "register_author",
    "register_owner_lookup",
    "reject",
    "rules_from_deferred",
    "rules_from_snapshot",
    "same_event",
    "setting_of",
    "single_valued_observations",
    "strengthened",
    "sync_core",
]
