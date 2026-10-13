"""What the scenario checks read from a catalog character's version document (#111). Not a
check.

A character version's ``content`` is #103's interim draft (``name``, ``public_description``,
``definition.personality``) until #115 gives it its full shape (``public_card {name,
public_description, images {primary, gallery}}``, ``definition {true_age, …}``, ``metadata
{aliases, labels, content_rating_id}``). These readers accept both and never fail: a value that
is missing or of another type reads as absent, and the check that needs it decides what absent
means (an eligibility filter on an absent value fails). #115 must keep these paths, or change
them here.
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


__all__ = ["images_of", "labels_of", "name_of", "rating_id_of", "true_age_of"]
