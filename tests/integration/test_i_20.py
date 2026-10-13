"""I-20: companion versioning, concurrency, and atomic publish (docs/04 §4), its M3 parts.

Every I-20 (M3) part lives in this file, because traceability counts a test ID per milestone in
one file. #116 adds its companion part here.

- #111's part, ``test_i_20__validation``: what a publish validates (FR-SCN-5) and the rating
  mismatch that only warns (FR-RATE-5). Every check has a draft that fails it (an error, or a
  warning) and one that passes; a dry run writes nothing; a publish refused for an error makes
  no version; the checks this deployment cannot run are listed; an Explicit character in an
  Everyone scenario warns and publishes, and a stricter one does not warn. It runs on its own
  scratch database, because it links a policy to the seeded Everyone rating. The details are in
  ``tests/integration/test_scenario_validation.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from epistrel.core.ids import new_ulid
from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID
from tests import scenario_support as scenarios
from tests.character_support import named
from tests.event_support import rows
from tests.media_support import media_client, scratch, uploaded, video
from tests.validation_support import (
    PROVIDER,
    PROVIDER_FIELD,
    codes,
    counts,
    entries,
    filled,
    global_wordlist,
    incomplete_seed,
    link_rating_policy,
    rated_character,
    validate,
    with_provider,
)


@pytest.mark.integration
@pytest.mark.part("M3", "FR-SCN-5; FR-RATE-5")
async def test_i_20__validation(pg_url: str, tmp_path: Path) -> None:
    """Every check fails a draft built to break it, and passes one built to keep it.

    The breaking draft has seven roles under a limit of six; a ``locked`` role naming no
    character, a ``default`` one naming an unpublished character, a ``locked`` one naming an
    archived character, and a ``default`` one whose character lacks the label its eligibility
    lists; a seed without an audience; a video as the cover and a gallery image retired since
    the draft added it; and a provider its content names but does not declare. Each is an error
    (``rejected``) at its field. It also has an Explicit character in a ``default`` role and an
    Explicit image in the gallery of this Everyone scenario, role text that asserts personality,
    and a seed that breaks a global input policy: warnings (``kept``, ``kept``, ``ignored``,
    ``kept``). A dry run reports all of it (``valid`` false) and writes nothing; a publish is 422
    ``publish_invalid`` with the same errors and makes no version. The report lists
    ``seed_normalization``, ``fact_consistency`` and ``state_schema`` as unavailable.

    The keeping draft validates with no error and no warning and publishes; the publish answers
    the report the dry run gave. An Explicit character in an Everyone scenario publishes with one
    ``rating_looser`` warning naming the character's version, and the version keeps that report.

    Complements asserted: an Everyone character in an Explicit scenario is stricter and gives no
    warning; the counts a dry run leaves alone are the counts it found."""
    async with (
        scratch(pg_url, tmp_path / "media", model_adapter="stub") as space,
        media_client(space.settings, space.db) as client,
    ):
        await link_rating_policy(client, space.db, EVERYONE_ID)
        fit, _ = await rated_character(
            client, space.db, "Ada Fit", rating_id=EVERYONE_ID, labels=["warm"], true_age=50
        )
        loose, loose_version = await rated_character(
            client, space.db, "Lux Loose", rating_id=EXPLICIT_ID, labels=["warm"], true_age=50
        )
        shelved, _ = await rated_character(client, space.db, "Sol Shelved", rating_id=EVERYONE_ID)
        archived = await client.post(
            f"/catalog/characters/{shelved}/archive", headers=scenarios.WRITE
        )
        assert archived.status_code == 200, archived.text
        unpublished = (
            await client.post(
                "/catalog/characters", json=named("Una Draft"), headers=scenarios.WRITE
            )
        ).json()["id"]
        cover = await uploaded(client, headers=scenarios.WRITE)
        wild_image = await uploaded(client, headers=scenarios.WRITE, content_rating_id=EXPLICIT_ID)
        film = await uploaded(
            client, video(), content_type="video/mp4", headers=scenarios.WRITE, kind="video"
        )
        word = f"zarquon{new_ulid().lower()}"
        policy = await global_wordlist(client, word)

        doomed = await uploaded(client, headers=scenarios.WRITE)
        breaking = with_provider(
            scenarios.scenario(
                "Broken",
                public_card={
                    "name": "Broken",
                    "media": {"cover": film["id"], "gallery": [doomed["id"], wild_image["id"]]},
                },
                cast={
                    "roles": [
                        filled("ghost", "locked", new_ulid()),
                        filled("drafted", "default", unpublished),
                        filled("shelf", "locked", shelved),
                        filled("picky", "default", fit, eligibility={"labels": ["cold"]}),
                        filled("wild", "default", loose),
                        scenarios.role(
                            "mother",
                            setup_description="She is a habitual liar. She raised four sons.",
                            seeded_memories=[
                                incomplete_seed(),
                                scenarios.seed(f"A {word} hides in the cellar.", holder="mother"),
                            ],
                        ),
                        scenarios.role("player", kind="player"),
                    ],
                    "max_characters_at_start": 6,
                },
            )
        )
        created = await scenarios.create(client, breaking)
        sid = created["id"]
        retired = await client.delete(f"/media/{doomed['id']}", headers=scenarios.WRITE)
        assert retired.status_code == 204, retired.text  # retired since the draft added it
        seeds = created["cast"]["roles"][5]["seeded_memories"]
        before = await counts(space.db)
        checked = await validate(client, sid)
        assert checked.status_code == 200, checked.text
        assert "Idempotency-Key" not in checked.headers
        assert checked.json()["valid"] is False
        report = checked.json()["report"]
        # Sorted by location: the entries naming no role first, then by role id and field.
        assert codes(report["errors"]) == [
            ("cast_limit_exceeded", "cast.roles"),
            ("media_kind_mismatch", "public_card.media.cover"),
            ("media_retired", "public_card.media.gallery[0]"),
            ("provider_undeclared", PROVIDER_FIELD),
            ("character_not_published", "cast.roles[1].character"),
            ("character_missing", "cast.roles[0].character"),
            ("seed_missing_audience", "cast.roles[5].seeded_memories[0].audience"),
            ("eligibility_failed", "cast.roles[3].eligibility.labels"),
            ("character_archived", "cast.roles[2].character"),
        ]
        assert {e["outcome"] for e in report["errors"]} == {"rejected"}
        assert [e["location"]["role"] for e in report["errors"]] == [
            None,
            None,
            None,
            None,
            "drafted",
            "ghost",
            "mother",
            "picky",
            "shelf",
        ]
        assert report["errors"][6]["location"]["seed_id"] == seeds[0]["id"]
        assert report["errors"][3]["text"] == PROVIDER
        assert [(e["code"], e["location"]["field"], e["outcome"]) for e in report["warnings"]] == [
            ("rating_looser", "public_card.media.gallery[1]", "kept"),
            ("input_policy", "cast.roles[5].seeded_memories[1].text", "kept"),
            ("personality_in_role", "cast.roles[5].setup_description", "ignored"),
            ("rating_looser", "cast.roles[4].character", "kept"),
        ]
        image, policed, personality, looser = report["warnings"]
        assert (looser["source"], image["source"]) == (f"{loose}@{loose_version}", wild_image["id"])
        assert (personality["text"], personality["rule"]) == (
            "She is a habitual liar.",
            "personality: character over scenario",
        )
        assert (policed["rule"], policed["text"], policed["location"]["seed_id"]) == (
            policy,
            word,
            seeds[1]["id"],
        )
        assert set(report["unavailable"]) >= set(scenarios.M3_UNAVAILABLE)
        assert await counts(space.db) == before

        refused = await scenarios.publish(client, sid, created["draft_revision"])
        assert (refused.status_code, scenarios.error_of(refused)["code"]) == (
            422,
            "publish_invalid",
        )
        assert scenarios.error_of(refused)["details"]["report"]["errors"] == report["errors"]
        assert await scenarios.version_rows(space.db, sid) == []
        assert await counts(space.db) == before

        keeping = with_provider(
            scenarios.scenario(
                "Kept",
                public_card={
                    "name": "Kept",
                    "media": {"cover": cover["id"], "videos": [{"media_id": film["id"]}]},
                },
                cast={
                    "roles": [
                        filled(
                            "aunt",
                            "default",
                            fit,
                            eligibility={
                                "labels": ["warm"],
                                "content_ratings": [EVERYONE_ID],
                                "age_range": [50, 50],
                            },
                        ),
                        scenarios.role(
                            "player",
                            kind="player",
                            seeded_memories=[scenarios.seed("The roof leaks.", holder="player")],
                        ),
                    ]
                },
            ),
            declared=[PROVIDER],
        )
        kept = await scenarios.create(client, keeping)
        clean = await validate(client, kept["id"])
        assert clean.json()["valid"] is True, clean.text
        assert (clean.json()["report"]["errors"], clean.json()["report"]["warnings"]) == ([], [])
        published = await scenarios.publish(client, kept["id"], kept["draft_revision"])
        assert published.status_code == 201, published.text
        assert published.json()["report"] == clean.json()["report"]

        mismatched = await scenarios.create(
            client,
            scenarios.scenario(
                "Looser",
                cast={
                    "roles": [
                        filled("wild", "default", loose),
                        scenarios.role("player", kind="player"),
                    ]
                },
            ),
        )
        warned = await scenarios.publish(client, mismatched["id"], mismatched["draft_revision"])
        assert warned.status_code == 201, warned.text
        assert [
            (e["code"], e["location"]["role"], e["source"])
            for e in warned.json()["report"]["warnings"]
        ] == [("rating_looser", "wild", f"{loose}@{loose_version}")]
        (stored,) = await rows(
            space.db,
            "SELECT report FROM catalog.scenario_versions WHERE catalog_id = :c",
            c=mismatched["id"],
        )
        assert stored.report == warned.json()["report"]

        stricter = await scenarios.create(
            client,
            scenarios.scenario(
                "Stricter",
                metadata={"labels": [], "content_rating_id": EXPLICIT_ID},
                cast={
                    "roles": [
                        filled("aunt", "default", fit),
                        scenarios.role("player", kind="player"),
                    ]
                },
            ),
        )
        calm = await validate(client, stricter["id"])
        assert calm.json()["valid"] is True, calm.text
        assert entries(calm.json()["report"], "rating_looser") == []
        assert calm.json()["report"]["warnings"] == []
