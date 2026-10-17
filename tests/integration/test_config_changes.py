"""The configuration surface (#64, FR-CONC-14,15): every deployment setting on
``/admin/settings``, and the configuration log listings ``GET /admin/config-changes`` and
``GET /stories/{id}/config-changes``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.config_changes import SECTIONS, read_section, read_settings, redact
from epistrel.core.deployment import SettingsPatch, apply_settings
from epistrel.core.errors import InvalidRequest
from epistrel.core.ids import new_ulid, ulid_after
from epistrel.core.settings_schema import DeploymentSettings, StorySettings, TurnQueueDefaults
from epistrel.core.story_settings import update_story_settings
from tests.auth_support import auth_settings
from tests.event_support import new_story as event_story
from tests.event_support import rows, scalar
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


STORED: dict[str, Any] = {
    "lite_summary_chunk": 12,
    "max_pinned_memories": 51,
    "idempotency_result_window": 25,
    "structured_retry_budget": 3,
    "rating_check_model_calls_per_record": 3,
    "audit_retention_days": 30,
    "job_retention_days": 8,
    "offboarding_report_retention_days": 91,
    "rating_check_retention_days": 31,
    "readset_change_retention_days": 8,
    "worker_heartbeat_timeout": 10.0,
    "timer_lag_alert": 120.0,
    "media_sweep_interval": 7200,
    "validation_model_calls": 1,
    "feedback_text_max_chars": 1999,
    "erasure_shared_media": "retire",
    "memory_importance_half_life_turns": 150,
    "reembed_batch_size": 400,
    "retrieval_rrf_k": 61,
    "neighborhood_max_facts": 40,
    "thunks_max_open": 499,
}
"""A non-default value of one setting or more in every section of the settings document
(all of #315's three missing sections' settings among them)."""


async def test_a_write_keeps_every_other_section(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """Every section of the settings document reads back as stored, and a ``PUT`` of one
    unrelated setting leaves the others as they were, both on ``/admin/settings`` and in what
    the retention sweep, ``/health`` and erasure read (``read_section``; #315, FR-CONC-14,15;
    FR-ADMIN-2 for the erasure's ``erasure_shared_media``).
    Before #315 the retention, worker-health and erasure settings read as their defaults and
    the next write reset them."""
    views = {name: view for view in SECTIONS for name in view.model_fields}
    assert {views[name] for name in STORED} == set(SECTIONS) - {TurnQueueDefaults}
    async with respond_client(stub_settings, engine) as (client, _):
        start = (await client.get("/admin/settings", headers=READ)).json()
        assert all(start[name] != value for name, value in STORED.items())
        try:
            stored = await client.put("/admin/settings", json=STORED, headers=OPERATOR)
            assert stored.status_code == 200, stored.text
            assert {name: stored.json()[name] for name in STORED} == STORED
            unrelated = await client.put(
                "/admin/settings", json={"turn_queue_max": 9}, headers=OPERATOR
            )
            assert unrelated.status_code == 200, unrelated.text
            assert unrelated.json()["turn_queue_max"] == 9
            assert {name: unrelated.json()[name] for name in STORED} == STORED
            after = (await client.get("/admin/settings", headers=READ)).json()
            assert {name: after[name] for name in STORED} == STORED
            for name, value in STORED.items():
                assert getattr(await read_section(engine, views[name]), name) == value, name
        finally:
            restored = await client.put(
                "/admin/settings",
                json={name: start[name] for name in (*STORED, "turn_queue_max")},
                headers=OPERATOR,
            )
            assert restored.status_code == 200, restored.text
            for name in STORED:
                assert getattr(await read_section(engine, views[name]), name) == start[name]


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


def _typed(model: type[BaseModel], kind: str) -> list[str]:
    """The settings of ``model`` whose JSON Schema allows a JSON ``kind`` (``integer`` or
    ``number``)."""
    properties = model.model_json_schema()["properties"]
    return sorted(
        name
        for name, schema in properties.items()
        if kind in {s.get("type") for s in [schema, *schema.get("anyOf", ())]}
    )


INTEGER_DEPLOYMENT = _typed(DeploymentSettings, "integer")
NUMBER_DEPLOYMENT = _typed(DeploymentSettings, "number")
INTEGER_STORY = _typed(StorySettings, "integer")
NOT_AN_INTEGER: tuple[Any, ...] = (True, False, "7", 2.5, 2048.0)
NOT_A_NUMBER: tuple[Any, ...] = (True, "0.5")


def _refused_at(model: type[BaseModel], document: dict[str, Any], name: str) -> None:
    with pytest.raises(ValidationError) as refused:
        model.model_validate(document)
    assert [error["loc"][0] for error in refused.value.errors()] == [name], document


def test_settings_refuse_values_only_lax_coercion_accepts() -> None:
    """Every integer deployment and story setting refuses a boolean, a numeric string and a
    float, integral or not, and every number setting a boolean and a numeric string, at its
    field: in ``SettingsPatch``, in ``DeploymentSettings`` (what every library write validates,
    ``apply_settings``) and in ``StorySettings`` (FR-API-4; #322). Before #322 ``true`` was
    stored as ``1``. A JSON integer is still a number."""
    assert {"turn_queue_max", "minor_age_threshold", "validation_model_calls"} <= set(
        INTEGER_DEPLOYMENT
    )
    assert {"uncertain_below", "worker_heartbeat_timeout"} <= set(NUMBER_DEPLOYMENT)
    assert {"turn_queue_max", "regenerate_retry_budget"} <= set(INTEGER_STORY)
    complete = {"default_content_rating_id": "general"}
    for names, values in ((INTEGER_DEPLOYMENT, NOT_AN_INTEGER), (NUMBER_DEPLOYMENT, NOT_A_NUMBER)):
        for name in names:
            for value in values:
                _refused_at(SettingsPatch, {name: value}, name)
                _refused_at(DeploymentSettings, {**complete, name: value}, name)
    for name in INTEGER_STORY:
        for value in NOT_AN_INTEGER:
            _refused_at(StorySettings, {name: value}, name)
    assert SettingsPatch.model_validate({"turn_queue_max": 7, "uncertain_below": 1}).changes() == {
        "turn_queue_max": 7,
        "uncertain_below": 1.0,
    }


async def test_library_writes_refuse_a_mistyped_integer(engine: AsyncEngine) -> None:
    """``apply_settings`` and ``update_story_settings`` refuse ``true``, ``"7"`` and ``2048.0``
    for an integer setting with 422 ``validation_error`` at ``body.<field>``, writing nothing
    (FR-API-4; #322)."""
    before = await read_settings(engine)
    story = await event_story(engine)
    revision = await scalar(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story)
    for value in (True, "7", 2048.0):
        with pytest.raises(InvalidRequest) as deployment:
            await apply_settings(engine, {"turn_queue_max": value}, principal="p-operator")
        assert deployment.value.details["field"] == "body.turn_queue_max"
        with pytest.raises(InvalidRequest) as story_write:
            await update_story_settings(
                engine, story, {"regenerate_retry_budget": value}, principal="p-author"
            )
        assert story_write.value.details["field"] == "body.regenerate_retry_budget"
    after = await read_settings(engine)
    assert after.config_revision == before.config_revision
    assert after.as_settings() == before.as_settings()
    assert (
        await scalar(engine, "SELECT config_revision FROM stories WHERE id = :s", s=story)
        == revision
    )


async def test_routes_refuse_a_mistyped_integer(
    stub_settings: Settings, engine: AsyncEngine
) -> None:
    """``PUT /admin/settings`` and ``PATCH /stories/{id}/settings`` answer 422
    ``validation_error`` at ``body.<field>`` for a boolean, a numeric string or a float given
    to an integer setting, writing nothing (FR-API-4; #322)."""
    async with respond_client(stub_settings, engine) as (client, _):
        start = (await client.get("/admin/settings", headers=READ)).json()
        story, _ = await new_story(client)
        settings = (await client.get(f"/stories/{story}/settings", headers=READ)).json()
        for value in (True, "7", 2.5, 2048.0):
            refused = await client.put(
                "/admin/settings", json={"validation_model_calls": value}, headers=OPERATOR
            )
            assert refused.status_code == 422, (value, refused.text)
            error = refused.json()["error"]
            assert (error["code"], error["details"]["field"]) == (
                "validation_error",
                "body.validation_model_calls",
            )
            patched = await client.patch(
                f"/stories/{story}/settings", json={"turn_queue_max": value}, headers=WRITE
            )
            assert patched.status_code == 422, (value, patched.text)
            error = patched.json()["error"]
            assert (error["code"], error["details"]["field"]) == (
                "validation_error",
                "body.turn_queue_max",
            )
        assert (await client.get("/admin/settings", headers=READ)).json() == start
        assert (await client.get(f"/stories/{story}/settings", headers=READ)).json() == settings
