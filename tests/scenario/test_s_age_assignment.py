"""S-AGE-ASSIGNMENT (M4): every character a structured story introduces gets one true age,
lazily (FR-CAT-12, FR-TURN-6, FR-RESP-12; docs/04 §7; #150). The M3 part (a role's eligibility
range) is in ``tests/integration/test_story_from_scenario.py``.

A narration introduces four people without an age: an old man who stops to help with a flat
(60s), a student on a college campus (18-23), a student at freshman orientation (18) and a
mother with three young children (27-35). The turn repairs them within itself: one batched
age-repair call answers each one integer inside its clue's range, recorded on the character
(``true_age_source = repaired``) and as a canon ``age`` fact from the turn's story-time. An
omniscient read returns the exact age; a character's read does not. When the old man later
says "I'm 70", the claim is checked against canon (64) and labelled on the three dimensions.

``test_s_age_assignment__m5`` (FR-CAT-12, FR-RESP-12; #170): with per-character partitions, each
rendered character's own context (``core.epistemic.context.build_context``) holds its true age
("You are 34."), and every other present character appears on its ``## Scene`` line with only
an apparent band (``age_band``: "mid thirties", "teens", "elderly"), never the integer, which is
also left out of the canon lines. Once Bobby believes Marcus is 17, Bobby's card shows the age
he believes. Complement: in shared mode (partitions off) no context carries an age line.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.epistemic.context import age_band
from epistrel.core.propositions.model import Interval, LiteralKind, Proposition, TypedLiteral
from tests import partition_support as parts
from tests import perception_support as ps
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ, character, override
from tests.structured_turn_support import (
    AGE,
    UNKNOWN,
    ages,
    beat,
    claim,
    history,
    labels_of,
    render,
    schema_calls,
    structured_story,
    text_of,
)

PEOPLE: list[dict[str, Any]] = [
    {"name": "the old man", "age_range": [60, 69], "clue": "stops to help with a flat tyre"},
    {"name": "Priya", "age_range": [18, 23], "clue": "a student crossing the college campus"},
    {"name": "Theo", "age_range": [18, 18], "clue": "a student at freshman orientation"},
    {"name": "Mara", "age_range": [27, 35], "clue": "a mother with three young children"},
]
REPAIRED = {"the old man": 64, "Priya": 20, "Theo": 18, "Mara": 31}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _cast(client: Any, story_id: str, viewpoint: str | None) -> dict[str, Any]:
    params = {} if viewpoint is None else {"viewpoint": viewpoint}
    response = await client.get(f"/stories/{story_id}", params=params, headers=READ)
    assert response.status_code == 200, response.text
    return {c["name"]: c for c in response.json()["characters"]}


@pytest.mark.part("M4", "FR-CAT-12; FR-TURN-6; FR-RESP-12")
async def test_s_age_assignment__structured(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """The four introductions each get one integer in range, recorded once with the turn's
    story-time; the omniscient read shows it, a character's does not; "I'm 70" against canon
    64 is ``false``, ``sincere`` and ``unknown``."""
    async with respond_client(stub_settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        # The repair ladder off (#151's ``repair.max_attempts`` 0): a false say stands, labelled;
        # tests/integration/test_i_12.py repairs one.
        await override(client, story, {"repair": {"max_attempts": 0}})
        wren = ids["Wren"]
        stub = epistrel.models.stub
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "narration",
                        "On the road, the old man stops to help with a flat tyre; Priya crosses "
                        "the campus, Theo waits at freshman orientation, and Mara walks by "
                        "with her three young children.",
                    ),
                    introduces=PEOPLE,
                ),
                render(beat("do", "Ada watches the road.")),
                ages(**REPAIRED),
            ],
        )
        response = await respond(client, story, body(wren, None, do="looks around the street"))
        assert response.status_code == 200, response.text
        (repair,) = schema_calls(stub, "AgeRepairAnswer")
        assert repair.params.temperature == 0.0
        for person in PEOPLE:
            assert person["clue"] in repair.messages[-1].content

        omniscient = await _cast(client, story, "omniscient")
        for name, age in REPAIRED.items():
            assert omniscient[name]["true_age"] == age
        recorded = await rows(
            engine,
            "SELECT name, true_age, true_age_source, age_recorded_at FROM characters "
            "WHERE story_id = :s AND true_age_source = 'repaired' ORDER BY ordinal",
            s=story,
        )
        assert {r.name: r.true_age for r in recorded} == REPAIRED
        for row, person in zip(recorded, PEOPLE, strict=True):
            low, high = person["age_range"]
            assert low <= row.true_age <= high
            assert row.age_recorded_at is not None
        # Each age is one canon fact, from the introducing turn's story-time.
        facts = await rows(
            engine,
            "SELECT c.name, p.literal, f.valid_from FROM facts f "
            "JOIN propositions p ON p.id = f.proposition_id "
            "JOIN characters c ON c.id = f.subject_id "
            "WHERE f.story_id = :s AND c.true_age_source = 'repaired'",
            s=story,
        )
        assert {f.name: f.literal["value"] for f in facts} == REPAIRED
        assert len(facts) == len(REPAIRED)
        assert len({f.valid_from for f in facts}) == 1
        # A character's read never carries a true age.
        mine = await _cast(client, story, f"character:{wren}")
        assert all("true_age" not in c for c in mine.values())

        # The old man joins the cast: "I'm 70" is checked against canon's 64.
        old_man = omniscient["the old man"]["id"]
        stub.script(
            "generation",
            [
                render(
                    beat(
                        "say",
                        "I'm 70, you know.",
                        intent="sincere",
                        propositions=[claim("I", "age", "70")],
                    )
                )
            ],
        )
        later = await respond(
            client, story, body(wren, "How old are you?", render_character_ids=[old_man])
        )
        assert later.status_code == 200, later.text
        (spoken,) = [b for b in later.json()["beats"] if b["kind"] == "beat_say"]
        assert labels_of(spoken) == ("false", UNKNOWN, "sincere")
        # #177: a character read carries no verdicts; the omniscient read keeps them.
        assert spoken["propositions"][0]["verdict"] is None
        seen = await history(client, story, "omniscient")
        (told,) = [i for i in seen if i["kind"] == "beat_say" and i["text"] == "I'm 70, you know."]
        assert told["propositions"][0]["verdict"] == "contradicted"
        # No new character, no second age.
        again = await _cast(client, story, "omniscient")
        assert len(again) == len(omniscient)
        assert again["the old man"]["true_age"] == 64


TRUE_AGES = {"Wren": 25, "Jenni": 34, "Marcus": 17, "Bobby": 71}
BANDS = {"Wren": "mid twenties", "Jenni": "mid thirties", "Marcus": "teens", "Bobby": "elderly"}


@pytest.mark.part("M5", "FR-CAT-12; FR-RESP-12")
async def test_s_age_assignment__m5(
    stub_settings: Settings,
    engine: AsyncEngine,
    drained_embedding_jobs: None,
    perception_delivered: None,
) -> None:
    """See the module docstring."""
    assert [age_band(a) for a in (None, 8, 15, 20, 24, 29, 34, 47, 69, 70)] == [
        None,
        "child",
        "teens",
        "early twenties",
        "mid twenties",
        "late twenties",
        "mid thirties",
        "late forties",
        "late sixties",
        "elderly",
    ]
    async with respond_client(stub_settings, engine) as (client, epistrel):
        cast = [character(name, age, is_player=(name == "Wren")) for name, age in TRUE_AGES.items()]
        story, ids = await structured_story(client, characters=cast)
        await ps.perceive(engine, story, "full")
        parlor = await ps.place_entity(engine, story, "parlor")
        for name in TRUE_AGES:
            await ps.put(engine, story, ids[name], parlor)
        stub = epistrel.models.stub

        async def context_of(name: str) -> str:
            stub.calls.clear()
            stub.script("generation", [render(beat("do", f"{name} nods."))])
            answer = await respond(
                client,
                story,
                body(ids["Wren"], "How old is everyone here?", render_character_ids=[ids[name]]),
            )
            assert answer.status_code == 200, answer.text
            (call,) = schema_calls(stub, "CharacterRender")
            return text_of(call)

        for name in ("Jenni", "Marcus", "Bobby"):
            own = await context_of(name)
            assert f"You are {TRUE_AGES[name]}." in own, name
            for other, age in TRUE_AGES.items():
                if other == name:
                    continue
                assert f"{other} ({BANDS[other]})" in own, (name, other)
                assert f"You are {age}." not in own, (name, other)
                assert f"({age})" not in own, (name, other)
                assert f"age {age}" not in own, (name, other)

        # Bobby comes to believe Marcus is 17: his card shows the age he believes.
        await parts.believe(
            engine,
            story,
            ids["Bobby"],
            Proposition(
                subject_id=ids["Marcus"],
                predicate_id=AGE,
                literal=TypedLiteral(kind=LiteralKind.INT, value=17),
                valid_time=Interval.of(None, None),
            ),
        )
        bobby = await context_of("Bobby")
        assert "Marcus (17)" in bobby
        assert f"Jenni ({BANDS['Jenni']})" in bobby

        # Complement: shared knowledge (partitions off) carries no age line.
        await ps.perceive(engine, story, "none")
        shared = await context_of("Jenni")
        assert "You are 34." not in shared
        assert "(mid thirties)" not in shared
