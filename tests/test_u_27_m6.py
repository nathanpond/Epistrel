"""U-27: facts from the scenario, personality from the character (docs/04 §3), its M6 part
(#202): scheduled events seeded by the world and by a role, on the pure plan of a structured
genesis running the clock (``core.genesis.structured.plan``).

The M3 and M4 parts live in ``tests/test_u_27.py`` and the M5 part in ``tests/test_u_27_m5.py``
(one file per test ID and milestone). The scenario is the Sunday Dinner of
``tests/seeded_clock_support`` (Sunday 1987-10-25, 14:00, the world's 18:00 dinner). That the
events and beliefs are written is the write's, asserted by I-27 M6.
"""

from __future__ import annotations

from typing import Any

import pytest

from epistrel.core.catalog.scenario_models import ScenarioDraft
from epistrel.core.genesis import structured
from epistrel.core.genesis.materialize import CastRole, materialize
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.resolution.entities import character_spec
from epistrel.core.seeds.profiles import structured_m4_settings
from tests import clock_support, state_support
from tests import seeded_clock_support as sc
from tests import seeded_partitions_support as sp
from tests.profile_support import merged

ROLES = (*sp.NPC_ROLES, "neighbour", "player")
IDS = {role: f"01J9Z8Q4N5V6W7X8Y9Z0A1C{n:03d}" for n, role in enumerate(ROLES)}
NAMES = {**sp.NAMES, "neighbour": "Mr Pike", "player": "Sam"}
WORLD_DINNER = "starting_state.scheduled_events[0]"
ROLE_DINNER = "starting_state.scheduled_events[1]"
ERRAND = "starting_state.scheduled_events[2]"


@pytest.fixture(autouse=True)
def _delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    clock_support.deliver_stops(monkeypatch)
    state_support.deliver(monkeypatch)


def _settings(*, partitions: bool = True) -> ProfileSettings:
    layers = [structured_m4_settings(), sc.SEEDED]
    if partitions:
        layers.insert(1, sp.PARTITIONED)
    return ProfileSettings.model_validate(merged(*layers))


def _body(*events: dict[str, Any]) -> dict[str, Any]:
    """The family with ``events`` after the world's dinner."""
    return sc.family(scheduled_events=[sc.DINNER, *events])


def _plan(body: dict[str, Any], settings: ProfileSettings) -> structured.Plan:
    draft = ScenarioDraft.model_validate(body)
    members = [
        CastRole(
            role,
            IDS[role],
            is_player=role == "player",
            persona={**sp.PERSONA, "gender": "male"} if role == "player" else None,
        )
        for role in ROLES
    ]
    found = materialize("01J9Z8Q4N5V6W7X8Y9Z0A1CSCN", 1, draft.document(), draft, members)
    inputs = [
        structured.CastInput(
            character_id=IDS[role],
            role_id=role,
            name=NAMES[role],
            true_age=22 if role == "player" else 50,
            spec=character_spec(IDS[role], NAMES[role]),
            is_player=role == "player",
        )
        for role in ROLES
    ]
    return structured.plan(
        "01J9Z8Q4N5V6W7X8Y9Z0A1CSCN",
        1,
        "01J9Z8Q4N5V6W7X8Y9Z0A1CSTY",
        found.deferred,
        inputs,
        settings=settings,
        groups=draft.cast.groups,
    )


def _events(found: structured.Plan) -> dict[str, structured.PlannedFact]:
    """The planned event facts by their item."""
    return {
        f.key.removeprefix("event:"): f
        for f in found.facts
        if f.event is not None and f.proposition.predicate_id == SCHEDULED
    }


def _entry(found: structured.Plan, field_ref: str) -> Any:
    (entry,) = [e for e in found.entries if e.field_ref == field_ref]
    return entry


@pytest.mark.part("M6", "FR-SCN-8; FR-CLOCK-9")
def test_u_27__m6() -> None:
    """**Scheduled events.** The world's 18:00 dinner against the Mother's own seed of the same
    dinner at 19:00 (same subject, the Mother, and the same event, by name) → one event, the
    world's at 18:00 (offset 240); the Mother's is reported ``merge_conflict`` (winner the
    world's item, rule ``scheduled_same_event``) and not written. The Mother's own 15:00 errand
    is a different event and coexists: canon owned by her character, visible to her alone
    (``_seed_visibility`` of its ``self`` audience), and under partitions her ``known`` belief
    tied to that fact. The world's dinner is objective canon owned by the world.

    Complements: two different events both survive, whatever their times; without partitions
    the errand is canon with no belief; with the clock off every event stays deferred."""
    role_dinner = {"proposition": "The Sunday Dinner", "planned": "19:00", "owner": "mother"}
    errand = {
        "proposition": "the pharmacy errand",
        "planned": "15:00",
        "owner": "mother",
        "participants": ["mother"],
    }
    found = _plan(_body(role_dinner, errand), _settings())
    events = _events(found)
    assert set(events) == {WORLD_DINNER, ERRAND}
    dinner, own = events[WORLD_DINNER], events[ERRAND]
    assert dinner.event is not None
    assert own.event is not None
    assert (dinner.event.planned, dinner.event.owner) == (sc.DINNER_AT, "world")
    assert dinner.proposition.subject_id == IDS["mother"]
    assert dinner.visibility.scope == "objective"
    lost = _entry(found, ROLE_DINNER)
    assert (lost.outcomes, lost.winner, lost.loser, lost.rule) == (
        ["merge_conflict"],
        WORLD_DINNER,
        ROLE_DINNER,
        structured.SAME_EVENT_RULE,
    )
    assert _entry(found, WORLD_DINNER).outcomes == ["canon", "event"]

    assert (own.event.planned, own.event.owner, own.event.participants) == (
        60,
        IDS["mother"],
        (IDS["mother"],),
    )
    assert own.proposition.object_id != dinner.proposition.object_id
    assert (own.visibility.scope, own.visibility.characters) == ("characters", (IDS["mother"],))
    assert found.epistemic is not None
    held = [b for b in found.epistemic.beliefs if b.fact_key == own.key]
    assert [(b.holder, b.level.value, b.disclosure) for b in held] == [
        (IDS["mother"], "known", "open")
    ]
    assert not [b for b in found.epistemic.beliefs if b.fact_key == dinner.key]

    # -- complements
    later = {**role_dinner, "proposition": "the late supper"}
    both = _events(_plan(_body(later), _settings()))
    assert set(both) == {WORLD_DINNER, ROLE_DINNER}
    supper = both[ROLE_DINNER].event
    assert supper is not None
    assert supper.planned == 19 * 60 - sc.START

    plain = _plan(_body(role_dinner, errand), _settings(partitions=False))
    assert set(_events(plain)) == {WORLD_DINNER, ERRAND}
    assert plain.epistemic is None

    off = ProfileSettings.model_validate(merged(structured_m4_settings(), sp.PARTITIONED))
    unclocked = _plan(_body(role_dinner, errand), off)
    assert _events(unclocked) == {}
    assert not {WORLD_DINNER, ROLE_DINNER, ERRAND} & unclocked.materialized


@pytest.mark.part("M6", "FR-SCN-8; FR-CLOCK-9")
def test_u_27__m6_reveal() -> None:
    """**A planned reveal's ``scheduled`` truth** is ``engine`` canon once the story runs the
    clock (#574: every reveal truth stays with the director), never a belief or an event.
    Complement: with the clock off it stays deferred."""
    body = _body()
    body["drivers"] = {
        "planned_reveals": [
            {
                "truth": sp.seed(sp.CASSEROLE, "sister", ["self"], kind="scheduled"),
                "allowed_roles": ["player"],
            }
        ]
    }
    truth = "drivers.planned_reveals[0].truth"
    found = _plan(body, _settings())
    assert _entry(found, truth).outcomes == ["canon"]
    (fact,) = [f for f in found.facts if f.key.startswith(truth)]
    assert (fact.visibility.scope, fact.event) == ("engine", None)
    assert found.epistemic is not None
    assert not [b for b in found.epistemic.beliefs if b.act == truth]
    off = ProfileSettings.model_validate(merged(structured_m4_settings(), sp.PARTITIONED))
    unclocked = _plan(body, off)
    assert _entry(unclocked, truth).outcomes == ["deferred"]
    assert truth not in unclocked.materialized
