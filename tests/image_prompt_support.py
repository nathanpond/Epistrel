"""Helpers for the image prompt builder tests (#259): the REST app with the stub model adapter,
a catalog character with a visual identity (a ``perceivable`` and a ``restricted`` reference
image, a restricted-details prompt, add-ons of each audience), ``POST /image-prompts`` and the
``image_prompt`` calls the stub saw.

The character is 34 (the band "mid thirties") and plants a phrase in every field an image
prompt may never receive (``NEVER``)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.engine import Engine
from epistrel.orchestrator.models.stub import StubAdapter, StubCall
from tests.catalog_support import BASE, WRITE, read_companion
from tests.character_support import complete
from tests.image_job_support import clean
from tests.media_support import png, uploaded
from tests.model_support import restored_model_config
from tests.respond_support import respond_client

TRUE_AGE = 34
BAND = "mid thirties"
APPEARANCE = "Odessa Thornquist is a tall woman with a copper braid and a sailcloth coat."
RESTRICTED_DETAILS = "a hidden kraken tattoo across her shoulders"
RESTRICTED_TERM = "kraken"
ADDON_RESTRICTED = "selkie-skin-lora"
ADDON_SEEN = "harbour-light-lora"
SEED = 271_828
IDENTITY_NEGATIVE = "text, watermark, Blurry"
BASE_PROMPT = "tall woman, copper braid, sailcloth coat"
NEVER = re.compile(rf"{RESTRICTED_TERM}|{ADDON_RESTRICTED}|\b34\b|thirty-four", re.IGNORECASE)
"""What no image prompt input or answer may hold: the restricted details, the restricted
add-on, and the true age."""
STYLE = "gilded umber brushwork"
STYLE_NEGATIVE = "lens flare, blurry"
PURPOSE = "image_prompt"
HEADERS = WRITE


@asynccontextmanager
async def prompt_app(
    settings: Settings, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, Engine]]:
    """The REST app with the stub model adapter over ``engine`` (its media prepared), with the
    model configuration and the image job tables restored afterwards."""
    async with restored_model_config(engine), respond_client(settings, engine) as (client, lib):
        lib.prepare_media()
        try:
            yield client, lib
        finally:
            await clean(engine)


async def style_companion(
    client: httpx.AsyncClient,
    character_id: str,
    prompt: str = STYLE,
    negative: str | None = STYLE_NEGATIVE,
) -> str:
    """Set the visual style on the character's companion scenario's draft; its id."""
    companion = await read_companion(client, character_id)
    style: dict[str, Any] = {"prompt": prompt}
    if negative is not None:
        style["negative_prompt"] = negative
    saved = await client.patch(
        f"/catalog/scenarios/{companion['id']}",
        json={"draft_revision": companion["draft_revision"], "visual_style": style},
        headers=WRITE,
    )
    assert saved.status_code == 200, saved.text
    return str(companion["id"])


async def references(client: httpx.AsyncClient) -> tuple[str, str]:
    """Two uploaded images: ``(perceivable, restricted)`` reference ids."""
    seen = await uploaded(client, png(nonce="seen"))
    hidden = await uploaded(client, png(nonce="hidden"))
    return seen["id"], hidden["id"]


def identity(seen: str, hidden: str, **fields: Any) -> dict[str, Any]:
    """A visual identity with a ``perceivable`` and a ``restricted`` reference image, a
    restricted-details prompt and add-ons of each audience."""
    found: dict[str, Any] = {
        "preferred_image_model": "sdxl-lightning",
        "base_appearance_prompt": BASE_PROMPT,
        "negative_prompt": IDENTITY_NEGATIVE,
        "seed": SEED,
        "reference_images": [
            {"media_id": seen, "audience": "perceivable"},
            {"media_id": hidden, "audience": "restricted"},
        ],
        "adapter_params": {
            "steps": 30,
            "addons": [
                {"name": ADDON_SEEN, "audience": "perceivable"},
                {"name": ADDON_RESTRICTED, "audience": "restricted"},
            ],
        },
        "restricted_details_prompt": RESTRICTED_DETAILS,
    }
    found.update(fields)
    return found


def character_body(seen: str, hidden: str, **identity_fields: Any) -> dict[str, Any]:
    """A complete, publishable character (34, Everyone) with that visual identity."""
    return complete(
        "Odessa Thornquist",
        definition_changes={"true_age": TRUE_AGE, "appearance": APPEARANCE},
        visual_identity=identity(seen, hidden, **identity_fields),
    )


async def create_character(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(BASE, json=body, headers=WRITE)
    assert response.status_code == 201, response.text
    found: dict[str, Any] = response.json()
    return found


async def publish(client: httpx.AsyncClient, character_id: str, revision: int) -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/{character_id}/publish", json={"draft_revision": revision}, headers=WRITE
    )
    assert response.status_code == 201, response.text
    found: dict[str, Any] = response.json()
    return found


async def build(
    client: httpx.AsyncClient,
    kind: str,
    entry_id: str,
    purpose: str,
    **target: Any,
) -> httpx.Response:
    """``POST /image-prompts`` for ``(kind, entry_id)`` (``draft=True``, ``version=N``)."""
    return await client.post(
        "/image-prompts",
        json={"target": {"kind": kind, "id": entry_id, **target}, "purpose": purpose},
        headers=WRITE,
    )


async def built(
    client: httpx.AsyncClient, kind: str, entry_id: str, purpose: str, **target: Any
) -> dict[str, Any]:
    response = await build(client, kind, entry_id, purpose, **target)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def prompt_calls(stub: StubAdapter) -> list[StubCall]:
    return [call for call in stub.calls if call.purpose == PURPOSE]


def rendered(call: StubCall) -> str:
    """Everything one call sent the model, as one text."""
    return "\n".join(message.content for message in call.messages)


def answer(stub: StubAdapter, *prompts: str) -> None:
    """Script the next ``image_prompt`` answers."""
    stub.script(PURPOSE, [json.dumps({"prompt": p}) for p in prompts])


async def image_rows(engine: AsyncEngine) -> dict[str, int]:
    """Rows in the image job tables and the image cost rows: none may be written."""
    async with engine.connect() as conn:
        found = {}
        for table in ("image_jobs", "image_reservations", "media_assets"):
            query = text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
            found[table] = int(await conn.scalar(query) or 0)
        found["jobs"] = int(
            await conn.scalar(text("SELECT count(*) FROM jobs WHERE kind = 'image_job'")) or 0
        )
    return found


__all__ = [
    "APPEARANCE",
    "BAND",
    "BASE_PROMPT",
    "IDENTITY_NEGATIVE",
    "NEVER",
    "RESTRICTED_DETAILS",
    "RESTRICTED_TERM",
    "SEED",
    "STYLE",
    "STYLE_NEGATIVE",
    "TRUE_AGE",
    "answer",
    "build",
    "built",
    "character_body",
    "create_character",
    "identity",
    "image_rows",
    "prompt_app",
    "prompt_calls",
    "publish",
    "references",
    "rendered",
    "style_companion",
]
