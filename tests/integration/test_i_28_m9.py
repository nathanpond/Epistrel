"""I-28's M9 parts: the story cast limit (FR-MULTI-12; #828).

The limit is the deployment setting ``max_story_cast`` (default 50, at most 200). It counts a
story's full characters (weight ``major``: the cast, the player, characters added later and
characters promoted), never a ``minor`` one a turn introduced or modeled. Creation, adding a
character and promoting one to ``major`` are refused at the limit with 422 ``validation_error``
``{cap: "cast_max", limit, actual}``; a turn, lazy modeling and the entity-model route never are.
Lowering the setting never breaks a story already above it.

The setting is deployment-wide: each test that changes it puts the previous value back
(``cast_limit``). Every I-28 M9 part lives in this file (the traceability gate takes one file per
test ID and milestone).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.deployment import SettingsPatch
from epistrel.core.errors import InvalidRequest
from epistrel.core.propositions import store
from epistrel.core.settings_schema import DEFAULT_MAX_STORY_CAST, STORY_CAST_LIMIT_MAX
from epistrel.core.stories import AdHocCharacter, CharacterSpec, CharacterWeightRequest, StorySpec
from tests import genesis_support as g
from tests.auth_support import auth_settings, bearer
from tests.event_support import rows
from tests.respond_support import body as respond_body
from tests.respond_support import respond, respond_client
from tests.story_support import WRITE, character, create
from tests.structured_turn_support import beat, render, structured_story

OPERATOR = {**bearer(), "X-Epistrel-Principal": "p-operator"}
LIBRARY = RequestContext(principal="p-author")
DRIVER: dict[str, Any] = {
    "name": "Dale",
    "definition": "A delivery driver in a hurry.",
    "true_age": 52,
}
SetLimit = Callable[[int], Awaitable[None]]


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


@pytest.fixture
async def cast_limit(stub_settings: Settings, engine: AsyncEngine) -> AsyncIterator[SetLimit]:
    """Sets ``max_story_cast`` through ``PUT /admin/settings``; the value it found is put back
    afterwards, so no other test sees a changed limit."""
    async with respond_client(stub_settings, engine) as (client, _):
        start = (await client.get("/admin/settings", headers=bearer())).json()["max_story_cast"]

        async def set_limit(value: int) -> None:
            response = await client.put(
                "/admin/settings", json={"max_story_cast": value}, headers=OPERATOR
            )
            assert response.status_code == 200, response.text
            assert response.json()["max_story_cast"] == value

        try:
            yield set_limit
        finally:
            await set_limit(start)


def cast_of(count: int) -> list[dict[str, Any]]:
    """``count`` characters named ``C01``… (the first is the player)."""
    return [character(f"C{n:02d}", 30, is_player=n == 1) for n in range(1, count + 1)]


def refusal(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    details = {k: v for k, v in error["details"].items() if k != "errors"}
    return response.status_code, error["code"], details


def cast_max(limit: int, actual: int) -> tuple[int, str, dict[str, Any]]:
    return (
        422,
        "validation_error",
        {"field": "body", "cap": "cast_max", "limit": limit, "actual": actual},
    )


async def add(client: httpx.AsyncClient, story_id: str, **fields: Any) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/characters", json={**DRIVER, **fields}, headers=WRITE
    )


async def weigh(
    client: httpx.AsyncClient, story_id: str, character_id: str, weight: str
) -> httpx.Response:
    return await client.post(
        f"/stories/{story_id}/characters/{character_id}/weight",
        json={"weight": weight},
        headers=WRITE,
    )


def _cap(error: InvalidRequest) -> dict[str, Any]:
    return {k: v for k, v in error.details.items() if k != "errors"}


async def weights(engine: AsyncEngine, story_id: str) -> dict[str, str]:
    found = await rows(
        engine,
        "SELECT name, weight::text AS weight FROM characters WHERE story_id = :s",
        s=story_id,
    )
    return {r.name: r.weight for r in found}


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_i_28__m9_default_boundary(stub_settings: Settings, engine: AsyncEngine) -> None:
    """At the default limit (50) a story is created with 50 characters in one request, and the
    51st added is refused with the setting's value (50, not the old 20). Complement: 51 in one
    creation is refused with the same body (``actual`` 51)."""
    assert DEFAULT_MAX_STORY_CAST == 50
    async with respond_client(stub_settings, engine) as (client, _):
        shown = (await client.get("/admin/settings", headers=bearer())).json()
        assert shown["max_story_cast"] == DEFAULT_MAX_STORY_CAST
        created = await client.post(
            "/stories", json={"characters": cast_of(DEFAULT_MAX_STORY_CAST)}, headers=WRITE
        )
        assert created.status_code == 201, created.text
        assert len(created.json()["characters"]) == DEFAULT_MAX_STORY_CAST
        story = created.json()["story_id"]
        assert refusal(await add(client, story)) == cast_max(50, 51)

        over = await client.post("/stories", json={"characters": cast_of(51)}, headers=WRITE)
        assert refusal(over) == cast_max(50, 51)


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_i_28__m9_setting(
    stub_settings: Settings, engine: AsyncEngine, cast_limit: SetLimit
) -> None:
    """``max_story_cast`` is served and updated by ``/admin/settings`` within 1..200: 0, 201, a
    string and ``null`` are 422 at ``body.max_story_cast`` and change nothing. At a setting of
    5 a story of 5 is created and its 6th character refused; a creation of 6 is refused with
    the same body. 201 characters (over the schema's ``maxItems``) get the same body too."""
    async with respond_client(stub_settings, engine) as (client, _):
        for bad in (0, STORY_CAST_LIMIT_MAX + 1, "5", None):
            response = await client.put(
                "/admin/settings", json={"max_story_cast": bad}, headers=OPERATOR
            )
            assert response.status_code == 422, (bad, response.text)
            error = response.json()["error"]
            assert error["code"] == "validation_error"
            assert error["details"]["field"] == "body.max_story_cast", bad
        shown = (await client.get("/admin/settings", headers=bearer())).json()
        assert shown["max_story_cast"] == DEFAULT_MAX_STORY_CAST

        await cast_limit(5)
        story = await create(client, {"characters": cast_of(5)})
        assert refusal(await add(client, story["story_id"])) == cast_max(5, 6)
        six = await client.post("/stories", json={"characters": cast_of(6)}, headers=WRITE)
        assert refusal(six) == cast_max(5, 6)

        await cast_limit(STORY_CAST_LIMIT_MAX)
        many = await client.post("/stories", json={"characters": cast_of(201)}, headers=WRITE)
        assert refusal(many) == cast_max(200, 201)


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_i_28__m9_lowered(
    stub_settings: Settings, engine: AsyncEngine, cast_limit: SetLimit
) -> None:
    """A story of 10 keeps its cast and its turns when the limit is lowered to 5: a turn still
    succeeds, and only adding a character is refused (``actual`` 11)."""
    async with respond_client(stub_settings, engine) as (client, _):
        story = await create(client, {"characters": cast_of(10)})
        sid = story["story_id"]
        player = story["characters"][0]["id"]
        await cast_limit(5)
        turn = await respond(client, sid, respond_body(player))
        assert turn.status_code == 200, turn.text
        assert refusal(await add(client, sid)) == cast_max(5, 11)
        assert len(await weights(engine, sid)) == 10


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_i_28__m9_minor_characters(
    stub_settings: Settings,
    engine: AsyncEngine,
    cast_limit: SetLimit,
    drained_embedding_jobs: None,
) -> None:
    """At a limit of 3 with 3 full characters, a structured turn whose render introduces a
    person succeeds and creates it ``minor``; the entity-model route still models a ``minor``.
    Adding a 4th, or promoting either ``minor`` to ``major``, is refused with ``cast_max``; a
    ``major`` set ``major`` again is not. Demoting a ``major`` frees a place for a promotion."""
    async with respond_client(stub_settings, engine) as (client, lib):
        story, ids = await structured_story(client, "Wren", "Ada", "Bo")
        await cast_limit(3)
        lib.models.stub.script(
            "generation",
            [
                render(
                    beat("narration", "Rhea, a courier on a bicycle, stops beside the road."),
                    introduces=[{"name": "Rhea", "age": 33}],
                ),
                render(beat("do", "Ada watches the road.")),
                render(beat("do", "Bo waves.")),
            ],
        )
        turn = await respond(client, story, respond_body(ids["Wren"], None, do="looks around"))
        assert turn.status_code == 200, turn.text
        assert await weights(engine, story) == {
            "Wren": "major",
            "Ada": "major",
            "Bo": "major",
            "Rhea": "minor",
        }

        async with engine.begin() as conn:
            neighbor = await store.add_entity(conn, story, "person", "The neighbor")
        modeled = await client.post(
            f"/stories/{story}/entities/{neighbor}/model", json={"true_age": 61}, headers=WRITE
        )
        assert modeled.status_code == 201, modeled.text
        assert (await weights(engine, story))["The neighbor"] == "minor"

        found = await rows(engine, "SELECT id, name FROM characters WHERE story_id = :s", s=story)
        by_name = {r.name: str(r.id) for r in found}
        assert refusal(await add(client, story)) == cast_max(3, 4)
        assert refusal(await weigh(client, story, by_name["Rhea"], "major")) == cast_max(3, 4)
        again = await weigh(client, story, ids["Bo"], "major")
        assert again.status_code == 200, again.text

        demoted = await weigh(client, story, ids["Ada"], "minor")
        assert demoted.status_code == 200, demoted.text
        promoted = await weigh(client, story, by_name["Rhea"], "major")
        assert promoted.status_code == 200, promoted.text
        assert refusal(await weigh(client, story, ids["Ada"], "major")) == cast_max(3, 4)
        assert (await weights(engine, story))["Rhea"] == "major"


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_i_28__m9_scenario(
    stub_settings: Settings, engine: AsyncEngine, cast_limit: SetLimit
) -> None:
    """A creation from a scenario counts every filled role, the player included: three roles
    against a limit of 2 are 422 ``cast_max`` with nothing created; at 3 the story is
    created."""
    async with respond_client(stub_settings, engine) as (client, _):
        scenario_id, _ = await g.published_scenario(
            client,
            g.scenario_body(g.role("mother"), g.role("aunt"), g.role("player", kind="player")),
        )
        body = g.from_scenario(
            scenario_id,
            slots={"mother": g.ad_hoc_slot("Mara", 52), "aunt": g.ad_hoc_slot("Iris", 49)},
        )
        await cast_limit(2)
        refused = await client.post("/stories", json=body, headers=g.headers())
        assert refusal(refused) == cast_max(2, 3)
        await cast_limit(3)
        made = await g.create_from(client, body)
        assert len(made["characters"]) == 3


@pytest.mark.part("M9", "FR-MULTI-12")
async def test_i_28__m9_library(
    stub_settings: Settings, engine: AsyncEngine, cast_limit: SetLimit
) -> None:
    """The library refuses as REST does: ``Engine.update_deployment_settings`` sets the limit,
    then ``create_story``, ``add_story_character`` and ``set_character_weight`` raise
    ``InvalidRequest`` with the ``cast_max`` details."""
    async with respond_client(stub_settings, engine) as (_, lib):
        await cast_limit(DEFAULT_MAX_STORY_CAST)
        updated = await lib.update_deployment_settings(
            RequestContext(principal="p-operator"), SettingsPatch(max_story_cast=2)
        )
        assert updated.as_settings().max_story_cast == 2
        cast = [
            CharacterSpec(name="Wren", definition="The player.", true_age=24, is_player=True),
            CharacterSpec(name="Ada", definition="An innkeeper.", true_age=41),
        ]
        created = await lib.create_story(LIBRARY, StorySpec(characters=cast))
        story = created.story_id
        with pytest.raises(InvalidRequest) as too_many:
            await lib.create_story(
                LIBRARY,
                StorySpec(
                    characters=[
                        *cast,
                        CharacterSpec(name="Bo", definition="A stable hand.", true_age=30),
                    ]
                ),
            )
        assert _cap(too_many.value) == {
            "field": "body",
            "cap": "cast_max",
            "limit": 2,
            "actual": 3,
        }
        with pytest.raises(InvalidRequest) as full:
            await lib.add_story_character(LIBRARY, story, AdHocCharacter.model_validate(DRIVER))
        assert full.value.details["cap"] == "cast_max"
        ada = created.characters[1].id
        await lib.set_character_weight(LIBRARY, story, ada, CharacterWeightRequest(weight="minor"))
        added = await lib.add_story_character(LIBRARY, story, AdHocCharacter.model_validate(DRIVER))
        assert added.name == "Dale"
        with pytest.raises(InvalidRequest) as promotion:
            await lib.set_character_weight(
                LIBRARY, story, ada, CharacterWeightRequest(weight="major")
            )
        assert _cap(promotion.value) == {
            "field": "body",
            "cap": "cast_max",
            "limit": 2,
            "actual": 3,
        }
