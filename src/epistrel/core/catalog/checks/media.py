"""The media check (FR-SCN-5, FR-MEDIA-1,2; #111): every media asset a scenario shows exists, is
live, and has a type and size the deployment allows now. Each finding is an error, naming the
asset in ``source``:

- ``media_missing``: no live or retired asset has the id (an unknown id, or a candidate that was
  never promoted);
- ``media_retired``: the asset was deleted (retired, FR-MEDIA-2) since it was added;
- ``media_not_allowed``: its MIME type is not in ``media_allowed_types``, or its size is above
  ``media_max_bytes``, as the settings stand at validation (one entry for each);
- ``media_kind_mismatch``: a video where an image belongs (the cover, the gallery, a character's
  images) or an image where a video belongs (``videos``).

The assets checked are the scenario's cover, gallery images and videos (at their field), and the
images on the public card of each ``locked`` or ``default`` role's character version (at the
role's ``cast.roles[i].character``; #115's ``public_card.images``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.cast_members import filled
from epistrel.core.catalog.checks.character_documents import images_of
from epistrel.core.catalog.checks.scenario import draft_of
from epistrel.core.catalog.report import Entry, Outcome
from epistrel.core.tables.media import MediaKind, MediaState, media_assets

MEMO = "media_assets"

MISSING_RULE = "a media asset a scenario shows exists and is live"
RETIRED_RULE = "a media asset a scenario shows is live, not retired"
KIND_RULE = {
    MediaKind.IMAGE: "an image belongs here (the cover, the gallery, a character's images)",
    MediaKind.VIDEO: "a video belongs here (the scenario's videos)",
}


@dataclass(frozen=True, slots=True)
class MediaUse:
    """One place a validated scenario shows an asset: its field (and role, for a character's
    image), the kind it must be, and the asset's id."""

    media_id: str
    field: str
    role: str | None
    kind: MediaKind
    character: str | None = None
    """``catalog_id@version`` of the character whose image this is (``None`` for the scenario's
    own media)."""


async def media_uses(subject: Subject, ctx: ValidationContext) -> list[MediaUse]:
    """Every place the scenario shows an asset (see the module docstring), in document order."""
    media = draft_of(subject).public_card.media
    uses: list[MediaUse] = []
    if media.cover is not None:
        uses.append(MediaUse(media.cover, "public_card.media.cover", None, MediaKind.IMAGE))
    for index, media_id in enumerate(media.gallery):
        uses.append(
            MediaUse(media_id, f"public_card.media.gallery[{index}]", None, MediaKind.IMAGE)
        )
    for index, video in enumerate(media.videos):
        uses.append(
            MediaUse(
                video.media_id, f"public_card.media.videos[{index}].media_id", None, MediaKind.VIDEO
            )
        )
    for member in await filled(subject, ctx):
        for _, media_id in images_of(member.document):
            uses.append(
                MediaUse(media_id, member.field, member.role.id, MediaKind.IMAGE, member.source)
            )
    return uses


async def assets(subject: Subject, ctx: ValidationContext) -> dict[str, Any]:
    """The ``media_assets`` rows of every asset the scenario shows, by id; read once per
    validation."""
    found = subject.memo.get(MEMO)
    if isinstance(found, dict):
        return found
    wanted = sorted({use.media_id for use in await media_uses(subject, ctx)})
    rows: dict[str, Any] = {}
    if wanted:
        result = await ctx.conn.execute(
            select(
                media_assets.c.id,
                media_assets.c.kind,
                media_assets.c.mime_type,
                media_assets.c.size,
                media_assets.c.state,
                media_assets.c.content_rating_version_id,
            ).where(media_assets.c.id.in_(wanted))
        )
        rows = {str(row.id): row for row in result}
    subject.memo[MEMO] = rows
    return rows


def _problems(use: MediaUse, row: Any, ctx: ValidationContext) -> list[tuple[str, str]]:
    """``(code, rule)`` for each way ``row`` cannot be shown at ``use``."""
    if row is None or row.state == MediaState.CANDIDATE.value:
        return [("media_missing", MISSING_RULE)]
    found: list[tuple[str, str]] = []
    if row.state == MediaState.RETIRED.value:
        found.append(("media_retired", RETIRED_RULE))
    if row.kind != use.kind.value:
        found.append(("media_kind_mismatch", KIND_RULE[use.kind]))
    allowed = ctx.settings.media_allowed_types
    if row.mime_type not in allowed:
        found.append(
            (
                "media_not_allowed",
                f"its type, {row.mime_type}, is not in the deployment's media_allowed_types",
            )
        )
    if int(row.size) > ctx.settings.media_max_bytes:
        found.append(
            (
                "media_not_allowed",
                f"its size, {int(row.size)} bytes, is above the deployment's media_max_bytes "
                f"({ctx.settings.media_max_bytes})",
            )
        )
    return found


class MediaCheck(Check):
    """See the module docstring."""

    name = "media"

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        rows = await assets(subject, ctx)
        entries: list[Entry] = []
        for use in await media_uses(subject, ctx):
            for code, rule in _problems(use, rows.get(use.media_id), ctx):
                entries.append(
                    subject.entry(
                        code,
                        Outcome.rejected,
                        field=use.field,
                        role=use.role,
                        source=use.media_id,
                        rule=rule,
                    )
                )
        return Findings(entries=entries)


CHECK = MediaCheck()

__all__ = ["CHECK", "MediaCheck", "MediaUse", "assets", "media_uses"]
