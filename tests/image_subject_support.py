"""Helpers for the depicted-subject tests (#266): a story whose images and image jobs declare or
name Marcus, and a catalog whose candidates declare or name one character.

**The story** (``story_images``): the whisper scene (``story_image_generation_support``), turn 1
played, then, for Bobby's viewpoint, slot uploads after turn 1 and image jobs at turn 1 beat 3:

- ``declared``: an upload declaring Marcus;
- ``named``: an upload declaring nobody, its caption naming Marcus;
- ``visual``: an upload declaring nobody and naming nobody (it may show anyone, visually);
- ``other``: an upload declaring Jenni, its caption naming her;
- ``built``: a generated image Epistrel built from Bobby's perception (it depicts Jenni and
  Marcus);
- ``prompted``: a generated image from a consumer prompt naming Marcus, declaring nobody;
- ``waiting``: a queued consumer job (never run) whose prompt names Marcus;
- ``kept_job``: a queued consumer job whose prompt names only Jenni.

**The catalog** (``catalog_candidates``): two characters with names no other test uses, each
with a visual identity and a styled companion, and candidates: ``own`` (the first's, which
declare her), ``named`` (the second's, whose prompt names the first) and ``kept`` (the second's,
whose prompt names nobody)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import catalog_image_support as cs
from tests import image_prompt_support as ip
from tests import story_image_generation_support as sg
from tests import story_image_support as si
from tests.character_support import complete
from tests.image_job_support import MINORS
from tests.image_support import Outputs, StubImageChecker
from tests.story_support import WRITE

MARCUS_CAPTION = "Marcus by the stove"
JENNI_CAPTION = "Jenni at the window"
VISUAL_ALT = "Two figures by the stove"
MARCUS_PROMPT = "Marcus leans on the bar, lamplight on his beard."
WAITING_PROMPT = "Marcus pours a drink for the room."
JENNI_PROMPT = "Jenni pours a drink for the room."
AT = {"turn": 1, "beat": 3}
SLOT = {"slot_after_turn": 1}

ERASED_NAME = "Ysolde Quillfeather"
KEPT_NAME = "Perpetua Hawksmoor"
NEUTRAL_PROMPT = "a lighthouse keeper's lamp on a foggy quay"
NAMING_PROMPT = f"{ERASED_NAME} on a foggy quay"


def marcus(ids: dict[str, str]) -> list[dict[str, str]]:
    return [{"kind": "character", "id": ids["Marcus"]}]


@dataclass
class StoryImages:
    story_id: str
    ids: dict[str, str]
    images: dict[str, str] = field(default_factory=dict)
    """Image ids by name (module docstring)."""
    assets: dict[str, str] = field(default_factory=dict)
    """Their asset ids."""
    jobs: dict[str, str] = field(default_factory=dict)
    """Job ids: ``built``, ``prompted``, ``waiting``, ``kept_job``."""


async def story_images(app: sg.GenerationApp, engine: AsyncEngine) -> StoryImages:
    """The story (module docstring)."""
    scene = await sg.whisper(app, engine)
    story, ids = scene.story_id, scene.ids
    bobby = sg.character(ids["Bobby"])
    found = StoryImages(story, ids)
    # The uploads pass the configured image_check model (#258).
    StubImageChecker(app.stub).answer({MINORS: "pass"})
    uploads = {
        "declared": si.meta(SLOT, bobby, subjects=marcus(ids)),
        "named": si.meta(SLOT, bobby, caption=MARCUS_CAPTION),
        "visual": si.meta(SLOT, bobby, alt_text=VISUAL_ALT),
        "other": si.meta(
            SLOT, bobby, subjects=[{"kind": "character", "id": ids["Jenni"]}], caption=JENNI_CAPTION
        ),
    }
    for name, metadata in uploads.items():
        placed = await si.placed(app.client, story, metadata)
        found.images[name] = placed["image_id"]
        found.assets[name] = placed["media_id"]
    found.jobs["built"] = (await sg.generated(app, story, position=AT, audience_viewpoint=bobby))[
        "id"
    ]
    found.jobs["prompted"] = (
        await sg.generated(
            app,
            story,
            position=AT,
            audience_viewpoint=bobby,
            prompt=MARCUS_PROMPT,
            depicted_subjects=[],
        )
    )["id"]
    found.jobs["waiting"] = await sg.queued(
        app,
        story,
        position=AT,
        audience_viewpoint=bobby,
        prompt=WAITING_PROMPT,
        depicted_subjects=[],
    )
    found.jobs["kept_job"] = await sg.queued(
        app, story, position=AT, audience_viewpoint=bobby, prompt=JENNI_PROMPT, depicted_subjects=[]
    )
    for row in await sg.story_image_rows(engine, story):
        for name in ("built", "prompted"):
            if row["job_id"] == found.jobs[name]:
                found.images[name] = str(row["id"])
                found.assets[name] = str(row["asset_id"])
    return found


async def image_ids(engine: AsyncEngine, story_id: str) -> set[str]:
    async with engine.connect() as conn:
        return {
            str(i)
            for i in await conn.scalars(
                text("SELECT id FROM story_images WHERE story_id = :s"), {"s": story_id}
            )
        }


async def asset_ids(engine: AsyncEngine, ids: list[str]) -> set[str]:
    """Which of ``ids`` still have a ``media_assets`` row."""
    async with engine.connect() as conn:
        return {
            str(i)
            for i in await conn.scalars(
                text("SELECT id FROM media_assets WHERE id = ANY(CAST(:i AS text[]))"), {"i": ids}
            )
        }


async def jobs(engine: AsyncEngine, job_ids: list[str]) -> dict[str, Any]:
    """``{job id: row}`` (status, prompt, negative prompt, params)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, status, status_reason, prompt, negative_prompt, params "
                "FROM image_jobs WHERE id = ANY(CAST(:i AS text[]))"
            ),
            {"i": job_ids},
        )
        return {str(r.id): r for r in rows}


@dataclass
class CatalogCandidates:
    erased: str
    kept: str
    candidates: dict[str, str] = field(default_factory=dict)
    """Candidate ids: ``own``, ``named``, ``kept``."""
    references: list[str] = field(default_factory=list)
    """The erased character's reference images' media ids."""


def _body(name: str, seen: str, hidden: str) -> dict[str, Any]:
    return complete(
        name,
        definition_changes={
            "true_age": ip.TRUE_AGE,
            "appearance": "a tall woman with a copper braid and a sailcloth coat",
        },
        visual_identity=ip.identity(seen, hidden),
    )


async def one(app: cs.CandidateApp, entry_id: str, prompt: str, size: tuple[int, int]) -> str:
    """One candidate of the entry whose built prompt is ``prompt``; its id."""
    stub = app.lib.models.stub
    assert stub is not None
    ip.answer(stub, prompt)
    _, [candidate] = await cs.generated(app, entry_id, Outputs(n=1, size=size))
    return candidate


async def catalog_candidates(app: cs.CandidateApp) -> CatalogCandidates:
    """The catalog (module docstring)."""
    made: list[dict[str, Any]] = []
    references: list[str] = []
    for name in (ERASED_NAME, KEPT_NAME):
        seen, hidden = await ip.references(app.client)
        created = await ip.create_character(app.client, _body(name, seen, hidden))
        await ip.style_companion(app.client, created["id"])
        made.append(created)
        references = references or [seen, hidden]
    await app.configure()
    found = CatalogCandidates(made[0]["id"], made[1]["id"], references=references)
    sizes = iter(sg.next_size() for _ in range(3))
    found.candidates["own"] = await one(app, found.erased, NEUTRAL_PROMPT, next(sizes))
    found.candidates["named"] = await one(app, found.kept, NAMING_PROMPT, next(sizes))
    found.candidates["kept"] = await one(app, found.kept, NEUTRAL_PROMPT, next(sizes))
    return found


async def candidate_ids(engine: AsyncEngine, ids: list[str]) -> set[str]:
    async with engine.connect() as conn:
        return {
            str(i)
            for i in await conn.scalars(
                text("SELECT id FROM catalog.image_candidates WHERE id = ANY(CAST(:i AS text[]))"),
                {"i": ids},
            )
        }


async def depicted(engine: AsyncEngine, media_id: str) -> list[dict[str, str]]:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT depicted_subjects FROM media_assets WHERE id = :i"), {"i": media_id}
        )
    return list(found or [])


async def boundary(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/admin/erasure/boundary", headers=WRITE)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found
