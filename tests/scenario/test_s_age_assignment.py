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
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import READ
from tests.structured_turn_support import (
    UNKNOWN,
    ages,
    beat,
    claim,
    labels_of,
    render,
    schema_calls,
    structured_story,
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
        assert spoken["propositions"][0]["verdict"] == "contradicted"
        # No new character, no second age.
        again = await _cast(client, story, "omniscient")
        assert len(again) == len(omniscient)
        assert again["the old man"]["true_age"] == 64
