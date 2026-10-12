"""The configuration surface (#64, FR-CONC-14,15): every deployment setting on
``/admin/settings``, and the configuration log listings ``GET /admin/config-changes`` and
``GET /stories/{id}/config-changes``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import redact
from epistrel.core.settings_schema import DeploymentSettings
from tests.auth_support import auth_settings
from tests.respond_support import new_story, respond_client
from tests.story_support import READ, WRITE

OPERATOR = {**READ, "X-Epistrel-Principal": "p-operator"}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_admin_settings_cover_every_setting(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``GET /admin/settings`` returns every ``DeploymentSettings`` field; ``PUT`` merges
    (``lite_generation_defaults`` key by key), writes one revision with a row per changed leaf
    carrying that leaf's class, and refuses an out-of-bounds value at its field with nothing
    written (FR-CONC-14,15)."""
    async with respond_client(stub_settings, engine) as (client, _):
        start = (await client.get("/admin/settings", headers=READ)).json()
        assert set(start) == {"config_revision", *DeploymentSettings.model_fields}
        try:
            put = await client.put(
                "/admin/settings",
                json={
                    "uncertain_below": 0.3,
                    "turn_queue_max": 9,
                    "lite_generation_defaults": {"temperature": 0.4},
                },
                headers=OPERATOR,
            )
            assert put.status_code == 200, put.text
            body = put.json()
            assert body["config_revision"] == start["config_revision"] + 1
            assert body["lite_generation_defaults"] == {
                **start["lite_generation_defaults"],
                "temperature": 0.4,
            }
            listed = await client.get(
                "/admin/config-changes",
                params={"since": "2000-01-01T00:00:00Z", "limit": 3},
                headers=READ,
            )
            assert listed.status_code == 200, listed.text
            items = listed.json()["items"]
            assert {(i["setting"], i["class"], i["actor"]) for i in items} == {
                ("lite_generation_defaults.temperature", "prospective", "p-operator"),
                ("turn_queue_max", "prospective", "p-operator"),
                ("uncertain_below", "interpretive", "p-operator"),
            }
            assert {i["config_revision"] for i in items} == {body["config_revision"]}
            assert next(i for i in items if i["setting"] == "uncertain_below")["new"] == 0.3

            for bad, field in (
                ({"uncertain_below": 1.5}, "body.uncertain_below"),
                ({"lite_generation_defaults": {"top_p": 2}}, "body.lite_generation_defaults.top_p"),
                ({"turn_queue_max": 0}, "body.turn_queue_max"),
            ):
                refused = await client.put("/admin/settings", json=bad, headers=OPERATOR)
                assert refused.status_code == 422, (bad, refused.text)
                assert refused.json()["error"]["details"]["field"] == field
            after = (await client.get("/admin/settings", headers=READ)).json()
            assert after["config_revision"] == body["config_revision"]
        finally:
            restored = await client.put(
                "/admin/settings",
                json={
                    key: start[key]
                    for key in ("uncertain_below", "turn_queue_max", "lite_generation_defaults")
                },
                headers=OPERATOR,
            )
            assert restored.status_code == 200, restored.text


async def test_listings_filter_page_and_redact(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``GET /stories/{id}/config-changes`` lists the story's changes newest first with
    class, values, actor and time; filters by setting (a dotted prefix too), class and time;
    pages with ``next``; an unknown story is 404. Secret-bearing values are redacted."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        url = f"/stories/{story}/config-changes"
        assert (await client.get(url, headers=READ)).json() == {"items": [], "next": None}
        for deadline in (30, 31, 32):
            patched = await client.patch(
                f"/stories/{story}/settings", json={"turn_deadline": deadline}, headers=WRITE
            )
            assert patched.status_code == 200, patched.text
        await client.patch(
            f"/stories/{story}/settings",
            json={"generation_settings": {"temperature": 0.5, "top_p": 0.5}},
            headers=WRITE,
        )

        everything = (await client.get(url, headers=READ)).json()["items"]
        assert [i["config_revision"] for i in everything] == [4, 4, 3, 2, 1]
        assert {i["setting"] for i in everything[:2]} == {
            "generation_settings.temperature",
            "generation_settings.top_p",
        }
        first: dict[str, Any] = everything[-1]
        assert (first["old"], first["new"], first["actor"], first["class"]) == (
            45.0,
            30,
            "p-author",
            "prospective",
        )

        page = (await client.get(url, params={"limit": 2}, headers=READ)).json()
        assert len(page["items"]) == 2
        assert page["next"]
        rest = (
            await client.get(url, params={"limit": 10, "after": page["next"]}, headers=READ)
        ).json()
        assert page["items"] + rest["items"] == everything
        assert rest["next"] is None

        nested = (
            await client.get(url, params={"setting": "generation_settings"}, headers=READ)
        ).json()
        assert {i["setting"] for i in nested["items"]} == {
            "generation_settings.temperature",
            "generation_settings.top_p",
        }
        interpretive = (
            await client.get(url, params={"class": "interpretive"}, headers=READ)
        ).json()
        assert interpretive["items"] == []
        future = (
            await client.get(url, params={"since": "2999-01-01T00:00:00Z"}, headers=READ)
        ).json()
        assert future["items"] == []

        missing = await client.get(
            "/stories/00000000000000000000000000/config-changes", headers=READ
        )
        assert missing.status_code == 404
        assert (
            await client.get(url, params={"after": "nonsense"}, headers=READ)
        ).status_code == 422

    assert redact({"model": "m", "api_key": "sk-1", "params": {"Authorization": "x", "n": 1}}) == {
        "model": "m",
        "api_key": "***",
        "params": {"Authorization": "***", "n": 1},
    }
    assert redact([{"token": "t"}, 3]) == [{"token": "***"}, 3]


async def test_setting_filter_refuses_unknown_names(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``setting`` is a closed vocabulary per scope (#279): a setting name or its dotted parent,
    or a row family the scope writes (``rating:``, ``policy:``, ``model_config:``, alone or with
    a member). Anything else is 422 ``validation_error`` at ``query.setting``, rather than a
    200 that silently matches nothing.

    Complement asserted: every accepted form answers 200, and a family the other scope writes
    (``rating:`` on a story) is refused."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        listings = {
            "/admin/config-changes": (
                ["lite_generation_defaults", "lite_generation_defaults.top_p", "rating:"],
                ["rating:01ARZ3NDEKTSV4RRFFQ69G5FAV", "policy:", "model_config:respond.model"],
            ),
            f"/stories/{story}/config-changes": (
                ["generation_settings", "generation_settings.temperature", "turn_deadline"],
                ["policy:", "policy:01ARZ3NDEKTSV4RRFFQ69G5FAV"],
            ),
        }
        for url, (names, families) in listings.items():
            for value in [*names, *families]:
                accepted = await client.get(url, params={"setting": value}, headers=READ)
                assert accepted.status_code == 200, (url, value, accepted.text)
            for value in ["false", "0.5", "AAA", "unknown_setting", "generation_settings.", "x:"]:
                refused = await client.get(url, params={"setting": value}, headers=READ)
                assert refused.status_code == 422, (url, value, refused.text)
                error = refused.json()["error"]
                assert error["code"] == "validation_error"
                assert error["details"]["field"].startswith("query.setting"), error
        on_story = await client.get(
            f"/stories/{story}/config-changes", params={"setting": "rating:"}, headers=READ
        )
        assert on_story.status_code == 422, on_story.text
