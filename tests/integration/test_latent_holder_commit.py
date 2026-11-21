"""#750 at the commit layer (FR-EPI-8, FR-TURN-4,12): the live shape, a latent with
``holder: "Ada"`` and ``visibility.characters: ["Ada"]`` (names, not ids), plays on the first
attempt and commits what the canonical form commits: canon's fact, scoped to Ada's id, with no
belief of Ada's written as its truth. The turn reports ``latent_holder_repaired``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body, respond, respond_client
from tests.story_support import override
from tests.structured_turn_support import beat, claim, place, render, structured_story

SECRET = "the key is under the third floorboard"


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def _play(
    settings: Settings, engine: AsyncEngine, latent: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One turn whose render carries ``latent`` (``"ADA"`` in it stands for Ada's id): the
    response, and what it committed for the latent."""
    async with respond_client(settings, engine) as (client, epistrel):
        story, ids = await structured_story(client, "Wren", "Ada")
        wren, ada = ids["Wren"], ids["Ada"]
        await override(client, story, {"repair": {"max_attempts": 0}})
        await place(engine, story, "tavern")
        fields = {
            k: ({**v, "characters": [ada if c == "ADA" else c for c in v["characters"]]})
            if k == "visibility" and "characters" in v
            else v
            for k, v in latent.items()
        }
        epistrel.models.stub.script(
            "generation",
            [
                render(
                    beat("do", "Ada sits down by the fire."),
                    beat(
                        "latent",
                        SECRET,
                        propositions=[claim("Ada", "located_at", "tavern")],
                        **fields,
                    ),
                )
            ],
        )
        response = await respond(client, story, body(wren, "Evening, Ada."))
        assert response.status_code == 200, response.text
        (fact,) = await rows(
            engine,
            "SELECT f.visibility, f.proposition_id FROM facts f "
            "WHERE f.story_id = :s AND f.provenance = 'render'",
            s=story,
        )
        held = await rows(
            engine,
            "SELECT holder_character_id FROM beliefs WHERE story_id = :s AND proposition_id = :p",
            s=story,
            p=fact.proposition_id,
        )
        found = {
            "visibility": fact.visibility,
            "believers": sorted(r.holder_character_id for r in held),
            "ada": ada,
        }
        return response.json(), found


async def test_a_named_latent_holder_commits_as_canon(
    stub_settings: Settings, engine: AsyncEngine, drained_embedding_jobs: None
) -> None:
    """Complement asserted: the canonical form (no holder, Ada's id) commits the same rows and
    reports no repair."""
    named, committed = await _play(
        stub_settings,
        engine,
        {"holder": "Ada", "visibility": {"scope": "characters", "characters": ["Ada"]}},
    )
    canonical, expected = await _play(
        stub_settings, engine, {"visibility": {"scope": "characters", "characters": ["ADA"]}}
    )
    assert committed["visibility"] == {"scope": "characters", "characters": [committed["ada"]]}
    assert expected["visibility"] == {"scope": "characters", "characters": [expected["ada"]]}
    # No viewpoint assigned the truth: Ada holds no belief written as the latent's truth.
    assert committed["ada"] not in committed["believers"]
    assert len(committed["believers"]) == len(expected["believers"])
    assert "latent_holder_repaired" in named["degradation"]
    assert "latent_holder_repaired" not in canonical["degradation"]
    (detail,) = [d for d in named["degradation_details"] if d["step"] == "latent_holder_repaired"]
    assert detail["cause"] == "latent"
