"""The ``catalog`` image job target: a catalog entry's private candidates (FR-IMG-4,7; #260).

A catalog job's ``target`` is ``{entry_kind, entry_id, slot, content_rating_id}`` and its
``origin`` ``{entry_kind, entry_id, draft_revision, image_input_fingerprint}`` (docs/02 §4.13).

- ``validate_enqueue``: the entry exists (404) and is not archived (422 ``entry_archived``).
- ``revalidate`` (at claim and before each provider call): the entry still exists, else
  ``TargetGone`` and the job ends ``target_gone`` with no provider call.
- ``promote`` (inside the promotion's transaction): the entry's row ``FOR SHARE`` (gone:
  ``TargetGone``, so nothing is promoted and nothing is partial), then per passing output a
  ``media_assets`` row in state ``candidate`` owned by the entry's owner (``source =
  generated``, the generation metadata and the output check's fields), and one
  ``catalog.image_candidates`` row each (``core.images.candidates.insert``: expiring
  ``candidate_ttl`` from now), and the job's ``result`` ``{candidate_ids}``. Catalog results
  are never attached automatically: the job ends ``candidates_ready``. Candidates are stored
  even when the draft's image inputs changed during generation; they list as ``stale`` and an
  attach refuses them.
- ``result_ids``: ``candidate_ids``, from the job's ``result`` (they outlive the candidate rows
  an attach or discard deletes).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.config_changes import read_section
from epistrel.core.enums import EntryStatus
from epistrel.core.errors import CharacterNotFound, EntryArchived, ScenarioNotFound
from epistrel.core.images import candidates
from epistrel.core.images.store import ImageJob, set_result
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.settings_schema import ImageJobSettings
from epistrel.core.tables import content_ratings
from epistrel.core.tables.catalog import character_catalog, scenario_catalog
from epistrel.core.tables.images import ImageJobKind, ImageJobStatus
from epistrel.core.tables.media import MediaState
from epistrel.orchestrator.images import jobs
from epistrel.orchestrator.images.port import MAX_IMAGES_PER_REQUEST

ENTRY_KINDS = ("character", "scenario")
JOB_LOCATION = jobs.JOB_LOCATION


class CatalogImageRequest(BaseModel):
    """``POST /catalog/{characters|scenarios}/{id}/images/generate`` (FR-IMG-7): the image
    ``slot`` the candidates are for (``primary`` or ``gallery`` for a character, ``cover`` for a
    scenario) and how many candidates, ``n`` (1 to 10; omitted: the deployment's
    ``catalog_candidates_n``, 4 by default)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"slot": "primary", "n": 4}]}
    )

    slot: Literal["primary", "gallery", "cover"]
    n: Annotated[StrictInt, Field(ge=1, le=MAX_IMAGES_PER_REQUEST)] | None = None


@dataclass(frozen=True, slots=True)
class CatalogImagesQueued:
    """A catalog image request's answer: 202 ``{job_id, status: queued}`` with ``Location:
    /image-jobs/{id}``, and the deployment's unchanged ``config_revision``. A replay of the
    same key answers the same job (``replayed``)."""

    job_id: str
    config_revision: int
    status: str = ImageJobStatus.QUEUED.value
    replayed: bool = False

    @property
    def location(self) -> str:
        return JOB_LOCATION.format(self.job_id)


def entry_of(
    character_id: str | None, scenario_id: str | None
) -> tuple[Literal["character", "scenario"], str]:
    """``(kind, id)`` of the entry a library call names: exactly one of the two."""
    if (character_id is None) == (scenario_id is None):
        raise TypeError("name exactly one of character_id and scenario_id")
    if character_id is not None:
        return "character", character_id
    assert scenario_id is not None  # noqa: S101 - checked above
    return "scenario", scenario_id


def _entry(job_or_target: Mapping[str, Any]) -> tuple[str, str]:
    return str(job_or_target["entry_kind"]), str(job_or_target["entry_id"])


async def require_entry(conn: AsyncConnection, entry_kind: str, entry_id: str) -> Any:
    """The entry's row (``status`` and ``draft``): 404 for an unknown entry, or an id of the
    other kind."""
    table = character_catalog if entry_kind == "character" else scenario_catalog
    row = (
        await conn.execute(select(table.c.status, table.c.draft).where(table.c.id == entry_id))
    ).one_or_none()
    if row is None:
        key = "character_id" if entry_kind == "character" else "scenario_id"
        missing = CharacterNotFound if entry_kind == "character" else ScenarioNotFound
        noun = "catalog character" if entry_kind == "character" else "scenario"
        raise missing(f"{noun} {entry_id} does not exist", {key: entry_id})
    return row


async def require_editable(conn: AsyncConnection, entry_kind: str, entry_id: str) -> None:
    """404 for an unknown entry (or an id of the other kind), 422 ``entry_archived`` for an
    archived one."""
    status = (await require_entry(conn, entry_kind, entry_id)).status
    key = "character_id" if entry_kind == "character" else "scenario_id"
    if status == EntryStatus.ARCHIVED.value:
        raise EntryArchived(
            f"{entry_kind} {entry_id} is archived; unarchive it to generate images for it",
            {key: entry_id},
        )


class CatalogTarget:
    """The catalog target (module docstring)."""

    success_status = ImageJobStatus.CANDIDATES_READY

    async def validate_enqueue(
        self, conn: AsyncConnection, target: Mapping[str, Any], origin: Mapping[str, Any]
    ) -> None:
        entry_kind, entry_id = _entry(origin)
        await require_editable(conn, entry_kind, entry_id)

    async def revalidate(self, conn: AsyncConnection, job: ImageJob) -> None:
        entry_kind, entry_id = _entry(job.origin)
        if await candidates.entry_owner(conn, entry_kind, entry_id) is None:
            raise jobs.TargetGone(f"{entry_kind} {entry_id}")

    async def _rating_version(self, conn: AsyncConnection, rating_id: Any) -> str:
        """The current version of the rating the request was built under (a retired rating
        still has one), else Everyone's."""
        found = None
        if isinstance(rating_id, str):
            found = await conn.scalar(
                select(content_ratings.c.current_version_id).where(
                    content_ratings.c.id == rating_id
                )
            )
        if found is None:
            found = await conn.scalar(
                select(content_ratings.c.current_version_id).where(
                    content_ratings.c.id == EVERYONE_ID
                )
            )
        return str(found)

    async def promote(
        self, conn: AsyncConnection, job: ImageJob, outputs: Sequence[jobs.PromotableOutput]
    ) -> list[str]:
        entry_kind, entry_id = _entry(job.origin)
        owner = await candidates.entry_owner(conn, entry_kind, entry_id, share=True)
        if owner is None:
            raise jobs.TargetGone(f"{entry_kind} {entry_id}")
        version = await self._rating_version(conn, job.target.get("content_rating_id"))
        depicted = (
            [{"kind": "catalog_character", "id": entry_id}] if entry_kind == "character" else []
        )
        asset_ids = [
            await jobs.insert_output_asset(
                conn,
                job,
                output,
                state=MediaState.CANDIDATE,
                owner=owner,
                content_rating_version_id=version,
                depicted_subjects=depicted,
            )
            for output in outputs
        ]
        settings = await read_section(conn, ImageJobSettings)
        candidate_ids = await candidates.insert(
            conn,
            entry_kind=entry_kind,
            entry_id=entry_id,
            job_id=job.id,
            slot=str(job.target.get("slot") or "gallery"),
            draft_revision=int(job.origin["draft_revision"]),
            fingerprint=str(job.origin["image_input_fingerprint"]),
            asset_ids=asset_ids,
            ttl_s=settings.candidate_ttl,
        )
        await set_result(conn, job.id, {"candidate_ids": candidate_ids})
        return asset_ids

    async def on_terminal(self, conn: AsyncConnection, job: ImageJob) -> None:
        return None

    async def result_ids(self, conn: AsyncConnection, job: ImageJob) -> tuple[str, list[str]]:
        found = (job.result or {}).get("candidate_ids") or []
        return "candidate_ids", [str(c) for c in found]


TARGET = CatalogTarget()
jobs.register_target(ImageJobKind.CATALOG, TARGET)
"""Registered on import (the Engine imports this module): tests swap it with
``testing.images.registered_target``, which restores it."""


__all__ = [
    "ENTRY_KINDS",
    "JOB_LOCATION",
    "TARGET",
    "CatalogImageRequest",
    "CatalogImagesQueued",
    "CatalogTarget",
    "entry_of",
    "require_editable",
    "require_entry",
]
