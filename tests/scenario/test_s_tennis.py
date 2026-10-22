"""S-TENNIS, M4 part: deep repair on real predicates (docs/04 §7; FR-VERIFY-1,6, FR-REPAIR-1,2,
FR-PROP-3,5,8,10; #151).

Every variant plays a structured turn end to end through ``respond`` (the stub adapter scripts
Bobby's render with its declared propositions and, where the ladder reaches rung 3, the
regeneration) over the Bobby education fixture (``tests/fixtures/bobby_education.py``):

- ``test_s_tennis__varsity`` (variant 1): the same line with the author's varsity rule and the
  closed set "Bobby's only enrollment is OSU": the rule derives ``enrolled_at(Bobby, University
  of Florida)`` through ``team_of``, a new member of the closed set, so the claim is
  ``contradicted`` citing the rule's join and the closed set (#441's forward implication); the
  repair reads the enrollment's neighborhood OSU → Ohio → cold winters and its regeneration is
  coherent (a winter club in Ohio, scripted);
- ``test_s_tennis`` (variant 1b): "I played on Florida's college team as an undergrad" declares
  ``member_of(Bobby, Florida varsity tennis, role = player)``, never a degree or an institution
  of degree; with no declared rule and no closed set it is ``novel`` (team membership
  establishes nothing about enrollment or degrees), then the permission check: his own minor
  backstory, permitted and canonized as membership only;
- ``test_s_tennis__degree`` (variant 2): "my degree is from Florida" resolves to ``bobby_ba``:
  the awarding set {Florida} against {OSU} on the same key is ``contradicted``
  (``functional_key``) and accepted at rung 2 ("from OSU"); the same claim in prose that never
  names Florida reaches rung 3, whose regeneration reads the contradicted degree and its
  neighborhood OSU → Ohio → cold winters (depth 2) and is coherent (a winter club in Ohio,
  scripted); a second bachelor's (a new degree id) is ``novel``, and ``contradicted`` only
  under the closed degree set "Bobby's only undergraduate degree is ``bobby_ba``" (a closed set
  over the key set ``degree_id``, #441); with ``bobby_ba`` jointly awarded by {OSU, Florida},
  "partly from Florida" is ``supported``, the closed degree set notwithstanding;
- ``test_s_tennis__camp`` (variant 3): a summer tennis camp at Florida is ``attended_program``
  (multi-valued): ``novel``, permitted as his own minor backstory, canonized;
- ``test_s_tennis__presence`` (variant 4): canon says "Bobby has never been to Florida" and
  places the University of Florida in the Florida peninsula. With the author's rule
  ``attended_program ⇒ located_at`` the camp claim is ``contradicted``, citing the negation and
  the containment (#441's forward implication, through containment); without the rule it is
  ``uncertain(world_knowledge)`` (#441's rule-free cross-predicate conflict) and goes to the
  model residual (the entailment answer is scripted), which the ladder repairs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.fixtures.bobby_education import (
    ATTENDED,
    DEGREE_AWARDED,
    ENROLLED_AT,
    MEMBER_OF,
    Education,
    bobby_education,
)
from tests.respond_support import body, respond, respond_client
from tests.structured_turn_support import beat, claim, render, turn_report

pytestmark = pytest.mark.integration

VARSITY_LINE = (
    "I played on Florida's college team as an undergrad, loved the year-round outdoor tennis."
)
WINTER_CLUB = "I studied at OSU and played at a winter club in Ohio, those cold winters."
CAMP_LINE = "I learned at a summer tennis camp at Florida."


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


def _say(text_: str, *props: dict[str, Any]) -> str:
    return render(beat("say", text_, intent="sincere", propositions=list(props)))


def _degree(school: str, degree_id: str = "bobby_ba", **extra: str) -> dict[str, Any]:
    return claim("I", "degree_awarded", school, qualifiers={"degree_id": degree_id, **extra})


def _member() -> dict[str, Any]:
    return claim("I", "member_of", "Florida varsity tennis", qualifiers={"role": "player"})


def _regenerated(text_: str) -> str:
    return json.dumps({"text": text_, "propositions": []})


async def _closed_set(engine: AsyncEngine, story_id: str, predicate: str) -> str:
    (row,) = await rows(
        engine,
        "SELECT id FROM closed_sets WHERE story_id = :s AND predicate_id = :p",
        s=story_id,
        p=predicate,
    )
    return str(row.id)


async def _play(client: Any, edu: Education, line: str) -> dict[str, Any]:
    response = await respond(client, edu.story_id, body(edu.cal, line))
    assert response.status_code == 200, response.text
    played: dict[str, Any] = response.json()
    return played


async def _verdict(engine: AsyncEngine, story_id: str) -> dict[str, Any]:
    (found,) = (await turn_report(engine, story_id))["verify"]["verdicts"]
    assert isinstance(found, dict)
    return found


async def _facts(engine: AsyncEngine, story_id: str, predicate: str) -> list[Any]:
    """The current canon facts of ``predicate``."""
    return await rows(
        engine,
        "SELECT f.id, p.object_id, p.object_set, p.qualifiers FROM facts f JOIN propositions p "
        "ON p.id = f.proposition_id WHERE f.story_id = :s AND f.predicate_id = :p "
        "AND f.tx_to IS NULL AND f.superseded_by IS NULL",
        s=story_id,
        p=predicate,
    )


async def _declared(engine: AsyncEngine, story_id: str) -> list[dict[str, Any]]:
    """The propositions Bobby's ``say`` declared, as its beat event stores them."""
    (event,) = await rows(
        engine,
        "SELECT payload FROM events WHERE story_id = :s AND type = 'beat_say'",
        s=story_id,
    )
    return [p["proposition"] for p in event.payload["propositions"]]


@pytest.mark.part("M4", "FR-VERIFY-1,6; FR-REPAIR-1,2; FR-PROP-3,5,8,10")
async def test_s_tennis__varsity(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Variant 1 (see the module docstring). Complement: variant 1b (``test_s_tennis``), the
    same line without the rule and the closed set, is ``novel``; here nothing from the
    rejected render is canonized."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        edu = await bobby_education(client, engine)
        stub = epistrel.models.stub
        stub.script("generation", [_say(VARSITY_LINE, _member())])
        stub.script("regeneration", [_regenerated(WINTER_CLUB)])
        played = await _play(client, edu, "Did you play in college?")
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["rule"]) == ("contradicted", "closed_set")
        only_osu = await _closed_set(engine, edu.story_id, ENROLLED_AT)
        assert {edu.team_school.proposition_id, only_osu} <= set(verdict["evidence_ids"])
        (entry,) = (await turn_report(engine, edu.story_id))["repairs"]
        assert (entry["rung"], entry["accepted"]) == (3, True)
        reached = {i["id"] for c in entry["neighborhood"] for i in c["items"]}
        assert {edu.enrollment.id, edu.osu_in_ohio.id, edu.winters.id} <= reached
        assert played["message"]["text"] == f'Bobby: "{WINTER_CLUB}"'
        assert await _facts(engine, edu.story_id, MEMBER_OF) == []
        assert [r.object_id for r in await _facts(engine, edu.story_id, ENROLLED_AT)] == [edu.osu]


@pytest.mark.part("M4", "FR-VERIFY-1,6; FR-REPAIR-1,2; FR-PROP-3,5,8,10")
async def test_s_tennis(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Variant 1b (see the module docstring). Complement: no enrollment or degree is
    written, and nothing is repaired."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        edu = await bobby_education(client, engine, varsity=False, closed_enrollment=False)
        epistrel.models.stub.script(
            "generation",
            [
                _say(
                    VARSITY_LINE,
                    claim(
                        "I", "member_of", "Florida varsity tennis", qualifiers={"role": "player"}
                    ),
                )
            ],
        )
        played = await _play(client, edu, "Did you play in college?")
        (declared,) = await _declared(engine, edu.story_id)
        assert (declared["predicate_id"], declared["object_id"]) == (MEMBER_OF, edu.team)
        assert declared["qualifiers"] == {"role": "player"}
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["permission"]) == ("novel", "permitted:own")
        assert "repairs" not in await turn_report(engine, edu.story_id)
        assert played["message"]["text"] == f'Bobby: "{VARSITY_LINE}"'
        assert [r.object_id for r in await _facts(engine, edu.story_id, MEMBER_OF)] == [edu.team]
        assert [r.object_id for r in await _facts(engine, edu.story_id, ENROLLED_AT)] == [edu.osu]
        assert len(await _facts(engine, edu.story_id, DEGREE_AWARDED)) == 1


@pytest.mark.part("M4", "FR-VERIFY-1,6; FR-REPAIR-1,2; FR-PROP-3,5,8,10")
async def test_s_tennis__degree(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Variant 2 (see the module docstring). Complement: no degree from Florida is ever
    canonized; the joint degree's claim is not repaired."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- rung 2: the awarding set takes canon's value
        edu = await bobby_education(client, engine)
        stub.script("generation", [_say("My degree is from Florida.", _degree("Florida"))])
        played = await _play(client, edu, "Where did you graduate?")
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["rule"]) == ("contradicted", "functional_key")
        (entry,) = (await turn_report(engine, edu.story_id))["repairs"]
        assert (entry["rung"], entry["contradicted_facts"]) == (2, [edu.degree.id])
        assert played["message"]["text"] == 'Bobby: "My degree is from OSU."'
        assert [list(r.object_set) for r in await _facts(engine, edu.story_id, DEGREE_AWARDED)] == [
            [edu.osu]
        ]

        # -- rung 3: the prose never names Florida; the regeneration reads the neighborhood
        stub.reset()
        stub.script(
            "generation",
            [_say("I graduated down south, in Gainesville.", _degree("Florida"))],
        )
        stub.script(
            "regeneration",
            [json.dumps({"text": WINTER_CLUB, "propositions": []})],
        )
        played = await _play(client, edu, "And where was that?")
        (entry,) = (await turn_report(engine, edu.story_id))["repairs"]
        assert (entry["rung"], entry["accepted"]) == (3, True)
        reached = {i["id"] for c in entry["neighborhood"] for i in c["items"]}
        assert {edu.degree.id, edu.osu_in_ohio.id, edu.winters.id} <= reached
        (call,) = [c for c in stub.calls if c.purpose == "regeneration"]
        prompt = "\n".join(m.content for m in call.messages)
        assert "OSU located_in Ohio" in prompt
        assert "Ohio climate cold winters" in prompt
        assert "I graduated down south, in Gainesville." in prompt
        assert played["message"]["text"] == f'Bobby: "{WINTER_CLUB}"'

        # -- a second bachelor's: a new degree id is novel
        second = _degree("Florida", "second", level="bachelor")
        second_line = "I got a second bachelor's at Florida in 2018."
        stub.reset()
        stub.script("generation", [_say(second_line, second)])
        await _play(client, edu, "Anything else?")
        assert (await _verdict(engine, edu.story_id))["kind"] == "novel"

        # -- ... and contradicted under the closed degree set
        edu = await bobby_education(client, engine, closed_degrees=True)
        stub.reset()
        stub.script("generation", [_say(second_line, second)])
        stub.script("regeneration", [_regenerated("I only have the one degree, from OSU.")])
        await _play(client, edu, "Anything else?")
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["rule"]) == ("contradicted", "closed_set")
        only_bachelor = await _closed_set(engine, edu.story_id, DEGREE_AWARDED)
        assert verdict["evidence_ids"] == [only_bachelor]
        assert [
            r.qualifiers["degree_id"] for r in await _facts(engine, edu.story_id, DEGREE_AWARDED)
        ] == ["bobby_ba"]

        # -- the joint degree: partly from Florida is supported
        edu = await bobby_education(client, engine, joint_degree=True, closed_degrees=True)
        stub.reset()
        stub.script(
            "generation",
            [_say("My degree is partly from Florida.", _degree("Florida", level="bachelor"))],
        )
        played = await _play(client, edu, "Where did you graduate?")
        assert (await _verdict(engine, edu.story_id))["kind"] == "supported"
        assert "repairs" not in await turn_report(engine, edu.story_id)
        assert played["message"]["text"] == 'Bobby: "My degree is partly from Florida."'


@pytest.mark.part("M4", "FR-VERIFY-1,6; FR-REPAIR-1,2; FR-PROP-3,5,8,10")
async def test_s_tennis__camp(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Variant 3 (see the module docstring). Complement: no enrollment at Florida follows from
    the camp (``attended_program`` implies no enrollment)."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        edu = await bobby_education(client, engine)
        epistrel.models.stub.script(
            "generation", [_say(CAMP_LINE, claim("I", "attended_program", "Florida"))]
        )
        played = await _play(client, edu, "Where did you learn tennis?")
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["permission"]) == ("novel", "permitted:own")
        assert played["message"]["text"] == f'Bobby: "{CAMP_LINE}"'
        assert [r.object_id for r in await _facts(engine, edu.story_id, ATTENDED)] == [edu.florida]
        assert [r.object_id for r in await _facts(engine, edu.story_id, ENROLLED_AT)] == [edu.osu]


@pytest.mark.part("M4", "FR-VERIFY-1,6; FR-REPAIR-1,2; FR-PROP-3,5,8,10")
async def test_s_tennis__presence(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Variant 4 (see the module docstring). Complement: variant 3 (``test_s_tennis__camp``),
    with no negated presence, is ``novel``; the rule-free conflict is never ``novel`` and never
    decided without the model; no camp is canonized either way."""
    elsewhere = "I learned at a summer tennis camp back in Ohio."
    async with respond_client(stub_settings, engine) as (client, epistrel):
        stub = epistrel.models.stub

        # -- with the presence rule: contradicted, citing the negation and the containment
        edu = await bobby_education(client, engine, never_in_florida=True, presence_rule=True)
        assert edu.never_there is not None
        assert edu.uf_on_peninsula is not None
        stub.script("generation", [_say(CAMP_LINE, claim("I", "attended_program", "Florida"))])
        stub.script("regeneration", [_regenerated(elsewhere)])
        played = await _play(client, edu, "Where did you learn tennis?")
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["rule"], verdict["escalated"]) == (
            "contradicted",
            "implication",
            False,
        )
        assert {edu.never_there.proposition_id, edu.uf_on_peninsula.proposition_id} <= set(
            verdict["evidence_ids"]
        )
        assert (await turn_report(engine, edu.story_id))["repairs"]
        assert played["message"]["text"] == f'Bobby: "{elsewhere}"'
        assert await _facts(engine, edu.story_id, ATTENDED) == []

        # -- without it: uncertain, so the model residual decides
        edu = await bobby_education(client, engine, never_in_florida=True)
        stub.reset()
        stub.script("generation", [_say(CAMP_LINE, claim("I", "attended_program", "Florida"))])
        stub.script("entailment", [json.dumps({"verdict": "contradicted", "confidence": 0.9})])
        stub.script("regeneration", [_regenerated(elsewhere)])
        played = await _play(client, edu, "Where did you learn tennis?")
        verdict = await _verdict(engine, edu.story_id)
        assert (verdict["kind"], verdict["reason"], verdict["escalated"]) == (
            "contradicted",
            "world_knowledge",
            True,
        )
        assert [c.purpose for c in stub.calls].count("entailment") == 1
        assert (await turn_report(engine, edu.story_id))["verify"]["escalated"] == 1
        assert (await turn_report(engine, edu.story_id))["repairs"]
        assert played["message"]["text"] == f'Bobby: "{elsewhere}"'
        assert await _facts(engine, edu.story_id, ATTENDED) == []
