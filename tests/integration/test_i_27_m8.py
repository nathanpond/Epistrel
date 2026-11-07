"""I-27 M8 (FR-SCN-11, FR-IMG-2; #259): the visual style is in every image prompt and in no
text prompt.

The Sunday Dinner scenario (``tests/lite_genesis_support.sunday_dinner``, whose visual style
plants ``STYLE_PHRASE`` and ``NEGATIVE_STYLE_PHRASE``) is published, and a Lite story is created
from it and plays a turn. Its cover prompt (``POST /image-prompts``) carries the style, in the
rendered ``image_prompt`` input and in the prompt it answers, and the style's negative prompt
becomes the prompt's negative. No text-generation prompt of the story (genesis, the turn: #57
and #150's captured prompts) carries either phrase. #263's ``test_i_27__m8_story_style``: a story
image prompt for that story (the builder's story variant) carries the style too.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import genesis_support as g
from tests import image_prompt_support as ip
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests.auth_support import auth_settings
from tests.respond_support import body as body_of
from tests.respond_support import respond, respond_client

pytestmark = pytest.mark.integration


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.mark.part("M8", "FR-SCN-11; FR-IMG-2")
async def test_i_27__m8_style(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The Sunday Dinner scenario's visual style appears in its cover's image prompt, as the
    rendered input's fixed "Visual style" section and in the answered prompt, with the style's
    negative prompt as the prompt's negative.

    Complement: a story created from that scenario plays a turn, and neither style phrase is
    in any text-generation prompt it sent."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = lg.sunday_dinner(locked, default)
        scenario_id, version = await g.published_scenario(client, body)

        stub.reset()
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
            ),
        )
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        played = await respond(client, made["story_id"], body_of(by_role["player"], "Hello?"))
        assert played.status_code == 200, played.text
        text_calls = [c for c in stub.calls if c.purpose != ip.PURPOSE]
        assert text_calls
        sent = "\n".join(ip.rendered(c) for c in text_calls)
        assert lg.STYLE_PHRASE not in sent
        assert lg.NEGATIVE_STYLE_PHRASE not in sent

        stub.reset()
        cover = await ip.built(client, "scenario", scenario_id, "cover")
        assert cover["target"]["version"] == version
        assert lg.STYLE_PHRASE in cover["prompt"]
        assert cover["negative_prompt"] == lg.NEGATIVE_STYLE_PHRASE
        assert (cover["seed"], cover["reference_image_ids"]) == (None, [])
        assert (cover["size"], cover["aspect_ratio"]) == (None, "16:9")
        [call] = ip.prompt_calls(stub)
        rendered = ip.rendered(call)
        assert f"## Visual style\n{lg.STYLE_PHRASE}" in rendered
        assert "## Rating image guidance\n" in rendered
        assert lg.PUBLIC_PHRASE in rendered
        # A cover depicts no slot characters: the subjects section is empty.
        assert "## Subjects\n(none)" in rendered


@pytest.mark.usefixtures("drained_embedding_jobs")
@pytest.mark.part("M8", "FR-SCN-11; FR-IMG-2")
async def test_i_27__m8_story_style(stub_settings: Settings, engine: AsyncEngine) -> None:
    """A story image prompt for a Lite story created from the Sunday Dinner scenario (the prompt
    builder's story variant, ``target.kind = story``, after the first reply) carries the
    scenario's visual style, in the rendered input's "Visual style" section and in the prompt it
    answers, with the style's negative prompt as the prompt's negative; and it is built from the
    message the player was shown.

    Complement asserted: the turn's text-generation prompts carry neither style phrase."""
    async with respond_client(stub_settings, engine) as (client, lib):
        stub = lib.models.stub
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        scenario_id, _version = await g.published_scenario(
            client, lg.sunday_dinner(locked, default)
        )
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
            ),
        )
        story = made["story_id"]
        by_role = {c["role_id"]: c["id"] for c in made["characters"]}
        stub.reset()
        played = await respond(client, story, body_of(by_role["player"], "Hello?"))
        assert played.status_code == 200, played.text
        sent = "\n".join(ip.rendered(c) for c in stub.calls if c.purpose != ip.PURPOSE)
        assert lg.STYLE_PHRASE not in sent
        assert lg.NEGATIVE_STYLE_PHRASE not in sent

        stub.reset()
        response = await client.post(
            "/image-prompts",
            json={
                "target": {
                    "kind": "story",
                    "story_id": story,
                    "position": {"turn": played.json()["turn"], "beat": 2},
                    "audience_viewpoint": "omniscient",
                }
            },
            headers=ip.HEADERS,
        )
        assert response.status_code == 200, response.text
        built = response.json()
        assert lg.STYLE_PHRASE in built["prompt"]
        assert built["negative_prompt"] == lg.NEGATIVE_STYLE_PHRASE
        assert built["viewpoint_basis"] == "filtered_inputs"
        [call] = ip.prompt_calls(stub)
        rendered = ip.rendered(call)
        assert f"## Visual style\n{lg.STYLE_PHRASE}" in rendered
        assert played.json()["message"]["text"] in rendered
