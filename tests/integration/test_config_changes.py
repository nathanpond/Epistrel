"""The configuration surface (#64, FR-CONC-14,15): every deployment setting on
``/admin/settings``, and the configuration log listings ``GET /admin/config-changes`` and
``GET /stories/{id}/config-changes``."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import read_settings, redact
from epistrel.core.deployment import apply_settings
from epistrel.core.ids import new_ulid, ulid_after
from epistrel.core.settings_schema import DeploymentSettings
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import new_story, respond_client
from tests.story_support import READ, WRITE, override

OPERATOR = {**READ, "X-Epistrel-Principal": "p-operator"}


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_admin_settings_cover_every_setting(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``GET /admin/settings`` returns every ``DeploymentSettings`` field; ``PUT`` merges (the
    engine profiles' ``profile_defaults`` setting by setting, #113), writes one revision with a
    row per changed leaf carrying that leaf's class, and refuses an out-of-bounds value at its
    field with nothing written (FR-CONC-14,15). M2's Lite generation defaults moved into
    ``profile_defaults``: the old key is refused, naming its replacement."""
    async with respond_client(stub_settings, engine) as (client, _):
        start = (await client.get("/admin/settings", headers=READ)).json()
        assert set(start) == {"config_revision", *DeploymentSettings.model_fields}
        try:
            put = await client.put(
                "/admin/settings",
                json={
                    "uncertain_below": 0.3,
                    "turn_queue_max": 9,
                    "profile_defaults": {"models": {"generation": {"temperature": 0.4}}},
                },
                headers=OPERATOR,
            )
            assert put.status_code == 200, put.text
            body = put.json()
            assert body["config_revision"] == start["config_revision"] + 1
            generation = start["profile_defaults"]["models"]["generation"]
            assert body["profile_defaults"]["models"]["generation"] == {
                **generation,
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
                ("profile_defaults.models.generation.temperature", "prospective", "p-operator"),
                ("turn_queue_max", "prospective", "p-operator"),
                ("uncertain_below", "interpretive", "p-operator"),
            }
            assert {i["config_revision"] for i in items} == {body["config_revision"]}
            assert next(i for i in items if i["setting"] == "uncertain_below")["new"] == 0.3

            for bad, field in (
                ({"uncertain_below": 1.5}, "body.uncertain_below"),
                (
                    {"profile_defaults": {"models": {"generation": {"top_p": 2}}}},
                    "body.profile_defaults.models.generation.top_p",
                ),
                ({"turn_queue_max": 0}, "body.turn_queue_max"),
                ({"lite_generation_defaults": {"top_p": 0.5}}, "body.lite_generation_defaults"),
            ):
                refused = await client.put("/admin/settings", json=bad, headers=OPERATOR)
                assert refused.status_code == 422, (bad, refused.text)
                assert refused.json()["error"]["details"]["field"] == field
            superseded = await client.put(
                "/admin/settings", json={"lite_window_messages": 12}, headers=OPERATOR
            )
            assert superseded.json()["error"]["details"]["replacement"] == (
                "profile_defaults.memory.rolling_summary.window_messages"
            )
            after = (await client.get("/admin/settings", headers=READ)).json()
            assert after["config_revision"] == body["config_revision"]
        finally:
            restored = await client.put(
                "/admin/settings",
                json={
                    key: start[key]
                    for key in ("uncertain_below", "turn_queue_max", "profile_defaults")
                },
                headers=OPERATOR,
            )
            assert restored.status_code == 200, restored.text


async def test_listings_filter_page_and_redact(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``GET /stories/{id}/config-changes`` lists the story's changes newest first with
    class, values, actor and time; filters by setting (a dotted prefix too), class and time;
    pages with ``next``; an unknown story is 404. Secret-bearing values are redacted. The
    story's overrides of its profile (#113) are listed with its settings, one row per dial."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        url = f"/stories/{story}/config-changes"
        assert (await client.get(url, headers=READ)).json() == {"items": [], "next": None}
        for queue_max in (3, 4, 5):
            patched = await client.patch(
                f"/stories/{story}/settings", json={"turn_queue_max": queue_max}, headers=WRITE
            )
            assert patched.status_code == 200, patched.text
        await override(
            client, story, {"models": {"generation": {"temperature": 0.5, "top_p": 0.5}}}
        )

        everything = (await client.get(url, headers=READ)).json()["items"]
        assert [i["config_revision"] for i in everything] == [4, 4, 3, 2, 1]
        assert {i["setting"] for i in everything[:2]} == {
            "profile_overrides.models.generation.temperature",
            "profile_overrides.models.generation.top_p",
        }
        first: dict[str, Any] = everything[-1]
        assert (first["old"], first["new"], first["actor"], first["class"]) == (
            None,
            3,
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
            await client.get(
                url, params={"setting": "profile_overrides.models.generation"}, headers=READ
            )
        ).json()
        assert {i["setting"] for i in nested["items"]} == {
            "profile_overrides.models.generation.temperature",
            "profile_overrides.models.generation.top_p",
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
    or a row family the scope writes (``rating:``, ``policy:``, ``model_config:``,
    ``engine_profile:``, alone or with a member). #113 adds the profile defaults
    (``profile_defaults.<path>``), a story's profile and overrides (``engine_profile_id``,
    ``profile_overrides.<path>``), and keeps M2's names of the dials it moved, which older rows
    carry. Anything else is 422 ``validation_error`` at ``query.setting``, rather than a 200
    that silently matches nothing.

    Complement asserted: every accepted form answers 200, and a family the other scope writes
    (``rating:`` on a story) is refused."""
    async with respond_client(stub_settings, engine) as (client, _):
        story, _ = await new_story(client)
        listings = {
            "/admin/config-changes": (
                [
                    "profile_defaults",
                    "profile_defaults.models.generation.top_p",
                    "lite_generation_defaults",
                    "lite_generation_defaults.top_p",
                    "rating:",
                ],
                [
                    "rating:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "policy:",
                    "model_config:respond.model",
                    "engine_profile:",
                    "engine_profile:01ARZ3NDEKTSV4RRFFQ69G5FAV.budgets.model_calls",
                ],
            ),
            f"/stories/{story}/config-changes": (
                [
                    "engine_profile_id",
                    "profile_overrides",
                    "profile_overrides.memory.rolling_summary.window_messages",
                    "generation_settings",
                    "generation_settings.temperature",
                    "turn_deadline",
                ],
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


async def test_rows_of_one_write_list_in_the_order_written(engine: AsyncEngine) -> None:
    """The rows one configuration write records (one revision) list newest first in the reverse
    of the order they were written, every time: a write records its changed settings in setting
    order, so ``GET /admin/config-changes`` lists them in reverse setting order. (#113 found it:
    ids minted in one millisecond sorted at random, so the rows of one revision came in either
    order, and two surfaces listing the same write could disagree, C-03.)

    Complement asserted: an id made after another sorts after it even in the same millisecond,
    where a fresh random one need not."""
    names = ["structured_retry_budget", "turn_queue_max", "uncertain_below"]
    before = (await read_settings(engine)).as_settings()
    try:
        for round_ in range(4):
            state = await apply_settings(
                engine,
                {
                    "structured_retry_budget": 3 + round_ % 2,
                    "turn_queue_max": 7 + round_ % 2,
                    "uncertain_below": 0.4 + (round_ % 2) / 10,
                },
                principal="p-operator",
            )
            listed = await rows(
                engine,
                "SELECT setting FROM config_changes WHERE scope = 'deployment' "
                "AND config_revision = :r ORDER BY config_revision DESC, change_id DESC",
                r=state.config_revision,
            )
            assert [r.setting for r in listed] == list(reversed(names))
    finally:
        await apply_settings(
            engine,
            {name: getattr(before, name) for name in names},
            principal="p-operator",
        )
    first = new_ulid()
    assert all(ulid_after(first) > first for _ in range(100))
    later = "7" + "Z" * 24 + "Y"  # far in the future: a fresh id would sort before it
    assert new_ulid() < later
    assert ulid_after(later) == "7" + "Z" * 25
