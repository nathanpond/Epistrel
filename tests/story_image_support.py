"""Helpers for the story image tests (#261): the whisper scene over REST (Bobby the player,
Jenni, Marcus), its turns on the stub adapter, uploads at a position for an audience, and the
images a history read returns in place.

Every upload carries a PNG of its own (``png()``'s nonce), so no two tests share a checksum on
the shared database."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx

from epistrel.core.idempotency import IDEMPOTENCY_HEADER
from tests.media_support import files, png
from tests.respond_support import body, played
from tests.respond_support import new_story as rest_story
from tests.story_support import READ, WRITE

BOBBY, JENNI, MARCUS = "Bobby", "Jenni", "Marcus"
OMNISCIENT = "omniscient"


def character(character_id: str) -> str:
    """The ``character:<id>`` viewpoint of ``character_id``."""
    return f"character:{character_id}"


async def scene(client: httpx.AsyncClient, turns: int = 2) -> tuple[str, dict[str, str]]:
    """The whisper scene (Bobby the player, Jenni, Marcus) with ``turns`` Lite turns played
    (each: the player input at beat 1, the reply at beat 2); its id and its ids by name."""
    story_id, ids = await rest_story(client, BOBBY, JENNI, MARCUS)
    for n in range(turns):
        await played(client, story_id, body(ids[BOBBY], say=f"Turn {n + 1}: who is there?"))
    return story_id, ids


async def turn(client: httpx.AsyncClient, story_id: str, ids: dict[str, str]) -> dict[str, Any]:
    """One more Lite turn."""
    return await played(client, story_id, body(ids[BOBBY], say="And now?"))


def meta(
    position: dict[str, int], audience: str, *, subjects: list[Any] | None = None, **fields: Any
) -> dict[str, Any]:
    """A ``metadata`` part: ``position``, ``audience`` and ``subjects`` (default none declared:
    ``[]``; pass ``subjects=None`` with ``omit_subjects=True`` to leave the list out)."""
    omit = fields.pop("omit_subjects", False)
    found: dict[str, Any] = {"position": position, "audience_viewpoint": audience, **fields}
    if not omit:
        found["depicted_subjects"] = [] if subjects is None else subjects
    return found


async def upload(
    client: httpx.AsyncClient,
    story_id: str,
    metadata: dict[str, Any] | str,
    *,
    data: bytes | None = None,
    key: str | None = None,
) -> httpx.Response:
    """``POST /stories/{id}/images`` with ``metadata`` and a fresh PNG (or ``data``)."""
    headers = {**WRITE, **({IDEMPOTENCY_HEADER: key} if key is not None else {})}
    raw = metadata if isinstance(metadata, str) else json.dumps(metadata)
    return await client.post(
        f"/stories/{story_id}/images",
        files=files(png() if data is None else data, raw),
        headers=headers,
    )


async def placed(
    client: httpx.AsyncClient, story_id: str, metadata: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """An upload that must answer 201; its body."""
    response = await upload(client, story_id, metadata, **kwargs)
    assert response.status_code == 201, response.text
    found: dict[str, Any] = response.json()
    return found


async def history(
    client: httpx.AsyncClient, story_id: str, viewpoint: str, **params: Any
) -> list[dict[str, Any]]:
    """The history items (images in place) for ``viewpoint``."""
    response = await client.get(
        f"/stories/{story_id}/history", params={"viewpoint": viewpoint, **params}, headers=READ
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def image_ids(items: list[dict[str, Any]]) -> list[str]:
    """The ids of the images among ``items``, in order."""
    return [i["image_id"] for i in items if i.get("kind") == "image"]


def after(items: list[dict[str, Any]], image_id: str) -> dict[str, Any] | None:
    """The item just before the image ``image_id`` (what it follows), or ``None`` when it is
    first."""
    for index, item in enumerate(items):
        if item.get("kind") == "image" and item["image_id"] == image_id:
            return items[index - 1] if index else None
    raise AssertionError(f"image {image_id} is not among the items")


async def delete(client: httpx.AsyncClient, story_id: str, image_id: str) -> httpx.Response:
    return await client.delete(f"/stories/{story_id}/images/{image_id}", headers=WRITE)


async def get(
    client: httpx.AsyncClient, story_id: str, image_id: str, viewpoint: str
) -> httpx.Response:
    return await client.get(
        f"/stories/{story_id}/images/{image_id}", params={"viewpoint": viewpoint}, headers=READ
    )


async def exported(
    client: httpx.AsyncClient, engine: Any, story_id: str, out: Any, *, blobs: Any = None
) -> bytes:
    """The story's bundle (#249): the export requested, its job run into ``out`` (media blobs
    read through ``blobs``, the Engine's reader), the archive read back."""
    from epistrel.core import bundles

    started = await client.post(f"/admin/stories/{story_id}/export", headers=WRITE)
    assert started.status_code == 202, started.text
    job = started.json()["job_id"]
    await bundles.run_export(engine, job, out, blobs=blobs)
    got = await client.get(f"/admin/stories/exports/{job}/bundle", headers=READ)
    assert got.status_code == 200, got.text
    return bytes(got.content)


async def imported(client: httpx.AsyncClient, engine: Any, data: bytes) -> str:
    """The bundle imported (#250): the request, then its ``story_import`` job run by a worker
    until it is ready; the new story's id."""
    from datetime import timedelta

    from sqlalchemy import text

    from epistrel.core import clock
    from epistrel.orchestrator.jobs.types import ClaimedJob
    from tests.offboarding_support import worker

    started = await client.post(
        "/admin/stories/import",
        files={
            "metadata": (None, "{}", "application/json"),
            "file": ("story.epistrel.tgz", data, "application/gzip"),
        },
        headers=WRITE,
    )
    assert started.status_code == 202, started.text
    bundle_id = started.json()["job_id"]
    runner = worker(engine)
    for _ in range(5):
        now = clock.now()
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE jobs SET status = 'running', lease_until = :lease, "
                        "attempt = attempt + 1, worker_id = 'w:test:0', updated_at = :now "
                        "WHERE id = (SELECT job_id FROM bundle_jobs WHERE id = :b) "
                        "AND status = 'queued' RETURNING *"
                    ),
                    {"b": bundle_id, "now": now, "lease": now + timedelta(seconds=60)},
                )
            ).one_or_none()
        if row is None:
            break
        await runner.execute(
            ClaimedJob(
                id=row.id,
                kind=row.kind,
                story_id=row.story_id,
                payload=dict(row.payload or {}),
                attempt=int(row.attempt),
                failures=int(row.failures),
                due_at=row.due_at,
                lease_until=row.lease_until,
                worker_id=row.worker_id,
            )
        )
    state = await client.get(f"/admin/stories/imports/{bundle_id}", headers=READ)
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "ready", state.json()
    return str(started.json()["story_id"])


__all__ = [
    "BOBBY",
    "JENNI",
    "MARCUS",
    "OMNISCIENT",
    "after",
    "character",
    "delete",
    "exported",
    "get",
    "history",
    "image_ids",
    "imported",
    "meta",
    "placed",
    "scene",
    "turn",
    "upload",
]
