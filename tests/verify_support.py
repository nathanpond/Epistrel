"""Fixtures for the deterministic verify tests (#140): a story registry of core and test
predicates, fixed entity ids, story-time helpers, and record and claim builders. ``check`` is
pure, so these tests need no database. #142's verifier tests add ``stub_models``, a stub Model
Access Layer with no database."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from epistrel.core.propositions.model import (
    Bound,
    Implication,
    Instant,
    Interval,
    Layer,
    Polarity,
    Predicate,
    Proposition,
    TemporalKind,
    TypedLiteral,
    story_minute,
)
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import CORE_BY_ID, CORE_BY_NAME
from epistrel.core.settings_schema import StructuredSettings
from epistrel.core.verify.rules import Record
from epistrel.orchestrator.models.access import ModelAccessLayer
from tests.auth_support import auth_settings
from tests.conftest import DUMMY_DSN

STORY = "01JS0000000000000000000001"

BOBBY = "01JA0000000000000000000001"
OHIO = "01JA0000000000000000000002"
SMITH = "01JA0000000000000000000003"
TEAM = "01JA0000000000000000000004"
OSU = "01JA0000000000000000000005"
FLORIDA = "01JA0000000000000000000006"
COLUMBUS = "01JA0000000000000000000007"
BOSTON = "01JA0000000000000000000008"
KITCHEN = "01JA0000000000000000000009"
GARDEN = "01JA000000000000000000000A"
HOUSE = "01JA000000000000000000000B"
SMITH_2 = "01JA000000000000000000000C"
DOOR = "01JA000000000000000000000D"
ANN = "01JA000000000000000000000E"
CAL = "01JA000000000000000000000F"
STRANGER = "01JA000000000000000000000G"

LOCATED_AT = CORE_BY_NAME["located_at"].id
LOCATED_IN = CORE_BY_NAME["located_in"].id
ENROLLED_AT = CORE_BY_NAME["enrolled_at"].id
MEMBER_OF = CORE_BY_NAME["member_of"].id
DEGREE = CORE_BY_NAME["degree_awarded"].id
ALIVE = CORE_BY_NAME["alive"].id
DEAD = CORE_BY_NAME["dead"].id
BORN = CORE_BY_NAME["born"].id
AGE = CORE_BY_NAME["age"].id
SIBLING_OF = CORE_BY_NAME["sibling_of"].id
SAME_AS = CORE_BY_NAME["same_as"].id

APPRENTICED = "01JP0000000000000000000001"
KNOCKED = "01JP0000000000000000000002"
PART_OF = "01JP0000000000000000000003"
FRIENDS_LIMITED = "01JP0000000000000000000004"
STEP_A = "01JP0000000000000000000005"
STEP_B = "01JP0000000000000000000006"
STEP_C = "01JP0000000000000000000007"
HAS_LOCK = "01JP0000000000000000000008"

STORY_PREDICATES: tuple[Predicate, ...] = (
    Predicate(
        id=APPRENTICED, story_id=STORY, name="apprenticed_with", layer=Layer.AUTO, qualifiers=None
    ),
    Predicate(
        id=KNOCKED,
        story_id=STORY,
        name="knocked_on",
        layer=Layer.AUTHOR,
        temporal_kind=TemporalKind.EVENT,
    ),
    Predicate(
        id=PART_OF,
        story_id=STORY,
        name="part_of",
        layer=Layer.AUTHOR,
        temporal_kind=TemporalKind.PERMANENT,
    ),
    Predicate(
        id=FRIENDS_LIMITED,
        story_id=STORY,
        name="close_friend_of",
        layer=Layer.AUTHOR,
        max_cardinality=2,
    ),
    Predicate(id=STEP_A, story_id=STORY, name="step_a", layer=Layer.AUTHOR),
    Predicate(id=STEP_B, story_id=STORY, name="step_b", layer=Layer.AUTHOR),
    Predicate(id=STEP_C, story_id=STORY, name="step_c", layer=Layer.AUTHOR),
    Predicate(id=HAS_LOCK, story_id=STORY, name="has_lock", layer=Layer.AUTO, qualifiers=None),
)

VARSITY_RULE = Implication(when=MEMBER_OF, then=ENROLLED_AT, via=PART_OF)
"""The author's rule of U-15: varsity membership implies enrollment at the team's school."""


def registry_of(*rules: Implication, extra: Iterable[Predicate] = ()) -> Registry:
    """The core vocabulary plus the test predicates (and ``extra``) and the story ``rules``."""
    found = {**CORE_BY_ID, **{p.id: p for p in (*STORY_PREDICATES, *extra)}}
    return Registry(STORY, 1, found, tuple(rules))


REGISTRY = registry_of()


def year(y: int) -> int:
    return story_minute(datetime(y, 1, 1, tzinfo=UTC))


def minute(moment: str) -> int:
    return story_minute(datetime.fromisoformat(moment).replace(tzinfo=UTC))


def span(lo: int | None, hi: int | None) -> Interval:
    return Interval.of(lo, hi)


def years(start: int | None, end: int | None) -> Interval:
    return Interval.of(None if start is None else year(start), None if end is None else year(end))


def approx(start: tuple[int, int] | None, end: tuple[int, int] | None) -> Interval:
    """An interval with approximate bounds: ``start = (outer, inner)``, ``end = (inner,
    outer)``."""
    return Interval(
        start=None if start is None else Bound(outer=start[0], inner=start[1]),
        end=None if end is None else Bound(inner=end[0], outer=end[1]),
    )


def prop(
    subject: str,
    predicate: str,
    obj: str | None = None,
    when: Interval | Instant | None = None,
    *,
    negated: bool = False,
    literal: TypedLiteral | None = None,
    object_set: tuple[str, ...] | None = None,
    qualifiers: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        literal=literal,
        object_set=object_set,
        qualifiers=qualifiers or {},
        polarity=Polarity.NEGATED if negated else Polarity.AFFIRMED,
        valid_time=when if when is not None else Interval.of(None, None),
        event_id=event_id,
    )


_counter = iter(range(1, 10**6))


def record(p: Proposition, proposition_id: str | None = None) -> Record:
    """A record with a fresh proposition id (and the same id as its fact)."""
    pid = proposition_id or f"01JR{next(_counter):022d}"
    return Record(p, pid, pid)


def records(*props: Proposition) -> list[Record]:
    return [record(p) for p in props]


def int_literal(value: int) -> TypedLiteral:
    return TypedLiteral(kind="int", value=value)


def stub_models(*, retries: int = 2, uncertain_below: float = 0.5) -> ModelAccessLayer:
    """A stub Model Access Layer with no database and the given structured settings (#142)."""
    mal = ModelAccessLayer(auth_settings(DUMMY_DSN, model_adapter="stub"))
    chosen = StructuredSettings(structured_retry_budget=retries, uncertain_below=uncertain_below)

    async def settings() -> StructuredSettings:
        return chosen

    mal.structured_settings = settings  # type: ignore[method-assign]
    return mal
