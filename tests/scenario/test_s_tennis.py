"""S-TENNIS, M4 part: deep repair on real predicates (docs/04 §7; FR-VERIFY-1,6, FR-REPAIR-1,2,
FR-PROP-3,5,8,10; #151).

Every variant plays a structured turn end to end through ``respond`` (the stub adapter scripts
Bobby's render with its declared propositions and, where the ladder reaches rung 3, the
regeneration) over the Bobby education fixture (``tests/fixtures/bobby_education.py``):

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
  scripted); a second bachelor's (a new degree id) is ``novel``; with ``bobby_ba`` jointly
  awarded by {OSU, Florida}, "partly from Florida" is ``supported``;
- ``test_s_tennis__camp`` (variant 3): a summer tennis camp at Florida is ``attended_program``
  (multi-valued): ``novel``, permitted as his own minor backstory, canonized.

Variant 1 with the varsity rule, variant 4 (with and without its rule) and the second degree
under a closed degree set need verdicts #140's rules do not give yet (forward implication of a
claim's consequences, a closed set over degree ids, a rule-free cross-predicate ``uncertain``):
#441.
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


def _degree(school: str, degree_id: str = "bobby_ba") -> dict[str, Any]:
    return claim("I", "degree_awarded", school, qualifiers={"degree_id": degree_id})


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
        stub.reset()
        stub.script(
            "generation",
            [_say("I got a second bachelor's at Florida in 2018.", _degree("Florida", "second"))],
        )
        await _play(client, edu, "Anything else?")
        assert (await _verdict(engine, edu.story_id))["kind"] == "novel"

        # -- the joint degree: partly from Florida is supported
        edu = await bobby_education(client, engine, joint_degree=True)
        stub.reset()
        stub.script("generation", [_say("My degree is partly from Florida.", _degree("Florida"))])
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
