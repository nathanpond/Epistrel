"""Story image uploads (#261): the subjects rule, idempotency and the order of the refusals.

The I-23 parts (the story images in place, their anchoring) and the C-24 part are in
``tests/integration/test_i_23.py`` and ``tests/conformance/test_c_24.py`` (one file per test ID
and milestone)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from tests import story_image_support as si
from tests.auth_support import auth_settings
from tests.respond_support import respond_client


@pytest.fixture
def stub_settings(pg_url: str) -> Settings:
    return auth_settings(pg_url, model_adapter="stub")


async def test_story_image_upload_subjects(stub_settings: Settings, engine: AsyncEngine) -> None:
    """An upload that leaves ``depicted_subjects`` out is 422 ``subjects_required`` and stores
    nothing; complement: an explicit ``[]`` declares none and is 201."""
    async with respond_client(stub_settings, engine) as (client, _):
        story_id, _ids = await si.scene(client, turns=1)
        omitted = await si.upload(
            client, story_id, si.meta({"turn": 1, "beat": 1}, si.OMNISCIENT, omit_subjects=True)
        )
        assert omitted.status_code == 422, omitted.text
        assert omitted.json()["error"]["code"] == "subjects_required"
        assert omitted.json()["error"]["details"]["field"] == "body.metadata.depicted_subjects"
        async with engine.connect() as conn:
            assert not await conn.scalar(
                text("SELECT count(*) FROM story_images WHERE story_id = :s"), {"s": story_id}
            )
        empty = await si.placed(client, story_id, si.meta({"turn": 1, "beat": 1}, si.OMNISCIENT))
        assert empty["depicted_subjects"] == []
        assert empty["source"] == "uploaded"


async def test_story_image_idempotent(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The same ``Idempotency-Key`` gives one row and one asset, the retry answering the stored
    201 as a replay; complement: another key places a second image."""
    async with respond_client(stub_settings, engine) as (client, _):
        story_id, _ids = await si.scene(client, turns=1)
        from tests.media_support import png

        data = png()
        body = si.meta({"turn": 1, "beat": 2}, si.OMNISCIENT)
        first = await si.upload(client, story_id, body, data=data, key="img-key-1")
        again = await si.upload(client, story_id, body, data=data, key="img-key-1")
        assert first.status_code == again.status_code == 201, (first.text, again.text)
        assert again.headers.get("Idempotent-Replay") == "true"
        assert again.json()["image_id"] == first.json()["image_id"]
        other = await si.upload(client, story_id, body, data=data, key="img-key-2")
        assert other.status_code == 201
        async with engine.connect() as conn:
            rows = await conn.scalar(
                text("SELECT count(*) FROM story_images WHERE story_id = :s"), {"s": story_id}
            )
        assert rows == 2


async def test_story_image_refusal_order(stub_settings: Settings, engine: AsyncEngine) -> None:
    """The body checks answer in order: ``position_invalid`` (an inactive turn, a beat the turn
    lacks, a slot after an earlier turn) before ``viewpoint_invalid`` before
    ``camera_pov_invalid`` before ``camera_pov_not_present`` before ``subjects_required``, and
    none stores anything. Complement: each one alone fixed moves the answer to the next."""
    async with respond_client(stub_settings, engine) as (client, _):
        story_id, ids = await si.scene(client, turns=2)
        outsider = "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"

        async def code(position: dict[str, int], audience: str, **fields: Any) -> str:
            response = await si.upload(client, story_id, si.meta(position, audience, **fields))
            assert response.status_code == 422, response.text
            found: str = response.json()["error"]["code"]
            return found

        worst = {
            "camera_pov": {"kind": "first_person", "character_id": outsider},
            "omit_subjects": True,
        }
        assert await code({"turn": 3, "beat": 1}, "character:nobody", **worst) == "position_invalid"
        assert await code({"turn": 2, "beat": 9}, si.OMNISCIENT) == "position_invalid"
        assert await code({"slot_after_turn": 1}, si.OMNISCIENT) == "position_invalid"
        assert await code({"turn": 2, "beat": 1}, "character:nobody", **worst) == (
            "viewpoint_invalid"
        )
        assert (
            await code(
                {"turn": 2, "beat": 1},
                si.OMNISCIENT,
                camera_pov={"kind": "first_person"},
                omit_subjects=True,
            )
            == "camera_pov_invalid"
        )
        assert (
            await code(
                {"turn": 2, "beat": 1},
                si.OMNISCIENT,
                camera_pov={"kind": "establishing", "character_id": ids[si.JENNI]},
            )
            == "camera_pov_invalid"
        )
        assert await code({"turn": 2, "beat": 1}, si.OMNISCIENT, **worst) == (
            "camera_pov_not_present"
        )
        assert (
            await code(
                {"turn": 2, "beat": 1},
                si.OMNISCIENT,
                camera_pov={"kind": "first_person", "character_id": ids[si.JENNI]},
                omit_subjects=True,
            )
            == "subjects_required"
        )
        async with engine.connect() as conn:
            assert not await conn.scalar(
                text("SELECT count(*) FROM story_images WHERE story_id = :s"), {"s": story_id}
            )
