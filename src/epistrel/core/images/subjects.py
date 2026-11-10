"""A catalog character's own images depict that character (FR-IMG-9; #266).

When a catalog character's draft gains an image (its primary image, a gallery image, a
reference image, or a candidate attached to one of those slots, #260), the character joins
that asset's ``depicted_subjects`` if it is not there yet, and the asset's ``subject_references``
rows are written again. All of this happens in the draft write's transaction, after the entry
row's update, so the lock order is entry row, then asset rows (``FOR UPDATE``, in id order),
then the index. The list stays de-duplicated and sorted by ``(kind, id)``. Detaching an image
never removes the character, and an asset another entry shares is updated in place:
``depicted_subjects`` is mutable metadata.

The character passes the erasure guard first (``assert_not_erased``, #127). A draft whose
character a catalog-scope erasure removed no longer exists, so the guard refuses only an id
that the ledger names.

Scenario drafts are not covered: a scenario is no subject. Log lines carry ids and counts only
(invariant 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.erasure.guard import assert_not_erased
from epistrel.core.erasure.index import index_records
from epistrel.core.erasure.records import media_record
from epistrel.core.tables import media_assets
from epistrel.core.tables.media import SubjectKind
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

OWN_FIELD = "depicted_subjects"


def _sorted(subjects: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``subjects`` de-duplicated by ``(kind, id)`` and sorted by it, each kept whole (a subject
    image's declaration carries its label and true age, #830)."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for subject in subjects:
        key = (str(subject.get("kind")), str(subject.get("id")))
        found.setdefault(key, {**subject, "kind": key[0], "id": key[1]})
    return [found[key] for key in sorted(found)]


async def include_own(conn: AsyncConnection, character_id: str, media_ids: Iterable[str]) -> int:
    """Add the catalog character ``character_id`` to the ``depicted_subjects`` of each asset in
    ``media_ids`` that lacks it, and index the asset again. This runs in ``conn``'s transaction,
    after the draft's entry row was locked by its write. Returns how many assets changed. 409
    ``erased_subject_referenced`` when a catalog-scope erasure removed the character."""
    wanted = sorted(set(media_ids))
    if not wanted:
        return 0
    await assert_not_erased(conn, [character_id], field=f"body.{OWN_FIELD}", path=__name__)
    own = {"kind": SubjectKind.CATALOG_CHARACTER.value, "id": character_id}
    rows = (
        await conn.execute(
            select(
                media_assets.c.id, media_assets.c.depicted_subjects, media_assets.c.viewpoint_basis
            )
            .where(media_assets.c.id.in_(wanted))
            .order_by(media_assets.c.id)
            .with_for_update()
        )
    ).all()
    changed = 0
    for row in rows:
        depicted = [d for d in (row.depicted_subjects or []) if isinstance(d, Mapping)]
        if any((d.get("kind"), d.get("id")) == (own["kind"], own["id"]) for d in depicted):
            continue
        await conn.execute(
            update(media_assets)
            .where(media_assets.c.id == row.id)
            .values(depicted_subjects=_sorted([*depicted, own]))
        )
        changed += 1
        if row.viewpoint_basis is None:
            # A story image's asset is indexed with its story image (#261), in its story.
            asset = (
                await conn.execute(select(media_assets).where(media_assets.c.id == row.id))
            ).one()
            await index_records(conn, [media_record(asset)])
    if changed:
        log.debug("own images depict their character", character_id=character_id, assets=changed)
    return changed


__all__ = ["OWN_FIELD", "include_own"]
