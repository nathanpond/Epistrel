"""The character content check (FR-CAT-2,4, FR-CAT-12, FR-RATE-3; #115): what a published catalog
character must hold. Every finding is an error, and all of them are reported at once, so one
refused publish names every missing field:

- ``field_required`` at each path of ``models.PUBLISH_REQUIRED`` the draft leaves unset or blank:
  the name, the public description, the appearance, the personality, each of the five
  disposition axes, the values and motivations, the speech style and the vocabulary; and of
  ``models.IDENTITY_REQUIRED`` when it has a visual identity (its preferred image model, base
  appearance prompt, negative prompt and seed). Lists may be empty, and the images, the
  background flavor, the deflection style, the safe fallback line and the visual identity are
  optional;
- ``true_age_missing`` at ``definition.true_age`` (FR-CAT-12: "required to publish");
- ``content_rating_missing`` at ``metadata.content_rating_id`` (FR-RATE-3: "required to
  publish"), or ``rating_retired`` there when the rating it names is unknown or retired (it
  cannot be newly assigned, #48);
- ``reference_image_missing_audience`` at ``visual_identity.reference_images[i].audience``: a
  reference image that declares no audience (FR-CAT-4: there is no default). A save refuses one,
  so only a document stored some other way can hold it;
- ``primary_image_required`` at ``public_card.images.primary`` when the deployment's
  ``require_primary_image`` (read with the publish's settings) is on and the draft has no primary
  image (FR-CAT-4). It is off by default: a text-only deployment never needs an image.

The images themselves (each exists, is live and is an image) are the media check's
(``checks.media``). This check runs after the document check, which gates, so the draft it reads
is valid.
"""

from __future__ import annotations

from typing import ClassVar

from epistrel.core.catalog.checks.base import Check, Findings, Subject, ValidationContext
from epistrel.core.catalog.checks.required import RETIRED_RULE, rating_unusable
from epistrel.core.catalog.models import (
    AUDIENCE_RULE,
    CharacterDraft,
    missing_audiences,
    unset_required,
)
from epistrel.core.catalog.report import Outcome
from epistrel.core.tables.ownership import EntityType

REQUIRED_RULE = "a published catalog character holds every field of its definition (FR-CAT-2)"
AGE_RULE = "a published catalog character declares its true age (FR-CAT-12)"
RATING_RULE = (
    "a published catalog character carries a content rating from the deployment's set (FR-RATE-3)"
)
PRIMARY_RULE = (
    "the deployment requires a primary image to publish a catalog character "
    "(require_primary_image, FR-CAT-4)"
)


class CharacterContentCheck(Check):
    """See the module docstring."""

    name = "character_content"
    kinds: ClassVar[frozenset[EntityType]] = frozenset({EntityType.CHARACTER})

    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        draft = CharacterDraft.model_validate(subject.document)
        found = Findings()

        def refuse(code: str, field: str, rule: str, source: str | None = None) -> None:
            found.entries.append(
                subject.entry(code, Outcome.rejected, field=field, source=source, rule=rule)
            )

        for path in unset_required(draft):
            refuse("field_required", path, REQUIRED_RULE)
        if draft.definition.true_age is None:
            refuse("true_age_missing", "definition.true_age", AGE_RULE)
        rating = draft.metadata.content_rating_id
        if rating is None:
            refuse("content_rating_missing", "metadata.content_rating_id", RATING_RULE)
        elif await rating_unusable(ctx.conn, rating):
            refuse("rating_retired", "metadata.content_rating_id", RETIRED_RULE, source=rating)
        for index in missing_audiences(draft):
            refuse(
                "reference_image_missing_audience",
                f"visual_identity.reference_images[{index}].audience",
                AUDIENCE_RULE,
            )
        if ctx.settings.require_primary_image and draft.public_card.images.primary is None:
            refuse("primary_image_required", "public_card.images.primary", PRIMARY_RULE)
        return found


CHECK = CharacterContentCheck()

__all__ = ["CHECK", "CharacterContentCheck"]
