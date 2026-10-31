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
CONSUMED = "01JPRED0000000000000000027"
INJURED = "01JPRED0000000000000000028"
TIME_IS = "01JPRED0000000000000000029"
ADJACENT_TO = "01JPRED0000000000000000030"
ON_CHANNEL = "01JPRED0000000000000000031"
SPOKE_TO = "01JPRED0000000000000000032"
SPOKE = "01JPRED0000000000000000033"
AGREED_TO = "01JPRED0000000000000000034"
STATE_VALUE = "01JPRED0000000000000000035"
CLOCK_ANCHOR = "01JPRED0000000000000000036"
TIME_OF_DAY_BAND = "01JPRED0000000000000000037"
ELAPSED_SINCE = "01JPRED0000000000000000038"
SCHEDULED_STATUS = "01JPRED0000000000000000039"
CLOCK_CHECKS: tuple[str, ...] = (TIME_IS, TIME_OF_DAY_BAND, ELAPSED_SINCE, SCHEDULED_STATUS)
"""The clock checks (#195): answered by the world clock's anchor when the story runs the clock
(``core.world_clock.verify``)."""

BARRIERS: tuple[str, ...] = ("none", "door_open", "door_closed", "window", "thin_wall", "wall")
"""The values of ``adjacent_to``'s ``barrier`` qualifier (#161): what stands between two
adjoining places. ``door_open`` when none is declared; only ``wall`` and ``door_closed`` block
sound, and an eavesdrop is plausible through anything but a ``wall``."""
DEFAULT_BARRIER = "door_open"


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
SCENE_TIME = Family("scene_time", "Scene time")
COMMUNICATION = Family("communication", "Perception and communication")
STATES = Family("states", "Tracked values")
WORLD_CLOCK = Family("world_clock", "World clock")

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
    SCENE_TIME,
    COMMUNICATION,
    STATES,
    WORLD_CLOCK,
)

_FAMILY_OF: dict[str, Family] = {}


def _core(family: Family, **fields: Any) -> Predicate:
    predicate = Predicate(layer=Layer.CORE, **fields)
    _FAMILY_OF[predicate.id] = family
    return predicate


_F, _M = Cardinality.FUNCTIONAL, Cardinality.MULTI
_PERM, _FLUENT, _EVENT = TemporalKind.PERMANENT, TemporalKind.FLUENT, TemporalKind.EVENT
_STR = QualifierKind.STR
_TRUST = {"trust": QualifierKind.FLOAT}
"""A relationship's optional ``trust`` (0 to 1, not part of the key): how far the subject trusts
the object, which #162's seeded trust reads (a scenario's ``cast.relationships[].trust``,
#173)."""

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
        LOCATION,
        id=ADJACENT_TO,
        name="adjacent_to",
        subject_type="place",
        object_type="place",
        cardinality=_M,
        temporal_kind=_FLUENT,
        symmetric=True,
        qualifiers={"barrier": _STR},
        docs="The two places adjoin (symmetric), with what stands between them as the optional "
        "`barrier`: `none`, `door_open` (the default), `door_closed`, `window`, `thin_wall` or "
        "`wall`. Perception routing (#161) reads it: an eavesdrop is plausible through anything "
        "but a `wall`, and a sound crosses anything but a `wall`. A later declaration of the "
        'pair ("she closes the door") is the barrier from then on.',
    ),
    _core(
        POSSESSION,
        id=HOLDS,
        name="holds",
        object_type="item",
        cardinality=_M,
        temporal_kind=_FLUENT,
        qualifiers={"quantity": _STR},
        projection="placement",
        docs="The subject physically has the item; a person may hold several. Its inverse "
        "`held_by` is functional: an item is in one pair of hands at a time. An optional "
        '`quantity` (`<number> <unit>`, "40 silver") says how much of it (#148\'s detectors).',
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
        POSSESSION,
        id=CONSUMED,
        name="consumed",
        object_type="item",
        cardinality=_M,
        temporal_kind=_EVENT,
        qualifiers={"quantity": _STR},
        docs="The subject used up the item (ate, drank, burned, spent it) at the instant: an "
        "event, with an optional `quantity`. Claim detection (#148) writes it for consumption "
        "language.",
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
        qualifiers=_TRUST,
        projection="relationships",
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
        qualifiers=_TRUST,
        projection="relationships",
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
        qualifiers=_TRUST,
        projection="relationships",
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
        qualifiers=_TRUST,
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
        qualifiers=_TRUST,
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
        qualifiers=_TRUST,
        projection="relationships",
        docs="The two are friends (symmetric).",
    ),
    _core(
        RELATIONSHIPS,
        id=ENEMY_OF,
        name="enemy_of",
        cardinality=_M,
        temporal_kind=_FLUENT,
        qualifiers=_TRUST,
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
        projection="relationships",
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
        projection="relationships",
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
        projection="relationships",
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
        LIFE,
        id=INJURED,
        name="injured",
        object_type=NONE,
        cardinality=_M,
        temporal_kind=_EVENT,
        qualifiers={"cause": QualifierKind.ID},
        docs="The subject was hurt at the instant (no object): an event, with the entity that "
        "caused it as the optional `cause`. Claim detection (#148) writes it for injury "
        "language.",
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
    _core(
        SCENE_TIME,
        id=TIME_IS,
        name="time_is",
        object_type="str",
        cardinality=_F,
        temporal_kind=_FLUENT,
        docs='What time a scene says it is (a `str` literal as said: "nearly midnight"), '
        "about the scene's place: one value at a time, so a later time supersedes it. Claim "
        "detection (#148) writes it for a time expression no other claim takes.",
    ),
    _core(
        COMMUNICATION,
        id=ON_CHANNEL,
        name="on_channel",
        subject_type="person",
        object_type="str",
        cardinality=_M,
        temporal_kind=_FLUENT,
        docs="The subject is on a remote channel (a call in progress, a radio link), named by a "
        "free-text token (a `str` literal, no entity). Perception routing (#161) lets a "
        "`remote_channel` qualifier reach exactly the channel's participants, wherever they are.",
    ),
    _core(
        COMMUNICATION,
        id=SPOKE_TO,
        name="spoke_to",
        subject_type="person",
        object_type="person",
        cardinality=_M,
        temporal_kind=_EVENT,
        docs="The subject said something to the object at the instant: the observable envelope "
        "of an utterance (FR-TURN-2), which a character perceives without its words.",
    ),
    _core(
        COMMUNICATION,
        id=SPOKE,
        name="spoke",
        subject_type="person",
        object_type=NONE,
        cardinality=_M,
        temporal_kind=_EVENT,
        docs="The subject said something (no object, no addressee) at the instant: the envelope "
        "of an utterance addressed to no one in particular.",
    ),
    _core(
        STATES,
        id=STATE_VALUE,
        name="state_value",
        cardinality=_F,
        temporal_kind=_FLUENT,
        key_qualifiers=("state",),
        qualifiers={"state": QualifierKind.ID},
        docs="A story State's value (#191, FR-STATE-8, FR-VERIFY-7): the `state` qualifier is "
        "the State's id, the subject the character a per-character State belongs to (the State's "
        "own id for a story State), the object the value as a literal. Its canon is the State's "
        "fold at the claim's position (`core.states.verify`, an owner lookup), never a fact: an "
        "objective or narrator claim is `supported` when the fold equals it (a number within "
        "the State's tolerance) and `contradicted` otherwise. A character's belief about a "
        "value is an ordinary belief, acquired by observation or testimony.",
    ),
    _core(
        STATES,
        id=AGREED_TO,
        name="agreed_to",
        cardinality=_M,
        temporal_kind=_EVENT,
        docs="The subject agreed to an action at the instant (#189, FR-STATE-11): the object is "
        "what the action is about (the item a sale pays for, else the other party). An action's "
        "preconditions read it as the counterparty's consent (`core.states.preconditions`). "
        "Consent is only an `agreed_to` a verified `say` or `do` beat of the subject declares, "
        "or a narrator beat declares; it is never inferred from prose. Negated, the subject "
        "refused.",
    ),
    _core(
        WORLD_CLOCK,
        id=CLOCK_ANCHOR,
        name="clock_anchor",
        object_type="str",
        cardinality=_M,
        temporal_kind=_EVENT,
        docs="A statement that anchors the world clock (#195, FR-CLOCK-3): the subject is the "
        "`world_clock` State's id, the object the components stated (a `str` literal holding "
        "the statement as JSON: admissible minutes of the day, weekday, year, month, day). It "
        "is a commitment written where the anchor is established, with `visibility = engine` "
        "(no character retrieval or context ever reads it); the clock's anchor at a position is "
        "the fold of the active anchor facts through it.",
    ),
    _core(
        WORLD_CLOCK,
        id=TIME_OF_DAY_BAND,
        name="time_of_day_band",
        object_type="str",
        cardinality=_F,
        temporal_kind=_FLUENT,
        docs="Which time-of-day band it is (a `str` literal naming one of the clock's bands: "
        '"morning", "evening"), about the scene\'s place. A clock check (#195): with the clock '
        "on it is answered by the anchor, `supported` only when every admissible time lies in "
        "the band and `contradicted` when none does; with the clock off it is "
        "`uncertain(no_clock)`.",
    ),
    _core(
        WORLD_CLOCK,
        id=ELAPSED_SINCE,
        name="elapsed_since",
        object_type="duration",
        cardinality=_F,
        temporal_kind=_FLUENT,
        key_qualifiers=("since",),
        qualifiers={"since": QualifierKind.INT},
        docs="How long it has been (a `duration` literal in minutes) since the story-minute "
        "`since`. A clock check (#195): the clock's offset is exact, so it is `supported` within "
        "the clock's `elapsed_tolerance_factor` and `contradicted` outside it; with the clock off "
        "it is `uncertain(no_clock)`.",
    ),
    _core(
        WORLD_CLOCK,
        id=SCHEDULED_STATUS,
        name="scheduled_status",
        subject_type="event",
        object_type="str",
        cardinality=_F,
        temporal_kind=_FLUENT,
        docs="A scheduled event's status (a `str` literal: `due`, `not_due`, `possibly_due`, "
        "`past`). A clock check (#195): `uncertain` until scheduled events are kept (#196, "
        "#197); with the clock off it is `uncertain(no_clock)`.",
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
    "ADJACENT_TO",
    "AGREED_TO",
    "BARRIERS",
    "CLOCK_ANCHOR",
    "CLOCK_CHECKS",
    "CORE_BY_ID",
    "CORE_BY_NAME",
    "CORE_NAMES",
    "CORE_PREDICATES",
    "DEFAULT_BARRIER",
    "EDUCATION_PREDICATES",
    "ELAPSED_SINCE",
    "FAMILIES",
    "ON_CHANNEL",
    "SCHEDULED_STATUS",
    "SPOKE",
    "SPOKE_TO",
    "STATE_VALUE",
    "TIME_OF_DAY_BAND",
    "Family",
    "family_of",
]
