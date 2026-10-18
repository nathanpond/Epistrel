"""Rows ↔ models for ``predicates`` and ``propositions`` (#131).

Pure functions, shared by the registry, the store, migration 0048's seed and the start-up
re-sync, so the stored shape is written in one place. A proposition's valid-time is four bounds,
the exact flags, the instant's granularity and the possible span (``core.tables.propositions``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.dialects.postgresql import Range

from epistrel.core.propositions.model import (
    Bound,
    Cardinality,
    Granularity,
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
    TypedLiteral,
)

SEMANTIC_COLUMNS = (
    "subject_type",
    "object_type",
    "cardinality",
    "max_cardinality",
    "key_qualifiers",
    "qualifiers",
    "set_object",
    "temporal_kind",
    "exclusions",
    "containment_of",
    "implications",
    "symmetric",
    "inverse_of",
    "owner",
    "projection",
    "docs",
)
"""The columns a predicate's semantics live in: what a start-up re-sync of a core row and an
approved proposal may change (its id, scope, name and layer never change)."""


def predicate_row(predicate: Predicate) -> dict[str, Any]:
    """``predicate`` as a ``predicates`` row."""
    return {
        "id": predicate.id,
        "story_id": predicate.story_id,
        "name": predicate.name,
        "layer": predicate.layer.value,
        "subject_type": predicate.subject_type,
        "object_type": predicate.object_type,
        "cardinality": predicate.cardinality.value,
        "max_cardinality": predicate.max_cardinality,
        "key_qualifiers": list(predicate.key_qualifiers),
        "qualifiers": None
        if predicate.qualifiers is None
        else {k: v.value for k, v in predicate.qualifiers.items()},
        "set_object": predicate.set_object,
        "temporal_kind": predicate.temporal_kind.value,
        "exclusions": list(predicate.exclusions),
        "containment_of": predicate.containment_of,
        "implications": [
            rule.model_dump(mode="json", exclude_none=True) for rule in predicate.implications
        ],
        "symmetric": predicate.symmetric,
        "inverse_of": predicate.inverse_of,
        "owner": predicate.owner.value,
        "projection": predicate.projection,
        "docs": predicate.docs,
    }


def predicate_from_row(row: Any) -> Predicate:
    """A ``predicates`` row (a SQLAlchemy row or a mapping) as a ``Predicate``."""
    m: Mapping[str, Any] = row if isinstance(row, Mapping) else row._mapping
    qualifiers = m["qualifiers"]
    return Predicate(
        id=str(m["id"]),
        story_id=None if m["story_id"] is None else str(m["story_id"]),
        name=str(m["name"]),
        layer=Layer(m["layer"]),
        subject_type=str(m["subject_type"]),
        object_type=str(m["object_type"]),
        cardinality=Cardinality(m["cardinality"]),
        max_cardinality=m["max_cardinality"],
        key_qualifiers=tuple(m["key_qualifiers"] or ()),
        qualifiers=None
        if qualifiers is None
        else {k: QualifierKind(v) for k, v in dict(qualifiers).items()},
        set_object=bool(m["set_object"]),
        temporal_kind=TemporalKind(m["temporal_kind"]),
        exclusions=tuple(str(x) for x in m["exclusions"] or ()),
        containment_of=None if m["containment_of"] is None else str(m["containment_of"]),
        implications=tuple(Implication.model_validate(x) for x in m["implications"] or ()),
        symmetric=bool(m["symmetric"]),
        inverse_of=None if m["inverse_of"] is None else str(m["inverse_of"]),
        owner=PredicateOwner(m["owner"]),
        projection=m["projection"],
        docs=str(m["docs"] or ""),
    )


def _span(lo: int | None, hi: int | None) -> Range[int]:
    return Range(lo, hi, bounds="[)")


def valid_time_columns(valid_time: Interval | Instant) -> dict[str, Any]:
    """The valid-time columns of a ``propositions`` row."""
    if isinstance(valid_time, Instant):
        return {
            "valid_from_inner": valid_time.at,
            "valid_from_outer": valid_time.at,
            "valid_to_inner": None,
            "valid_to_outer": None,
            "from_exact": True,
            "to_exact": True,
            "instant_granularity": valid_time.granularity.value,
            "valid_span": _span(valid_time.at, valid_time.until),
        }
    start, end = valid_time.start, valid_time.end
    return {
        "valid_from_inner": None if start is None else start.inner,
        "valid_from_outer": None if start is None else start.outer,
        "valid_to_inner": None if end is None else end.inner,
        "valid_to_outer": None if end is None else end.outer,
        "from_exact": start is None or start.exact,
        "to_exact": end is None or end.exact,
        "instant_granularity": None,
        "valid_span": _span(*valid_time.possible()),
    }


def proposition_row(proposition: Proposition) -> dict[str, Any]:
    """``proposition``'s columns (without ``id`` and ``story_id``)."""
    return {
        "subject_id": proposition.subject_id,
        "predicate_id": proposition.predicate_id,
        "object_id": proposition.object_id,
        "literal": None
        if proposition.literal is None
        else proposition.literal.model_dump(mode="json", exclude_none=True),
        "object_set": None if proposition.object_set is None else list(proposition.object_set),
        "qualifiers": {
            k: list(v) if isinstance(v, tuple) else v for k, v in proposition.qualifiers.items()
        },
        "polarity": proposition.polarity.value,
        "event_id": proposition.event_id,
        **valid_time_columns(proposition.valid_time),
    }


def _bound(inner: int | None, outer: int | None) -> Bound | None:
    if inner is None and outer is None:
        return None
    if inner is None or outer is None:
        value = inner if inner is not None else outer
        assert value is not None  # noqa: S101 - one of the two is set
        return Bound.at(int(value))
    return Bound(inner=int(inner), outer=int(outer))


def proposition_from_row(row: Any) -> Proposition:
    """A ``propositions`` row as a ``Proposition``."""
    m: Mapping[str, Any] = row if isinstance(row, Mapping) else row._mapping
    granularity = m["instant_granularity"]
    valid_time: Interval | Instant
    if granularity is not None:
        span = m["valid_span"]
        valid_time = Instant(
            at=int(m["valid_from_inner"]),
            granularity=Granularity(granularity),
            until=int(span.upper),
        )
    else:
        valid_time = Interval(
            start=_bound(m["valid_from_inner"], m["valid_from_outer"]),
            end=_bound(m["valid_to_inner"], m["valid_to_outer"]),
        )
    literal = m["literal"]
    object_set = m["object_set"]
    return Proposition(
        subject_id=str(m["subject_id"]),
        predicate_id=str(m["predicate_id"]),
        object_id=None if m["object_id"] is None else str(m["object_id"]),
        literal=None if literal is None else TypedLiteral.model_validate(literal),
        object_set=None if object_set is None else tuple(str(x) for x in object_set),
        qualifiers={
            k: tuple(v) if isinstance(v, list) else v for k, v in dict(m["qualifiers"]).items()
        },
        polarity=Polarity(m["polarity"]),
        valid_time=valid_time,
        event_id=None if m["event_id"] is None else str(m["event_id"]),
    )


__all__ = [
    "SEMANTIC_COLUMNS",
    "predicate_from_row",
    "predicate_row",
    "proposition_from_row",
    "proposition_row",
    "valid_time_columns",
]
