"""C-15: the catalog and story-creation API (docs/04 §5), its M3 parts.

Every C-15 (M3) part lives in this file (traceability counts a test ID per milestone in one file).

- #103's part, ``test_c_15__catalog_lifecycle``: a catalog character's drafts and immutable
  published versions (FR-CAT-1,3) and the ``card``/``full`` projections (FR-SCN-10). The details
  are in ``tests/integration/test_catalog_characters.py``.
- #105's part, ``test_c_15__media_upload``: the media upload endpoint (FR-MEDIA-1, FR-API-6).
  The details are in ``tests/integration/test_media_assets.py``.
- #108's parts: ``test_c_15__scenario_lifecycle``, a scenario's drafts, immutable published
  versions and its ``card``/``full`` projections (FR-SCN-1,2,10); and
  ``test_c_15__scenario_field_classes``, the build-time check that every scenario field carries
  its class (FR-SCN-2). The details are in ``tests/integration/test_scenario_catalog.py``; the
  roles and the starting-cast limit are U-26's (``tests/test_u_26.py``).
"""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.catalog.scenario_models import (
    FieldClass,
    ScenarioDraft,
    Use,
    classification_problems,
    scenario_schema_classes,
)
from tests import scenario_support as scenarios
from tests.catalog_support import (
    BASE,
    CARD_KEYS,
    READ,
    VERSION_KEYS,
    WRITE,
    character,
    create,
    publish,
    read,
    save,
    version_rows,
)
from tests.event_support import execute_in_tx, sqlstate
from tests.media_support import (
    READ as MEDIA_READ,
)
from tests.media_support import (
    blob_path,
    media_client,
    metadata,
    png,
    set_media,
    sha256,
    upload,
)
from tests.story_support import rest

PLANTED = "counts the silver twice before the harbour wakes"
"""A phrase only the private definition holds."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-1,3; FR-SCN-10")
async def test_c_15__catalog_lifecycle(settings: Settings, engine: AsyncEngine) -> None:
    """A catalog character starts as an editable draft, is published as version 1, edited (the
    edit shows only in a draft read until it is published) and published as version 2; version 1
    reads unchanged afterwards and its row cannot be updated; the version list names both, each
    with its publish time and publisher. A read with no projection is the public card of the
    ``published`` entry: exactly its nine keys and never a definition field (the phrase planted
    in the private personality appears in no card), while ``full`` returns it; an archived entry
    still reads in both projections.

    Complement asserted: the phrase is present in ``full``, so its absence from every card is
    the projection's doing."""
    async with rest(settings, engine) as client:
        created = await create(
            client, character(definition={"personality": f"Generous, yet she {PLANTED}."})
        )
        cid = created["id"]
        assert (created["status"], created["draft_revision"], created["version"]) == (
            "draft",
            1,
            None,
        )
        edited = await save(client, cid, 1, public_description="Keeper of the Gull Rock light.")
        assert (edited.status_code, edited.json()["draft_revision"]) == (200, 2)
        first = await publish(client, cid, 2)
        assert (first.status_code, first.json()["version"]) == (201, 1)
        version_1 = (await read(client, cid, version=1, projection="full")).json()

        edited = await save(client, cid, 3, public_description="Keeper, retired.")
        assert edited.status_code == 200
        # Until it is published, the edit is the draft's alone: a read still serves version 1.
        shown = (await read(client, cid)).json()
        assert (shown["version"], shown["public_description"]) == (
            1,
            "Keeper of the Gull Rock light.",
        )
        pending = (await read(client, cid, draft=True)).json()
        assert (pending["version"], pending["public_description"]) == (1, "Keeper, retired.")
        second = await publish(client, cid, 4)
        assert (second.status_code, second.json()["version"]) == (201, 2)

        again = (await read(client, cid, version=1, projection="full")).json()
        for key in ("version", "published_at", "name", "public_description", "definition"):
            assert again[key] == version_1[key], key
        stored = await version_rows(engine, cid)
        with pytest.raises(DBAPIError) as refused:
            await execute_in_tx(
                engine,
                (
                    "UPDATE catalog.character_versions SET content = '{}'::jsonb "
                    "WHERE catalog_id = :c AND version = 1",
                    {"c": cid},
                ),
            )
        assert sqlstate(refused.value) == "EPI01"
        assert await version_rows(engine, cid) == stored
        listed = await client.get(f"{BASE}/{cid}/versions", headers=READ)
        assert [set(v) for v in listed.json()["items"]] == [VERSION_KEYS, VERSION_KEYS]
        assert [(v["version"], v["published_at"]) for v in listed.json()["items"]] == [
            (1, version_1["published_at"]),
            (2, second.json()["published_at"]),
        ]

        card = await read(client, cid)
        assert card.status_code == 200
        assert set(card.json()) == CARD_KEYS
        assert (
            card.json()["version"],
            card.json()["status"],
            card.json()["public_description"],
        ) == (2, "published", "Keeper, retired.")
        assert PLANTED not in card.text
        full = await read(client, cid, projection="full")
        assert PLANTED in full.json()["definition"]["personality"]

        archived = await client.post(f"{BASE}/{cid}/archive", headers=WRITE)
        assert archived.json()["status"] == "archived"
        for projection in ("card", "full"):
            found = await read(client, cid, projection=projection)
            assert (found.status_code, found.json()["status"]) == (200, "archived")
            assert (PLANTED in found.text) == (projection == "full")


GENERATION = {
    "adapter": "draw-things",
    "model": "sdxl-turbo",
    "prompt": "A lighthouse keeper at dusk, oil on canvas",
    "negative_prompt": "text, watermark",
    "seed": 4_294_967_295,
    "parameters": {"steps": 30, "cfg_scale": 7.5, "sampler": "euler_a", "hires": {"scale": 1.5}},
    "reference_ids": ["01J9Z8Q4N5V6W7X8Y9Z0A1B2C8"],
}
"""FR-CAT-4's generation metadata: seed, model, prompts, parameters and reference ids."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-MEDIA-1; FR-API-6")
async def test_c_15__media_upload(settings: Settings, engine: AsyncEngine) -> None:
    """``POST /media`` stores an image with its metadata (201): the declared subjects, the
    sniffed type, dimensions, checksum and rating, and generation metadata that reads back
    exactly as sent; ``GET /media/{id}`` and ``…/content`` serve it. An upload that omits
    ``depicted_subjects`` is 422 ``subjects_required``, while an explicitly empty list is
    accepted (201). A type outside ``media_allowed_types`` is 415 ``media_type_not_allowed``
    and a file over ``media_max_bytes`` is 413 ``media_too_large``; neither stores a row or a
    blob.

    Complement asserted: the same bytes refused for their size are accepted once the limit
    allows them, so the 413 was the limit's doing."""
    subjects = [{"kind": "catalog_character", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C5"}]
    data = png(8, 6)
    async with media_client(settings, engine) as client:
        created = await upload(
            client,
            data,
            metadata(
                alt_text="Mara at the door",
                caption="Gull Rock",
                generation_metadata=GENERATION,
                depicted_subjects=subjects,
            ),
        )
        assert created.status_code == 201, created.text
        asset = created.json()
        assert (asset["kind"], asset["mime_type"], asset["width"], asset["height"]) == (
            "image",
            "image/png",
            8,
            6,
        )
        assert (asset["checksum"], asset["size"]) == (sha256(data), len(data))
        assert asset["depicted_subjects"] == subjects
        assert asset["generation_metadata"] == GENERATION
        read = await client.get(f"/media/{asset['id']}", headers=MEDIA_READ)
        assert read.json()["generation_metadata"] == GENERATION
        assert read.json()["state"] == "live"
        content = await client.get(f"/media/{asset['id']}/content", headers=MEDIA_READ)
        assert (content.status_code, content.content) == (200, data)

        omitted_bytes = png()
        omitted = await upload(client, omitted_bytes, metadata(depicted_subjects=None))
        assert (omitted.status_code, omitted.json()["error"]["code"]) == (422, "subjects_required")
        assert not blob_path(settings.media_root, sha256(omitted_bytes)).exists()
        empty = await upload(client, omitted_bytes, metadata(depicted_subjects=[]))
        assert (empty.status_code, empty.json()["depicted_subjects"]) == (201, [])

        gif = b"GIF89a" + png()[8:]
        refused_type = await upload(client, gif, content_type="image/gif")
        assert (refused_type.status_code, refused_type.json()["error"]["code"]) == (
            415,
            "media_type_not_allowed",
        )
        assert not blob_path(settings.media_root, sha256(gif)).exists()

        big = png(40, 40)
        try:
            await set_media(client, media_max_bytes=len(big) - 1)
            refused_size = await upload(client, big)
            assert (refused_size.status_code, refused_size.json()["error"]["code"]) == (
                413,
                "media_too_large",
            )
            assert not blob_path(settings.media_root, sha256(big)).exists()
            await set_media(client, media_max_bytes=len(big))
            assert (await upload(client, big)).status_code == 201
        finally:
            await set_media(client, media_max_bytes=50 * 1024 * 1024)


@pytest.mark.integration
@pytest.mark.part("M3", "FR-SCN-1,2,10")
async def test_c_15__scenario_lifecycle(settings: Settings, engine: AsyncEngine) -> None:
    """A scenario that fills every group starts as an editable draft; a save based on an older
    draft revision is 409 ``draft_stale`` and changes nothing. It is published as version 1,
    edited (the edit shows only in a draft read until it is published) and published as
    version 2; version 1 reads unchanged afterwards and its row cannot be updated; the version
    list names both. A read with no projection is the public card of the ``published``
    scenario: exactly the card's keys (the envelope, name, public description, media, labels,
    content rating, and each role's id, public label, setup description, kind and fill), and
    none of the phrases planted in the world description, a role's seeded memory and the
    director guidance; ``full`` returns every group. An archived scenario still reads in both
    projections.

    Complement asserted: each planted phrase is present in ``full``, so its absence from every
    card is the projection's doing."""
    base = scenarios.BASE
    async with rest(settings, engine) as client:
        created = await scenarios.create(client, scenarios.complete())
        sid = created["id"]
        assert (created["status"], created["draft_revision"], created["version"]) == (
            "draft",
            1,
            None,
        )
        edited = await scenarios.save(client, sid, 1, public_card={"public_description": "One."})
        assert (edited.status_code, edited.json()["draft_revision"]) == (200, 2)
        stale = await scenarios.save(client, sid, 1, public_card={"public_description": "Lost."})
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "draft_stale")
        assert stale.json()["error"]["details"]["current_draft_revision"] == 2
        first = await scenarios.publish(client, sid, 2)
        assert (first.status_code, first.json()["version"]) == (201, 1)
        version_1 = (await scenarios.read(client, sid, version=1, projection="full")).json()

        edited = await scenarios.save(client, sid, 3, public_card={"public_description": "Two."})
        assert edited.status_code == 200
        shown = (await scenarios.read(client, sid)).json()
        assert (shown["version"], shown["public_description"]) == (1, "One.")
        pending = (await scenarios.read(client, sid, draft=True)).json()
        assert (pending["version"], pending["public_description"]) == (1, "Two.")
        second = await scenarios.publish(client, sid, 4)
        assert (second.status_code, second.json()["version"]) == (201, 2)

        again = (await scenarios.read(client, sid, version=1, projection="full")).json()
        for key in ("version", "published_at", *scenarios.GROUPS):
            assert again[key] == version_1[key], key
        stored = await scenarios.version_rows(engine, sid)
        with pytest.raises(DBAPIError) as refused:
            await execute_in_tx(
                engine,
                (
                    "UPDATE catalog.scenario_versions SET content = '{}'::jsonb "
                    "WHERE catalog_id = :c AND version = 1",
                    {"c": sid},
                ),
            )
        assert sqlstate(refused.value) == "EPI01"
        assert await scenarios.version_rows(engine, sid) == stored
        listed = await client.get(f"{base}/{sid}/versions", headers=scenarios.READ)
        assert [(v["version"], v["published_at"]) for v in listed.json()["items"]] == [
            (1, version_1["published_at"]),
            (2, second.json()["published_at"]),
        ]

        card = await scenarios.read(client, sid)
        assert card.status_code == 200
        assert set(card.json()) == scenarios.CARD_KEYS
        assert [set(r) for r in card.json()["roles"]] == [scenarios.CARD_ROLE_KEYS] * 3
        assert (card.json()["version"], card.json()["status"], card.json()["name"]) == (
            2,
            "published",
            "The Harrow Inheritance",
        )
        assert [r["public_label"] for r in card.json()["roles"]] == [
            "Mother",
            "The younger brother",
            "The eldest",
        ]
        full = await scenarios.read(client, sid, projection="full")
        assert set(full.json()) == scenarios.FULL_KEYS
        for phrase in scenarios.PRIVATE_PHRASES:
            assert phrase not in card.text, phrase
            assert phrase in full.text, phrase

        archived = await client.post(f"{base}/{sid}/archive", headers=scenarios.WRITE)
        assert archived.json()["status"] == "archived"
        for projection in ("card", "full"):
            found = await scenarios.read(client, sid, projection=projection)
            assert (found.status_code, found.json()["status"]) == (200, "archived")
            for phrase in scenarios.PRIVATE_PHRASES:
                assert (phrase in found.text) == (projection == "full"), (projection, phrase)


class _Untagged(BaseModel):
    """A fixture document with one field of each kind of mistake."""

    model_config = ConfigDict(extra="forbid")

    fine: Annotated[str, FieldClass.public_card] = "x"
    missing: str = "x"
    twice: Annotated[str, FieldClass.public_card, FieldClass.director] = "x"
    no_use: Annotated[int, FieldClass.configuration] = 1
    stray_use: Annotated[int, FieldClass.style, Use.policy] = 1


@pytest.mark.part("M3", "FR-SCN-2")
def test_c_15__scenario_field_classes() -> None:
    """Every field of the scenario document carries its class (and, for ``configuration``, its
    use), checked by walking the model, groups and list items included; the map genesis and the
    validation report read names every field, with all eight classes and all three uses in use.

    Complement asserted: the same walk reports a fixture's untagged field, a field with two
    classes, a ``configuration`` field without a use and a use on another class, so an
    untagged field fails the build."""
    assert classification_problems(ScenarioDraft) == []
    classes = scenario_schema_classes()
    assert {found for found, _ in classes.values()} == set(FieldClass)
    assert {use for _, use in classes.values() if use is not None} == set(Use)
    assert {use for found, use in classes.values() if found is FieldClass.configuration} == set(Use)
    for path, expected in CLASSES.items():
        assert classes[path] == expected, path

    assert classification_problems(_Untagged) == [
        "missing: untagged",
        "twice: more than one class",
        "no_use: a configuration field needs exactly one use",
        "stray_use: only a configuration field has a use",
    ]


CLASSES: dict[str, tuple[FieldClass, Use | None]] = {
    "public_card.name": (FieldClass.public_card, None),
    "public_card.media.videos[].caption": (FieldClass.public_card, None),
    "metadata.labels": (FieldClass.public_card, None),
    "world.description": (FieldClass.knowledge_seed, None),
    "world.entities[].aliases": (FieldClass.knowledge_seed, None),
    "cast.roles[].public_label": (FieldClass.public_card, None),
    "cast.roles[].setup_description": (FieldClass.public_card, None),
    "cast.roles[].fill": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].eligibility.age_range": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].seeded_memories[].audience": (FieldClass.knowledge_seed, None),
    "cast.roles[].relationships[].to_role": (FieldClass.knowledge_seed, None),
    "cast.roles[].name_overrides.aliases": (FieldClass.knowledge_seed, None),
    "cast.roles[].starting_location": (FieldClass.knowledge_seed, None),
    "cast.roles[].state_initial_values": (FieldClass.starting_state, None),
    "cast.roles[].participation": (FieldClass.configuration, Use.story_setting),
    "cast.max_characters_at_start": (FieldClass.configuration, Use.creation_only),
    "starting_state.opening.narration": (FieldClass.starting_state, None),
    "starting_state.opening.roles_present": (FieldClass.starting_state, None),
    "starting_state.predicates": (FieldClass.story_rule, None),
    "starting_state.scheduled_events": (FieldClass.knowledge_seed, None),
    "drivers.director_guidance": (FieldClass.director, None),
    "drivers.planned_reveals[].truth.text": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].allowed_roles": (FieldClass.director, None),
    "story_settings_defaults.turn_concurrency": (FieldClass.configuration, Use.story_setting),
    "story_settings_defaults.policies[].rule": (FieldClass.configuration, Use.policy),
    "story_settings_defaults.required_providers": (FieldClass.configuration, Use.policy),
    "recommendation.engine_profile_id": (FieldClass.recommendation, None),
    "visual_style.negative_prompt": (FieldClass.style, None),
    "creation_rules.overridable": (FieldClass.configuration, Use.creation_only),
}
"""Where FR-SCN-11 and docs/02 §4.10 put a field, for a sample of every class and use."""
