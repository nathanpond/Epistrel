"""A scenario's typed starting state read the one way publish and genesis both need
(FR-STATE-3, FR-CLOCK-3,9, FR-SCN-5; #202). Pure: nothing here reads the database.

- **States** (``definition_of``): a ``ScenarioState`` as #185's declaration input, its
  ``authorization.sources`` ``role:<slug>`` mapped to ``character:<id>`` (an unfilled role's
  dropped; when none is left the State admits the ``author`` only), its ``entity_id`` (a role or
  a world location or entity id) mapped to the story's entity, ``created_by = genesis``. The
  publish check maps every role to a placeholder id, so #185's ``validate_definition`` reads a
  scenario's declaration as genesis will. ``genesis_order`` puts a derived State after the
  States it reads (#192: ``define_state`` checks a derived State's inputs exist).
- **The clock** (``clock_settings``): ``starting_state.clock`` as #193's ``ClockSettings``,
  checked by ``check_clock_settings``, its ``anchor`` (#195's components: the date, the weekday,
  the exact time or only its band) folded into one statement at offset 0 and stored as the
  ``anchor`` setting (``Anchor.document``), which ``world_clock.base_anchor`` reads back. A band
  and a time that disagree, or a date the calendar has not, is 422 ``validation_error``
  (``reason: anchor``).
- **Scheduled events** (``planned_at``): a planned offset as given; a time of day ``"HH:MM"``
  is the next occurrence at or after the start when the anchor fixes the minute, else the frame
  minute #197 holds it at (``Anchor.place``), ``possibly_due`` by #197's rules until the anchor
  narrows (the Discretion of #202: an absolute time on an unanchored clock waits for the
  anchor).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from pydantic import ValidationError

from epistrel.core.catalog.scenario_models import (
    ScenarioAnchor,
    ScenarioClock,
    ScenarioState,
)
from epistrel.core.errors import InvalidRequest
from epistrel.core.membership import Position
from epistrel.core.states.authorize import AUTHOR, CHARACTER_PREFIX
from epistrel.core.states.expressions import DerivedRule, parse_rules
from epistrel.core.states.model import ClockSettings, check_clock_settings
from epistrel.core.world_clock.anchor import (
    Anchor,
    Clockwork,
    Contradiction,
    Statement,
    band_statement,
)

ROLE_PREFIX: Final = "role:"
"""A scenario's authorization source naming a role (``role:<slug>``)."""
GENESIS: Final = "genesis"


def _invalid(message: str, field: str, reason: str) -> InvalidRequest:
    return InvalidRequest(
        message,
        {
            "field": field,
            "reason": reason,
            "errors": [{"field": field, "message": message, "type": "value_error"}],
        },
    )


def role_sources(state: ScenarioState) -> list[str]:
    """The roles ``state``'s ``authorization.sources`` name (``role:<slug>``), in order."""
    sources = (state.authorization or {}).get("sources")
    if not isinstance(sources, list):
        return []
    return [
        s[len(ROLE_PREFIX) :] for s in sources if isinstance(s, str) and s.startswith(ROLE_PREFIX)
    ]


def definition_of(
    state: ScenarioState,
    *,
    characters: Mapping[str, str],
    entities: Mapping[str, str],
) -> dict[str, Any]:
    """``state`` as #185's declaration input (see the module docstring): ``characters`` maps a
    role id to its character id (an unfilled role absent), ``entities`` a role or world id to
    its entity id. An ``entity_id`` no map holds is left as written (``validate_definition``
    then fails ``define_state``'s entity check, so the caller checks it first)."""
    doc = state.model_dump(mode="json", exclude_none=True)
    authorization = doc.get("authorization")
    if isinstance(authorization, dict) and isinstance(authorization.get("sources"), list):
        mapped: list[Any] = []
        for source in authorization["sources"]:
            if isinstance(source, str) and source.startswith(ROLE_PREFIX):
                character = characters.get(source[len(ROLE_PREFIX) :])
                if character is not None:
                    mapped.append(f"{CHARACTER_PREFIX}{character}")
            else:
                mapped.append(source)
        doc["authorization"] = {**authorization, "sources": mapped or [AUTHOR]}
    entity = doc.get("entity_id")
    if isinstance(entity, str) and entity in entities:
        doc["entity_id"] = entities[entity]
    doc["created_by"] = GENESIS
    return doc


def derived_inputs(state: ScenarioState) -> frozenset[str] | None:
    """The States a derived ``state`` reads (``None``: not derived, or rules that do not
    parse, which the declaration check reports)."""
    try:
        rule = parse_rules(state.rules or {})
    except InvalidRequest:
        return None
    return rule.inputs if isinstance(rule, DerivedRule) else None


def genesis_order(states: Sequence[ScenarioState]) -> list[int]:
    """The indexes of ``states`` in an order ``define_state`` accepts: a derived State after
    every scenario State it reads (document order otherwise; a cycle, which publish refuses,
    keeps document order)."""
    by_name = {s.name: i for i, s in enumerate(states)}
    done: list[int] = []
    seen: set[int] = set()

    def visit(index: int, path: frozenset[int]) -> None:
        if index in seen or index in path:
            return
        for name in sorted(derived_inputs(states[index]) or ()):
            other = by_name.get(name)
            if other is not None:
                visit(other, path | {index})
        if index not in seen:
            seen.add(index)
            done.append(index)

    for index in range(len(states)):
        visit(index, frozenset())
    return done


# -- the clock ---------------------------------------------------------------------------------


def _minutes(value: str) -> int:
    return int(value[:2]) * 60 + int(value[3:])


def anchor_document(anchor: ScenarioAnchor, clockwork: Clockwork) -> dict[str, Any] | None:
    """The starting anchor as the ``anchor`` setting stores it (``None``: nothing fixed), or 422
    (``field: anchor.<component>``, ``reason: anchor``)."""
    statement = anchor_statement(anchor, clockwork)
    if statement.empty:
        return None
    try:
        refined = Anchor.unanchored(clockwork).refine(statement, 0, Position(0))
    except (ValueError, IndexError, KeyError) as exc:
        raise _invalid(
            f"the anchor is not a time of the calendar: {exc}", "anchor", "anchor"
        ) from None
    if isinstance(refined, Contradiction):
        raise _invalid(
            f"the anchor contradicts itself ({refined.component})",
            f"anchor.{refined.component}",
            "anchor",
        )
    return refined.document()


def anchor_statement(anchor: ScenarioAnchor, clockwork: Clockwork) -> Statement:
    """The starting anchor as one statement of the absolute time (what ``anchor_document``
    refines the unanchored clock with; #698: a later materialization commits it as the
    clock's first anchor fact), or 422 for a band, a time or a weekday the calendar lacks."""
    time: tuple[tuple[int, int], ...] | None = None
    if anchor.band is not None:
        found = band_statement(anchor.band, clockwork)
        if found is None:
            raise _invalid(f"the calendar has no band {anchor.band!r}", "anchor.band", "anchor")
        time = found.time
    if anchor.time is not None:
        minute = _minutes(anchor.time)
        if minute >= clockwork.day_minutes or (
            time is not None and not any(lo <= minute < hi for lo, hi in time)
        ):
            raise _invalid(
                "the anchor's time lies outside its band or the day", "anchor.time", "anchor"
            )
        time = ((minute, minute + 1),)
    weekday: int | None = None
    if anchor.weekday is not None:
        names = [name.casefold() for name in clockwork.weekdays]
        if anchor.weekday.casefold() not in names:
            raise _invalid(
                f"the calendar has no weekday {anchor.weekday!r}", "anchor.weekday", "anchor"
            )
        weekday = names.index(anchor.weekday.casefold())
    return Statement(
        time=time, weekday=weekday, year=anchor.year, month=anchor.month, day=anchor.day
    )


def clock_settings(clock: ScenarioClock | None) -> ClockSettings:
    """``starting_state.clock`` as the world clock's settings (see the module docstring); the
    defaults for ``None``. 422 ``validation_error`` (``details.field`` relative to the clock)
    when a setting or the anchor is invalid."""
    if clock is None:
        return ClockSettings()
    doc = clock.model_dump(mode="json", exclude_none=True, exclude={"anchor"})
    try:
        settings = ClockSettings.model_validate(doc)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors()) from None
    settings = check_clock_settings(settings)
    if clock.anchor is None:
        return settings
    from epistrel.core.world_clock import clockwork

    return settings.model_copy(
        update={"anchor": anchor_document(clock.anchor, clockwork(settings))}
    )


def starting_anchor(settings: ClockSettings) -> Anchor:
    """The anchor the clock starts with (``world_clock.base_anchor``)."""
    from epistrel.core.world_clock import base_anchor

    return base_anchor(settings)


def planned_at(planned: int | str, anchor: Anchor) -> tuple[int, int | None]:
    """``(planned offset, frame minute)`` of a seeded event planned at ``planned`` (see the
    module docstring); the frame minute is ``None`` when the offset is exact. 422 for a time of
    day the calendar's day does not have."""
    if isinstance(planned, int):
        return planned, None
    minute = _minutes(planned)
    day = anchor.clockwork.day_minutes
    if minute >= day:
        raise _invalid("the calendar's day has no such time", "planned", "planned")
    if not anchor.is_unanchored:
        segments = anchor.absolute_at(0).segments
        if len(segments) == 1 and segments[0].hi - segments[0].lo == 1:
            return (minute - segments[0].lo) % day, None
    at = anchor.place(minute, 0)
    return anchor.due_window(at)[1], at


def unknown_roles(names: Iterable[str | None], roles: Iterable[str]) -> list[str]:
    """The ``names`` (role ids) that name no role of ``roles``."""
    known = set(roles)
    return [n for n in names if n is not None and n not in known]


__all__ = [
    "GENESIS",
    "ROLE_PREFIX",
    "anchor_document",
    "anchor_statement",
    "clock_settings",
    "definition_of",
    "derived_inputs",
    "genesis_order",
    "planned_at",
    "role_sources",
    "starting_anchor",
    "unknown_roles",
]
