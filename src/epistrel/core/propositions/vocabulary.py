"""The core vocabulary: the predicates the engine ships, with documented semantics (FR-PROP-5;
#131).

Each core predicate has a fixed ULID (so a proposition names it the same way in every
deployment), a family, its FR-PROP-3 semantics and a ``docs`` string. Migration 0048 inserts
them into ``predicates`` (``story_id`` null, ``layer = core``) and every start re-syncs the rows
from here (``registry.sync_core``), so a new predicate or a changed doc string reaches an
existing deployment. ``docs/predicates.md`` is rendered from this module
(``python -m epistrel.tools.predicates``; CI runs it with ``--check``).

**Education keeps distinct predicates for distinct facts** and the vocabulary ships no
implications among them: team membership does not imply enrollment, enrollment does not imply a
degree, and a degree does not name its only institution. An author declares such a rule per
scenario where it holds (``starting_state.rules``).

**Direction.** A symmetric predicate is stored with the lower entity id as subject; of an
inverse pair, the row of the predicate without ``inverse_of`` is stored (``held_by(i, p)`` is
stored as ``holds(p, i)``, ``child_of(c, p)`` as ``parent_of(p, c)``; ``registry.canonical``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from epistrel.core.propositions.model import (
    NONE,
    Cardinality,
    Layer,
    Predicate,
    QualifierKind,
    TemporalKind,
)

SAME_AS = "01JPRED0000000000000000001"
LOCATED_AT = "01JPRED0000000000000000002"
PRESENT_AT = "01JPRED0000000000000000003"
LOCATED_IN = "01JPRED0000000000000000004"
HOLDS = "01JPRED0000000000000000005"
HELD_BY = "01JPRED0000000000000000006"
OWNS = "01JPRED0000000000000000007"
ALIVE = "01JPRED0000000000000000008"
DEAD = "01JPRED0000000000000000009"
BORN = "01JPRED0000000000000000010"
PARENT_OF = "01JPRED0000000000000000011"
CHILD_OF = "01JPRED0000000000000000012"
SIBLING_OF = "01JPRED0000000000000000013"
MARRIED_TO = "01JPRED0000000000000000014"
KNOWS = "01JPRED0000000000000000015"
FRIEND_OF = "01JPRED0000000000000000016"
ENEMY_OF = "01JPRED0000000000000000017"
MEMBER_OF = "01JPRED0000000000000000018"
HOLDS_POSITION = "01JPRED0000000000000000019"
ENROLLED_AT = "01JPRED0000000000000000020"
ATTENDED_PROGRAM = "01JPRED0000000000000000021"
DEGREE_AWARDED = "01JPRED0000000000000000022"
EMPLOYED_BY = "01JPRED0000000000000000023"
AGE = "01JPRED0000000000000000024"
NAME = "01JPRED0000000000000000025"
SCHEDULED = "01JPRED0000000000000000026"


@dataclass(frozen=True, slots=True)
class Family:
    """A heading of ``docs/predicates.md``: one table per family."""

    key: str
    title: str


IDENTITY = Family("identity", "Identity")
LOCATION = Family("location", "Location and presence")
POSSESSION = Family("possession", "Possession")
LIFE = Family("life", "Life and death")
KINSHIP = Family("kinship", "Kinship")
RELATIONSHIPS = Family("relationships", "Relationships")
MEMBERSHIP = Family("membership", "Membership and roles")
EDUCATION = Family("education", "Education")
EMPLOYMENT = Family("employment", "Employment")
ATTRIBUTES = Family("attributes", "Basic attributes")
SCHEDULES = Family("schedules", "Schedules")

FAMILIES: tuple[Family, ...] = (
    IDENTITY,
    LOCATION,
    POSSESSION,
    LIFE,
    KINSHIP,
    RELATIONSHIPS,
    MEMBERSHIP,
    EDUCATION,
    EMPLOYMENT,
    ATTRIBUTES,
    SCHEDULES,
)

_FAMILY_OF: dict[str, Family] = {}


def _core(family: Family, **fields: Any) -> Predicate:
    predicate = Predicate(layer=Layer.CORE, **fields)
    _FAMILY_OF[predicate.id] = family
    return predicate


_F, _M = Cardinality.FUNCTIONAL, Cardinality.MULTI
_PERM, _FLUENT, _EVENT = TemporalKind.PERMANENT, TemporalKind.FLUENT, TemporalKind.EVENT
_STR = QualifierKind.STR

CORE_PREDICATES: tuple[Predicate, ...] = (
    _core(
        IDENTITY,
        id=SAME_AS,
        name="same_as",
        cardinality=_M,
        temporal_kind=_PERM,
        symmetric=True,
        docs='The two entities are one referent ("the stranger" is Bobby). Identity is an '
        "ordinary proposition with a holder, so one character may know it while another does "
        "not; merges and splits found later are amendments (FR-PROP-7).",
    ),
    _core(
        LOCATION,
        id=LOCATED_AT,
        name="located_at",
        object_type="place",
        cardinality=_F,
        temporal_kind=_FLUENT,
        containment_of=LOCATED_IN,
        projection="placement",
        docs="Where the subject is: one place at a time. A place that contains the other "
        "(through `located_in`) is no conflict: Columbus and Ohio agree.",
    ),
    _core(
        LOCATION,
        id=PRESENT_AT,
        name="present_at",
        cardinality=_M,
        temporal_kind=_FLUENT,
        projection="scene",
        docs="The subject takes part in a scene or an occasion; several at once are allowed.",
    ),
    _core(
        LOCATION,
        id=LOCATED_IN,
        name="located_in",
        cardinality=_M,
        temporal_kind=_PERM,
        docs="Containment of places and organizations (Columbus `located_in` Ohio): the "
        "hierarchy `located_at` and the contradiction rules read.",
    ),
    _core(
        POSSESSION,
        id=HOLDS,
        name="holds",
        object_type="item",
        cardinality=_M,
        temporal_kind=_FLUENT,
        docs="The subject physically has the item; a person may hold several. Its inverse "
        "`held_by` is functional: an item is in one pair of hands at a time.",
    ),
    _core(
        POSSESSION,
        id=HELD_BY,
        name="held_by",
        subject_type="item",
        cardinality=_F,
        temporal_kind=_FLUENT,
        inverse_of=HOLDS,
        docs="The inverse of `holds`, stored as `holds`: an item has one holder at a time.",
    ),
    _core(
        POSSESSION,
        id=OWNS,
        name="owns",
        cardinality=_M,
        temporal_kind=_FLUENT,
        docs="Ownership, independent of who holds the thing now.",
    ),
    _core(
        LIFE,
        id=ALIVE,
        name="alive",
        subject_type="person",
        object_type=NONE,
        cardinality=_F,
        temporal_kind=_FLUENT,
        exclusions=(DEAD,),
        docs="The subject is alive (no object); negated, not alive. Excludes `dead` over "
        "overlapping time.",
    ),
    _core(
        LIFE,
        id=DEAD,
        name="dead",
        subject_type="person",
        object_type=NONE,
        cardinality=_F,
        temporal_kind=_PERM,
        exclusions=(ALIVE,),
        docs="The subject has died (no object): from then on, permanently. Excludes `alive` "
        "over overlapping time and so closes its interval.",
    ),
    _core(
        LIFE,
        id=BORN,
        name="born",
        subject_type="person",
        object_type=NONE,
        cardinality=_F,
        temporal_kind=_EVENT,
        docs="The subject's birth (no object): a unique event per person, so two different "
        "birth times contradict, while a coarser time compatible with canon does not.",
    ),
    _core(
        KINSHIP,
        id=PARENT_OF,
        name="parent_of",
        subject_type="person",
        object_type="person",
        cardinality=_M,
        temporal_kind=_PERM,
        docs="The subject is a parent of the object.",
    ),
    _core(
        KINSHIP,
        id=CHILD_OF,
        name="child_of",
        subject_type="person",
        object_type="person",
        cardinality=_M,
        temporal_kind=_PERM,
        inverse_of=PARENT_OF,
        docs="The inverse of `parent_of`, stored as `parent_of`.",
    ),
    _core(
        KINSHIP,
        id=SIBLING_OF,
        name="sibling_of",
        subject_type="person",
        object_type="person",
        cardinality=_M,
        temporal_kind=_PERM,
        symmetric=True,
        docs="The two people are siblings (symmetric).",
    ),
    _core(
        KINSHIP,
        id=MARRIED_TO,
        name="married_to",
        subject_type="person",
        object_type="person",
        cardinality=_M,
        temporal_kind=_FLUENT,
        symmetric=True,
        projection="relationships",
        docs="The two people are married (symmetric, over an interval).",
    ),
    _core(
        RELATIONSHIPS,
        id=KNOWS,
        name="knows",
        subject_type="person",
        cardinality=_M,
        temporal_kind=_FLUENT,
        projection="relationships",
        docs="The subject is acquainted with the object (not symmetric: being known is not "
        "knowing).",
    ),
    _core(
        RELATIONSHIPS,
        id=FRIEND_OF,
        name="friend_of",
        subject_type="person",
        object_type="person",
        cardinality=_M,
        temporal_kind=_FLUENT,
        symmetric=True,
        projection="relationships",
        docs="The two are friends (symmetric).",
    ),
    _core(
        RELATIONSHIPS,
        id=ENEMY_OF,
        name="enemy_of",
        cardinality=_M,
        temporal_kind=_FLUENT,
        projection="relationships",
        docs="The subject is hostile to the object (not symmetric).",
    ),
    _core(
        MEMBERSHIP,
        id=MEMBER_OF,
        name="member_of",
        subject_type="person",
        object_type="organization",
        cardinality=_M,
        temporal_kind=_FLUENT,
        qualifiers={"role": _STR},
        docs="Membership of a team, club or other organization, with an optional `role` (not "
        "part of the key). Implies no enrollment anywhere.",
    ),
    _core(
        MEMBERSHIP,
        id=HOLDS_POSITION,
        name="holds_position",
        subject_type="person",
        object_type="organization",
        cardinality=_M,
        temporal_kind=_FLUENT,
        qualifiers={"title": _STR},
        docs="An office or position in an organization, with its `title`.",
    ),
    _core(
        EDUCATION,
        id=ENROLLED_AT,
        name="enrolled_at",
        subject_type="person",
        object_type="organization",
        cardinality=_M,
        temporal_kind=_FLUENT,
        docs="Enrollment at an institution; a person may be enrolled at several. Implies no "
        "degree.",
    ),
    _core(
        EDUCATION,
        id=ATTENDED_PROGRAM,
        name="attended_program",
        subject_type="person",
        cardinality=_M,
        temporal_kind=_FLUENT,
        docs="Attendance of a program, camp or course; any number, none implying enrollment.",
    ),
    _core(
        EDUCATION,
        id=DEGREE_AWARDED,
        name="degree_awarded",
        subject_type="person",
        object_type="organization",
        cardinality=_F,
        temporal_kind=_EVENT,
        key_qualifiers=("degree_id",),
        qualifiers={"degree_id": _STR, "level": _STR, "field": _STR},
        set_object=True,
        docs="A degree awarded to the subject, keyed by (person, `degree_id`): a person may "
        "hold any number of degrees, and each degree's awarding institutions are one set (a "
        "joint degree's set is {A, B}). `level` and `field` are free text outside the key.",
    ),
    _core(
        EMPLOYMENT,
        id=EMPLOYED_BY,
        name="employed_by",
        object_type="organization",
        cardinality=_M,
        temporal_kind=_FLUENT,
        qualifiers={"role": _STR},
        docs="Employment by an organization or a person, with an optional `role`; several "
        "employers at once are allowed.",
    ),
    _core(
        ATTRIBUTES,
        id=AGE,
        name="age",
        subject_type="person",
        object_type="int",
        cardinality=_F,
        temporal_kind=_FLUENT,
        docs="The subject's age in years (an `int` literal): one value at a time.",
    ),
    _core(
        ATTRIBUTES,
        id=NAME,
        name="name",
        object_type="str",
        cardinality=_F,
        temporal_kind=_FLUENT,
        docs="The name the subject goes by (a `str` literal): one at a time; aliases are the "
        "entity's, not propositions.",
    ),
    _core(
        SCHEDULES,
        id=SCHEDULED,
        name="scheduled",
        object_type="event",
        cardinality=_M,
        temporal_kind=_EVENT,
        qualifiers={"participants": QualifierKind.IDS, "place": QualifierKind.ID},
        docs="A planned event owned by the subject (an appointment, a deadline, a departure): "
        "its planned time is the valid-time instant, its `participants` and `place` "
        "qualifiers (FR-CLOCK-9).",
    ),
)
"""Every core predicate, in documentation order."""

CORE_BY_ID: Mapping[str, Predicate] = {p.id: p for p in CORE_PREDICATES}
CORE_BY_NAME: Mapping[str, Predicate] = {p.name: p for p in CORE_PREDICATES}
CORE_NAMES = frozenset(CORE_BY_NAME)

EDUCATION_PREDICATES = (MEMBER_OF, ENROLLED_AT, ATTENDED_PROGRAM, DEGREE_AWARDED)
"""The predicates FR-PROP-5 keeps distinct, with no implications among them."""


def family_of(predicate_id: str) -> Family:
    return _FAMILY_OF[predicate_id]


__all__ = [
    "CORE_BY_ID",
    "CORE_BY_NAME",
    "CORE_NAMES",
    "CORE_PREDICATES",
    "EDUCATION_PREDICATES",
    "FAMILIES",
    "Family",
    "family_of",
]
