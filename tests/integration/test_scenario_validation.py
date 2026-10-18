"""The catalog validation in detail (#111): each check with a draft that fails it and one that
passes, the input policies' model-assisted budget, the lock a publish never holds across a model
call, the log line, and what a publish without the Orchestrator lists. The milestone parts are
I-20 (``tests/integration/test_i_20.py``), U-26 (``tests/test_u_26.py``), C-15
(``tests/conformance/test_c_15.py``), C-20 (``tests/conformance/test_c_20.py``) and U-29
(``tests/test_u_29.py``); the pure parts are in ``tests/test_catalog_validation.py``."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from epistrel.core.catalog import scenarios as catalog_scenarios
from epistrel.core.catalog.scenario_models import ScenarioCreate
from epistrel.core.catalog.service import PublishRequest
from epistrel.core.context import RequestContext
from epistrel.core.deployment import SettingsPatch
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import retire_rating
from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID
from epistrel.engine import Engine
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.testing import pauses
from tests import scenario_support as scenarios
from tests.catalog_support import scratch_catalog
from tests.character_support import complete
from tests.event_support import rows, sqlstate
from tests.media_support import media_client, png, scratch, set_media, uploaded
from tests.policy_support import global_policy
from tests.story_support import rest
from tests.validation_support import (
    PROVIDER,
    PROVIDER_FIELD,
    codes,
    counts,
    entries,
    filled,
    incomplete_seed,
    link_rating_policy,
    list_policy,
    rated_character,
    validate,
    with_provider,
)

CTX = RequestContext(principal="p-validation")
VIOLATION = '{"violation": true, "confidence": 0.97}'
ASSISTED = {"type": "model_assisted", "prompt": "Is the text about smuggling?"}
AGE_CONDITIONED = "age_conditioned_policies"
MODEL_ASSISTED = "input_policy_model_assisted"


async def _create(lib: Engine, body: dict[str, Any]) -> dict[str, Any]:
    created = await lib.create_scenario_draft(CTX, ScenarioCreate.model_validate(body))
    return created.scenario.model_dump(mode="json")


async def _report(lib: Engine, scenario_id: str) -> dict[str, Any]:
    found: dict[str, Any] = (await lib.validate_scenario(CTX, scenario_id)).report.as_json()
    return found


async def _budget(lib: Engine, calls: int) -> None:
    await lib.update_deployment_settings(CTX, SettingsPatch(validation_model_calls=calls))


# -- media -------------------------------------------------------------------------------------


async def test_media_checks(pg_url: str, tmp_path: Path) -> None:
    """A media asset a scenario shows must exist, be live, and have a type, a size and a kind
    the place allows, as the settings stand now: an image in ``videos`` is
    ``media_kind_mismatch``; a type dropped from ``media_allowed_types`` or a size above a
    lowered ``media_max_bytes`` is ``media_not_allowed`` (one entry each); an id no asset has
    (a draft planted past the save's check) is ``media_missing``. A default role's character's
    images are checked at the role's character: a retired one is ``media_retired`` and an
    Explicit one in an Everyone scenario is ``rating_looser``, each naming the asset. Each passes
    once the place, the settings or the image allow it."""
    async with (
        scratch(pg_url, tmp_path / "media") as space,
        media_client(space.settings, space.db) as client,
    ):
        await link_rating_policy(client, space.db, EVERYONE_ID)
        cover = await uploaded(client, headers=scenarios.WRITE)
        shown = scenarios.scenario(
            "Shown",
            public_card={
                "name": "Shown",
                "media": {"cover": cover["id"], "videos": [{"media_id": cover["id"]}]},
            },
        )
        created = await scenarios.create(client, shown)
        report = (await validate(client, created["id"])).json()["report"]
        assert codes(report["errors"]) == [
            ("media_kind_mismatch", "public_card.media.videos[0].media_id")
        ]
        assert report["errors"][0]["source"] == cover["id"]
        fine = await scenarios.save(client, created["id"], 1, public_card={"media": {"videos": []}})
        assert fine.status_code == 200, fine.text
        assert (await validate(client, created["id"])).json()["valid"] is True

        size = len(png())  # every image the helper makes has this size
        try:
            await set_media(client, media_allowed_types=["image/jpeg"], media_max_bytes=size - 1)
            refused = (await validate(client, created["id"])).json()["report"]["errors"]
            assert codes(refused) == [
                ("media_not_allowed", "public_card.media.cover"),
                ("media_not_allowed", "public_card.media.cover"),
            ]
            assert sorted(e["rule"] for e in refused) == [
                f"its size, {size} bytes, is above the deployment's media_max_bytes ({size - 1})",
                "its type, image/png, is not in the deployment's media_allowed_types",
            ]
        finally:
            await set_media(
                client,
                media_allowed_types=["image/png", "image/jpeg", "image/webp", "video/mp4"],
                media_max_bytes=50 * 1024 * 1024,
            )
        assert (await validate(client, created["id"])).json()["valid"] is True

        ghost = new_ulid()
        async with space.db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE catalog.scenario_catalog SET draft = jsonb_set(draft, "
                    "'{public_card,media,gallery}', CAST(:g AS jsonb)) WHERE id = :s"
                ),
                {"g": json.dumps([ghost]), "s": created["id"]},
            )
        missing = (await validate(client, created["id"])).json()["report"]["errors"]
        assert [(e["code"], e["location"]["field"], e["source"]) for e in missing] == [
            ("media_missing", "public_card.media.gallery[0]", ghost)
        ]

        retired = await uploaded(client, headers=scenarios.WRITE)
        wild = await uploaded(client, headers=scenarios.WRITE, content_rating_id=EXPLICIT_ID)
        pictured, _ = await rated_character(
            client,
            space.db,
            "Pia Pictured",
            rating_id=EVERYONE_ID,
            images=[retired["id"], wild["id"]],
        )
        assert (await client.delete(f"/media/{retired['id']}", headers=scenarios.WRITE)).is_success
        cast = scenarios.scenario(
            "Pictured",
            cast={
                "roles": [
                    filled("aunt", "default", pictured),
                    scenarios.role("player", kind="player"),
                ]
            },
        )
        made = await scenarios.create(client, cast)
        report = (await validate(client, made["id"])).json()["report"]
        assert [
            (e["code"], e["location"]["role"], e["location"]["field"], e["source"])
            for e in (*report["errors"], *report["warnings"])
        ] == [
            ("media_retired", "aunt", "cast.roles[0].character", retired["id"]),
            ("rating_looser", "aunt", "cast.roles[0].character", wild["id"]),
        ]


# -- input policies ----------------------------------------------------------------------------


async def test_model_assisted_policies_run_within_the_budget(pg_url: str, tmp_path: Path) -> None:
    """Model-assisted input policies cost a model call per policy and text, within the
    deployment's ``validation_model_calls`` (default 0): at 0 none runs, no model is called, and
    ``input_policy_model_assisted`` is listed; with room for every text, each text the model
    flags is an ``input_policy`` warning quoting the start of its text, and nothing is listed;
    with room for one of two texts, one call is made and the rest is listed; a model that cannot
    answer is listed and the publish still succeeds. No validation writes an audit row.

    Complement asserted: the flag comes from the model (a text it does not flag gets no
    warning)."""
    async with scratch(pg_url, tmp_path / "media", model_adapter="stub") as space:
        lib, stub = space.lib, space.lib.models.stub
        policy = await global_policy(lib, ASSISTED)
        flagged = "Crates of contraband arrive at midnight."
        body = scenarios.scenario(
            "Assisted",
            world={"description": flagged, "locations": [{"id": "kitchen", "name": "The kitchen"}]},
        )
        sid = (await _create(lib, body))["id"]
        audits = await counts(space.db)

        idle = await _report(lib, sid)
        assert MODEL_ASSISTED in idle["unavailable"]
        assert entries(idle, "input_policy") == []
        assert [c for c in stub.calls if c.purpose == "guardrail_check"] == []

        stub.when("contraband", VIOLATION, purpose="guardrail_check")
        await _budget(lib, 50)
        checked = await _report(lib, sid)
        assert MODEL_ASSISTED not in checked["unavailable"]
        (warning,) = entries(checked, "input_policy")
        assert (warning["location"]["field"], warning["rule"], warning["text"]) == (
            "world.description",
            policy.id,
            flagged,
        )
        texts = len([c for c in stub.calls if c.purpose == "guardrail_check"])
        assert texts > 1  # every text was checked, and only one was flagged

        stub.calls.clear()
        await _budget(lib, 1)
        partial = await _report(lib, sid)
        assert MODEL_ASSISTED in partial["unavailable"]
        assert len([c for c in stub.calls if c.purpose == "guardrail_check"]) == 1

        await _budget(lib, 50)
        stub.fault("guardrail_check", "http_500", n=50)
        failed = await _report(lib, sid)
        assert MODEL_ASSISTED in failed["unavailable"]
        published = await lib.publish_scenario(CTX, sid, PublishRequest(draft_revision=1))
        assert published.report.ok
        after = await counts(space.db)
        assert after["audit_log"] == audits["audit_log"]


async def test_no_lock_is_held_across_a_model_call(pg_url: str, tmp_path: Path) -> None:
    """A publish runs its model-assisted input policies before it locks the scenario, so no
    lock is held across a model call: while the model answers, another session can lock the
    scenario's row at once and no session sits in an open transaction; the publish then
    completes with the model's warning.

    Complement asserted: once the publish holds its lock (held at its pause point), the same
    attempt cannot lock the row."""
    async with scratch(pg_url, tmp_path / "media", model_adapter="stub") as space:
        lib, stub = space.lib, space.lib.models.stub
        await global_policy(lib, ASSISTED)
        await _budget(lib, 50)
        stub.when("contraband", VIOLATION, purpose="guardrail_check")
        body = scenarios.scenario(
            "Unlocked",
            world={
                "description": "Crates of contraband in the barn.",
                "locations": [{"id": "kitchen", "name": "The kitchen"}],
            },
        )
        sid = (await _create(lib, body))["id"]
        latch = stub.hold("guardrail_check")
        publishing = asyncio.ensure_future(
            lib.publish_scenario(CTX, sid, PublishRequest(draft_revision=1))
        )
        await asyncio.wait_for(latch.entered.wait(), 10)
        async with space.db.connect() as probe:
            async with probe.begin():
                locked = await probe.scalar(
                    text("SELECT id FROM catalog.scenario_catalog WHERE id = :s FOR UPDATE NOWAIT"),
                    {"s": sid},
                )
                idle = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
                    )
                )
            assert (locked, idle) == (sid, 0)
        latch.release()
        published = await asyncio.wait_for(publishing, 30)
        assert [w.code for w in published.report.warnings] == ["input_policy"]

        with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
            again = asyncio.ensure_future(
                lib.publish_scenario(CTX, sid, PublishRequest(draft_revision=2))
            )
            await hold.arrived(1)
            async with space.db.connect() as probe:
                with pytest.raises(DBAPIError) as refused:
                    async with probe.begin():
                        await probe.execute(
                            text(
                                "SELECT id FROM catalog.scenario_catalog WHERE id = :s "
                                "FOR UPDATE NOWAIT"
                            ),
                            {"s": sid},
                        )
            assert sqlstate(refused.value) == "55P03"  # lock_not_available
            hold.release()
            assert (await asyncio.wait_for(again, 30)).version == 2


async def test_age_conditioned_policies_are_listed(pg_url: str, tmp_path: Path) -> None:
    """A scenario has no cast, so an age-conditioned input policy (the seeded minors policy)
    cannot be decided at publish: it is listed ``age_conditioned_policies``, never run. Once no
    such policy is live, it is not listed."""
    async with scratch(pg_url, tmp_path / "media", model_adapter="stub") as space:
        lib = space.lib
        sid = (await _create(lib, scenarios.scenario("Ages")))["id"]
        assert AGE_CONDITIONED in (await _report(lib, sid))["unavailable"]
        await lib.policies_retire(RequestContext(principal="p-operator"), MINORS_POLICY_ID)
        assert AGE_CONDITIONED not in (await _report(lib, sid))["unavailable"]


async def test_a_publish_without_the_orchestrator_lists_the_policies(
    settings: Any, engine: Any
) -> None:
    """Core's publish without the Engine's ports (the default, which evaluates no policy) lists
    ``input_policy`` as unavailable rather than passing it; through the Engine, the policies run
    and it is not listed."""
    async with rest(settings, engine) as client:
        created = await scenarios.create(client, scenarios.scenario("Ported"))
        bare = await catalog_scenarios.publish(
            engine, created["id"], PublishRequest(draft_revision=1), principal="p-core"
        )
        assert "input_policy" in bare.report.unavailable
        checked = (await validate(client, created["id"])).json()["report"]
        assert "input_policy" not in checked["unavailable"]


# -- the other checks ----------------------------------------------------------------------------


async def test_the_document_check_gates_the_others(settings: Any, engine: Any) -> None:
    """A stored draft the scenario model no longer accepts (planted past the save) is refused at
    the field, and no other check runs on it (its emptied cast is not reported, and no
    capability-gated check the deployment can run lists itself, which each does when it runs);
    the checks this deployment cannot run are still listed, exactly those (FR-SCN-5).

    Complement asserted: the draft as it was saved validates, listing the same checks (the
    gated ones it can run, run: #149)."""
    async with rest(settings, engine) as client:
        created = await scenarios.create(client, scenarios.scenario("Planted"))
        whole = (await validate(client, created["id"])).json()
        assert whole["valid"] is True
        assert set(whole["report"]["unavailable"]) >= set(scenarios.undeliverable_checks())
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE catalog.scenario_catalog SET draft = jsonb_set("
                    "draft || CAST(:extra AS jsonb), '{cast,roles}', '[]'::jsonb) WHERE id = :s"
                ),
                {"extra": json.dumps({"retired_group": 1}), "s": created["id"]},
            )
        report = (await validate(client, created["id"])).json()["report"]
        assert codes(report["errors"]) == [("extra_forbidden", "retired_group")]
        assert report["warnings"] == []
        assert sorted(report["unavailable"]) == scenarios.undeliverable_checks()
        assert scenarios.runnable_gated_checks()  # M4 on: the structured checks can run
        assert not set(report["unavailable"]) & set(scenarios.runnable_gated_checks())


async def test_a_retired_rating_is_an_error(settings: Any, engine: Any) -> None:
    """A scenario whose content rating was retired since it was saved cannot publish:
    ``rating_retired`` at ``metadata.content_rating_id``, naming the rating."""
    async with rest(settings, engine) as client:
        made = await client.post(
            "/admin/content-ratings",
            json={"name": f"Retiring {new_ulid()}"},
            headers={**scenarios.WRITE, "X-Epistrel-Principal": "p-operator"},
        )
        rating = made.json()["id"]
        body = scenarios.scenario("Retiring", metadata={"labels": [], "content_rating_id": rating})
        created = await scenarios.create(client, body)
        assert (await validate(client, created["id"])).json()["valid"] is True
        await retire_rating(engine, rating, principal="p-operator")
        errors = (await validate(client, created["id"])).json()["report"]["errors"]
        assert [(e["code"], e["location"]["field"], e["source"]) for e in errors] == [
            ("rating_retired", "metadata.content_rating_id", rating)
        ]


async def test_the_cast_limit_counts_every_role(settings: Any, engine: Any) -> None:
    """Every role counts toward the starting-cast limit, the player's and the optional ones
    included: four roles, two of them optional, under the scenario's own limit of 3 are one
    ``cast_limit_exceeded`` error at ``cast.roles``, naming the limit and the count.

    Complement asserted: with the limit raised to 4, the same roles validate."""
    async with rest(settings, engine) as client:
        cast = {
            "roles": [
                scenarios.role("mother"),
                scenarios.role("uncle", required=False),
                scenarios.role("cousin", required=False),
                scenarios.role("player", kind="player"),
            ],
            "max_characters_at_start": 3,
        }
        created = await scenarios.create(client, scenarios.scenario("Counted", cast=cast))
        errors = (await validate(client, created["id"])).json()["report"]["errors"]
        assert [
            (e["code"], e["location"]["field"], e["location"]["role"], e["outcome"], e["rule"])
            for e in errors
        ] == [
            (
                "cast_limit_exceeded",
                "cast.roles",
                None,
                "rejected",
                "a story starts with at most 3 characters, player and optional roles included "
                "(FR-SCN-4); this scenario has 4 roles",
            )
        ]
        raised = await scenarios.save(
            client, created["id"], created["draft_revision"], cast={"max_characters_at_start": 4}
        )
        assert raised.status_code == 200, raised.text
        assert (await validate(client, created["id"])).json()["valid"] is True


async def test_role_characters_must_be_published_and_live(settings: Any, engine: Any) -> None:
    """Each ``locked`` and ``default`` role's catalog character must exist (at its pinned
    version, when the role pins one), be published and not be archived. Each failure is an
    error at the role's ``cast.roles[i].character`` naming the character: an id no character
    has (``character_missing``, the id), a pinned version that does not exist
    (``character_missing``, ``id@version``), a character never published
    (``character_not_published``) and an archived one (``character_archived``, the version
    used).

    Complements asserted: a published, live character gives no entry; once the drafted
    character is published, the archived one unarchived, and the other two roles name a
    character and a version that exist, the scenario validates."""
    async with rest(settings, engine) as client:
        live = await scenarios.published_character(client, "Lina Live")
        shelved = await scenarios.published_character(client, "Saul Shelved")
        archived = await client.post(
            f"/catalog/characters/{shelved}/archive", headers=scenarios.WRITE
        )
        assert archived.status_code == 200, archived.text
        made = await client.post(
            "/catalog/characters", json=complete("Dora Draft"), headers=scenarios.WRITE
        )
        assert made.status_code == 201, made.text
        drafted = made.json()["id"]
        ghost = new_ulid()
        roles = [
            filled("ghost", "locked", ghost),
            scenarios.role("pinned", fill="default", character={"catalog_id": live, "version": 9}),
            filled("drafted", "default", drafted),
            filled("shelf", "locked", shelved),
            filled("kept", "default", live),
            scenarios.role("player", kind="player"),
        ]
        created = await scenarios.create(
            client, scenarios.scenario("Filled", cast={"roles": roles})
        )
        report = (await validate(client, created["id"])).json()["report"]
        assert [
            (e["code"], e["location"]["role"], e["location"]["field"], e["source"])
            for e in report["errors"]
        ] == [
            ("character_not_published", "drafted", "cast.roles[2].character", drafted),
            ("character_missing", "ghost", "cast.roles[0].character", ghost),
            ("character_missing", "pinned", "cast.roles[1].character", f"{live}@9"),
            ("character_archived", "shelf", "cast.roles[3].character", f"{shelved}@1"),
        ]
        assert {e["outcome"] for e in report["errors"]} == {"rejected"}

        published = await client.post(
            f"/catalog/characters/{drafted}/publish",
            json={"draft_revision": 1},
            headers=scenarios.WRITE,
        )
        assert published.status_code == 201, published.text
        unarchived = await client.post(
            f"/catalog/characters/{shelved}/unarchive", headers=scenarios.WRITE
        )
        assert unarchived.status_code == 200, unarchived.text
        roles[0] = filled("ghost", "locked", live)
        roles[1] = scenarios.role(
            "pinned", fill="default", character={"catalog_id": live, "version": 1}
        )
        saved = await scenarios.save(
            client, created["id"], created["draft_revision"], cast={"roles": roles}
        )
        assert saved.status_code == 200, saved.text
        assert (await validate(client, created["id"])).json()["valid"] is True


async def test_eligibility_labels_and_age(pg_url: str) -> None:
    """A role's labels and age range hold its character to them, with one ``eligibility_failed``
    error per failing filter, at the filter's field, naming the character's version: a
    character that lacks a label the role lists, or is older than the range's upper end, fails;
    so does one that cannot be judged, with no labels or no true age. The rule names what is
    missing, or the range with an open end as ``any``.

    Complements asserted: either end of an age range may be open and both are inclusive, so a
    character at the lower end of ``[60, any]`` or the upper end of ``[any, 60]`` passes, and so
    does one holding every listed label (``Warm`` holds ``warm``)."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        aged, aged_version = await rated_character(
            client, db, "Ola Aged", rating_id=EVERYONE_ID, labels=["Warm", "patient"], true_age=60
        )
        blank, blank_version = await rated_character(client, db, "Bea Blank", rating_id=EVERYONE_ID)
        roles = [
            filled(
                "blank", "default", blank, eligibility={"labels": ["warm"], "age_range": [18, None]}
            ),
            filled(
                "old",
                "default",
                aged,
                eligibility={"labels": ["warm", "stern"], "age_range": [None, 59]},
            ),
            filled(
                "fit",
                "default",
                aged,
                eligibility={"labels": ["warm", "patient"], "age_range": [60, None]},
            ),
            filled("edge", "locked", aged, eligibility={"age_range": [None, 60]}),
            scenarios.role("player", kind="player"),
        ]
        created = await scenarios.create(
            client, scenarios.scenario("Filtered", cast={"roles": roles})
        )
        errors = (await validate(client, created["id"])).json()["report"]["errors"]
        age_rule = "eligibility: the character's true age is within the role's age range"
        label_rule = "eligibility: the character holds every label the role lists"
        assert [
            (e["code"], e["location"]["role"], e["location"]["field"], e["source"], e["rule"])
            for e in errors
        ] == [
            (
                "eligibility_failed",
                "blank",
                "cast.roles[0].eligibility.age_range",
                f"{blank}@{blank_version}",
                f"{age_rule} (18 to any, inclusive)",
            ),
            (
                "eligibility_failed",
                "blank",
                "cast.roles[0].eligibility.labels",
                f"{blank}@{blank_version}",
                f"{label_rule} (missing: warm)",
            ),
            (
                "eligibility_failed",
                "old",
                "cast.roles[1].eligibility.age_range",
                f"{aged}@{aged_version}",
                f"{age_rule} (any to 59, inclusive)",
            ),
            (
                "eligibility_failed",
                "old",
                "cast.roles[1].eligibility.labels",
                f"{aged}@{aged_version}",
                f"{label_rule} (missing: stern)",
            ),
        ]


async def test_seeds_are_located(settings: Any, engine: Any) -> None:
    """Each missing field of each seed is an error whose code names it
    (``seed_missing_holder``, ``seed_missing_audience``, ``seed_missing_disclosure``), at that
    field with the seed's id: a world seed and a planned reveal's truth with no role, a role's
    seed with its role.

    Complement asserted: the same three seeds, each declaring all three fields, validate."""
    async with rest(settings, engine) as client:
        body = scenarios.scenario(
            "Seeded",
            drivers={"planned_reveals": [{"truth": incomplete_seed("The deed is forged.")}]},
        )
        body["starting_state"]["seeded_memories"] = [{"text": "The farm is old."}]
        body["cast"]["roles"][0]["seeded_memories"] = [incomplete_seed()]
        created = await scenarios.create(client, body)
        ids = {
            "world": created["starting_state"]["seeded_memories"][0]["id"],
            "role": created["cast"]["roles"][0]["seeded_memories"][0]["id"],
            "reveal": created["drivers"]["planned_reveals"][0]["truth"]["id"],
        }
        errors = (await validate(client, created["id"])).json()["report"]["errors"]
        assert [
            (e["code"], e["location"]["role"], e["location"]["field"], e["location"]["seed_id"])
            for e in errors
        ] == [
            (
                "seed_missing_audience",
                None,
                "drivers.planned_reveals[0].truth.audience",
                ids["reveal"],
            ),
            (
                "seed_missing_audience",
                None,
                "starting_state.seeded_memories[0].audience",
                ids["world"],
            ),
            (
                "seed_missing_disclosure",
                None,
                "starting_state.seeded_memories[0].disclosure",
                ids["world"],
            ),
            (
                "seed_missing_holder",
                None,
                "starting_state.seeded_memories[0].holder",
                ids["world"],
            ),
            (
                "seed_missing_audience",
                "mother",
                "cast.roles[0].seeded_memories[0].audience",
                ids["role"],
            ),
        ]
        assert {e["outcome"] for e in errors} == {"rejected"}

        whole = scenarios.scenario(
            "Seeded whole",
            drivers={"planned_reveals": [{"truth": scenarios.seed("The deed is forged.")}]},
        )
        whole["starting_state"]["seeded_memories"] = [scenarios.seed("The farm is old.")]
        whole["cast"]["roles"][0]["seeded_memories"] = [scenarios.seed("The roof leaks.")]
        complete = await scenarios.create(client, whole)
        assert (await validate(client, complete["id"])).json()["valid"] is True


async def test_providers_must_be_declared(settings: Any, engine: Any) -> None:
    """Every provider the content names, in a scheduled item or at any depth of a starting
    value, must be declared in ``story_settings_defaults.required_providers``: each one that is
    not is a ``provider_undeclared`` error at its ``provider`` key, quoting the name.

    Complements asserted: declaring one name clears its error and leaves the other's; with
    both declared, the scenario validates."""
    async with rest(settings, engine) as client:
        body = with_provider(scenarios.scenario("Feeds"))
        body["starting_state"]["states"] = [{"name": "tide", "source": {"provider": "tide_table"}}]
        created = await scenarios.create(client, body)
        nested = "starting_state.states[0].source.provider"

        async def errors() -> list[tuple[str, str | None, str | None, str]]:
            report = (await validate(client, created["id"])).json()["report"]
            return [
                (e["code"], e["location"]["field"], e["text"], e["outcome"])
                for e in report["errors"]
            ]

        assert await errors() == [
            ("provider_undeclared", PROVIDER_FIELD, PROVIDER, "rejected"),
            ("provider_undeclared", nested, "tide_table", "rejected"),
        ]
        one = await scenarios.save(
            client, created["id"], 1, story_settings_defaults={"required_providers": [PROVIDER]}
        )
        assert one.status_code == 200, one.text
        assert await errors() == [("provider_undeclared", nested, "tide_table", "rejected")]
        both = await scenarios.save(
            client,
            created["id"],
            2,
            story_settings_defaults={"required_providers": [PROVIDER, "tide_table"]},
        )
        assert both.status_code == 200, both.text
        assert await errors() == []


async def test_personality_warnings(pg_url: str) -> None:
    """Role text that asserts personality warns for ``open`` and ``default`` roles, the player's
    included, in the setup description and in each of the role's seeds (with its id); a
    ``default`` role's warning names its character, whose personality applies. A ``locked``
    role's text and a negated term do not warn."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        aunt, _ = await rated_character(client, db, "Ivy Harrow", rating_id=EVERYONE_ID)
        brother, _ = await rated_character(client, db, "Tom Harrow", rating_id=EVERYONE_ID)
        body = scenarios.scenario(
            "Tempers",
            cast={
                "roles": [
                    filled(
                        "aunt",
                        "default",
                        aunt,
                        setup_description="She drove up with a casserole. She is bossy.",
                    ),
                    filled("brother", "locked", brother, setup_description="He is cruel."),
                    scenarios.role(
                        "mother",
                        setup_description="She is not a liar.",
                        seeded_memories=[scenarios.seed("She was always shy.", holder="mother")],
                    ),
                    scenarios.role(
                        "player", kind="player", setup_description="You are short-tempered."
                    ),
                ]
            },
        )
        created = await scenarios.create(client, body)
        seed_id = created["cast"]["roles"][2]["seeded_memories"][0]["id"]
        warnings = (await validate(client, created["id"])).json()["report"]["warnings"]
        assert [
            (
                e["code"],
                e["location"]["role"],
                e["location"]["field"],
                e["location"]["seed_id"],
                e["text"],
                e["source"],
                e["outcome"],
            )
            for e in warnings
        ] == [
            (
                "personality_in_role",
                "aunt",
                "cast.roles[0].setup_description",
                None,
                "She is bossy.",
                "Ivy Harrow",
                "ignored",
            ),
            (
                "personality_in_role",
                "mother",
                "cast.roles[2].seeded_memories[0].text",
                seed_id,
                "She was always shy.",
                None,
                "ignored",
            ),
            (
                "personality_in_role",
                "player",
                "cast.roles[3].setup_description",
                None,
                "You are short-tempered.",
                None,
                "ignored",
            ),
        ]
        assert {e["rule"] for e in warnings} == {"personality: character over scenario"}


async def test_a_retired_rating_still_matches_eligibility(pg_url: str) -> None:
    """A role's eligibility names ratings by id: a character whose rating was retired since its
    version was published still matches the role that lists that rating. Complement: a role
    that lists only another rating refuses it at ``content_ratings``."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        made = await client.post(
            "/admin/content-ratings",
            json={"name": f"Retired later {new_ulid()}"},
            headers={**scenarios.WRITE, "X-Epistrel-Principal": "p-operator"},
        )
        rating = made.json()["id"]
        held, _ = await rated_character(client, db, "Ret Ired", rating_id=rating)
        await retire_rating(db, rating, principal="p-operator")
        cast = scenarios.scenario(
            "Eligible",
            cast={
                "roles": [
                    filled("kept", "default", held, eligibility={"content_ratings": [rating]}),
                    filled(
                        "other", "default", held, eligibility={"content_ratings": [EVERYONE_ID]}
                    ),
                    scenarios.role("player", kind="player"),
                ]
            },
        )
        created = await scenarios.create(client, cast)
        errors = (await validate(client, created["id"])).json()["report"]["errors"]
        assert [(e["code"], e["location"]["role"], e["location"]["field"]) for e in errors] == [
            ("eligibility_failed", "other", "cast.roles[1].eligibility.content_ratings")
        ]


async def test_ratings_compare_at_the_stored_version(pg_url: str) -> None:
    """An item is compared at the rating version it was stored with: a character published
    while Explicit listed no policy stays looser than an Everyone scenario after Explicit lists
    Everyone's policy too, while a character published after that edit is not looser."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        guard = await link_rating_policy(client, db, EVERYONE_ID)
        before, before_version = await rated_character(
            client, db, "Old Rating", rating_id=EXPLICIT_ID
        )
        await list_policy(db, EXPLICIT_ID, guard)
        after, _ = await rated_character(client, db, "New Rating", rating_id=EXPLICIT_ID)
        cast = scenarios.scenario(
            "Compared",
            cast={
                "roles": [
                    filled("old", "default", before),
                    filled("new", "default", after),
                    scenarios.role("player", kind="player"),
                ]
            },
        )
        created = await scenarios.create(client, cast)
        warnings = (await validate(client, created["id"])).json()["report"]["warnings"]
        assert [(e["code"], e["location"]["role"], e["source"]) for e in warnings] == [
            ("rating_looser", "old", f"{before}@{before_version}")
        ]


async def test_the_report_is_logged_without_text(
    settings: Any, engine: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A publish logs its report at INFO with codes and locations only: never the text an entry
    quotes."""
    secret = f"a habitual liar {new_ulid()}"
    caplog.set_level(logging.INFO, logger="epistrel.core.catalog.report")
    async with rest(settings, engine) as client:
        body = scenarios.scenario(
            "Logged",
            cast={
                "roles": [
                    scenarios.role("mother", setup_description=f"She is {secret}."),
                    scenarios.role("player", kind="player"),
                ]
            },
        )
        created = await scenarios.create(client, body)
        assert (await scenarios.publish(client, created["id"], 1)).status_code == 201
    lines = [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == "validation report"
    ]
    (line,) = [found for found in lines if found.get("entry_id") == created["id"]]
    assert line["warnings"] == [
        {
            "code": "personality_in_role",
            "entry": created["id"],
            "version": "draft",
            "role": "mother",
            "field": "cast.roles[0].setup_description",
        }
    ]
    assert secret not in repr(lines)
    assert secret not in caplog.text


async def test_the_version_keeps_its_report(settings: Any, engine: Any) -> None:
    """A published version stores the report its publish answered; versions published before
    #111 have none."""
    async with rest(settings, engine) as client:
        sid, _ = await scenarios.published(client, scenarios.scenario("Kept report"))
        (row,) = await rows(
            engine, "SELECT report FROM catalog.scenario_versions WHERE catalog_id = :c", c=sid
        )
        assert row.report["errors"] == []
        assert set(row.report["unavailable"]) >= set(scenarios.undeliverable_checks())
