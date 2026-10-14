"""What the catalog checks read from a catalog character's document (#111, #115). Not a check.

A character's draft and version ``content`` have #115's shape (``core.catalog.models``):
``public_card {name, public_description, images {primary, gallery}}``, ``definition {true_age,
…}``, ``metadata {aliases, labels, content_rating_id}`` and ``visual_identity
{reference_images [{media_id, audience}], …}``. Migration 0032 moved the name and public
description of #103's interim documents under ``public_card``; ``name_of`` still reads a
top-level ``name``, which only a document planted outside the catalog can hold. These readers
never fail: a value that is missing or of another type reads as absent, and the check that needs
it decides what absent means (an eligibility filter on an absent value fails).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _group(document: Mapping[str, Any] | None, *path: str) -> Mapping[str, Any]:
    found: Any = document
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def name_of(document: Mapping[str, Any] | None) -> str | None:
    """The character's name: ``public_card.name``, else the interim top-level ``name``."""
    found = _group(document, "public_card").get("name")
    if not isinstance(found, str) and isinstance(document, Mapping):
        found = document.get("name")
    return found if isinstance(found, str) else None


def labels_of(document: Mapping[str, Any] | None) -> frozenset[str]:
    """The character's labels (``metadata.labels``), trimmed and lower-cased as labels are
    stored."""
    found = _group(document, "metadata").get("labels")
    if not isinstance(found, list):
        return frozenset()
    return frozenset(label.strip().lower() for label in found if isinstance(label, str))


def rating_id_of(document: Mapping[str, Any] | None) -> str | None:
    """The content rating the document names (``metadata.content_rating_id``)."""
    found = _group(document, "metadata").get("content_rating_id")
    return found if isinstance(found, str) else None


def true_age_of(document: Mapping[str, Any] | None) -> int | None:
    """The character's true age (``definition.true_age``, FR-CAT-12)."""
    found = _group(document, "definition").get("true_age")
    return found if isinstance(found, int) and not isinstance(found, bool) else None


def images_of(document: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """``(field, media id)`` for each image the character's public card shows: the primary
    image and the gallery (``public_card.images``)."""
    images = _group(document, "public_card", "images")
    found: list[tuple[str, str]] = []
    primary = images.get("primary")
    if isinstance(primary, str):
        found.append(("public_card.images.primary", primary))
    gallery = images.get("gallery")
    for index, media_id in enumerate(gallery if isinstance(gallery, list) else []):
        if isinstance(media_id, str):
            found.append((f"public_card.images.gallery[{index}]", media_id))
    return found


def reference_images_of(document: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """``(field, media id)`` for each reference image of the character's visual identity
    (``visual_identity.reference_images[i].media_id``)."""
    found: list[tuple[str, str]] = []
    images = _group(document, "visual_identity").get("reference_images")
    for index, image in enumerate(images if isinstance(images, list) else []):
        media_id = image.get("media_id") if isinstance(image, Mapping) else None
        if isinstance(media_id, str):
            found.append((f"visual_identity.reference_images[{index}].media_id", media_id))
    return found


__all__ = [
    "images_of",
    "labels_of",
    "name_of",
    "rating_id_of",
    "reference_images_of",
    "true_age_of",
]
