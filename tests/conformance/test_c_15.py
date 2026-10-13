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
  its class, and that each is at the class (and use) ``CLASSES`` decides for it (FR-SCN-2). The
  details are in ``tests/integration/test_scenario_catalog.py``; the roles and the starting-cast
  limit are U-26's (``tests/test_u_26.py``).
- #110's part, ``test_c_15__listing_status``: the listing status (FR-CAT-11) set, refused,
  reconfigured and recorded, the lists and search that filter by it (FR-API-6), and that it
  gates nothing (FR-AUTH-8). It runs on its own scratch database, because it changes the
  deployment's values. The details are in ``tests/integration/test_catalog_search.py``; the
  filters' behavioural matrix is I-21's (``tests/integration/test_i_21.py``). Using a
  ``private`` entry to create a story is #119's ``test_i_22__creation``.
- #111's part, ``test_c_15__validate``: ``POST /catalog/scenarios/{id}/validate`` reports errors
  and warnings without side effects (FR-API-6), in the report a publish answers. The checks
  themselves are I-20's (``tests/integration/test_i_20.py``) and U-26's; the details are in
  ``tests/integration/test_scenario_validation.py``.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx2 as httpx
import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
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
from epistrel.core.ids import new_ulid
from tests import scenario_support as scenarios
from tests.catalog_support import (
    BASE,
    CARD_KEYS,
    LIST_EXTRA_KEYS,
    READ,
    VERSION_KEYS,
    WRITE,
    character,
    create,
    ids,
    publish,
    read,
    save,
    scratch_catalog,
    set_listing_status,
    version_rows,
)
from tests.event_support import execute_in_tx, rows, sqlstate
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
from tests.validation_support import counts, validate

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
    """A scenario that fills every group starts as an editable draft that holds the document as
    sent: every value it sets, each role's fields and each seed's holder, audience and
    disclosure among them, and every field it leaves out at its default, with an id the server
    assigned to each seed. A save based on an older draft revision is 409 ``draft_stale`` and
    changes nothing. It is published as version 1, edited (the edit shows only in a draft read
    until it is published) and published as version 2; version 1 reads unchanged afterwards
    and its row cannot be updated; the version list names both. A read with no projection is
    the public card of the ``published`` scenario: exactly the card's keys (the envelope, name,
    public description, media, labels, content rating, and each role's id, public label, setup
    description, kind and fill, shown as written for an open, a locked and a default role and
    the player's), and none of the phrases planted in the world description, a role's seeded
    memory and the director guidance; ``full`` returns version 2's whole document as sent, the
    edit included. An archived scenario still reads in both projections.

    Complement asserted: each planted phrase is present in ``full``, so its absence from every
    card is the projection's doing."""
    base = scenarios.BASE
    async with rest(settings, engine) as client:
        sent = scenarios.complete_with(*await scenarios.cast_characters(client))
        created = await scenarios.create(client, sent)
        sid = created["id"]
        assert (created["status"], created["draft_revision"], created["version"]) == (
            "draft",
            1,
            None,
        )
        assert scenarios.document_of(created) == scenarios.stored_form(sent)
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
        assert [set(r) for r in card.json()["roles"]] == [scenarios.CARD_ROLE_KEYS] * 4
        assert (card.json()["version"], card.json()["status"], card.json()["name"]) == (
            2,
            "published",
            "The Harrow Inheritance",
        )
        assert [r["public_label"] for r in card.json()["roles"]] == [
            "Mother",
            "The younger brother",
            "Aunt Ivy",
            "The eldest",
        ]
        assert card.json()["roles"] == scenarios.CARD_ROLES
        full = await scenarios.read(client, sid, projection="full")
        assert set(full.json()) == scenarios.FULL_KEYS
        sent["public_card"]["public_description"] = "Two."
        assert scenarios.document_of(full.json()) == scenarios.stored_form(sent)
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
    use), checked by walking the model, groups and list items included. The map genesis and the
    validation report read is exactly ``CLASSES``: every field, each at the class and use the
    requirements give it, so no field changes class or use, and none is added or dropped,
    without this test failing. All eight classes and all three uses are in use.

    Complement asserted: the same walk reports a fixture's untagged field, a field with two
    classes, a ``configuration`` field without a use and a use on another class, so an
    untagged field fails the build."""
    assert classification_problems(ScenarioDraft) == []
    classes = scenario_schema_classes()
    assert {found for found, _ in classes.values()} == set(FieldClass)
    assert {use for _, use in classes.values() if use is not None} == set(Use)
    assert {use for found, use in classes.values() if found is FieldClass.configuration} == set(Use)
    assert classes == CLASSES

    assert classification_problems(_Untagged) == [
        "missing: untagged",
        "twice: more than one class",
        "no_use: a configuration field needs exactly one use",
        "stray_use: only a configuration field has a use",
    ]


CLASSES: dict[str, tuple[FieldClass, Use | None]] = {
    "schema_version": (FieldClass.configuration, Use.creation_only),
    "public_card.name": (FieldClass.public_card, None),
    "public_card.public_description": (FieldClass.public_card, None),
    "public_card.media.cover": (FieldClass.public_card, None),
    "public_card.media.gallery": (FieldClass.public_card, None),
    "public_card.media.videos[].media_id": (FieldClass.public_card, None),
    "public_card.media.videos[].caption": (FieldClass.public_card, None),
    "metadata.labels": (FieldClass.public_card, None),
    "metadata.content_rating_id": (FieldClass.public_card, None),
    "world.description": (FieldClass.knowledge_seed, None),
    "world.locations[].id": (FieldClass.knowledge_seed, None),
    "world.locations[].name": (FieldClass.knowledge_seed, None),
    "world.locations[].description": (FieldClass.knowledge_seed, None),
    "world.entities[].id": (FieldClass.knowledge_seed, None),
    "world.entities[].name": (FieldClass.knowledge_seed, None),
    "world.entities[].aliases": (FieldClass.knowledge_seed, None),
    "world.entities[].description": (FieldClass.knowledge_seed, None),
    "cast.roles[].id": (FieldClass.public_card, None),
    "cast.roles[].public_label": (FieldClass.public_card, None),
    "cast.roles[].setup_description": (FieldClass.public_card, None),
    "cast.roles[].kind": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].required": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].fill": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].character.catalog_id": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].character.version": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].eligibility.labels": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].eligibility.content_ratings": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].eligibility.age_range": (FieldClass.configuration, Use.creation_only),
    "cast.roles[].seeded_memories[].id": (FieldClass.knowledge_seed, None),
    "cast.roles[].seeded_memories[].text": (FieldClass.knowledge_seed, None),
    "cast.roles[].seeded_memories[].holder": (FieldClass.knowledge_seed, None),
    "cast.roles[].seeded_memories[].audience": (FieldClass.knowledge_seed, None),
    "cast.roles[].seeded_memories[].disclosure": (FieldClass.knowledge_seed, None),
    "cast.roles[].seeded_memories[].kind": (FieldClass.knowledge_seed, None),
    "cast.roles[].seeded_memories[].valid_time": (FieldClass.knowledge_seed, None),
    "cast.roles[].relationships[].to_role": (FieldClass.knowledge_seed, None),
    "cast.roles[].relationships[].name": (FieldClass.knowledge_seed, None),
    "cast.roles[].state_initial_values": (FieldClass.starting_state, None),
    "cast.roles[].starting_location": (FieldClass.knowledge_seed, None),
    "cast.roles[].participation": (FieldClass.configuration, Use.story_setting),
    "cast.roles[].name_overrides.canonical": (FieldClass.knowledge_seed, None),
    "cast.roles[].name_overrides.aliases": (FieldClass.knowledge_seed, None),
    "cast.max_characters_at_start": (FieldClass.configuration, Use.creation_only),
    "starting_state.seeded_memories[].id": (FieldClass.knowledge_seed, None),
    "starting_state.seeded_memories[].text": (FieldClass.knowledge_seed, None),
    "starting_state.seeded_memories[].holder": (FieldClass.knowledge_seed, None),
    "starting_state.seeded_memories[].audience": (FieldClass.knowledge_seed, None),
    "starting_state.seeded_memories[].disclosure": (FieldClass.knowledge_seed, None),
    "starting_state.seeded_memories[].kind": (FieldClass.knowledge_seed, None),
    "starting_state.seeded_memories[].valid_time": (FieldClass.knowledge_seed, None),
    "starting_state.states": (FieldClass.starting_state, None),
    "starting_state.clock": (FieldClass.starting_state, None),
    "starting_state.scheduled_events": (FieldClass.knowledge_seed, None),
    "starting_state.predicates": (FieldClass.story_rule, None),
    "starting_state.closed_sets": (FieldClass.story_rule, None),
    "starting_state.rules": (FieldClass.story_rule, None),
    "starting_state.opening.narration": (FieldClass.starting_state, None),
    "starting_state.opening.starting_location": (FieldClass.knowledge_seed, None),
    "starting_state.opening.roles_present": (FieldClass.starting_state, None),
    "drivers.plot_hooks": (FieldClass.director, None),
    "drivers.objectives": (FieldClass.director, None),
    "drivers.director_guidance": (FieldClass.director, None),
    "drivers.planned_reveals[].truth.id": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].truth.text": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].truth.holder": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].truth.audience": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].truth.disclosure": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].truth.kind": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].truth.valid_time": (FieldClass.knowledge_seed, None),
    "drivers.planned_reveals[].allowed_roles": (FieldClass.director, None),
    "drivers.planned_reveals[].conditions": (FieldClass.director, None),
    "drivers.milestones": (FieldClass.director, None),
    "drivers.endings": (FieldClass.director, None),
    "story_settings_defaults.interaction_mode": (FieldClass.configuration, Use.story_setting),
    "story_settings_defaults.turn_concurrency": (FieldClass.configuration, Use.story_setting),
    "story_settings_defaults.player_authority": (FieldClass.configuration, Use.story_setting),
    "story_settings_defaults.default_output_viewpoint": (
        FieldClass.configuration,
        Use.story_setting,
    ),
    "story_settings_defaults.ambiguous_retcon_timing": (
        FieldClass.configuration,
        Use.story_setting,
    ),
    "story_settings_defaults.backfill_conflict_policy": (
        FieldClass.configuration,
        Use.story_setting,
    ),
    "story_settings_defaults.audit_retention_days": (FieldClass.configuration, Use.story_setting),
    "story_settings_defaults.policies[].name": (FieldClass.configuration, Use.policy),
    "story_settings_defaults.policies[].applies_to": (FieldClass.configuration, Use.policy),
    "story_settings_defaults.policies[].rule": (FieldClass.configuration, Use.policy),
    "story_settings_defaults.policies[].age_condition": (FieldClass.configuration, Use.policy),
    "story_settings_defaults.policies[].message": (FieldClass.configuration, Use.policy),
    "story_settings_defaults.required_providers": (FieldClass.configuration, Use.policy),
    "recommendation.engine_profile_id": (FieldClass.recommendation, None),
    "recommendation.capabilities": (FieldClass.recommendation, None),
    "visual_style.prompt": (FieldClass.style, None),
    "visual_style.negative_prompt": (FieldClass.style, None),
    "creation_rules.overridable": (FieldClass.configuration, Use.creation_only),
    "creation_rules.bounds": (FieldClass.configuration, Use.creation_only),
}
"""Where FR-SCN-11 and docs/02 §4.10 put each field of the scenario document: every leaf, by
the dotted path ``scenario_schema_classes()`` names it with, at its class and, for
``configuration``, its use. The part asserts that map equals this one, so a field retagged,
given another use, added or removed fails the build until this map is changed with it: a
class is decided here, never picked up from the model."""


# -- listing status (#110) ---------------------------------------------------------------------

CHARACTERS = BASE
SCENARIOS = scenarios.BASE
DEFAULT_VALUES = ["private", "in_review", "public"]


async def _status_events(db: AsyncEngine, entry_id: str) -> list[tuple[Any, ...]]:
    """The entry's listing-status events: ``(old, new, acting principal)``, oldest first."""
    found = await rows(
        db,
        "SELECT old, new, acting_principal, at FROM ownership_events "
        "WHERE entity_id = :e AND kind = 'listing_status' ORDER BY at, id",
        e=entry_id,
    )
    assert all(row.at is not None for row in found)
    return [(row.old, row.new, row.acting_principal) for row in found]


async def _values(client: httpx.AsyncClient, values: list[str]) -> httpx.Response:
    return await client.put(
        "/admin/settings", json={"listing_status_values": values}, headers=WRITE
    )


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-11; FR-AUTH-8; FR-API-6")
async def test_c_15__listing_status(pg_url: str) -> None:
    """A new catalog character and a new scenario start at the first of the deployment's listing
    status values, ``private``. ``PUT …/listing-status`` accepts ``public`` and ``in_review`` and
    refuses ``bogus`` (422 ``invalid_listing_status`` naming the allowed values, nothing
    recorded); each change is recorded with the acting principal and its time. The lists filter
    by the value, and an unfiltered list holds every value and every owner. A ``private`` entry
    reads in both projections, and lists, for a principal that does not own it. A companion
    scenario's value changes with its character's and is refused on its own; the companion
    still reads directly, and a scenario list shows it only with ``include_companions``.
    Reconfigured, the
    deployment accepts its own values: a value an entry still holds cannot be removed (409
    ``listing_status_in_use``); once no entry holds the defaults they can be removed, after
    which a default is refused, a configured value accepted, and a new entry starts at the new
    first value.

    Complements asserted: the refused value changed nothing; the companion refused alone kept
    its character's value; the filter for a removed value is refused, not silently empty."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        mara = await create(client, character("Mara Venn", owner="p-a"))
        assert mara["listing_status"] == "private"
        drafted = await scenarios.create(client, scenarios.scenario("Sunday Dinner", owner="p-a"))
        assert drafted["listing_status"] == "private"
        for base, entry in ((CHARACTERS, mara), (SCENARIOS, drafted)):
            done = await client.post(
                f"{base}/{entry['id']}/publish", json={"draft_revision": 1}, headers=WRITE
            )
            assert done.status_code == 201, done.text
        ivo = await create(client, character("Ivo Brand", owner="p-b"))
        await publish(client, ivo["id"], 1)

        made_public = await set_listing_status(
            client, CHARACTERS, mara["id"], "public", principal="p-editor"
        )
        assert made_public.status_code == 200, made_public.text
        assert made_public.json() == {
            "id": mara["id"],
            "listing_status": "public",
            "config_revision": made_public.json()["config_revision"],
            "idempotency_key": made_public.headers["Idempotency-Key"],
        }
        refused = await set_listing_status(client, CHARACTERS, mara["id"], "bogus")
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert (error["code"], error["details"]["value"]) == ("invalid_listing_status", "bogus")
        assert error["details"]["allowed_values"] == DEFAULT_VALUES
        in_review = await set_listing_status(
            client, SCENARIOS, drafted["id"], "in_review", principal="p-reviewer"
        )
        assert in_review.status_code == 200, in_review.text
        assert await _status_events(db, mara["id"]) == [("private", "public", "p-editor")]
        assert await _status_events(db, drafted["id"]) == [("private", "in_review", "p-reviewer")]
        assert (await read(client, mara["id"])).json()["listing_status"] == "public"

        # The lists filter by the value, and only when asked: every value, every owner otherwise.
        assert await ids(client, CHARACTERS, listing_status="public") == [mara["id"]]
        assert await ids(client, CHARACTERS, listing_status="private") == [ivo["id"]]
        assert await ids(client, SCENARIOS, listing_status="in_review") == [drafted["id"]]
        assert await ids(client, SCENARIOS, listing_status="public") == []
        everyone = await ids(client, CHARACTERS)
        assert sorted(everyone) == sorted([mara["id"], ivo["id"]])

        # Nothing gates: B's `private` entry reads in both projections, and lists, for another.
        stranger = {**READ, "X-Epistrel-Principal": "p-stranger"}
        for projection in ("card", "full"):
            found = await client.get(
                f"{CHARACTERS}/{ivo['id']}", params={"projection": projection}, headers=stranger
            )
            assert (found.status_code, found.json()["listing_status"]) == (200, "private")
        assert "Ivo Brand is patient" in found.text  # the definition, in `full`
        assert await ids(client, CHARACTERS, principal="p-stranger") == everyone
        listed = await client.get(CHARACTERS, headers=stranger)
        cards = {item["id"]: item for item in listed.json()["items"]}
        assert set(cards[ivo["id"]]) == CARD_KEYS | LIST_EXTRA_KEYS
        assert (cards[ivo["id"]]["owner_principal"], cards[ivo["id"]]["listing_status"]) == (
            "p-b",
            "private",
        )

        # A companion follows its character (#116 makes companions; here SQL does), and lists
        # only when asked for.
        companion = await scenarios.create(client, scenarios.scenario("Gull Rock"))
        await scenarios.publish(client, companion["id"], 1)
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE catalog.scenario_catalog SET companion_of = :c, "
                    "listing_status = 'public' WHERE id = :s"
                ),
                {"c": mara["id"], "s": companion["id"]},
            )
        moved = await set_listing_status(
            client, CHARACTERS, mara["id"], "in_review", principal="p-editor"
        )
        assert moved.status_code == 200, moved.text
        shown = await scenarios.read(client, companion["id"])
        assert shown.json()["listing_status"] == "in_review"
        assert await _status_events(db, companion["id"]) == [("public", "in_review", "p-editor")]
        alone = await set_listing_status(client, SCENARIOS, companion["id"], "public")
        assert alone.status_code == 422, alone.text
        assert alone.json()["error"]["code"] == "companion_follows_character"
        shown = await scenarios.read(client, companion["id"])
        assert (shown.json()["listing_status"], shown.json()["companion_of"]) == (
            "in_review",
            mara["id"],
        )
        assert await ids(client, SCENARIOS) == [drafted["id"]]
        assert await ids(client, SCENARIOS, include_companions=True) == [
            companion["id"],
            drafted["id"],
        ]

        # The deployment's own values: never one an entry still holds removed.
        custom = ["pending", "live"]
        held = await _values(client, custom)
        assert held.status_code == 409, held.text
        assert held.json()["error"]["code"] == "listing_status_in_use"
        assert held.json()["error"]["details"]["value"] == "private"
        assert held.json()["error"]["details"]["count"] == 1
        assert (await _values(client, [*DEFAULT_VALUES, *custom])).status_code == 200
        for base, entry_id, value in (
            (CHARACTERS, ivo["id"], "pending"),
            (CHARACTERS, mara["id"], "live"),
            (SCENARIOS, drafted["id"], "live"),
        ):
            changed = await set_listing_status(client, base, entry_id, value)
            assert changed.status_code == 200, changed.text
        settled = await _values(client, custom)
        assert settled.status_code == 200, settled.text
        assert settled.json()["listing_status_values"] == custom
        default = await set_listing_status(client, CHARACTERS, ivo["id"], "public")
        assert default.status_code == 422, default.text
        assert default.json()["error"]["details"]["allowed_values"] == custom
        accepted = await set_listing_status(client, CHARACTERS, ivo["id"], "live")
        assert accepted.json()["listing_status"] == "live"
        fresh = await create(client, character("Wren Hale"))
        assert fresh["listing_status"] == "pending"
        gone = await client.get(CHARACTERS, params={"listing_status": "public"}, headers=READ)
        assert (gone.status_code, gone.json()["error"]["code"]) == (422, "invalid_listing_status")
        assert await ids(client, CHARACTERS, listing_status="live") == [ivo["id"], mara["id"]]


@pytest.mark.integration
@pytest.mark.part("M3", "FR-API-6")
async def test_c_15__validate(settings: Settings, engine: AsyncEngine) -> None:
    """``POST /catalog/scenarios/{id}/validate`` reports errors and warnings without side
    effects. On a draft whose ``locked`` role names no catalog character and whose ``open`` role
    text asserts personality it answers 200 ``{valid: false, report}``: the error
    ``character_missing`` (``rejected``) at the role's character, the warning
    ``personality_in_role`` (``ignored``) quoting the sentence with the rule ``personality:
    character over scenario``, and the checks this deployment cannot run listed in
    ``unavailable``. It writes nothing (no operation, version, idempotency key, audit row or
    configuration change) and answers no ``Idempotency-Key``. A publish of that draft is 422
    ``publish_invalid`` carrying the same report. A ``draft_revision`` the draft has moved on
    from is 409 ``draft_stale``; naming it with ``version`` is 422; an unknown version or
    scenario is 404. Once the role names a published character, the draft validates (``valid``
    true, the warning kept), publishes (201, the warning on the answer), and ``version=1``
    validates the published version; an archived scenario still validates.

    Complement asserted: the same counts are found after every dry run as before the first."""
    async with rest(settings, engine) as client:
        ghost = new_ulid()
        body = scenarios.scenario(
            "Checked",
            cast={
                "roles": [
                    scenarios.role("mother", setup_description="She is a habitual liar."),
                    scenarios.role("brother", fill="locked", character={"catalog_id": ghost}),
                    scenarios.role("player", kind="player"),
                ]
            },
        )
        created = await scenarios.create(client, body)
        sid = created["id"]
        before = await counts(engine)
        checked = await validate(client, sid)
        assert checked.status_code == 200, checked.text
        assert "Idempotency-Key" not in checked.headers
        assert checked.json()["valid"] is False
        report = checked.json()["report"]
        (missing,) = report["errors"]
        assert (missing["code"], missing["outcome"], missing["source"]) == (
            "character_missing",
            "rejected",
            ghost,
        )
        assert missing["location"] == {
            "entry": sid,
            "version": "draft",
            "role": "brother",
            "field": "cast.roles[1].character",
            "seed_id": None,
        }
        (personality,) = report["warnings"]
        assert (personality["code"], personality["outcome"], personality["text"]) == (
            "personality_in_role",
            "ignored",
            "She is a habitual liar.",
        )
        assert (personality["rule"], personality["source"]) == (
            "personality: character over scenario",
            None,
        )
        assert set(report["unavailable"]) >= set(scenarios.M3_UNAVAILABLE)
        assert await counts(engine) == before

        refused = await scenarios.publish(client, sid, created["draft_revision"])
        assert (refused.status_code, scenarios.error_of(refused)["code"]) == (
            422,
            "publish_invalid",
        )
        assert scenarios.error_of(refused)["details"]["report"] == report
        assert await scenarios.version_rows(engine, sid) == []

        stale = await validate(client, sid, {"draft_revision": 5})
        assert (stale.status_code, scenarios.error_of(stale)["code"]) == (409, "draft_stale")
        assert scenarios.error_of(stale)["details"]["current_draft_revision"] == 1
        both = await validate(client, sid, {"draft_revision": 1}, version=1)
        assert (both.status_code, scenarios.error_of(both)["details"]["field"]) == (
            422,
            "query.version",
        )
        assert (await validate(client, sid, version=1)).status_code == 404
        assert (await validate(client, new_ulid())).status_code == 404
        assert await counts(engine) == before

        brother = await scenarios.published_character(client, "Tom Harrow")
        cast = body["cast"]
        cast["roles"][1]["character"] = {"catalog_id": brother}
        fixed = await scenarios.save(client, sid, 1, cast=cast)
        assert fixed.status_code == 200, fixed.text
        valid = await validate(client, sid, {"draft_revision": 2})
        assert valid.json()["valid"] is True, valid.text
        assert [w["code"] for w in valid.json()["report"]["warnings"]] == ["personality_in_role"]
        published = await scenarios.publish(client, sid, 2)
        assert published.status_code == 201, published.text
        assert published.json()["report"] == valid.json()["report"]
        version = await validate(client, sid, version=1)
        assert version.json()["valid"] is True, version.text
        assert {w["location"]["version"] for w in version.json()["report"]["warnings"]} == {1}
        archived = await client.post(f"{scenarios.BASE}/{sid}/archive", headers=WRITE)
        assert archived.status_code == 200, archived.text
        assert (await validate(client, sid)).json()["valid"] is True
