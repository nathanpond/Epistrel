"""C-23's M8 parts: a character's visual identity is versioned with the character, and a
prompt built from a version uses that version's identity (FR-CAT-4; #259).

- Publishing after editing the visual identity makes a new version. The previous version's
  identity reads back unchanged, and ``POST /image-prompts`` for ``version: 1`` uses version
  1's seed and references while the latest uses version 2's.
- ``visual_identity.adapter_params.addons`` has a shape: each add-on declares its
  ``audience``. A save or publish whose add-on lacks one, or carries another value, is 422 at
  ``visual_identity.adapter_params.addons[i].audience``. A ``restricted`` add-on is saved and
  never reaches a catalog image's inputs; a ``perceivable`` one does.

The M3 parts (``test_c_23__delivery``, ``test_c_23__optional_images``) live in
``tests/integration/test_media_delivery.py``; every C-23 M8 part lives in this file (the
traceability gate takes one file per test ID and milestone)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.orchestrator.images.prompt_builder import build_catalog_image_inputs
from tests import image_prompt_support as ip
from tests.auth_support import auth_settings
from tests.catalog_support import BASE, WRITE, read, save
from tests.model_support import config_key

pytestmark = pytest.mark.integration


@pytest.fixture
def image_settings(pg_url: str, tmp_path: Path) -> Settings:
    return auth_settings(
        pg_url, model_adapter="stub", config_key=config_key(), media_root=tmp_path / "media"
    )


@pytest.mark.part("M8", "FR-CAT-4")
async def test_c_23__m8_visual_identity(image_settings: Settings, engine: AsyncEngine) -> None:
    """Version 1 publishes the identity with seed 1 and reference A. Editing it to seed 2 and
    reference B and publishing makes version 2. Version 1 still reads seed 1 and reference A,
    and a prompt built from ``version: 1`` answers them. The latest (no ``draft``, no
    ``version``) answers version 2's. An unknown version is 404, an entry never published is
    422 ``not_published`` without ``draft``, ``draft`` with ``version`` and a ``cover`` for a
    character are 422 ``validation_error``. An archived entry still builds.

    Complement: the draft builds from its own revision (``target.draft_revision``), and a
    prompt from version 2 never carries version 1's reference."""
    async with ip.prompt_app(image_settings, engine) as (client, _lib):
        first, _ = await ip.references(client)
        second, hidden = await ip.references(client)
        created = await ip.create_character(client, ip.character_body(first, hidden, seed=1))
        character_id = created["id"]
        published = await ip.publish(client, character_id, created["draft_revision"])
        edited = await save(
            client,
            character_id,
            published["draft_revision"],
            visual_identity={
                "seed": 2,
                "reference_images": [
                    {"media_id": second, "audience": "perceivable"},
                    {"media_id": hidden, "audience": "restricted"},
                ],
            },
        )
        assert edited.status_code == 200, edited.text
        again = await ip.publish(client, character_id, edited.json()["draft_revision"])
        assert again["version"] == 2

        one = await read(client, character_id, version=1, projection="full")
        assert one.status_code == 200, one.text
        identity = one.json()["visual_identity"]
        assert identity["seed"] == 1
        assert [r["media_id"] for r in identity["reference_images"]] == [first, hidden]

        v1 = await ip.built(client, "character", character_id, "primary", version=1)
        assert (v1["seed"], v1["reference_image_ids"]) == (1, [first])
        assert v1["target"] == {
            "kind": "character",
            "id": character_id,
            "version": 1,
            "draft_revision": None,
        }
        latest = await ip.built(client, "character", character_id, "gallery")
        assert (latest["seed"], latest["reference_image_ids"]) == (2, [second])
        assert latest["target"]["version"] == 2
        assert first not in latest["reference_image_ids"]
        draft = await ip.built(client, "character", character_id, "primary", draft=True)
        assert draft["target"]["draft_revision"] == again["draft_revision"]

        missing = await ip.build(client, "character", character_id, "primary", version=9)
        assert missing.status_code == 404, missing.text
        both = await ip.build(client, "character", character_id, "primary", draft=True, version=1)
        assert (both.status_code, both.json()["error"]["code"]) == (422, "validation_error")
        cover = await ip.build(client, "character", character_id, "cover")
        assert cover.status_code == 422
        assert cover.json()["error"]["details"]["field"] == "body.purpose"

        archived = await client.post(f"{BASE}/{character_id}/archive", headers=WRITE)
        assert archived.status_code == 200, archived.text
        assert (await ip.built(client, "character", character_id, "primary"))["seed"] == 2

        unpublished = await ip.create_character(client, ip.character_body(first, hidden))
        never = await ip.build(client, "character", unpublished["id"], "primary")
        assert (never.status_code, never.json()["error"]["code"]) == (422, "not_published")


@pytest.mark.part("M8", "FR-CAT-4")
async def test_c_23__m8_addon_audience(image_settings: Settings, engine: AsyncEngine) -> None:
    """A save whose add-on lacks ``audience`` is 422 naming the index, as is one whose
    audience is neither ``perceivable`` nor ``restricted``; other ``adapter_params`` keys stay
    opaque. With ``audience: restricted`` the add-on is saved, and it never reaches the
    catalog image's inputs. A draft stored with an add-on lacking an audience (before the rule)
    is refused at its next publish, at the same field.

    Complement: the ``perceivable`` add-on does reach the inputs."""
    async with ip.prompt_app(image_settings, engine) as (client, lib):
        seen, hidden = await ip.references(client)
        created = await ip.create_character(client, ip.character_body(seen, hidden))
        character_id, revision = created["id"], created["draft_revision"]
        params = {"steps": 30, "sampler": {"name": "euler"}}
        for addon, kind in (
            ({"name": "x"}, "missing"),
            ({"name": "x", "audience": "everyone"}, "enum"),
        ):
            refused = await save(
                client,
                character_id,
                revision,
                visual_identity={
                    "adapter_params": {
                        **params,
                        "addons": [{"name": "ok", "audience": "perceivable"}, addon],
                    }
                },
            )
            assert refused.status_code == 422, refused.text
            error = refused.json()["error"]
            assert error["code"] == "validation_error"
            assert error["details"]["field"] == (
                "body.visual_identity.adapter_params.addons[1].audience"
            )
            assert error["details"]["errors"][0]["type"] == kind

        saved = await save(
            client,
            character_id,
            revision,
            visual_identity={
                "adapter_params": {
                    **params,
                    "addons": [
                        {"name": ip.ADDON_SEEN, "audience": "perceivable", "weight": 0.5},
                        {"name": ip.ADDON_RESTRICTED, "audience": "restricted"},
                    ],
                }
            },
        )
        assert saved.status_code == 200, saved.text
        stored = saved.json()["visual_identity"]["adapter_params"]
        assert stored["sampler"] == {"name": "euler"}
        async with engine.connect() as conn:
            view = await build_catalog_image_inputs(
                conn,
                "character",
                character_id,
                draft_revision=saved.json()["draft_revision"],
                purpose="primary",
            )
        [subject] = view.subjects
        assert [a["name"] for a in subject.adapter_addons] == [ip.ADDON_SEEN]
        assert ip.ADDON_RESTRICTED not in repr(view)
        found = await ip.built(client, "character", character_id, "primary", draft=True)
        assert ip.ADDON_RESTRICTED not in json.dumps(found)
        assert all(
            ip.ADDON_RESTRICTED not in ip.rendered(c) for c in ip.prompt_calls(lib.models.stub)
        )

        # A draft stored before the rule: refused at its next publish.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE catalog.character_catalog SET draft = jsonb_set(draft, "
                    "'{visual_identity,adapter_params,addons}', '[{\"name\": \"old\"}]') "
                    "WHERE id = :id"
                ),
                {"id": character_id},
            )
        refused = await client.post(
            f"{BASE}/{character_id}/publish",
            json={"draft_revision": saved.json()["draft_revision"]},
            headers=WRITE,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "validation_error"
        assert refused.json()["error"]["details"]["field"] == (
            "visual_identity.adapter_params.addons[0].audience"
        )
