"""The Bobby education fixture (docs/04 §7; shared by the education scenarios, #151's
S-TENNIS first).

A structured story (``Structured (M4)``) with Cal, the player, and Bobby. Canon, the author's:
``enrolled_at(Bobby, OSU)`` and ``degree_awarded(Bobby, {OSU}, degree_id = bobby_ba, level =
bachelor)``; OSU ``located_in`` Ohio, whose ``climate`` (an author predicate) is cold winters;
and the author's closed set "Bobby's only enrollment is OSU". The story's rules are registered
the way #149's structured genesis registers a scenario's ``starting_state`` rules
(``core.propositions.registry.register_author``): the author predicates ``team_of`` (a team's
school, the varsity rule's ``via`` join, #131) and ``climate``, the closed set and, when asked,
the declared implication ``varsity``: varsity ``member_of`` a college team implies
``enrolled_at`` its school (``{when: member_of, then: enrolled_at, via: team_of}``). The
University of Florida (alias "Florida") and its varsity tennis team (``team_of`` it) are
entities. (#441 adds variant 4's presence rule and the closed degree set.)

No core implication links team membership, enrollment and degrees; the scenarios that need one
declare it. The story's clock is unanchored, so the undergraduate years are open intervals here:
an undated claim holds from the turn on, and overlaps them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.canon_support import commit
from tests.structured_turn_support import structured_story

from epistrel.core.canon.facts import Fact
from epistrel.core.enums import OperationKind
from epistrel.core.propositions import registry, store
from epistrel.core.propositions.model import Granularity, Instant, Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.resolution.time import STORY_MINUTES_PER_TURN

ENROLLED_AT = CORE_BY_NAME["enrolled_at"].id
DEGREE_AWARDED = CORE_BY_NAME["degree_awarded"].id
LOCATED_IN = CORE_BY_NAME["located_in"].id
MEMBER_OF = CORE_BY_NAME["member_of"].id
ATTENDED = CORE_BY_NAME["attended_program"].id
ALWAYS = Interval.of(None, None)
GRADUATION = STORY_MINUTES_PER_TURN
"""The story-minute ``bobby_ba`` was awarded (an event): the first turn's, because an undated
claim about an event is normalized at its turn's minute (``default_time``) and the unanchored
clock has no calendar to date it by, so a first-turn claim about the degree meets it."""

VARSITY = {"when": "member_of", "then": "enrolled_at", "via": "team_of"}


@dataclass(frozen=True)
class Education:
    """The fixture's story, its character ids and the canon facts it wrote."""

    story_id: str
    cal: str
    bobby: str
    osu: str
    florida: str
    """The University of Florida (alias "Florida")."""
    team: str
    """Florida's varsity tennis team."""
    ohio: str
    cold: str
    enrollment: Fact
    degree: Fact
    osu_in_ohio: Fact
    winters: Fact


async def _author(engine: AsyncEngine, story_id: str, proposition: Proposition) -> Fact:
    return await commit(
        engine, story_id, proposition, kind=OperationKind.CONTROL, provenance="author"
    )


def _p(subject: str, predicate: str, obj: str | None = None, **fields: Any) -> Proposition:
    return Proposition(
        subject_id=subject,
        predicate_id=predicate,
        object_id=obj,
        valid_time=fields.pop("valid_time", ALWAYS),
        **fields,
    )


async def bobby_education(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    *,
    varsity: bool = True,
    closed_enrollment: bool = True,
    joint_degree: bool = False,
) -> Education:
    """The fixture (see the module docstring); ``joint_degree``: ``bobby_ba`` was awarded by
    {OSU, the University of Florida} jointly."""
    story, ids = await structured_story(client, "Cal", "Bobby")
    bobby = ids["Bobby"]
    async with engine.begin() as conn:
        osu = await store.add_entity(conn, story, "organization", "OSU", aliases=("Ohio State",))
        florida = await store.add_entity(
            conn, story, "organization", "University of Florida", aliases=("Florida",)
        )
        team = await store.add_entity(
            conn,
            story,
            "organization",
            "Florida varsity tennis",
            aliases=("Florida's college team", "Florida's team"),
        )
        ohio = await store.add_entity(conn, story, "place", "Ohio")
        cold = await store.add_entity(conn, story, "other", "cold winters")
        rules = registry.ScenarioRules.parse(
            {
                "predicates": [
                    {
                        "name": "team_of",
                        "subject_type": "organization",
                        "object_type": "organization",
                        "cardinality": "functional",
                        "temporal_kind": "permanent",
                        "docs": "The school a varsity team belongs to.",
                    },
                    {
                        "name": "climate",
                        "subject_type": "place",
                        "temporal_kind": "permanent",
                        "docs": "A place's climate.",
                    },
                ],
                "closed_sets": (
                    [{"predicate": "enrolled_at", "subject": "bobby", "members": ["osu"]}]
                    if closed_enrollment
                    else []
                ),
                "rules": [VARSITY] if varsity else [],
            }
        )
        reg = await registry.register_author(
            conn, story, rules, resolve={"bobby": bobby, "osu": osu}.__getitem__
        )
    team_of, climate = reg.named("team_of"), reg.named("climate")
    assert team_of is not None
    assert climate is not None
    enrollment = await _author(engine, story, _p(bobby, ENROLLED_AT, osu))
    degree = await _author(
        engine,
        story,
        Proposition(
            subject_id=bobby,
            predicate_id=DEGREE_AWARDED,
            object_set=(osu, florida) if joint_degree else (osu,),
            qualifiers={"degree_id": "bobby_ba", "level": "bachelor"},
            valid_time=Instant.of(GRADUATION, Granularity.MINUTE),
        ),
    )
    osu_in_ohio = await _author(engine, story, _p(osu, LOCATED_IN, ohio))
    winters = await _author(engine, story, _p(ohio, climate.id, cold))
    await _author(engine, story, _p(team, team_of.id, florida))
    return Education(
        story_id=story,
        cal=ids["Cal"],
        bobby=bobby,
        osu=osu,
        florida=florida,
        team=team,
        ohio=ohio,
        cold=cold,
        enrollment=enrollment,
        degree=degree,
        osu_in_ohio=osu_in_ohio,
        winters=winters,
    )


__all__ = [
    "ATTENDED",
    "DEGREE_AWARDED",
    "ENROLLED_AT",
    "MEMBER_OF",
    "VARSITY",
    "Education",
    "bobby_education",
]
