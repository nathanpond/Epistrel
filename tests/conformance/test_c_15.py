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
- #115's part, ``test_c_15__character_content``: a catalog character's public card, private
  definition, metadata and visual identity (reference images with their audience), and the
  publish checks over them (FR-CAT-2,4). The details are in
  ``tests/integration/test_character_content.py``.
- #122's part, ``test_c_15__story_creation``: a story's creation report under ``card`` (the
  story, its cast as cards, counts by field class and capability, the unavailable checks) and
  ``full`` (every entry, deferred item and inactive setting), and public descriptions and labels
  seeded nowhere (FR-SCN-10). The materialisation itself is I-27's
  (``tests/integration/test_i_27.py``).
- #128's parts (FR-API-6 as a whole): ``test_c_15__api_surface``, the route table
  (``tests/fixtures/api/fr_api_6_routes.json``, ``tests/api_surface_support.py``) against the
  full app in both directions, with C-03's parity list and C-01's schema check over the same
  app; ``test_c_15__replay``, every mutating route's replay under its ``Idempotency-Key`` on
  both surfaces; and ``test_c_15__operation_stages``, ``GET /operations/{id}`` for the four
  long-running kinds. The replay and stage helpers are in ``tests/api_surface_support.py``
  and ``tests/replay_support.py``.
"""

from __future__ import annotations

import copy
import json
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.api import create_app
from epistrel.api.routes import media
from epistrel.config import Settings
from epistrel.core.catalog.characters import CharacterCreate
from epistrel.core.catalog.scenario_models import (
    FieldClass,
    ScenarioDraft,
    Use,
    classification_problems,
    scenario_schema_classes,
)
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.ids import new_ulid
from epistrel.core.operation_view import ERASURE_STAGES, GENESIS_STAGES
from epistrel.core.seeds.ratings import EXPLICIT_ID, TEEN_ID
from epistrel.engine import Engine
from tests import genesis_support as g
from tests import lite_genesis_support as lg
from tests import scenario_support as scenarios
from tests.api_surface_support import load_table, served_routes, surface_problems
from tests.auth_support import auth_settings
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
    read_companion,
    save,
    scratch_catalog,
    set_listing_status,
    version_rows,
)
from tests.character_support import (
    DISPOSITIONS,
    HIDDEN,
    PRIVATE_FIELDS,
    SEEN,
    complete,
    require_primary_image,
    visual_identity,
)
from tests.character_support import PLANTED as PRIVATE_PHRASES
from tests.conformance import test_c_03 as c03
from tests.conformance.conftest import migrated_database
from tests.conformance.test_c_01 import schema_problems
from tests.erasure_support import linda
from tests.erasure_support import request as erasure_request
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
    uploaded,
)
from tests.model_support import config_key
from tests.rating_change_support import change_rating, guard_rating, rating_db, run_jobs
from tests.replay_support import (
    grown,
    mutating_routes,
    replay_library,
    replay_rest,
    table_counts,
)
from tests.respond_support import FIRST, PLAYER, new_story
from tests.story_support import rest, story_body
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


def _report(response: httpx.Response) -> list[tuple[str, str]]:
    """``(code, field)`` of each error of a refused publish's report."""
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "publish_invalid", response.text
    return [(e["code"], e["location"]["field"]) for e in error["details"]["report"]["errors"]]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.mark.integration
@pytest.mark.part("M3", "FR-CAT-2,4")
async def test_c_15__character_content(pg_url: str) -> None:
    """A catalog character holds a public card, a private definition (its true age, the five
    disposition axes and the rest), metadata and a visual identity whose reference images each
    declare who may see them. A publish collects every error in its report and writes nothing:
    without a true age it is 422 ``publish_invalid`` naming ``definition.true_age``, without a
    rating it names ``metadata.content_rating_id``, and a stored reference image without its
    audience is named by its index (a save of one is refused at that index). With the
    deployment's ``require_primary_image`` on, a character without a primary image fails; with
    the setting off (the default) the same character publishes. Each image's generation metadata
    (adapter, model, prompts, seed, parameters, reference ids) reads back in ``full`` exactly as
    the upload stored it; the card shows each image as its id and alt text only and never a
    definition field, while ``full`` holds every one. The visual identity is versioned with the
    character: after an edit the new version holds the new identity and the first keeps its
    own. The labels and the public description are never seeded into a story (#122's prompt
    test asserts it).

    Complements asserted: each refused character publishes once the field is restored, and every
    phrase missing from the card is present in ``full``."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        assets = [
            await uploaded(
                client,
                png(nonce=new_ulid()),
                alt_text=f"Mara, view {n}",
                caption=f"Gull Rock, plate {n}",
                generation_metadata=GENERATION,
            )
            for n in range(4)
        ]
        first, second, seen, hidden = (asset["id"] for asset in assets)
        identity = visual_identity((seen, SEEN), (hidden, HIDDEN))
        body = complete(
            "Mara Venn",
            images={"primary": first, "gallery": [second]},
            definition_changes=PRIVATE_FIELDS,
            metadata={"aliases": ["The Keeper"], "labels": ["Quiet", "stern"]},
            visual_identity=identity,
        )
        created = await create(client, body)
        cid, revision = created["id"], created["draft_revision"]

        # No true age, then no rating: each refused with its field named, nothing written.
        for patch, expected in (
            ({"definition": {"true_age": None}}, ("true_age_missing", "definition.true_age")),
            (
                {"metadata": {"content_rating_id": None}},
                ("content_rating_missing", "metadata.content_rating_id"),
            ),
        ):
            removed = await save(client, cid, revision, **patch)
            assert removed.status_code == 200, removed.text
            revision = removed.json()["draft_revision"]
            assert _report(await publish(client, cid, revision)) == [expected]
            assert await version_rows(db, cid) == []
            restored = body["definition"] if "definition" in patch else body["metadata"]
            group = next(iter(patch))
            back = await save(client, cid, revision, **{group: restored})
            assert back.status_code == 200, back.text
            revision = back.json()["draft_revision"]

        # A reference image without an audience: a save is refused at its index; one stored
        # some other way is refused at publish, at the same place.
        missing = copy.deepcopy(identity)
        del missing["reference_images"][1]["audience"]
        refused = await save(client, cid, revision, visual_identity=missing)
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["field"] == (
            "body.visual_identity.reference_images[1].audience"
        )
        await execute_in_tx(
            db,
            (
                "UPDATE catalog.character_catalog SET draft = draft #- "
                "'{visual_identity,reference_images,1,audience}' WHERE id = :c",
                {"c": cid},
            ),
        )
        assert _report(await publish(client, cid, revision)) == [
            ("reference_image_missing_audience", "visual_identity.reference_images[1].audience")
        ]
        back = await save(client, cid, revision, visual_identity=identity)
        assert back.status_code == 200, back.text
        published = await publish(client, cid, back.json()["draft_revision"])
        assert (published.status_code, published.json()["version"]) == (201, 1), published.text

        # Every image's generation metadata, as the upload stored it, in ``full``.
        card = await read(client, cid)
        full = await read(client, cid, projection="full")
        shown = full.json()
        pictures = [
            shown["images"]["primary"],
            *shown["images"]["gallery"],
            *shown["visual_identity"]["reference_images"],
        ]
        assert [p["media_id"] for p in pictures] == [first, second, seen, hidden]
        for item in pictures:
            stored = (await client.get(f"/media/{item['media_id']}", headers=MEDIA_READ)).json()
            assert item["generation_metadata"] == stored["generation_metadata"] == GENERATION
            assert _canonical(item["generation_metadata"]) == _canonical(GENERATION)
            assert (item["alt_text"], item["caption"], item["mime_type"]) == (
                stored["alt_text"],
                stored["caption"],
                "image/png",
            )
        assert [p["audience"] for p in shown["visual_identity"]["reference_images"]] == [
            SEEN,
            HIDDEN,
        ]
        kept = {k: v for k, v in identity.items() if k != "reference_images"}
        assert {k: shown["visual_identity"][k] for k in kept} == kept

        # The card: ids and alt text, labels and rating, never a definition field.
        assert set(card.json()) == CARD_KEYS
        assert card.json()["images"] == {
            "primary": {"media_id": first, "alt_text": "Mara, view 0"},
            "gallery": [{"media_id": second, "alt_text": "Mara, view 1"}],
        }
        assert card.json()["labels"] == ["quiet", "stern"]
        assert "definition" not in card.json()
        assert "visual_identity" not in card.json()
        for phrase in PRIVATE_PHRASES:
            assert phrase not in card.text, phrase
            assert phrase in full.text, phrase
        assert shown["aliases"] == ["The Keeper"]
        assert shown["definition"]["true_age"] == PRIVATE_FIELDS["true_age"]
        assert {
            axis: value["setting"] for axis, value in shown["definition"]["dispositions"].items()
        } == {axis: value["setting"] for axis, value in DISPOSITIONS.items()}

        # Versioned with the character: version 1 keeps its identity after an edit.
        edited = await save(
            client,
            cid,
            published.json()["draft_revision"],
            visual_identity={"seed": 7, "reference_images": [{"media_id": seen, "audience": SEEN}]},
        )
        assert edited.status_code == 200, edited.text
        second_version = await publish(client, cid, edited.json()["draft_revision"])
        assert second_version.json()["version"] == 2, second_version.text
        old = (await read(client, cid, version=1, projection="full")).json()["visual_identity"]
        new = (await read(client, cid, version=2, projection="full")).json()["visual_identity"]
        assert (old["seed"], [p["media_id"] for p in old["reference_images"]]) == (
            identity["seed"],
            [seen, hidden],
        )
        assert (new["seed"], [p["media_id"] for p in new["reference_images"]]) == (7, [seen])

        # The deployment's setting: on, a character without a primary image fails; off (the
        # default), the same draft publishes.
        plain = await create(client, complete("Ivo Brand"))
        await require_primary_image(client, True)
        assert _report(await publish(client, plain["id"], 1)) == [
            ("primary_image_required", "public_card.images.primary")
        ]
        await require_primary_image(client, False)
        done = await publish(client, plain["id"], 1)
        assert (done.status_code, done.json()["version"]) == (201, 1), done.text


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
    scenario's value (the one created with its character, #116) changes with its character's
    and is refused on its own; the companion still reads directly, and a scenario list shows it
    only with ``include_companions``.
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

        # A companion follows its character: Mara's, created with her (#116), changed with her
        # above, and lists only when asked for.
        companion = await read_companion(client, mara["id"])
        assert companion["listing_status"] == "public"
        assert await _status_events(db, companion["id"]) == [("private", "public", "p-editor")]
        moved = await set_listing_status(
            client, CHARACTERS, mara["id"], "in_review", principal="p-editor"
        )
        assert moved.status_code == 200, moved.text
        shown = await scenarios.read(client, companion["id"])
        assert shown.json()["listing_status"] == "in_review"
        assert await _status_events(db, companion["id"]) == [
            ("private", "public", "p-editor"),
            ("public", "in_review", "p-editor"),
        ]
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
            (await read_companion(client, ivo["id"]))["id"],
        ]

        # The deployment's own values: never one an entry still holds removed.
        custom = ["pending", "live"]
        held = await _values(client, custom)
        assert held.status_code == 409, held.text
        assert held.json()["error"]["code"] == "listing_status_in_use"
        assert held.json()["error"]["details"]["value"] == "private"
        assert held.json()["error"]["details"]["count"] == 2  # Ivo and his companion
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


CARD_PHRASE = "June Harrow keeps the light on Gull Rock."
"""The catalog character's public description (``tests.character_support.complete``'s)."""

SEEDED_TABLES = (
    "characters",
    "events",
    "transcript_messages",
    "genesis_deferred",
    "policies",
    "story_texts",
    "story_heads",
)
"""Where a story's genesis seeds content; the snapshots (``stories.scenario_snapshot``,
``character_snapshots``) copy the published versions whole (FR-SCN-9), and the creation's own
answer (its stored report) is not story content."""


async def _story_text(engine: AsyncEngine, story_id: str) -> str:
    """Every seeded row of the story, and its ``stories`` row without the snapshot, as text."""
    found: list[str] = []
    for table in SEEDED_TABLES:
        found += [
            str(r[0])
            for r in await rows(
                engine,
                f'SELECT t::text FROM "{table}" t WHERE story_id = :s',  # noqa: S608
                s=story_id,
            )
        ]
    found += [
        str(r[0])
        for r in await rows(
            engine,
            "SELECT (to_jsonb(t) - 'scenario_snapshot')::text FROM stories t WHERE id = :s",
            s=story_id,
        )
    ]
    return "\n".join(found)


@pytest.mark.integration
@pytest.mark.part("M3", "FR-SCN-10")
async def test_c_15__story_creation(settings: Settings, engine: AsyncEngine) -> None:
    """Creating a story from a scenario with the default ``card`` projection answers the story,
    its cast as cards (a catalog character's public card with its role and source version; the
    player's name and kind), the counts of merge conflicts and warnings, of deferred and
    inactive items by field class and capability, and the names of the checks that could not
    run; with ``full`` the same counts and every report entry: the warnings with location and
    exact text, every deferred item and every inactive setting. Phrases planted in the
    scenario's and the catalog character's public descriptions and in a label are seeded
    nowhere in the story.

    Complement asserted: the card answer quotes none of the scenario's private phrases, and
    the full one carries them in its deferred items."""
    async with rest(settings, engine) as client:
        locked, default = await scenarios.cast_characters(client)
        mother = await g.catalog_character_id(client, "June Harrow", age=60, labels=["warm"])
        body = lg.sunday_dinner(locked, default)
        scenario_id, _ = await g.published_scenario(client, body)
        request = g.from_scenario(
            scenario_id, slots={"mother": g.catalog_slot(mother)}, ignore_warnings=True
        )
        card = (await g.create_from(client, request))["report"]
        full = (await g.create_from(client, {**request, "projection": "full"}))["report"]

        assert set(card) == {
            "story_id",
            "cast",
            "counts",
            "unavailable",
            "errors",
            "warnings",
            "deferred",
            "inactive",
        }
        assert [card[k] for k in ("errors", "warnings", "deferred", "inactive")] == [None] * 4
        assert card["counts"] == full["counts"]
        assert set(card["counts"]) == {"errors", "conflicts", "warnings", "deferred", "inactive"}
        assert set(card["counts"]["deferred"]) == {"by_class", "by_capability"}
        assert card["counts"]["deferred"]["by_capability"] == lg.counted(lg.expected_deferred(body))
        assert card["unavailable"] == list(scenarios.M3_UNAVAILABLE)
        mother_card = card["cast"][0]
        assert (mother_card["kind"], mother_card["role_id"], mother_card["name"]) == (
            "catalog",
            "mother",
            "June Harrow",
        )
        assert mother_card["source"] == {"catalog_id": mother, "version": 1}
        assert mother_card["public_description"] == CARD_PHRASE
        assert "definition" not in mother_card
        assert card["cast"][-1] == {
            "kind": "player",
            "character_id": card["cast"][-1]["character_id"],
            "role_id": "player",
            "name": "Sam",
        }
        quoted = json.dumps(card)
        for phrase in (*scenarios.PRIVATE_PHRASES, *PRIVATE_PHRASES):
            assert phrase not in quoted, phrase

        assert full["counts"]["warnings"] == len(full["warnings"])
        assert len(full["deferred"]) == sum(full["counts"]["deferred"]["by_class"].values())
        assert len(full["inactive"]) == sum(full["counts"]["inactive"]["by_class"].values())
        assert scenarios.ROLE_SECRET in json.dumps(full["deferred"])

        seeded = await _story_text(engine, full["story_id"])
        for phrase in (lg.PUBLIC_PHRASE, lg.LABEL_PHRASE, CARD_PHRASE):
            assert phrase not in seeded, phrase
        assert scenarios.ROLE_SECRET in seeded


# -- #128: FR-API-6 as a whole -------------------------------------------------------------------


@pytest.mark.part("M3", "FR-API-6")
def test_c_15__api_surface(dummy_dsn: str) -> None:
    """Every endpoint FR-API-6 names for M3 is served with its method; the ones it names for
    later milestones are not, and say which; every served route has a place in the table; and
    C-03's parity list and C-01's schema check hold over the same app.

    Complements asserted in ``test_c_15__api_surface_bites``: removing one route, or serving
    one the table does not list, is reported."""

    app = create_app(auth_settings(dummy_dsn, mode="all"))
    table = load_table()
    assert surface_problems(app, table) == []
    later = {
        (row["item"], row["milestone"]) for row in table["routes"] if row["status"] != "present"
    }
    assert {milestone for _, milestone in later} == {"M7", "M8"}
    assert c03.parity_gaps(Engine, app, c03._steps()) == []
    assert schema_problems(app.openapi(), app) == []


def test_c_15__api_surface_bites(dummy_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route-table check reports an app built minus one route, and a route the table does
    not list."""

    kept = [
        r
        for r in media.router.routes
        if not (isinstance(r, APIRoute) and r.operation_id == "get_media_url")
    ]
    assert len(kept) == len(media.router.routes) - 1
    monkeypatch.setattr(media.router, "routes", kept)
    app = create_app(auth_settings(dummy_dsn, mode="all"))
    assert ("GET", "/media/{media_id}/url") not in served_routes(app)
    assert surface_problems(app, load_table()) == [
        "not served: GET /media/{media_id}/url (media delivery — signed URLs)"
    ]
    monkeypatch.undo()

    app = create_app(auth_settings(dummy_dsn, mode="all"))

    @app.get("/catalog/characters/{character_id}/unlisted", operation_id="get_character_x")
    async def unlisted(character_id: str) -> dict[str, str]:
        return {}

    assert surface_problems(app, load_table()) == [
        "served, but in no list: GET /catalog/characters/{character_id}/unlisted"
    ]


@pytest.fixture(scope="module")
def replay_surfaces(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """C-03's two surfaces again, on databases of their own (each with its own ledger file)."""

    with (
        migrated_database(pg_url, "epistrel_c15_replay_rest") as rest_url,
        migrated_database(pg_url, "epistrel_c15_replay_lib") as lib_url,
    ):
        yield tuple(
            auth_settings(
                url,
                model_adapter="stub",
                config_key=config_key(),
                erasure_ledger_dir=tmp_path_factory.mktemp(f"c15-replay-{name}"),
            )
            for name, url in (("rest", rest_url), ("lib", lib_url))
        )


REPLAY_EXCLUDED: dict[str, str] = {}
"""Mutating routes the replay does not reach, each with the reason (none: every mutating route
is replayed; ``replay_support.REPLACED_NOT_STORED`` names the one replayed without a stored
result)."""


@pytest.mark.integration
@pytest.mark.part("M3", "FR-API-6")
async def test_c_15__replay(replay_surfaces: tuple[Settings, Settings], dummy_dsn: str) -> None:
    """Every mutating route accepts an ``Idempotency-Key``: its replay answers the stored result
    (``Idempotent-Replay: true`` over REST) and writes no row, over REST and in-process.

    Complement asserted: a mutating route that no keyed step reached fails the coverage check
    (``mutating_routes`` minus the replayed set is empty only because each was replayed), and
    the replays were real (the run replayed every one, and the flagged ``mutating=False`` routes
    are not among them)."""

    app = create_app(auth_settings(dummy_dsn, mode="all"))
    mutating = set(mutating_routes(app))
    assert {"suggest_ratings_character", "validate_scenario", "impact_preview"}.isdisjoint(mutating)
    rest_settings, lib_settings = replay_surfaces
    for settings, run in ((rest_settings, replay_rest), (lib_settings, replay_library)):
        db = make_engine(settings)
        try:
            seen = await run(c03, settings, db)
        finally:
            await db.dispose()
        assert seen.problems == [], f"{run.__name__}:\n" + "\n".join(seen.problems)
        missing = sorted(mutating - seen.replayed - set(REPLAY_EXCLUDED))
        assert missing == [], f"{run.__name__}: no replayed step for {missing} {seen.answers}"
    assert len(seen.answers) > len(mutating), "the sequence ran to its end"

    # Complement: the row check sees a write. The same draft under a new key is a second row;
    # under the first key again, nothing.

    db = make_engine(lib_settings)
    draft = CharacterCreate.model_validate({"public_card": {"name": "Complement"}})
    try:
        async with c03.surface_engine(lib_settings) as (engine, _):
            first = RequestContext(principal="p-replay", idempotency_key="complement-1")
            await engine.create_character_draft(first, draft)
            before = await table_counts(db)
            await engine.create_character_draft(first, draft)
            assert grown(before, await table_counts(db)) == {}
            second = RequestContext(principal="p-replay", idempotency_key="complement-2")
            await engine.create_character_draft(second, draft)
            assert "catalog.character_catalog" in grown(before, await table_counts(db))
    finally:
        await db.dispose()


def _stages(view: dict[str, Any]) -> list[tuple[str, str]]:
    return [(stage["name"], stage["status"]) for stage in view["stages"]]


@pytest.mark.integration
@pytest.mark.part("M3", "FR-API-6")
async def test_c_15__operation_stages(pg_url: str) -> None:
    """``GET /operations/{id}`` reports a genesis, a rating check, an offboarding and an
    erasure as ``{kind, label, status, stages [{name, status, at}]}``, with the request's
    stored result within the window; the job-backed ones follow their job.

    Complements asserted: a check's and an offboarding's stages move once their job runs (they
    are read, not fixed), a blank story's genesis skips ``snapshotted`` where a scenario's does
    not, a catalog erasure skips the story stages a story erasure runs, and an operation that
    is none of these has no label and no stages."""

    async def view(client: httpx.AsyncClient, operation_id: str) -> dict[str, Any]:
        response = await client.get(f"/operations/{operation_id}", headers=READ)
        assert response.status_code == 200, response.text
        found: dict[str, Any] = response.json()
        return found

    async def one(db: AsyncEngine, sql: str, **params: Any) -> str:
        (row,) = await rows(db, sql, **params)
        return str(row[0])

    async with rating_db(pg_url) as s:
        client = s.client

        # A blank story's genesis, and one from a scenario.
        blank = await client.post(
            "/stories",
            json=story_body(PLAYER, FIRST),
            headers={**WRITE, "Idempotency-Key": "stages-blank"},
        )
        assert blank.status_code == 201, blank.text
        genesis = await one(
            s.db,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'genesis'",
            s=blank.json()["story_id"],
        )
        found = await view(client, genesis)
        assert (found["kind"], found["label"], found["status"]) == (
            "genesis",
            "genesis",
            "committed",
        )
        assert _stages(found) == [
            ("validated", "done"),
            ("snapshotted", "skipped"),
            ("materialized", "done"),
        ]
        assert {stage["at"] for stage in found["stages"]} == {found["committed_at"]}
        # The stored result is the Engine's (``CODECS``), whose fields REST renders.
        assert found["result"]["status"] == 201
        assert found["result"]["body"]["story_id"] == blank.json()["story_id"]

        scenario_id, _ = await g.published_scenario(client, g.scenario_body())
        made = await g.create_from(
            client,
            g.from_scenario(
                scenario_id, slots={"mother": g.ad_hoc_slot("Linda", 50)}, ignore_warnings=True
            ),
            key="stages-scenario",
        )
        genesis = await one(
            s.db,
            "SELECT operation_id FROM operations WHERE story_id = :s AND kind = 'genesis'",
            s=made["story_id"],
        )
        found = await view(client, genesis)
        assert _stages(found) == [(name, "done") for name in GENESIS_STAGES]
        assert found["result"]["status"] == 201
        assert found["result"]["body"]["story_id"] == made["story_id"]

        # A rating change's check: running while the job waits, done once it ran.
        await guard_rating(s)
        story_id, _ = await new_story(client, PLAYER, FIRST, content_rating_id=EXPLICIT_ID)
        started = await change_rating(client, story_id, TEEN_ID, **{"Idempotency-Key": "stages-r"})
        assert started.status_code == 202, started.text
        check = await one(
            s.db,
            "SELECT operation_id FROM rating_check_jobs WHERE id = :j",
            j=started.json()["job_id"],
        )
        found = await view(client, check)
        assert (found["kind"], found["label"]) == ("control", "rating_check")
        assert _stages(found) == [("checking", "running"), ("applied", "pending")]
        assert found["stages"][1]["at"] is None
        assert found["result"]["status"] == 202
        assert found["result"]["body"]["job_id"] == started.json()["job_id"]
        assert await run_jobs(s.db) >= 1
        found = await view(client, check)
        assert _stages(found) == [("checking", "done"), ("applied", "done")]
        assert all(stage["at"] for stage in found["stages"])

        # An offboarding: one stage per entity type, pending until its job runs.
        leaving = {**WRITE, "X-Epistrel-Principal": "p-stages-leaving"}
        owned = await client.post("/stories", json=story_body(PLAYER, FIRST), headers=leaving)
        assert owned.status_code == 201, owned.text
        offboarded = await client.post(
            "/admin/offboarding",
            json={
                "principal": "p-stages-leaving",
                "actions": {"stories": "delete"},
                "ignore_warnings": True,
            },
            headers={**WRITE, "Idempotency-Key": "stages-offboarding"},
        )
        assert offboarded.status_code == 202, offboarded.text
        operation = await one(
            s.db,
            "SELECT operation_id FROM offboarding_jobs WHERE id = :j",
            j=offboarded.json()["job_id"],
        )
        found = await view(client, operation)
        assert (found["kind"], found["label"]) == ("control", "offboarding")
        names = [name for name, _ in _stages(found)]
        assert names
        assert _stages(found) == [(name, "pending") for name in names]
        assert found["result"]["status"] == 202
        assert found["result"]["body"]["job_id"] == offboarded.json()["job_id"]
        assert await run_jobs(s.db) >= 1
        found = await view(client, operation)
        assert _stages(found) == [(name, "done") for name in names]

        # A story erasure, and a catalog one (which runs no story stage).
        erased_story, ids = await new_story(client)
        erased = await client.post(
            "/admin/erasure",
            json=erasure_request("character", ids[FIRST], erased_story),
            headers={**WRITE, "Idempotency-Key": "stages-erasure"},
        )
        assert erased.status_code == 200, erased.text
        found = await view(client, erased.json()["operation_id"])
        assert (found["kind"], found["label"]) == ("control", "erasure")
        # A new story has no summary segment, so the regeneration recorded it had nothing to do.
        assert _stages(found) == [
            ("redacted", "done"),
            ("ledger_written", "done"),
            ("regenerated", "skipped"),
            ("rebuilt", "done"),
        ]
        assert [stage["name"] for stage in found["stages"]] == list(ERASURE_STAGES)
        assert all(stage["at"] for stage in found["stages"])
        assert found["result"]["status"] == 200
        assert found["result"]["body"]["operation_id"] == erased.json()["operation_id"]

        catalog_id = await linda(client)
        erased = await client.post(
            "/admin/erasure",
            json=erasure_request("catalog_character", catalog_id),
            headers={**WRITE, "Idempotency-Key": "stages-catalog-erasure"},
        )
        assert erased.status_code == 200, erased.text
        found = await view(client, erased.json()["operation_id"])
        assert (found["label"], found["story_id"]) == ("erasure", None)
        assert _stages(found) == [
            ("redacted", "done"),
            ("ledger_written", "done"),
            ("regenerated", "skipped"),
            ("rebuilt", "skipped"),
        ]

        # Any other operation: no label, no stages.
        other = await one(
            s.db,
            "SELECT operation_id FROM operations o WHERE kind = 'control' AND stage_log IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM rating_check_jobs j "
            "WHERE j.operation_id = o.operation_id) "
            "AND NOT EXISTS (SELECT 1 FROM offboarding_jobs j "
            "WHERE j.operation_id = o.operation_id) LIMIT 1",
        )
        found = await view(client, other)
        assert (found["label"], found["stages"]) == (None, [])
