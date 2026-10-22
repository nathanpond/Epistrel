"""I-20: companion versioning, concurrency, and atomic publish (docs/04 §4), its M3 parts and
its M4 part.

Every I-20 part lives in this file, because traceability counts a test ID per milestone in one
file. #149's M4 part, ``test_i_20__alias_seed``, is the publish-time seed check inside the
companion's validation (FR-CAT-10, FR-SCN-5, FR-PROP-12).

- #111's part, ``test_i_20__validation``: what a publish validates (FR-SCN-5) and the rating
  mismatch that only warns (FR-RATE-5). Every check has a draft that fails it (an error, or a
  warning) and one that passes; a dry run writes nothing; a publish refused for an error makes
  no version; the checks this deployment cannot run are listed; an Explicit character in an
  Everyone scenario warns and publishes, and a stricter one does not warn. It runs on its own
  scratch database, because it links a policy to the seeded Everyone rating. The details are in
  ``tests/integration/test_scenario_validation.py``.
- #116's part, ``test_i_20__companion``: a character and its companion scenario keep separate
  version sequences linked by a pin (FR-CAT-10): the companion published alone, the character
  alone, both together; the atomic failure and its fix, an injected failure, the rating
  difference that only warns (FR-RATE-5), and two editors at once (FR-CAT-3). Also on its own
  scratch database, for the same reason. The details are in
  ``tests/integration/test_companion.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.catalog import companion as companions
from epistrel.core.catalog.characters import CharacterPublishRequest
from epistrel.core.catalog.companion_slots import pin_of, repinned
from epistrel.core.context import RequestContext
from epistrel.core.ids import new_ulid
from epistrel.core.seeds.ratings import EVERYONE_ID, EXPLICIT_ID
from epistrel.testing import pauses
from tests import scenario_support as scenarios
from tests.catalog_support import read_companion
from tests.character_support import complete, named
from tests.event_support import rows
from tests.media_support import media_client, scratch, uploaded, video
from tests.scenario_support import OPENING
from tests.story_support import rest
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
    rating_version,
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
    ``publish_invalid`` with the same errors and makes no version. The report lists the checks
    the deployment cannot run as unavailable (``state_schema``; the seed checks run once the
    structured pipeline is delivered, #149).

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
        assert set(report["unavailable"]) >= set(scenarios.undeliverable_checks())
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


# -- #116: the companion's versions ---------------------------------------------------------------

LINDA_WORLD = {"description": "A terraced house on a wet street in Leeds."}
NEW_OPENING = "Linda opens the door before you knock."
HOOK = "A letter from Linda's sister lies unopened on the table."


async def _answer(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _refusal(response: httpx.Response) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def _publish_character(
    client: httpx.AsyncClient,
    character_id: str,
    revision: int,
    *,
    companion: int | None = None,
    key: str | None = None,
) -> httpx.Response:
    """``POST /catalog/characters/{id}/publish``, with ``companion``: the companion's draft at
    that revision too (``with_companion=true``)."""
    body: dict[str, Any] = {"draft_revision": revision}
    params: dict[str, str] = {}
    if companion is not None:
        body["companion_draft_revision"] = companion
        params["with_companion"] = "true"
    sent = scenarios.WRITE if key is None else {**scenarios.WRITE, "Idempotency-Key": key}
    return await client.post(
        f"/catalog/characters/{character_id}/publish", json=body, params=params, headers=sent
    )


async def _companion_save(
    client: httpx.AsyncClient, character_id: str, **groups: Any
) -> dict[str, Any]:
    """Save the companion's draft at its current revision: the ``full`` draft answered."""
    draft = await read_companion(client, character_id)
    return await _answer(
        await scenarios.save(client, draft["id"], draft["draft_revision"], **groups)
    )


async def _character_save(
    client: httpx.AsyncClient, character_id: str, revision: int, **groups: Any
) -> dict[str, Any]:
    return await _answer(
        await client.patch(
            f"/catalog/characters/{character_id}",
            json={"draft_revision": revision, **groups},
            headers=scenarios.WRITE,
        )
    )


async def _pins(db: AsyncEngine, scenario_id: str) -> list[tuple[Any, ...]]:
    """Each version of the companion: ``(version, pinned character id, pinned version, the pin
    its content holds)``, oldest first."""
    found = await rows(
        db,
        "SELECT version, pinned_character_id, pinned_character_version, content "
        "FROM catalog.scenario_versions WHERE catalog_id = :s ORDER BY version",
        s=scenario_id,
    )
    return [
        (r.version, r.pinned_character_id, r.pinned_character_version, pin_of(r.content))
        for r in found
    ]


async def _versions_of(db: AsyncEngine, character_id: str) -> int:
    found = await rows(
        db,
        "SELECT count(*) AS n FROM catalog.character_versions WHERE catalog_id = :c",
        c=character_id,
    )
    return int(found[0].n)


async def _waiting(db: AsyncEngine, within: float = 10.0) -> None:
    """Return once a backend of this database waits on a lock."""
    deadline = time.monotonic() + within
    while not await rows(
        db,
        "SELECT 1 FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock'",
    ):
        assert time.monotonic() < deadline, "no request waited on the lock in time"
        await asyncio.sleep(0.02)


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-3,5,10; FR-SCN-5; FR-RATE-5")
async def test_i_20__companion(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linda's companion keeps its own version sequence, linked to hers by the pin.

    - **First publish.** Before Linda's first publish her companion cannot be published alone
      (``character_not_published``). Her first publish (version 1) creates companion version 1
      from the companion's defaults (its two slots, no world), pinned to 1, although the
      companion's draft already holds an unpublished world: the draft stays a draft, its pin
      moved to 1.
    - **Companion alone.** The companion's draft with a new opening publishes as version 2,
      pinned to Linda's latest published version, 1; Linda's versions do not change.
    - **Character alone.** Linda's version 2, published while the companion's draft holds an
      unpublished plot hook, creates companion version 3: the last published companion content,
      pinned to 2. The draft keeps the plot hook, unpublished, its pin moved to 2.
    - **Both together.** ``with_companion`` publishes Linda's version 3 and the companion's draft
      (the plot hook) as one pair, pinned to 3.
    - **Atomic failure.** Linda's version 4 published with a companion draft whose new seed
      declares no audience is 422 ``publish_invalid`` naming the companion and the seed: no
      character version 4 and no new companion version exist, and Linda's draft revision is
      unchanged. Fixing the seed and publishing both together succeeds.
    - **Injected failure.** A failure after the character-version insert (the companion step
      raising) rolls back both.
    - **Rating difference.** Once Everyone carries a policy Explicit lacks, a companion rated
      Explicit beside Linda rated Everyone publishes with one ``rating_looser`` warning naming
      the companion, and Linda rated Explicit beside a companion rated Everyone publishes the
      pair with one naming Linda's version: a difference warns either way, never blocks. The
      dry run reports it too, and a copied companion version records its rating's version now
      current.
    - **Concurrency.** Two saves of the companion's draft from one revision: the second is 409
      ``draft_stale``. Two publishes of Linda's draft at once make one character version and
      one companion version, the other 409 ``draft_stale`` naming Linda; under one
      ``Idempotency-Key`` the second answers the first's result.
    - **Pins.** Every companion version records the character version its locked slot uses, in
      its columns and its content alike, and that version exists. Stories created from them
      (each snapshotting its pinned pair) are #119's ``test_i_22__creation``.

    It runs on its own scratch database, because it links a policy to the seeded Everyone rating
    (the seeded ratings carry no policies, so no pair of them differs otherwise).

    Complements asserted: each refused publish leaves both version counts as they were; the
    warnings' publishes made their versions."""
    async with (
        scratch(pg_url, tmp_path / "media", model_adapter="stub") as space,
        media_client(space.settings, space.db) as client,
    ):
        db = space.db
        made = await client.post(
            "/catalog/characters", json=complete("Linda Okafor"), headers=scenarios.WRITE
        )
        linda = await _answer(made, 201)
        cid, sid = linda["id"], linda["companion_id"]

        # -- the first publish
        edited = await _companion_save(
            client, cid, world=LINDA_WORLD, starting_state={"opening": {"narration": OPENING}}
        )
        early = await scenarios.publish(client, sid, edited["draft_revision"])
        status, code, details = _refusal(early)
        assert (status, code) == (422, "publish_invalid")
        assert codes(details["report"]["errors"]) == [
            ("character_not_published", "cast.roles[0].character")
        ]
        first = await _answer(await _publish_character(client, cid, 1), 201)
        assert (first["version"], first["companion"]["version"]) == (1, 1)
        v1 = await _answer(
            await client.get(
                f"{scenarios.BASE}/{sid}",
                params={"version": "1", "projection": "full"},
                headers=scenarios.READ,
            )
        )
        assert v1["world"]["description"] is None
        assert v1["starting_state"]["opening"]["narration"] is None
        assert [(r["id"], r["fill"], r["character"]) for r in v1["cast"]["roles"]] == [
            ("npc", "locked", {"catalog_id": cid, "version": 1}),
            ("player", "open", None),
        ]
        draft = await read_companion(client, cid)
        assert draft["world"] == {**v1["world"], **LINDA_WORLD}
        assert (draft["pinned_character_version"], draft["draft_revision"]) == (
            1,
            edited["draft_revision"] + 1,
        )
        assert first["companion"]["draft_revision"] == draft["draft_revision"]

        # -- the companion alone
        opened = await _companion_save(
            client, cid, starting_state={"opening": {"narration": NEW_OPENING}}
        )
        alone = await _answer(await scenarios.publish(client, sid, opened["draft_revision"]), 201)
        assert alone["version"] == 2
        assert await _pins(db, sid) == [(1, cid, 1, 1), (2, cid, 1, 1)]
        assert await _versions_of(db, cid) == 1

        # -- the character alone, while the companion's draft holds an unpublished plot hook
        hooked = await _companion_save(client, cid, drivers={"plot_hooks": [HOOK]})
        saved = await _character_save(
            client, cid, first["draft_revision"], public_card={"public_description": "Retired."}
        )
        second = await _answer(await _publish_character(client, cid, saved["draft_revision"]), 201)
        assert (second["version"], second["companion"]["version"]) == (2, 3)
        contents = {
            r.version: r.content
            for r in await rows(
                db,
                "SELECT version, content FROM catalog.scenario_versions WHERE catalog_id = :s",
                s=sid,
            )
        }
        assert contents[3] == repinned(contents[2], 2)
        assert contents[3]["starting_state"]["opening"]["narration"] == NEW_OPENING
        assert HOOK not in json.dumps(contents[3])
        draft = await read_companion(client, cid)
        assert draft["drivers"]["plot_hooks"] == [HOOK]
        assert (draft["pinned_character_version"], draft["draft_revision"]) == (
            2,
            hooked["draft_revision"] + 1,
        )

        # -- both together
        third = await _answer(
            await _publish_character(
                client, cid, second["draft_revision"], companion=draft["draft_revision"]
            ),
            201,
        )
        assert (third["version"], third["companion"]["version"]) == (3, 4)
        v4 = await _answer(
            await client.get(
                f"{scenarios.BASE}/{sid}",
                params={"version": "4", "projection": "full"},
                headers=scenarios.READ,
            )
        )
        assert (v4["drivers"]["plot_hooks"], v4["pinned_character_version"]) == ([HOOK], 3)

        # -- the atomic failure: a seed with no audience refuses the pair, and nothing is written
        broken = await _companion_save(
            client, cid, starting_state={"seeded_memories": [incomplete_seed("Rain gets in.")]}
        )
        seed_id = broken["starting_state"]["seeded_memories"][0]["id"]
        before = (await _versions_of(db, cid), len(await _pins(db, sid)))
        refused = await _publish_character(
            client, cid, third["draft_revision"], companion=broken["draft_revision"]
        )
        status, code, details = _refusal(refused)
        assert (status, code, details["entity"], details["scenario_id"]) == (
            422,
            "publish_invalid",
            "companion",
            sid,
        )
        errors = details["report"]["errors"]
        assert codes(errors) == [
            ("seed_missing_audience", "starting_state.seeded_memories[0].audience")
        ]
        assert errors[0]["location"]["seed_id"] == seed_id
        assert (await _versions_of(db, cid), len(await _pins(db, sid))) == before
        unchanged = await _answer(
            await client.get(
                f"/catalog/characters/{cid}",
                params={"draft": "true", "projection": "full"},
                headers=scenarios.READ,
            )
        )
        assert unchanged["draft_revision"] == third["draft_revision"]
        fixed = await _companion_save(
            client,
            cid,
            starting_state={"seeded_memories": [scenarios.seed("Rain gets in.", id=seed_id)]},
        )
        fourth = await _answer(
            await _publish_character(
                client, cid, third["draft_revision"], companion=fixed["draft_revision"]
            ),
            201,
        )
        assert (fourth["version"], fourth["companion"]["version"]) == (4, 5)

        # -- a failure injected after the character-version insert rolls back both
        async def failing(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("injected after the character version")

        before = (await _versions_of(db, cid), len(await _pins(db, sid)))
        monkeypatch.setattr(companions, "copy_companion", failing)
        with pytest.raises(RuntimeError, match="injected"):
            await space.lib.publish_character(
                RequestContext(principal="p-injected"),
                cid,
                CharacterPublishRequest(draft_revision=fourth["draft_revision"]),
            )
        monkeypatch.undo()
        assert (await _versions_of(db, cid), len(await _pins(db, sid))) == before

        # -- a rating difference warns, either way, and never blocks
        await link_rating_policy(client, db, EVERYONE_ID)
        fifth = await _answer(await _publish_character(client, cid, fourth["draft_revision"]), 201)
        assert fifth["version"] == 5
        # The copy records the rating's version now current (the policy made a new one).
        stored_ratings = {
            r.version: r.content_rating_version_id
            for r in await rows(
                db,
                "SELECT version, content_rating_version_id FROM catalog.scenario_versions "
                "WHERE catalog_id = :s",
                s=sid,
            )
        }
        copy = fifth["companion"]["version"]
        assert stored_ratings[copy] == await rating_version(db, EVERYONE_ID)
        assert stored_ratings[copy] != stored_ratings[copy - 1]
        explicit = await _companion_save(client, cid, metadata={"content_rating_id": EXPLICIT_ID})
        dry = await validate(client, sid)
        assert [
            (e["location"]["field"], e["source"])
            for e in entries(dry.json()["report"], "rating_looser")
        ] == [("metadata.content_rating_id", f"{sid}@draft")]
        looser = await _answer(
            await scenarios.publish(client, sid, explicit["draft_revision"]), 201
        )
        assert [
            (e["code"], e["location"]["field"], e["source"]) for e in looser["report"]["warnings"]
        ] == [("rating_looser", "metadata.content_rating_id", f"{sid}@draft")]
        rated = await _character_save(
            client, cid, fifth["draft_revision"], metadata={"content_rating_id": EXPLICIT_ID}
        )
        everyone = await _companion_save(client, cid, metadata={"content_rating_id": EVERYONE_ID})
        pair = await _answer(
            await _publish_character(
                client, cid, rated["draft_revision"], companion=everyone["draft_revision"]
            ),
            201,
        )
        assert pair["report"]["warnings"] == []
        assert [
            (e["code"], e["location"]["field"], e["source"])
            for e in pair["companion"]["report"]["warnings"]
        ] == [("rating_looser", "cast.roles[0].character", f"{cid}@{pair['version']}")]

        # -- concurrency: two saves from one revision; two publishes of one draft
        draft = await read_companion(client, cid)
        saves = [
            await scenarios.save(client, sid, draft["draft_revision"], drivers={"objectives": [x]})
            for x in ("Stay for tea.", "Leave before dark.")
        ]
        assert [s.status_code for s in saves] == [200, 409]
        assert scenarios.error_of(saves[1])["code"] == "draft_stale"
        before = (await _versions_of(db, cid), len(await _pins(db, sid)))
        with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
            winning = asyncio.ensure_future(_publish_character(client, cid, pair["draft_revision"]))
            await hold.arrived(1)
            losing = asyncio.ensure_future(_publish_character(client, cid, pair["draft_revision"]))
            await _waiting(db)
            hold.release()
            won, lost = await asyncio.gather(winning, losing)
        assert won.status_code == 201, won.text
        assert _refusal(lost)[:2] == (409, "draft_stale")
        assert _refusal(lost)[2]["entity"] == "character"
        assert (await _versions_of(db, cid), len(await _pins(db, sid))) == (
            before[0] + 1,
            before[1] + 1,
        )
        key = f"twice-{cid}"
        revision = won.json()["draft_revision"]
        with pauses.held(pauses.CATALOG_PUBLISH_LOCKED) as hold:
            original = asyncio.ensure_future(_publish_character(client, cid, revision, key=key))
            await hold.arrived(1)
            duplicate = asyncio.ensure_future(_publish_character(client, cid, revision, key=key))
            await asyncio.sleep(0.2)  # the duplicate waits on the key's original
            hold.release()
            first_answer, second_answer = await asyncio.gather(original, duplicate)
        assert first_answer.status_code == second_answer.status_code == 201
        assert second_answer.json() == first_answer.json()
        assert second_answer.headers.get("Idempotent-Replay") == "true"
        assert (await _versions_of(db, cid), len(await _pins(db, sid))) == (
            before[0] + 2,
            before[1] + 2,
        )

        # -- every companion version records the character version its locked slot uses
        pins = await _pins(db, sid)
        assert [p[0] for p in pins] == list(range(1, len(pins) + 1))
        for _, character, pinned, held in pins:
            assert (character, pinned) == (cid, held)
            assert await rows(
                db,
                "SELECT 1 FROM catalog.character_versions WHERE catalog_id = :c AND version = :v",
                c=cid,
                v=pinned,
            )


ALIAS = "Lin"
ALIAS_SEED = "Only her brother still calls her Lin."
"""A companion seed naming Linda by the alias her new version drops (I-20 M4)."""


@pytest.mark.integration
@pytest.mark.part("M4", "FR-CAT-10; FR-SCN-5; FR-PROP-12")
async def test_i_20__alias_seed(settings: Any, engine: AsyncEngine) -> None:
    """Linda's companion holds a seed that names her by her alias "Lin". While her version has
    the alias, the pair publishes. Her next version drops it: publishing both together fails
    as the M3 atomic failure does, 422 ``publish_invalid`` naming the companion
    (``details.entity``), with the seed-normalization error ``seed_unnormalizable`` at the
    seed's text and its ``seed_id``; neither a character version nor a companion version is
    made. The companion's validation inside the character's publish
    (``companion.copy_companion``) runs the check: no story exists, so the seed is read against
    the companion's roles, and no open role could carry the name.

    Complements asserted: with the alias, the same seed publishes; after the refusal both
    version counts are as they were, and putting the alias back publishes the pair."""
    async with rest(settings, engine) as client:
        made = await client.post(
            "/catalog/characters",
            json=complete("Linda Okafor", metadata={"aliases": [ALIAS]}),
            headers=scenarios.WRITE,
        )
        linda = await _answer(made, 201)
        cid, sid = linda["id"], linda["companion_id"]
        first = await _answer(await _publish_character(client, cid, 1), 201)
        seeded = await _companion_save(
            client,
            cid,
            world=LINDA_WORLD,
            starting_state={
                "opening": {"narration": OPENING},
                "seeded_memories": [scenarios.seed(ALIAS_SEED, audience=["public"])],
            },
        )
        seed_id = seeded["starting_state"]["seeded_memories"][0]["id"]
        paired = await _answer(
            await _publish_character(
                client, cid, first["draft_revision"], companion=seeded["draft_revision"]
            ),
            201,
        )
        assert (paired["version"], paired["companion"]["version"]) == (2, 2)
        assert not [
            e for e in paired["companion"]["report"]["errors"] if e["code"] == "seed_unnormalizable"
        ]

        dropped = await _character_save(
            client, cid, paired["draft_revision"], metadata={"aliases": []}
        )
        draft = await read_companion(client, cid)
        before = (await _versions_of(engine, cid), len(await _pins(engine, sid)))
        refused = await _publish_character(
            client, cid, dropped["draft_revision"], companion=draft["draft_revision"]
        )
        status, code, details = _refusal(refused)
        assert (status, code, details["entity"], details["scenario_id"]) == (
            422,
            "publish_invalid",
            "companion",
            sid,
        )
        errors = details["report"]["errors"]
        assert codes(errors) == [("seed_unnormalizable", "starting_state.seeded_memories[0].text")]
        assert errors[0]["location"]["seed_id"] == seed_id
        assert errors[0]["source"] == "unknown_name"
        assert (await _versions_of(engine, cid), len(await _pins(engine, sid))) == before

        restored = await _character_save(
            client, cid, dropped["draft_revision"], metadata={"aliases": [ALIAS]}
        )
        fixed = await _answer(
            await _publish_character(
                client, cid, restored["draft_revision"], companion=draft["draft_revision"]
            ),
            201,
        )
        assert (fixed["version"], fixed["companion"]["version"]) == (3, 3)
