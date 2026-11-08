"""Story images in a history check (FR-RATE-7, FR-IMG-3; #272): ``core.ratings.change``'s
``image_check`` over #258's ``check_output``.

A rating tightening's check, a reactivation check (#120, both the ``rating_check`` job) and an
import's guardrail gate (``orchestrator.import_gates``) judge each story image by its prompt
(core's text path) and by the image itself, here: ``StoryImageCheck(models, db, media)`` reads
the image's row and asset, its bytes from the blob store, and its safety context from the
asset's declared subjects (``build_upload_safety_context``, as an upload's), then sends it to
the ``image_check`` model with the policies core has no stored verdict for. It answers
``{policy_id: pass|violation}`` (``check_output`` already applies each policy's
``on_uncertain``), or ``None`` (the image stays unchecked, a violation ``image_unchecked``)
when ``image_check`` has no model or the image's row, asset or bytes cannot be read. A checker
that cannot answer (``ModelError``) propagates: the job retries with #70's backoff, an import
fails its attempt.

Nothing is written back to the asset: the check's verdicts live only in
``guardrail_check_results``. Logs carry ids and counts only (invariant 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.images.safety import UploadSubject, build_upload_safety_context
from epistrel.core.policies.model import Policy
from epistrel.core.ratings.change import IMAGE_KEY, HistoryRecord, ImageVerdicts
from epistrel.core.tables import media_assets, story_images
from epistrel.core.tables.media import MediaKind
from epistrel.orchestrator.images.output_check import check_output, checker

if TYPE_CHECKING:
    from epistrel.core.media.store import BlobStore
    from epistrel.orchestrator.models.access import ModelAccessLayer

logger = logging.getLogger(__name__)


def _read(store: BlobStore, checksum: str) -> bytes | None:
    try:
        handle = store.open(checksum)
    except FileNotFoundError:
        return None
    try:
        return handle.read()
    finally:
        handle.close()


@dataclass(frozen=True, slots=True)
class StoryImageCheck:
    """``change.ImageCheck`` for story images (module docstring)."""

    models: ModelAccessLayer
    db: AsyncEngine
    media: BlobStore

    async def __call__(
        self, record: HistoryRecord, policies: Sequence[Policy]
    ) -> ImageVerdicts | None:
        if not policies or not record.key.startswith(IMAGE_KEY):
            return None
        found = await checker(self.models, self.db)
        if found is None:
            return None
        image_id = record.key.removeprefix(IMAGE_KEY)
        s, m = story_images.c, media_assets.c
        async with self.db.connect() as conn, conn.begin():
            row = (
                await conn.execute(
                    select(
                        s.prompt,
                        m.kind,
                        m.mime_type,
                        m.checksum,
                        m.caption,
                        m.alt_text,
                        m.depicted_subjects,
                    )
                    .join(media_assets, m.id == s.asset_id)
                    .where(s.id == image_id)
                )
            ).one_or_none()
            if row is None or row.kind != MediaKind.IMAGE.value:
                return None
            declared = [
                UploadSubject(str(d["kind"]), str(d["id"]))
                for d in row.depicted_subjects or ()
                if isinstance(d, dict) and d.get("kind") and d.get("id")
            ]
            context = await build_upload_safety_context(
                conn, declared, caption=row.caption, alt_text=row.alt_text
            )
        image = await asyncio.to_thread(_read, self.media, str(row.checksum))
        if image is None:
            logger.info("story image check: bytes missing for image %s", image_id)
            return None
        verdict = await check_output(
            self.models,
            self.db,
            image,
            str(row.mime_type),
            prompt=row.prompt,
            caption=row.caption,
            alt_text=row.alt_text,
            safety_context=context,
            policies=policies,
            found_checker=found,
        )
        return {v.policy_id: v.result for v in verdict.per_policy}


__all__ = ["StoryImageCheck"]
