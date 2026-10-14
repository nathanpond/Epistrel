"""A catalog character's content in detail (#115): what a draft may hold and what a publish needs,
the save rules beyond the schema, the images and the rating checked at save and at publish, the
two projections, the visual identity, the deployment's ``require_primary_image`` setting, and the
deflection style a story character stores. The milestone parts are ``test_c_15__character_content``
(``tests/conformance/test_c_15.py``), ``test_c_23__optional_images``
(``tests/integration/test_media_delivery.py``), ``test_r_05__deflection_style``
(``tests/redteam/test_r_05.py``) and ``test_s_selfpolice__deflection``
(``tests/scenario/test_s_selfpolice.py``).

Each test that changes the deployment's settings, ratings or policies runs on its own scratch
database (``scratch_catalog``) or puts back what it changed."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.catalog.models import (
    DISPOSITION_AXES,
    IDENTITY_REQUIRED,
    PUBLISH_REQUIRED,
    Caution,
    CharacterDefinition,
    CharacterDraft,
    Dominance,
    Honesty,
    ThreatResponse,
    Warmth,
    unset_required,
)
from epistrel.core.ids import new_ulid
from epistrel.core.ratings import retire_rating
from epistrel.core.seeds.ratings import EVERYONE_ID
from tests.auth_support import auth_settings
from tests.catalog_support import (
    BASE,
    WRITE,
    count,
    create,
    entry_row,
    publish,
    read,
    save,
    scratch_catalog,
    version_rows,
)
from tests.character_support import (
    HIDDEN,
    OPERATOR,
    SEEN,
    complete,
    named,
    require_primary_image,
    visual_identity,
)
from tests.event_support import rows, scalar
from tests.media_support import mp4, png, uploaded
from tests.policy_support import unique
from tests.story_support import WRITE as STORY_WRITE
from tests.story_support import create as create_story
from tests.story_support import rest, story_body

DETAIL_KEYS = frozenset(
    {"media_id", "alt_text", "caption", "mime_type", "width", "height", "generation_metadata"}
)
"""What ``full`` shows of each image (a reference image adds its ``audience``)."""
FULL_KEYS = frozenset(
    {
        "id",
        "status",
        "listing_status",
        "owner_principal",
        "version",
        "current_version",
        "created_at",
        "updated_at",
        "published_at",
        "companion_id",
        "name",
        "public_description",
        "images",
        "labels",
        "content_rating",
        "aliases",
        "definition",
        "visual_identity",
    }
)
"""A version read in ``full`` (a draft read adds ``draft_revision``)."""


@pytest.fixture
async def client(settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with rest(settings, engine) as found:
        yield found


def _refused(response: httpx.Response) -> tuple[int, str, str]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"].get("field", "")


def _errors(response: httpx.Response) -> list[tuple[str, str]]:
    """``(code, field)`` of each error of a refused publish's report, as reported."""
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "publish_invalid", response.text
    return [(e["code"], e["location"]["field"]) for e in error["details"]["report"]["errors"]]


# -- what a draft holds, what a publish needs ---------------------------------------------------


async def test_a_draft_needs_only_a_name(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """A name alone is a draft, read back in ``full`` with every definition field unset. Its
    publish is refused with every missing field at once: ``field_required`` at each required
    path, ``true_age_missing`` and ``content_rating_missing``; nothing is written. Complement: the
    complete document publishes."""
    created = await create(client, named("Mara Venn"))
    assert created["definition"] == {
        "true_age": None,
        "appearance": None,
        "personality": None,
        "likes_dislikes": None,
        "dispositions": None,
        "values_motivations": None,
        "interaction_style": None,
        "background_flavor": None,
        "deflection_style": None,
        "safe_fallback_text": None,
    }
    assert (created["aliases"], created["labels"], created["visual_identity"]) == ([], [], None)
    entry, operations = (
        await entry_row(engine, created["id"]),
        await count(engine, "SELECT count(*) FROM operations"),
    )
    refused = await publish(client, created["id"], 1)
    expected = [
        ("field_required", "public_card.public_description"),
        ("field_required", "definition.appearance"),
        ("field_required", "definition.personality"),
        ("field_required", "definition.dispositions.threat_response"),
        ("field_required", "definition.dispositions.dominance"),
        ("field_required", "definition.dispositions.honesty"),
        ("field_required", "definition.dispositions.warmth"),
        ("field_required", "definition.dispositions.caution"),
        ("field_required", "definition.values_motivations"),
        ("field_required", "definition.interaction_style.speech_style"),
        ("field_required", "definition.interaction_style.vocabulary"),
        ("true_age_missing", "definition.true_age"),
        ("content_rating_missing", "metadata.content_rating_id"),
    ]
    assert sorted(_errors(refused)) == sorted(expected)
    assert {path for code, path in expected if code == "field_required"} == (
        set(PUBLISH_REQUIRED) - {"public_card.name"}
    )
    assert await entry_row(engine, created["id"]) == entry
    assert await count(engine, "SELECT count(*) FROM operations") == operations
    assert await version_rows(engine, created["id"]) == []

    filled = await save(client, created["id"], 1, **{k: v for k, v in complete().items()})
    assert filled.status_code == 200, filled.text
    done = await publish(client, created["id"], 2)
    assert done.status_code == 201, done.text


async def test_blank_text_does_not_count(client: httpx.AsyncClient) -> None:
    """A required text that holds only spaces is missing at publish, as is each disposition axis
    a draft leaves out, while a list left empty is not. Complement: the same draft with the text
    and the axis filled publishes."""
    body = complete("Ilse Marr")
    body["definition"]["appearance"] = "   "
    del body["definition"]["dispositions"]["warmth"]
    body["definition"]["likes_dislikes"] = {"likes": [], "dislikes": []}
    created = await create(client, body)
    assert sorted(_errors(await publish(client, created["id"], 1))) == [
        ("field_required", "definition.appearance"),
        ("field_required", "definition.dispositions.warmth"),
    ]
    fixed = await save(
        client,
        created["id"],
        1,
        definition={
            "appearance": "Thin and tall.",
            "dispositions": {"warmth": {"setting": "warm"}},
        },
    )
    assert fixed.status_code == 200, fixed.text
    assert (await publish(client, created["id"], 2)).status_code == 201


async def test_a_visual_identity_needs_its_fields(client: httpx.AsyncClient) -> None:
    """With a visual identity, a publish also needs its preferred image model, base appearance
    prompt, negative prompt and seed; its reference images, adapter parameters and restricted
    details prompt are optional. Without one, none of them is needed."""
    partial = complete("Tess Lyle", visual_identity={"base_appearance_prompt": "a sailor"})
    created = await create(client, partial)
    missing = sorted(set(IDENTITY_REQUIRED) - {"visual_identity.base_appearance_prompt"})
    assert sorted(_errors(await publish(client, created["id"], 1))) == [
        ("field_required", path) for path in missing
    ]
    whole = visual_identity()
    for optional in ("reference_images", "adapter_params", "restricted_details_prompt"):
        del whole[optional]
    saved = await save(client, created["id"], 1, visual_identity=whole)
    assert saved.status_code == 200, saved.text
    assert (await publish(client, created["id"], 2)).status_code == 201
    plain = await create(client, complete("Ned Rowe"))
    assert (await publish(client, plain["id"], 1)).status_code == 201


# -- the save rules --------------------------------------------------------------------------------

REFUSED_DRAFTS: list[tuple[dict[str, Any], str]] = [
    (named("Mara", metadata={"aliases": ["MARA"]}), "body.metadata.aliases[0]"),
    (named("Mara", metadata={"aliases": ["Keeper", " keeper "]}), "body.metadata.aliases[1]"),
    (named("Mara", metadata={"aliases": ["x" * 61]}), "body.metadata.aliases[0]"),
    (named("Mara", metadata={"aliases": [f"a{n}" for n in range(21)]}), "body.metadata.aliases"),
    (named("Mara", metadata={"labels": ["Quiet", "quiet"]}), "body.metadata.labels"),
    (named("Mara", metadata={"labels": ["no!"]}), "body.metadata.labels[0]"),
    (named("Mara", metadata={"labels": [f"l{n}" for n in range(31)]}), "body.metadata.labels"),
    (
        named("Mara", definition={"dispositions": {"honesty": {"setting": "sometimes"}}}),
        "body.definition.dispositions.honesty.setting",
    ),
    (
        named("Mara", definition={"dispositions": {"honesty": {"nuance": "Mostly."}}}),
        "body.definition.dispositions.honesty.setting",
    ),
    (
        named(
            "Mara",
            definition={"dispositions": {"caution": {"setting": "bold", "nuance": "x" * 501}}},
        ),
        "body.definition.dispositions.caution.nuance",
    ),
    (named("Mara", definition={"true_age": 151}), "body.definition.true_age"),
    (named("Mara", definition={"true_age": True}), "body.definition.true_age"),
    (
        named("Mara", definition={"likes_dislikes": {"likes": [" "]}}),
        "body.definition.likes_dislikes.likes[0]",
    ),
    (
        named("Mara", definition={"interaction_style": {"mannerisms": ["m"] * 21}}),
        "body.definition.interaction_style.mannerisms",
    ),
    (
        named("Mara", definition={"deflection_style": "x" * 2001}),
        "body.definition.deflection_style",
    ),
    (named("x" * 121), "body.public_card.name"),
    (named("Mara", visual_identity={"seed": 2**32}), "body.visual_identity.seed"),
    (named("Mara", visual_identity={"seed": -1}), "body.visual_identity.seed"),
    (named("Mara", visual_identity={"seed": "7"}), "body.visual_identity.seed"),
    (
        named("Mara", visual_identity={"adapter_params": {"blob": "x" * 9000}}),
        "body.visual_identity.adapter_params",
    ),
    (
        named("Mara", visual_identity={"base_appearance_prompt": "x" * 2001}),
        "body.visual_identity.base_appearance_prompt",
    ),
]
"""Drafts a create refuses (422 ``validation_error``), each at its first field."""


async def test_saves_refuse_what_breaks_a_rule(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """A create or a save is refused at the field that breaks a rule: an alias that is the name
    or another alias (ignoring case), a label twice once lower-cased, a closed setting given
    another value or left out of its axis, the length and count limits, a seed outside 0 to
    2^32 - 1 (or not an integer), adapter parameters over 8 KiB. A reference image without its
    audience, or with a ``null`` one, is refused at its index, on a create and on a save, and a
    reference image listed twice or a value that is not an audience is refused too. Nothing is
    stored. Complement: labels are stored trimmed and lower-cased, and a reference image with
    each audience saves."""
    before = await count(engine, "SELECT count(*) FROM catalog.character_catalog")
    for body, field in REFUSED_DRAFTS:
        refused = await client.post(BASE, json=body, headers=WRITE)
        assert _refused(refused) == (422, "validation_error", field), (field, refused.text)
    assert await count(engine, "SELECT count(*) FROM catalog.character_catalog") == before

    media = [new_ulid() for _ in range(3)]
    created = await create(client, named("Mara Venn", metadata={"labels": [" Quiet ", "STERN"]}))
    assert created["labels"] == ["quiet", "stern"]
    cid = created["id"]
    for references, field in (
        ([{"media_id": media[0]}], "body.visual_identity.reference_images[0].audience"),
        (
            [{"media_id": media[0], "audience": SEEN}, {"media_id": media[1], "audience": None}],
            "body.visual_identity.reference_images[1].audience",
        ),
        (
            [{"media_id": media[0], "audience": "public"}],
            "body.visual_identity.reference_images[0].audience",
        ),
        (
            [{"media_id": media[0], "audience": SEEN}, {"media_id": media[0], "audience": HIDDEN}],
            "body.visual_identity.reference_images",
        ),
        (
            [{"media_id": media[n % 3], "audience": SEEN} for n in range(11)],
            "body.visual_identity.reference_images",
        ),
    ):
        refused = await save(client, cid, 1, visual_identity={"reference_images": references})
        assert _refused(refused) == (422, "validation_error", field), (field, refused.text)
        posted = await client.post(
            BASE,
            json=named("Ghost", visual_identity={"reference_images": references}),
            headers=WRITE,
        )
        assert _refused(posted) == (422, "validation_error", field), field
    aliased = await save(client, cid, 1, metadata={"aliases": ["mara venn"]})
    assert _refused(aliased) == (422, "validation_error", "body.metadata.aliases[0]")
    assert (await read(client, cid, draft=True, projection="full")).json()["draft_revision"] == 1


async def test_numbers_in_adapter_params_must_be_finite(client: httpx.AsyncClient) -> None:
    """``adapter_params`` is stored and read back as sent, nested values included; a number JSON
    cannot hold (``1e400``) is refused at the field rather than stored as ``null``. Complement:
    finite numbers, nulls and nested objects round-trip. A save is refused the same way, and leaves
    the stored parameters as they were; complement: a finite save merges (a ``null`` removes)."""
    raw = '{"public_card": {"name": "Mara"}, "visual_identity": {"adapter_params": {"x": 1e400}}}'
    refused = await client.post(
        BASE, content=raw, headers={**WRITE, "Content-Type": "application/json"}
    )
    assert _refused(refused)[:2] == (422, "validation_error")
    assert _refused(refused)[2].startswith("body.visual_identity.adapter_params")
    params = {"lora": {"weight": 0.75, "tags": ["a", None]}, "cfg": 12.5, "steps": 30}
    created = await create(client, named("Mara", visual_identity={"adapter_params": params}))
    assert created["visual_identity"]["adapter_params"] == params
    # A save is checked the same way: dumped as JSON, the infinity would be a ``null``, which
    # the merge reads as "remove this key" (#115's verification, round 1).
    cid = created["id"]
    for number in ("1e400", "-1e400", "NaN", "Infinity"):
        raw = (
            '{"draft_revision": 1, "visual_identity": {"adapter_params": '
            f'{{"cfg": {number}, "steps": 31}}}}}}'
        )
        patched = await client.patch(
            f"{BASE}/{cid}", content=raw, headers={**WRITE, "Content-Type": "application/json"}
        )
        assert _refused(patched)[:2] == (422, "validation_error"), (number, patched.text)
        assert _refused(patched)[2].startswith("body.visual_identity.adapter_params"), number
    draft = (await read(client, cid, draft=True, projection="full")).json()
    assert draft["draft_revision"] == 1
    assert draft["visual_identity"]["adapter_params"] == params
    merged = await save(
        client, cid, 1, visual_identity={"adapter_params": {"cfg": None, "steps": 31}}
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["visual_identity"]["adapter_params"] == {
        "lora": {"weight": 0.75, "tags": ["a", None]},
        "steps": 31,
    }


# -- images and the rating, at save and at publish -------------------------------------------------


async def test_images_are_checked_at_save_and_at_publish(pg_url: str) -> None:
    """A save that adds an image or a reference image that is not live (retired, or unknown) is
    422 ``media_retired`` at the field. A video where an image belongs (the primary image, a
    reference image) saves, and the publish refuses it (``media_kind_mismatch``, naming the
    asset), as it refuses an image retired since it was added (``media_retired``), each at its
    field, all at once. Complement: once the draft shows live images only, it publishes."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        live = await uploaded(client, png(nonce=new_ulid()))
        later = await uploaded(client, png(nonce=new_ulid()))
        gone = await uploaded(client, png(nonce=new_ulid()))
        clip = await uploaded(
            client, mp4(), kind="video", content_type="video/mp4", poster=live["id"]
        )
        assert (await client.delete(f"/media/{gone['id']}", headers=OPERATOR)).status_code == 204
        cid = (await create(client, complete("Mara Venn")))["id"]
        for patch, field in (
            (
                {"public_card": {"images": {"primary": gone["id"]}}},
                "body.public_card.images.primary",
            ),
            (
                {"public_card": {"images": {"gallery": [live["id"], new_ulid()]}}},
                "body.public_card.images.gallery[1]",
            ),
            (
                {"visual_identity": visual_identity((gone["id"], HIDDEN))},
                "body.visual_identity.reference_images[0].media_id",
            ),
        ):
            refused = await save(client, cid, 1, **patch)
            assert _refused(refused) == (422, "media_retired", field), (field, refused.text)
        shown = await save(
            client,
            cid,
            1,
            public_card={"images": {"primary": clip["id"], "gallery": [later["id"]]}},
            visual_identity=visual_identity((clip["id"], SEEN)),
        )
        assert shown.status_code == 200, shown.text
        assert (await client.delete(f"/media/{later['id']}", headers=OPERATOR)).status_code == 204
        refused = await publish(client, cid, 2)
        assert sorted(_errors(refused)) == [
            ("media_kind_mismatch", "public_card.images.primary"),
            ("media_kind_mismatch", "visual_identity.reference_images[0].media_id"),
            ("media_retired", "public_card.images.gallery[0]"),
        ]
        sources = {e["source"] for e in refused.json()["error"]["details"]["report"]["errors"]}
        assert sources == {clip["id"], later["id"]}
        fixed = await save(
            client,
            cid,
            2,
            public_card={"images": {"primary": live["id"], "gallery": []}},
            visual_identity=visual_identity((live["id"], SEEN)),
        )
        assert fixed.status_code == 200, fixed.text
        assert (await publish(client, cid, 3)).status_code == 201


async def test_the_rating_is_checked_at_save_and_at_publish(pg_url: str) -> None:
    """A save naming a rating that does not exist is 422 ``rating_retired`` at
    ``body.metadata.content_rating_id``. A rating retired after it was saved refuses the publish
    (``rating_retired`` at ``metadata.content_rating_id``, naming the rating). Complement: back
    on the Everyone rating, the draft publishes and its version records that rating's version."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        made = await client.post(
            "/admin/content-ratings", json={"name": unique("Shelved")}, headers=OPERATOR
        )
        assert made.status_code == 201, made.text
        rating = made.json()["id"]
        cid = (await create(client, complete("Mara Venn", metadata={"content_rating_id": rating})))[
            "id"
        ]
        unknown = await save(client, cid, 1, metadata={"content_rating_id": new_ulid()})
        assert _refused(unknown) == (422, "rating_retired", "body.metadata.content_rating_id")
        await retire_rating(db, rating, principal="p-operator")
        refused = await publish(client, cid, 1)
        assert _errors(refused) == [("rating_retired", "metadata.content_rating_id")]
        (entry,) = refused.json()["error"]["details"]["report"]["errors"]
        assert entry["source"] == rating
        back = await save(client, cid, 1, metadata={"content_rating_id": EVERYONE_ID})
        assert back.status_code == 200, back.text
        assert (await publish(client, cid, 2)).status_code == 201
        recorded = await scalar(
            db,
            "SELECT content_rating_version_id FROM catalog.character_versions "
            "WHERE catalog_id = :c",
            c=cid,
        )
        assert recorded == (await read(client, cid)).json()["content_rating"]["version_id"]


# -- the projections -------------------------------------------------------------------------------


async def test_full_inlines_each_image_and_the_card_never_does(pg_url: str) -> None:
    """``full`` shows each image (the primary, the gallery, each reference image) with its media
    row: its alt text, caption, MIME type, dimensions and generation metadata, a reference image
    with its audience too; the card shows only ids and alt text, and never the visual identity
    or the aliases. A version read in ``full`` has exactly the planned keys, and the definition
    every field of the document's definition."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        assets = [
            await uploaded(
                client, png(5, 4, nonce=new_ulid()), alt_text=f"Alt {n}", caption=f"harbour {n}"
            )
            for n in range(3)
        ]
        body = complete(
            "Mara Venn",
            images={"primary": assets[0]["id"], "gallery": [assets[1]["id"], assets[0]["id"]]},
            metadata={"aliases": ["The Keeper"]},
            visual_identity=visual_identity((assets[2]["id"], HIDDEN)),
        )
        created = await create(client, body)
        assert (await publish(client, created["id"], 1)).status_code == 201
        full = (await read(client, created["id"], projection="full")).json()
        assert set(full) == FULL_KEYS
        assert set(full["definition"]) == set(CharacterDefinition.model_fields)
        primary = full["images"]["primary"]
        assert set(primary) == DETAIL_KEYS
        assert (primary["alt_text"], primary["caption"], primary["mime_type"]) == (
            "Alt 0",
            "harbour 0",
            "image/png",
        )
        assert (primary["width"], primary["height"]) == (5, 4)
        assert [g["media_id"] for g in full["images"]["gallery"]] == [
            assets[1]["id"],
            assets[0]["id"],
        ]
        (reference,) = full["visual_identity"]["reference_images"]
        assert set(reference) == DETAIL_KEYS | {"audience"}
        assert (reference["media_id"], reference["audience"], reference["caption"]) == (
            assets[2]["id"],
            HIDDEN,
            "harbour 2",
        )
        card = (await read(client, created["id"])).json()
        assert card["images"] == {
            "primary": {"media_id": assets[0]["id"], "alt_text": "Alt 0"},
            "gallery": [
                {"media_id": assets[1]["id"], "alt_text": "Alt 1"},
                {"media_id": assets[0]["id"], "alt_text": "Alt 0"},
            ],
        }
        for absent in ("visual_identity", "aliases", "definition", "harbour", "The Keeper"):
            assert absent not in json.dumps(card), absent


# -- the schema ------------------------------------------------------------------------------------


def _property_names(schema: dict[str, Any]) -> set[str]:
    """Every property name anywhere in a JSON Schema with ``$defs``."""
    found: set[str] = set()
    pending: list[Any] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("properties"), dict):
                found.update(item["properties"])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def test_a_character_holds_no_world_truths() -> None:
    """The draft's schema has no field for facts about a world: none of its property names is a
    fact, a truth, a seed, a memory, a secret or a world, and the draft model's docstring says
    where they belong (the companion scenario, #116). Complement: the definition's fields are
    all there."""
    names = _property_names(CharacterDraft.model_json_schema())
    assert names.isdisjoint(
        {"facts", "fact", "truths", "world", "seeds", "seeded_memories", "memories", "secrets"}
    )
    assert {"appearance", "dispositions", "values_motivations", "deflection_style"} <= names
    doc = " ".join((CharacterDraft.__doc__ or "").split())
    assert "companion scenario" in doc
    assert "#116" in doc


async def test_disposition_settings_are_closed_and_documented(client: httpx.AsyncClient) -> None:
    """Each disposition axis takes exactly its five planned steps, and the served OpenAPI
    document lists them as the axis's enum."""
    planned = {
        "ThreatResponse": ["flees", "yields", "deflects", "stands", "attacks"],
        "Dominance": ["very_submissive", "submissive", "balanced", "dominant", "very_dominant"],
        "Honesty": ["very_deceitful", "deceitful", "balanced", "honest", "very_honest"],
        "Warmth": ["cold", "cool", "neutral", "warm", "very_warm"],
        "Caution": ["reckless", "bold", "balanced", "careful", "very_cautious"],
    }
    enums = {"ThreatResponse": ThreatResponse, "Dominance": Dominance, "Honesty": Honesty}
    enums |= {"Warmth": Warmth, "Caution": Caution}
    assert {name: [m.value for m in enum] for name, enum in enums.items()} == planned
    served = (await client.get("/openapi.json")).json()["components"]["schemas"]
    assert {name: served[name]["enum"] for name in planned} == planned
    assert DISPOSITION_AXES == ("threat_response", "dominance", "honesty", "warmth", "caution")


# -- the deployment's setting ----------------------------------------------------------------------


async def test_require_primary_image_is_a_deployment_setting(pg_url: str) -> None:
    """``require_primary_image`` reads ``false`` by default; a write takes a JSON boolean only
    (a string is 422 at its field) and is recorded as one prospective configuration change.
    While it is on, a draft without a primary image is refused; once the draft names one, it
    publishes."""
    async with scratch_catalog(pg_url) as (settings, db), rest(settings, db) as client:
        assert (await client.get("/admin/settings", headers=OPERATOR)).json()[
            "require_primary_image"
        ] is False
        refused = await client.put(
            "/admin/settings", json={"require_primary_image": "true"}, headers=OPERATOR
        )
        assert _refused(refused) == (422, "validation_error", "body.require_primary_image")
        await require_primary_image(client, True)
        changes = await rows(
            db,
            "SELECT setting, class, old, new, actor FROM config_changes "
            "WHERE setting = 'require_primary_image'",
        )
        assert [tuple(row) for row in changes] == [
            ("require_primary_image", "prospective", False, True, "p-operator")
        ]
        picture = await uploaded(client, png(nonce=new_ulid()))
        cid = (await create(client, complete("Mara Venn")))["id"]
        assert _errors(await publish(client, cid, 1)) == [
            ("primary_image_required", "public_card.images.primary")
        ]
        named_one = await save(client, cid, 1, public_card={"images": {"primary": picture["id"]}})
        assert named_one.status_code == 200, named_one.text
        assert (await publish(client, cid, 2)).status_code == 201


# -- the deflection style of a story character -----------------------------------------------------


async def test_story_characters_store_their_deflection_style(
    pg_url: str, engine: AsyncEngine
) -> None:
    """A story's cast and a character added later each store an optional ``deflection_style``
    (1 to 2,000 characters): an empty or too long one is 422 at its field. The input gate checks
    it like the definition: a style holding a word an input policy refuses is 422
    ``guardrail_violation`` at ``characters[i].deflection_style`` (``deflection_style`` for an
    addition) and creates nothing. A promotion copies the style into the catalog draft's
    definition. Complement: a clean style is stored as sent."""
    settings = auth_settings(pg_url, model_adapter="stub")
    word = f"quux{new_ulid().lower()}"
    async with rest(settings, engine) as client:
        made = await client.post(
            "/admin/policies",
            json={
                "scope": "global",
                "name": unique("deflect"),
                "rule": {"type": "wordlist", "words": [word]},
                "message": "Not in a deflection.",
            },
            headers=OPERATOR,
        )
        assert made.status_code == 201, made.text
        try:
            for style, field in (
                ("", "body.characters[1].deflection_style"),
                ("x" * 2001, "body.characters[1].deflection_style"),
            ):
                body = story_body("Wren", "Ada")
                body["characters"][1]["deflection_style"] = style
                refused = await client.post("/stories", json=body, headers=STORY_WRITE)
                assert _refused(refused) == (422, "validation_error", field), style
            stories_before = await count(engine, "SELECT count(*) FROM stories")
            body = story_body("Wren", "Ada")
            body["characters"][1]["deflection_style"] = f"She says {word} and walks off."
            gated = await client.post("/stories", json=body, headers=STORY_WRITE)
            assert (gated.status_code, gated.json()["error"]["code"]) == (
                422,
                "guardrail_violation",
            )
            (violation,) = gated.json()["error"]["details"]["violations"]
            assert (violation["policy_id"], violation["locator"]) == (
                made.json()["id"],
                "characters[1].deflection_style",
            )
            assert await count(engine, "SELECT count(*) FROM stories") == stories_before

            body["characters"][1]["deflection_style"] = "She shrugs it off."
            story = await create_story(client, body)
            ada = story["characters"][1]["id"]
            stored = await scalar(
                engine, "SELECT deflection_style FROM characters WHERE id = :c", c=ada
            )
            assert stored == "She shrugs it off."
            player = story["characters"][0]["id"]
            assert (
                await scalar(
                    engine, "SELECT deflection_style FROM characters WHERE id = :c", c=player
                )
                is None
            )

            added = {"name": "Dale", "definition": "A courier.", "true_age": 50}
            refused = await client.post(
                f"/stories/{story['story_id']}/characters",
                json={**added, "deflection_style": f"Dale mutters {word}."},
                headers=STORY_WRITE,
            )
            assert refused.status_code == 422, refused.text
            (violation,) = refused.json()["error"]["details"]["violations"]
            assert violation["locator"] == "deflection_style"
            joined = await client.post(
                f"/stories/{story['story_id']}/characters",
                json={**added, "deflection_style": "Dale checks his watch."},
                headers=STORY_WRITE,
            )
            assert joined.status_code == 201, joined.text
            assert (
                await scalar(
                    engine,
                    "SELECT deflection_style FROM characters WHERE id = :c",
                    c=joined.json()["id"],
                )
                == "Dale checks his watch."
            )

            promoted = await client.post(
                f"/stories/{story['story_id']}/characters/{ada}/promote",
                json={},
                headers=STORY_WRITE,
            )
            assert promoted.status_code == 201, promoted.text
            draft = await scalar(
                engine,
                "SELECT draft FROM catalog.character_catalog WHERE id = :c",
                c=promoted.json()["catalog_id"],
            )
            assert draft["definition"]["deflection_style"] == "She shrugs it off."
            assert draft["public_card"]["name"] == "Ada"
        finally:
            await client.delete(f"/admin/policies/{made.json()['id']}", headers=OPERATOR)


def test_the_complete_fixture_is_a_valid_draft() -> None:
    """The tests' complete character validates as a draft and leaves nothing a publish needs
    unset (``models.unset_required``), so a publish refusing it would be the code's doing."""
    draft = CharacterDraft.model_validate(copy.deepcopy(complete()))
    assert list(unset_required(draft)) == []
    assert draft.definition.true_age is not None
    assert draft.metadata.content_rating_id == EVERYONE_ID
