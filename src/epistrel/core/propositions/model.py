"""Propositions, valid-time and predicate semantics (FR-PROP-1,2,3; docs/02 §4.9; #131).

**A proposition** is viewpoint-free (FR-PROP-2): ``(subject, predicate, object | literal | object
set | none, qualifiers, polarity, valid_time)`` plus an optional ``event_id`` that groups the
propositions describing one event. Who holds it (canon, a belief, an utterance), its provenance,
visibility and confidence belong to the record that wraps it (``facts``, #133; beliefs, M5), so
a canon fact, a false belief and a lie share one shape.

**Story-time** is an integer: minutes from the story's epoch (``stories.story_epoch``, the
calendar instant of minute 0; ``DEFAULT_EPOCH`` when the story has none), so intervals compare as
integers. The calendar is only needed to find a granule's end (a month, a year).

**Valid-time** is an ``Interval`` or an ``Instant``:

- ``Interval``: ``[start, end)``, each bound exact or approximate, or open (``None``). A bound is
  ``Bound(inner, outer)``: an approximate start lies somewhere in ``[outer, inner]`` (outer is the
  earliest), an approximate end in ``[inner, outer]`` (outer is the latest); ``exact`` means
  ``inner == outer``. Every admissible reading of the interval contains its **definite** span
  ``[start.inner, end.inner)`` and lies within its **possible** span ``[start.outer,
  end.outer)``. ``start.inner < end.inner`` is required, so every reading is non-empty and a
  literal ``[t, t)`` can never be built (``InvalidValidTime``, U-15: rejected at normalization).
- ``Instant``: a punctual time ``at`` with a ``granularity`` (minute, hour, day, month, year) and
  the end of its granule ``until`` (``Instant.of`` computes both from any minute inside it). An
  instant stated only approximately ("sometime in 2012") is an ``Interval`` with approximate
  bounds, not an instant.

The relations answer for **every admissible reading**: ``True`` when it holds in every reading,
``False`` when it holds in none, ``None`` when the bounds cannot decide (#140 maps ``None`` to
``uncertain``):

- ``covers(canon, claim)``: the claim's every reading lies inside the canon's (an instant is
  covered iff its whole granule is);
- ``overlaps(a, b)``;
- ``exactly_equal(a, b)``: the same instant at the same granularity, or the same bounds;
- ``coarsely_compatible(a, b)``: the finer instant falls within the coarser one's granule ("May
  10" is compatible with "May 10 09:00" and with "May 10 17:00", which are not compatible with
  each other); for an interval, that some readings meet. Compatibility says two times *could*
  agree. **Event identity is never established by time** (``registry.same_event``).

**Literals** are typed (``TypedLiteral``: ``int``, ``float``, ``str``, ``bool``, ``date``,
``duration`` in minutes, ``quantity`` with a unit); equality is by kind, except that ``int`` and
``float`` compare by value (``int 40`` equals ``float 40.0``; ``str "40"`` equals neither).

**Predicates** (``Predicate``) carry the semantics FR-PROP-3 names; the registry
(``core.propositions.registry``) holds them by layer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import ULID_PATTERN

Ulid = Annotated[str, StringConstraints(pattern=ULID_PATTERN)]

DEFAULT_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
"""The calendar instant of story-minute 0 for a story with no ``story_epoch`` (an unanchored
story): only month and year granules depend on it."""


class InvalidValidTime(InvalidRequest):
    """A valid-time that is empty or inverted: ``start >= end`` in its definite span (a literal
    ``[t, t)`` above all), an approximate bound whose outer side is on the wrong side of its
    inner side, or an instant whose granule is empty. A domain error: normalization raises it
    and #140 maps it to ``uncertain(unresolved_time)``; over REST it is 422 ``validation_error``
    with ``details.reason = invalid_valid_time``."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, {"reason": "invalid_valid_time", **(details or {})})


# -- granules ----------------------------------------------------------------------------------


class Granularity(StrEnum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


GRANULARITY_ORDER: tuple[Granularity, ...] = tuple(Granularity)
"""Finest first."""


def _minutes(moment: datetime, epoch: datetime) -> int:
    return int((moment - epoch) // timedelta(minutes=1))


def granule(t: int, granularity: Granularity, epoch: datetime | None = None) -> tuple[int, int]:
    """The granule of ``granularity`` that story-minute ``t`` falls in, as ``[start, end)`` in
    story-minutes, on the calendar that ``epoch`` (default ``DEFAULT_EPOCH``) anchors."""
    if granularity is Granularity.MINUTE:
        return t, t + 1
    origin = epoch or DEFAULT_EPOCH
    moment = origin + timedelta(minutes=t)
    if granularity is Granularity.HOUR:
        start = moment.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    elif granularity is Granularity.DAY:
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif granularity is Granularity.MONTH:
        start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    else:
        start = moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    return _minutes(start, origin), _minutes(end, origin)


def story_minute(moment: datetime, epoch: datetime | None = None) -> int:
    """The story-minute of calendar ``moment`` (floor), on the calendar ``epoch`` anchors."""
    return _minutes(moment, epoch or DEFAULT_EPOCH)


# -- valid-time --------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bound(_Model):
    """One end of an interval: exact (``inner == outer``) or approximate (see the module
    docstring for which side is outer)."""

    inner: int
    outer: int

    @classmethod
    def at(cls, t: int) -> Bound:
        """An exact bound."""
        return cls(inner=t, outer=t)

    @property
    def exact(self) -> bool:
        return self.inner == self.outer


Span = tuple[int | None, int | None]
"""``[lo, hi)`` in story-minutes; ``None`` is unbounded on that side."""


class Interval(_Model):
    """``[start, end)``: see the module docstring. ``None`` bounds are open-ended."""

    kind: Literal["interval"] = "interval"
    start: Bound | None = None
    end: Bound | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start is not None and self.start.outer > self.start.inner:
            raise InvalidValidTime(
                "an approximate start's outer (earliest) bound is after its inner bound",
                {"bound": "start"},
            )
        if self.end is not None and self.end.inner > self.end.outer:
            raise InvalidValidTime(
                "an approximate end's outer (latest) bound is before its inner bound",
                {"bound": "end"},
            )
        if self.start is not None and self.end is not None and self.start.inner >= self.end.inner:
            raise InvalidValidTime(
                "a valid-time interval needs start < end; [t, t) is empty and never used",
                {"start": self.start.inner, "end": self.end.inner},
            )
        return self

    @classmethod
    def of(cls, start: int | None, end: int | None) -> Interval:
        """An interval with exact (or open) bounds."""
        return cls(
            start=None if start is None else Bound.at(start),
            end=None if end is None else Bound.at(end),
        )

    def definite(self) -> Span:
        return (
            None if self.start is None else self.start.inner,
            None if self.end is None else self.end.inner,
        )

    def possible(self) -> Span:
        return (
            None if self.start is None else self.start.outer,
            None if self.end is None else self.end.outer,
        )

    @property
    def exact(self) -> bool:
        return all(b is None or b.exact for b in (self.start, self.end))


class Instant(_Model):
    """A punctual time: ``at`` (the granule's first minute), its ``granularity`` and the
    granule's end ``until``. Build one with ``Instant.of``."""

    kind: Literal["instant"] = "instant"
    at: int
    granularity: Granularity
    until: int

    @model_validator(mode="after")
    def _granule(self) -> Self:
        if self.until <= self.at:
            raise InvalidValidTime("an instant's granule must end after it starts", {"at": self.at})
        return self

    @classmethod
    def of(cls, t: int, granularity: Granularity, epoch: datetime | None = None) -> Instant:
        """The instant of ``granularity`` that story-minute ``t`` falls in."""
        start, end = granule(t, granularity, epoch)
        return cls(at=start, granularity=granularity, until=end)

    def definite(self) -> Span:
        return self.at, self.until

    def possible(self) -> Span:
        return self.at, self.until


ValidTime = Annotated[Interval | Instant, Field(discriminator="kind")]


def _le(a: int | None, b: int | None, *, low: bool) -> bool:
    """``a <= b`` where ``None`` is -infinity for lower bounds (``low``) and +infinity for
    upper ones."""
    if a is None and b is None:
        return True
    if a is None:
        return low
    if b is None:
        return not low
    return a <= b


def _contains(outer: Span, inner: Span) -> bool:
    return _le(outer[0], inner[0], low=True) and _le(inner[1], outer[1], low=False)


def _meets(a: Span, b: Span) -> bool:
    """Whether ``a`` and ``b`` share a minute."""
    lows = [x for x in (a[0], b[0]) if x is not None]
    highs = [x for x in (a[1], b[1]) if x is not None]
    if not lows or not highs:
        return True
    return max(lows) < min(highs)


def covers(canon: Interval | Instant, claim: Interval | Instant) -> bool | None:
    """Whether ``canon`` covers ``claim`` in every admissible reading (``True``), in none
    (``False``), or the bounds cannot decide (``None``)."""
    if _contains(canon.definite(), claim.possible()):
        return True
    if not _contains(canon.possible(), claim.definite()):
        return False
    return None


def overlaps(a: Interval | Instant, b: Interval | Instant) -> bool | None:
    """Whether ``a`` and ``b`` share time in every reading, in none, or undecidable."""
    if _meets(a.definite(), b.definite()):
        return True
    if not _meets(a.possible(), b.possible()):
        return False
    return None


def _bound_equal(a: Bound | None, b: Bound | None) -> bool | None:
    if a is None or b is None:
        return a is None and b is None
    if a.exact and b.exact:
        return a.inner == b.inner
    if max(min(a.inner, a.outer), min(b.inner, b.outer)) > min(
        max(a.inner, a.outer), max(b.inner, b.outer)
    ):
        return False
    return None


def exactly_equal(a: Interval | Instant, b: Interval | Instant) -> bool | None:
    """Exact equality: two instants at the same minute and granularity, or two intervals with
    the same bounds (``None`` when approximate bounds might or might not agree)."""
    if isinstance(a, Instant) and isinstance(b, Instant):
        return a.at == b.at and a.granularity is b.granularity
    if isinstance(a, Interval) and isinstance(b, Interval):
        parts = [_bound_equal(a.start, b.start), _bound_equal(a.end, b.end)]
        if False in parts:
            return False
        return True if all(parts) else None
    return False


def finer(a: Granularity, b: Granularity) -> bool:
    """Whether ``a`` is strictly finer than ``b``."""
    return GRANULARITY_ORDER.index(a) < GRANULARITY_ORDER.index(b)


def coarsely_compatible(a: Interval | Instant, b: Interval | Instant) -> bool:
    """Coarse compatibility (FR-PROP-1): for two instants, the finer one's granule lies in the
    coarser one's (at one granularity, only an equal instant is compatible); otherwise, that
    some readings of the two meet. Compatible times *could* agree; they are not shown equal."""
    if isinstance(a, Instant) and isinstance(b, Instant):
        if a.granularity is b.granularity:
            return a.at == b.at
        fine, coarse = (a, b) if finer(a.granularity, b.granularity) else (b, a)
        return _contains(coarse.definite(), fine.definite())
    return _meets(a.possible(), b.possible())


def refines(fine: Interval | Instant, coarse: Interval | Instant) -> bool:
    """Whether ``fine`` is a strictly finer instant inside ``coarse``'s granule: "May 10 09:00"
    refines "May 10" (canon at the hour supports a claim of the day, never the reverse)."""
    return (
        isinstance(fine, Instant)
        and isinstance(coarse, Instant)
        and finer(fine.granularity, coarse.granularity)
        and _contains(coarse.definite(), fine.definite())
    )


# -- literals ----------------------------------------------------------------------------------


class LiteralKind(StrEnum):
    INT = "int"
    FLOAT = "float"
    STR = "str"
    BOOL = "bool"
    DATE = "date"
    DURATION = "duration"
    QUANTITY = "quantity"


NUMERIC_KINDS = frozenset({LiteralKind.INT, LiteralKind.FLOAT})


class TypedLiteral(_Model):
    """A literal object: ``{"kind": "int", "value": 40}``; a ``quantity`` also has a
    ``unit``; a ``date`` is an ISO date string; a ``duration`` is whole minutes."""

    kind: LiteralKind
    value: int | float | str | bool
    unit: str | None = None

    @model_validator(mode="after")
    def _typed(self) -> Self:
        value, kind = self.value, self.kind
        ok = {
            LiteralKind.INT: isinstance(value, int) and not isinstance(value, bool),
            LiteralKind.DURATION: isinstance(value, int) and not isinstance(value, bool),
            LiteralKind.FLOAT: isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value),
            LiteralKind.QUANTITY: isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value),
            LiteralKind.STR: isinstance(value, str),
            LiteralKind.BOOL: isinstance(value, bool),
            LiteralKind.DATE: isinstance(value, str) and _is_date(value),
        }[kind]
        if not ok:
            raise ValueError(f"a {kind.value} literal cannot hold {value!r}")
        if (kind is LiteralKind.QUANTITY) != (self.unit is not None):
            raise ValueError("a quantity literal has a unit, and only a quantity has one")
        return self

    def equals(self, other: TypedLiteral) -> bool:
        """Equality by kind; ``int`` and ``float`` compare by value."""
        if self.kind in NUMERIC_KINDS and other.kind in NUMERIC_KINDS:
            return self.value == other.value
        return self.kind is other.kind and self.value == other.value and self.unit == other.unit


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# -- propositions ------------------------------------------------------------------------------


class Polarity(StrEnum):
    AFFIRMED = "affirmed"
    NEGATED = "negated"


QualifierValue = str | int | float | bool | tuple[str, ...]
"""A qualifier's value: a JSON scalar, or a list of ids (``scheduled``'s participants)."""


class Proposition(_Model):
    """A viewpoint-free proposition (FR-PROP-1,2): see the module docstring. At most one of
    ``object_id``, ``literal`` and ``object_set`` is given (none: an object-less predicate such
    as ``alive``); an object set is stored sorted and without repeats."""

    subject_id: Ulid
    predicate_id: Ulid
    object_id: Ulid | None = None
    literal: TypedLiteral | None = None
    object_set: tuple[Ulid, ...] | None = None
    qualifiers: Mapping[str, QualifierValue] = Field(default_factory=dict)
    polarity: Polarity = Polarity.AFFIRMED
    valid_time: ValidTime
    event_id: Ulid | None = None

    @model_validator(mode="after")
    def _one_object(self) -> Self:
        given = [x for x in (self.object_id, self.literal, self.object_set) if x is not None]
        if len(given) > 1:
            raise ValueError("a proposition has one object: an entity, a literal or a set")
        if self.object_set is not None:
            if not self.object_set:
                raise ValueError("an object set has at least one member")
            object.__setattr__(self, "object_set", tuple(sorted(set(self.object_set))))
        return self

    def object_key(self) -> tuple[str, Any]:
        """The object, comparable across propositions (literals by ``TypedLiteral.equals``:
        numeric kinds collapse to one key)."""
        if self.object_id is not None:
            return ("entity", self.object_id)
        if self.object_set is not None:
            return ("set", self.object_set)
        if self.literal is not None:
            literal = self.literal
            if literal.kind in NUMERIC_KINDS:
                return ("number", float(literal.value))
            return (literal.kind.value, (literal.value, literal.unit))
        return ("none", None)

    def qualifier_key(self) -> tuple[tuple[str, Any], ...]:
        return tuple(sorted((k, _freeze(v)) for k, v in self.qualifiers.items()))

    def normalized_key(self) -> tuple[Any, ...]:
        """Subject, predicate, object and qualifiers: two propositions with the same key and
        opposite polarity over overlapping time are a direct polarity conflict (FR-PROP-4)."""
        return (self.subject_id, self.predicate_id, self.object_key(), self.qualifier_key())

    def negation(self) -> Proposition:
        """The same proposition with the other polarity."""
        flipped = Polarity.NEGATED if self.polarity is Polarity.AFFIRMED else Polarity.AFFIRMED
        return self.model_copy(update={"polarity": flipped})


def _freeze(value: Any) -> Any:
    return tuple(value) if isinstance(value, list | tuple) else value


# -- predicate semantics -----------------------------------------------------------------------


class Layer(StrEnum):
    CORE = "core"
    AUTHOR = "author"
    AUTO = "auto"


class Cardinality(StrEnum):
    FUNCTIONAL = "functional"
    MULTI = "multi"


class TemporalKind(StrEnum):
    PERMANENT = "permanent"
    FLUENT = "fluent"
    EVENT = "event"


class PredicateOwner(StrEnum):
    STORY = "story"
    EXTERNAL = "external"


ENTITY_KINDS = ("person", "place", "organization", "item", "event", "other")
"""The entity kinds argument types name (#139 resolves mentions to them). FR-PROP-6's
"character" is ``person`` and its "object" is ``item``."""
GENDERS = (None, "masculine", "feminine")
"""An entity's gender, the pronoun source of #139's resolver (``None``: any pronoun)."""
ANY = "any"
NONE = "none"
"""The object type of an object-less predicate (``alive``, ``dead``, ``born``)."""
ARGUMENT_TYPES = frozenset({*ENTITY_KINDS, *(k.value for k in LiteralKind), ANY, NONE})


class QualifierKind(StrEnum):
    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    DATE = "date"
    ID = "id"
    IDS = "ids"


class Implication(_Model):
    """A declared rule ``when(s, o) ⇒ then(s, o')``: same subject; the object carries over, or,
    with ``via``, maps through one join ``via(o, o')`` (a team ``via`` its institution:
    ``member_of(p, team) ∧ part_of(team, X) ⇒ enrolled_at(p, X)``)."""

    when: Ulid
    then: Ulid
    via: Ulid | None = None


class Predicate(_Model):
    """A registered predicate with the semantics FR-PROP-3 names. ``max_cardinality`` is 1 for
    a functional predicate (the migration's CHECK says the same); ``key_qualifiers`` are the
    qualifiers that join the subject in the uniqueness key; ``qualifiers`` declares every
    qualifier a proposition may carry (``None``: undeclared, an auto predicate's, which records
    whatever it was given and keys on none of it); ``set_object`` allows set-valued objects;
    ``exclusions``, ``containment_of`` and ``inverse_of`` are predicate ids."""

    id: Ulid
    story_id: Ulid | None = None
    name: str
    layer: Layer
    subject_type: str = ANY
    object_type: str = ANY
    cardinality: Cardinality = Cardinality.MULTI
    max_cardinality: int | None = Field(default=None, ge=1)
    key_qualifiers: tuple[str, ...] = ()
    qualifiers: Mapping[str, QualifierKind] | None = Field(default_factory=dict)
    set_object: bool = False
    temporal_kind: TemporalKind = TemporalKind.FLUENT
    exclusions: tuple[Ulid, ...] = ()
    containment_of: Ulid | None = None
    implications: tuple[Implication, ...] = ()
    symmetric: bool = False
    inverse_of: Ulid | None = None
    owner: PredicateOwner = PredicateOwner.STORY
    projection: str | None = None
    docs: str = ""

    @model_validator(mode="after")
    def _semantics(self) -> Self:
        if self.cardinality is Cardinality.FUNCTIONAL:
            if self.max_cardinality not in (None, 1):
                raise ValueError("a functional predicate has at most one object per key")
            object.__setattr__(self, "max_cardinality", 1)
        for side in (self.subject_type, self.object_type):
            if side not in ARGUMENT_TYPES:
                raise ValueError(f"{side!r} is not an argument type")
        if self.key_qualifiers:
            declared = self.qualifiers or {}
            missing = [q for q in self.key_qualifiers if q not in declared]
            if missing:
                raise ValueError(f"key qualifiers {missing} are not declared qualifiers")
        if self.symmetric and self.inverse_of is not None:
            raise ValueError("a predicate is symmetric or has an inverse, not both")
        for rule in self.implications:
            if rule.when != self.id:
                raise ValueError("a predicate's own implication starts from that predicate")
        return self

    @property
    def weak(self) -> bool:
        """The weakest semantics (FR-PROP-4): ``multi``, no maximum, no exclusions, implications
        or containment. A weak predicate never yields ``contradicted`` from cardinality,
        exclusion, implication or containment; only a direct polarity conflict."""
        return (
            self.cardinality is Cardinality.MULTI
            and self.max_cardinality is None
            and not self.exclusions
            and not self.implications
            and self.containment_of is None
        )

    def uniqueness_key(self, proposition: Proposition) -> tuple[Any, ...]:
        """The subject plus the key qualifiers' values: cardinality counts objects per key
        (a person's degrees each have their own key; residence is keyed by person alone)."""
        return (
            proposition.subject_id,
            tuple(
                (name, _freeze(proposition.qualifiers.get(name))) for name in self.key_qualifiers
            ),
        )

    def qualifier_errors(self, proposition: Proposition) -> list[str]:
        """What ``proposition``'s qualifiers break of this predicate's declaration (``[]`` for
        an auto predicate's undeclared qualifiers)."""
        if self.qualifiers is None:
            return []
        problems = []
        for name, value in proposition.qualifiers.items():
            kind = self.qualifiers.get(name)
            if kind is None:
                problems.append(f"{name!r} is not a qualifier of {self.name}")
            elif not _qualifier_ok(kind, value):
                problems.append(f"{name!r} must be {kind.value}")
        return problems


_ULID = re.compile(ULID_PATTERN)


def _is_ulid(value: object) -> bool:
    return isinstance(value, str) and _ULID.match(value) is not None


def _qualifier_ok(kind: QualifierKind, value: QualifierValue) -> bool:
    if kind is QualifierKind.IDS:
        return isinstance(value, tuple | list) and all(_is_ulid(v) for v in value)
    if isinstance(value, tuple | list):
        return False
    if kind is QualifierKind.ID:
        return _is_ulid(value)
    if kind is QualifierKind.BOOL:
        return isinstance(value, bool)
    if kind is QualifierKind.INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is QualifierKind.FLOAT:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind is QualifierKind.DATE:
        return isinstance(value, str) and _is_date(value)
    return isinstance(value, str)


__all__ = [
    "ANY",
    "ARGUMENT_TYPES",
    "DEFAULT_EPOCH",
    "ENTITY_KINDS",
    "GENDERS",
    "GRANULARITY_ORDER",
    "NONE",
    "Bound",
    "Cardinality",
    "Granularity",
    "Implication",
    "Instant",
    "Interval",
    "InvalidValidTime",
    "Layer",
    "LiteralKind",
    "Polarity",
    "Predicate",
    "PredicateOwner",
    "Proposition",
    "QualifierKind",
    "TemporalKind",
    "TypedLiteral",
    "ValidTime",
    "coarsely_compatible",
    "covers",
    "exactly_equal",
    "finer",
    "granule",
    "overlaps",
    "refines",
    "story_minute",
]
